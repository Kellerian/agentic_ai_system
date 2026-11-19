"""Exercise the installed PostToolUse command using Codex event payloads."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


KIT = Path(__file__).resolve().parents[1] / "codex-kit"


class PlanHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project with spaces"
        shutil.copytree(KIT, self.root)
        self.folder = self.root / ".plans"
        self.folder.mkdir()
        self.plan = self.folder / "план с пробелами.md"
        template = self.root / ".agents/skills/dev-plan/references/plan-template.md"
        self.contents = template.read_text(encoding="utf-8").replace("YYYY-MM-DD", "2026-11-19")
        self.plan.write_text(self.contents, encoding="utf-8")

    def event(self, operations, begin="*** Begin Patch"):
        payload = {"hook_event_name": "PostToolUse", "tool_name": "apply_patch", "cwd": str(self.root),
                   "tool_input": {"command": begin + "\n" + operations + "\n*** End Patch"}}
        result = subprocess.run([sys.executable, "-B", ".codex/scripts/plan_hook.py"], cwd=self.root,
                                input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, result.returncode, result.stderr)
        if result.stdout:
            output = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual("PostToolUse", output["hookEventName"])
            return [json.loads(s) for s in output["additionalContext"].splitlines()]
        return []

    def test_move_and_add_validate_only_final_plans(self):
        other = self.folder / "another.md"
        other.write_text(self.contents.replace("Статус: todo", "Статус: typo"), encoding="utf-8")
        rows = self.event(f"*** Update File: old.md\n*** Move to: .plans/{self.plan.name}\n@@\n-old\n+new\n"
                          f"*** Add File: {other}\n+content\n*** Update File: docs/readme.md\n@@\n-old\n+new")
        self.assertEqual(["valid", "invalid"], [row["status"] for row in rows])
        self.assertEqual(".plans/" + self.plan.name, rows[0]["plan"])
        self.assertEqual(0, rows[0]["progress"]["done"])
        self.assertIn("receipt", rows[0])
        self.assertNotIn("receipt", rows[1])
        self.assertTrue(rows[1]["errors"])

    def test_unrelated_deleted_and_outside_files_are_silent(self):
        self.assertEqual([], self.event("*** Delete File: .plans/old.md\n*** Add File: docs/notes.md\n"
                                        "+*** Add File: .plans/fake.md\n*** Update File: .plans/../README.md"))

    def test_patch_marker_whitespace_and_context_lines(self):
        rows = self.event(f"  *** Update File: old.md \r\n*** Move to: .plans/{self.plan.name}  \r\n@@\r\n"
                          " *** Add File: .plans/unrelated.md\r\n+text\u2028*** Add File: .plans/fake.md",
                          begin="*** Begin Patch ")
        self.assertEqual([".plans/" + self.plan.name], [row["plan"] for row in rows])

    def test_receipt_tracks_file_and_validator_contents(self):
        def receipt():
            return self.event(f"*** Update File: .plans/{self.plan.name}\n@@\n-old\n+new")[0]["receipt"]
        def matches(token, path=None):
            return subprocess.run([sys.executable, "-B", ".codex/scripts/plan_hook.py",
                                   "--matches", str(path or self.plan), token], cwd=self.root).returncode
        first = receipt()
        self.assertEqual(0, matches(first))
        copy = self.folder / "copy.md"
        shutil.copyfile(self.plan, copy)
        self.assertEqual(1, matches(first, copy))
        self.assertEqual(first, receipt())
        self.plan.write_text(self.contents + "\n", encoding="utf-8")
        second = receipt()
        self.assertEqual(1, matches(first))
        self.assertNotEqual(first, second)
        validator = self.root / ".codex/scripts/plans.py"
        with validator.open("a", encoding="utf-8") as stream:
            stream.write("\n# Updated validator\n")
        self.assertNotEqual(second, receipt())
        self.assertEqual(1, matches(second))

    def test_legacy_plan_returns_unsupported_without_success_receipt(self):
        self.plan.write_text("# Legacy plan", encoding="utf-8")
        row, = self.event(f"*** Add File: .plans/{self.plan.name}")
        self.assertEqual("unsupported", row["status"])
        self.assertNotIn("receipt", row)

    def test_unreadable_plan_returns_error_without_receipt(self):
        self.plan.write_bytes(b"\xff")
        row, = self.event(f"*** Update File: .plans/{self.plan.name}\n@@\n-old\n+new")
        self.assertEqual("error", row["status"])
        self.assertNotIn("receipt", row)

    def test_registered_hook_runs_from_git_subdirectory(self):
        subprocess.run(["git", "init", "--quiet", str(self.root)], check=True)
        cwd = self.root / "src"
        cwd.mkdir()
        config = json.loads((self.root / ".codex/hooks.json").read_text(encoding="utf-8"))
        group, = config["hooks"]["PostToolUse"]
        self.assertIsNotNone(re.fullmatch(group["matcher"], "apply_patch"))
        handler, = group["hooks"]
        command = handler["commandWindows"] if os.name == "nt" else handler["command"]
        patch = f"*** Begin Patch\n*** Update File: ../.plans/{self.plan.name}\n@@\n-old\n+new\n*** End Patch"
        payload = {"hook_event_name": "PostToolUse", "cwd": str(cwd), "tool_name": "apply_patch",
                   "tool_input": {"command": patch}}
        result = subprocess.run(command, shell=True, cwd=cwd, input=json.dumps(payload),
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, result.returncode, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual("valid", json.loads(context)["status"])
