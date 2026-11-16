"""Test history behavior with isolated working trees and real temporary Git repos."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("changes", ROOT / "codex-kit/.codex/scripts/changes.py")
changes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changes)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project with spaces"
        self.root.mkdir()

    def record(self, **fields):
        return changes.create_entry(self.root, {"kind": "fixed", "component": "Печать",
            "user": "Исправлена повторная печать.", "support": "Потеря ответа. Проверен повтор с тем же ID.", **fields})

    def g(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
        p = subprocess.run(["git", "-C", str(self.root), *args], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if p.returncode:
            self.fail(p.stderr.decode("utf-8", errors="replace"))
        return p.stdout.decode("utf-8").strip()

    def init_git(self):
        self.g("init", "-b", "main", "--template=")
        self.g("config", "user.name", "Fixture")
        self.g("config", "user.email", "fixture@example.invalid")
        self.g("config", "commit.gpgsign", "false")
        (self.root / "code.txt").write_text("initial\n", encoding="utf-8")
        return self.save("initial")

    def save(self, message):
        self.g("add", "--all")
        self.g("commit", "-m", message)
        return self.g("rev-parse", "HEAD")

    def test_parallel_creation_has_no_shared_file_or_collision(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(lambda _: self.record(), range(32)))
        self.assertEqual(32, len(set(paths)))
        self.assertEqual(32, len(changes.worktree_snapshot(self.root)))
        self.assertTrue(all(p.parent.name.isdigit() for p in paths))

    def test_encoding_failure_leaves_no_visible_record(self):
        with self.assertRaises(UnicodeEncodeError):
            self.record(support="invalid surrogate \udcff")
        self.assertEqual({}, changes.worktree_snapshot(self.root))

    def test_reader_sees_nothing_until_publication(self):
        real_fsync = changes.os.fsync
        def observe(fd):
            self.assertEqual({}, changes.worktree_snapshot(self.root))
            real_fsync(fd)
        with patch.object(changes.os, "fsync", side_effect=observe):
            self.record()
        self.assertEqual(1, len(changes.worktree_snapshot(self.root)))

    def test_collision_cannot_overwrite_existing_record(self):
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 11, 14, 12, 0, 0, 123456, tzinfo=timezone.utc)
        uid = UUID("765c5c85-f2c0-4be1-bd4e-5156a6a3f831")
        with patch.object(changes, "datetime", FrozenDatetime), patch.object(changes, "uuid4", return_value=uid):
            path = self.record()
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                self.record(support="must not overwrite")
        self.assertEqual(original, path.read_bytes())
        self.assertFalse(list(path.parent.glob(".pending-*")))

    def test_audiences_are_separated(self):
        self.record(support="TECHONLY", references=["PRIVATEREF"])
        self.record(kind="internal", user=None, internal_reason="INTERNALREASON", support="INTERNALDETAILS")
        snapshot = changes.worktree_snapshot(self.root)
        user = changes.render(snapshot, "test", "2026-11-14", "user", draft=False)
        support = changes.render(snapshot, "test", "2026-11-14", "support", draft=False)
        for marker in ("TECHONLY", "PRIVATEREF", "INTERNALREASON", "INTERNALDETAILS"):
            self.assertNotIn(marker, user)
            self.assertIn(marker, support)

    def test_breaking_change_requires_user_action(self):
        with self.assertRaises(changes.ChangeError):
            self.record(breaking=True)
        self.record(breaking=True, action_required="Обновите настройки подключения.")
        result = changes.render(changes.worktree_snapshot(self.root), "test", "2026-11-14", "user", draft=False)
        self.assertIn("Обновите настройки", result)

    def test_id_timestamp_and_filename_are_checked(self):
        path = self.record()
        entry = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path.relative_to(self.root), changes.entry_path(entry))
        entry["created_at"] = "2026-11-14T12:00:00+03:00"
        path.write_bytes(changes.json_bytes(entry))
        with self.assertRaises(changes.ChangeError):
            changes.worktree_snapshot(self.root)

    def test_duplicate_json_keys_are_rejected(self):
        path = self.record()
        raw = path.read_text(encoding="utf-8").replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
        path.write_text(raw, encoding="utf-8")
        with self.assertRaises(changes.ChangeError):
            changes.worktree_snapshot(self.root)

    def test_multiline_text_is_literal_not_markdown_structure(self):
        self.record(user="plain\r\n\r\n# heading\n- list\n~~~\n<script>\r1. numbered\n\n    indented\n\ttabbed")
        text = changes.render(changes.worktree_snapshot(self.root), "test", "2026-11-14", "user", draft=False)
        self.assertNotIn("\r", text)
        for escaped in (r"\# heading", r"\- list", r"\~\~\~", r"\<script\>", r"1\. numbered"):
            self.assertIn(escaped, text)
        self.assertIn("&#32;   indented", text)
        self.assertIn("&#9;tabbed", text)
        self.assertNotIn("\n      indented", text)

    def test_existing_history_cannot_be_changed_or_deleted(self):
        path = self.record()
        before = changes.worktree_snapshot(self.root)
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["support"] = "Changed historical description"
        path.write_bytes(changes.json_bytes(entry))
        with self.assertRaises(changes.ChangeError):
            changes.select_entries(before, changes.worktree_snapshot(self.root))
        path.unlink()
        with self.assertRaises(changes.ChangeError):
            changes.select_entries(before, changes.worktree_snapshot(self.root))

    def test_known_ids_are_not_repeated(self):
        self.record()
        baseline = changes.worktree_snapshot(self.root)
        self.record()
        selected = changes.select_entries(baseline, changes.worktree_snapshot(self.root))
        self.assertEqual(1, len(selected))
        self.assertTrue(set(selected).isdisjoint(baseline))

    def test_output_is_reproducible_and_not_overwritten(self):
        self.record()
        snapshot = changes.worktree_snapshot(self.root)
        out1, out2 = self.root / "out1", self.root / "out2"
        for out in (out1, out2):
            changes.build_output(snapshot, out, "build-label", "2026-11-14", "base", "head")
        for name in ("CHANGELOG.md", "SUPPORT-CHANGELOG.md", "manifest.json"):
            self.assertEqual((out1 / name).read_bytes(), (out2 / name).read_bytes())
        with self.assertRaises(FileExistsError):
            changes.build_output(snapshot, out1, "different", "2026-11-15", None, None)

    def test_preview_without_git_is_marked_draft(self):
        self.record()
        with contextlib.redirect_stdout(io.StringIO()):
            result = changes.main(["--root", str(self.root), "preview", "--all", "--release", "test",
                "--date", "2026-11-14", "--output", str(self.root / "preview")])
        self.assertEqual(0, result)
        manifest = json.loads((self.root / "preview/manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["draft"])
        self.assertIsNone(manifest["source_commit"])

    def test_build_reads_committed_snapshot_not_dirty_files(self):
        base = self.init_git()
        path = self.record()
        head = self.save("change")
        expected = changes.worktree_snapshot(self.root)
        path.write_text("broken uncommitted JSON", encoding="utf-8")
        selected, _, resolved_head = changes.compare(self.root, base, head)
        self.assertEqual(expected, selected)
        self.assertEqual(head, resolved_head)

    def test_branches_merge_without_history_conflict_and_old_timestamp_is_included(self):
        self.init_git()
        self.g("checkout", "-b", "feature")
        feature_path = self.record(component="Feature")
        feature_id = json.loads(feature_path.read_text(encoding="utf-8"))["id"]
        self.save("feature with early timestamp")
        self.g("checkout", "main")
        self.record(component="Main")
        base = self.save("later release on main")
        self.g("merge", "--no-edit", "feature")
        selected, _, _ = changes.compare(self.root, base, "HEAD")
        self.assertEqual({feature_id}, set(selected))

    def test_backport_keeps_identity_in_another_release_line(self):
        initial = self.init_git()
        path = self.record()
        uid = json.loads(path.read_text(encoding="utf-8"))["id"]
        feature = self.save("fix")
        self.g("checkout", "-b", "maintenance", initial)
        self.g("cherry-pick", feature)
        selected, _, _ = changes.compare(self.root, initial, "HEAD")
        self.assertEqual({uid}, set(selected))

    def test_unrelated_release_base_is_rejected(self):
        initial = self.init_git()
        self.record(component="Main")
        unrelated = self.save("main change")
        self.g("checkout", "-b", "other", initial)
        self.record(component="Other")
        self.save("other change")
        with self.assertRaises(changes.ChangeError):
            changes.compare(self.root, unrelated, "HEAD")

    def test_ci_requires_new_entry_for_changed_project_files(self):
        base = self.init_git()
        (self.root / "code.txt").write_text("changed\n", encoding="utf-8")
        self.save("missing entry")
        args = ["--root", str(self.root), "check", "--base", base, "--require-entry"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, changes.main(args))
        self.record()
        self.save("entry")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, changes.main(args))

    def test_empty_base_is_not_all_history(self):
        self.init_git()
        output = self.root / "out"
        for base in ("", "   "):
            with contextlib.redirect_stderr(io.StringIO()):
                result = changes.main(["--root", str(self.root), "build", "--base", base,
                    "--release", "test", "--date", "2026-11-14", "--output", str(output)])
            self.assertEqual(1, result)
            self.assertFalse(output.exists())

    def test_changes_readme_is_not_exempt_from_ci_record(self):
        base = self.init_git()
        folder = self.root / ".changes"
        folder.mkdir()
        (folder / "README.md").write_text("documentation\n", encoding="utf-8")
        self.save("documentation without record")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, changes.main(["--root", str(self.root), "check", "--base", base, "--require-entry"]))

    def test_rename_into_entries_does_not_hide_project_change(self):
        base = self.init_git()
        (self.root / ".changes/entries").mkdir(parents=True)
        self.g("mv", "code.txt", ".changes/entries/moved.txt")
        self.save("rename without record")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, changes.main(["--root", str(self.root), "check", "--base", base, "--require-entry"]))


if __name__ == "__main__":
    unittest.main()
