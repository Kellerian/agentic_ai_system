"""Create immutable change fragments and build two deterministic Markdown reports.

Python 3.11+, standard library. Git is needed only for committed comparisons/builds.
No version bump, commits, deletion of history, or external publication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import string
import subprocess
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4


ENTRIES = Path(".changes/entries")
KINDS = {"added": "Новое", "changed": "Улучшения", "fixed": "Исправления",
         "removed": "Удалено", "security": "Безопасность", "docs": "Документация",
         "internal": "Внутренние изменения"}
FIELDS = {"schema_version", "id", "created_at", "kind", "component", "user",
          "support", "internal_reason", "breaking", "action_required", "references"}
Snapshot = dict[str, tuple[str, dict]]


class ChangeError(ValueError):
    """Invalid history or a build that cannot safely be produced."""


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def fingerprint(entry: dict) -> str:
    return hashlib.sha256(json_bytes(entry)).hexdigest()


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ChangeError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _text(value: object, field: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ChangeError(f"{field}: nonempty text required")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise ChangeError(f"{field}: control characters are forbidden")


def entry_path(entry: dict) -> Path:
    timestamp = datetime.strptime(entry["created_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
    name = f"{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}_{entry['id']}.json"
    return ENTRIES / timestamp.strftime("%Y") / name


def validate_entry(entry: object, path: str | None = None) -> dict:
    if not isinstance(entry, dict) or set(entry) != FIELDS:
        raise ChangeError("Entry must contain exactly the documented schema fields")
    if type(entry["schema_version"]) is not int or entry["schema_version"] != 1:
        raise ChangeError("Unsupported schema_version")
    try:
        uid = UUID(entry["id"])
        if uid.version != 4 or str(uid) != entry["id"]:
            raise ValueError()
        stamp = entry["created_at"]
        if not isinstance(stamp, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", stamp):
            raise ValueError()
        expected = entry_path(entry).as_posix()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ChangeError("id must be canonical UUID4; created_at must be UTC with six fractional digits") from exc
    if path is not None and path != expected:
        raise ChangeError(f"Filename does not match identity/timestamp: {path}")
    if entry["kind"] not in KINDS:
        raise ChangeError("Unknown kind")
    for field in ("component", "support"):
        _text(entry[field], field)
    if "\n" in entry["component"] or "\r" in entry["component"]:
        raise ChangeError("component must be one line")
    for field in ("user", "internal_reason", "action_required"):
        _text(entry[field], field, optional=True)
    if (entry["user"] is None) != (entry["internal_reason"] is not None):
        raise ChangeError("Specify user text OR internal_reason, not both")
    if entry["kind"] == "internal" and entry["user"] is not None:
        raise ChangeError("internal entries cannot appear in the user report")
    if type(entry["breaking"]) is not bool:
        raise ChangeError("breaking must be boolean")
    if entry["breaking"] and (entry["user"] is None or entry["action_required"] is None):
        raise ChangeError("A breaking change requires user text and action_required")
    if not isinstance(entry["references"], list):
        raise ChangeError("references must be a list")
    for reference in entry["references"]:
        _text(reference, "reference")
    return entry


def _add(snapshot: Snapshot, path: str, raw: bytes) -> None:
    try:
        entry = validate_entry(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys), path)
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ChangeError(f"{path}: {exc}") from exc
    if entry["id"] in snapshot:
        raise ChangeError(f"Duplicate change id: {entry['id']}")
    snapshot[entry["id"]] = (path, entry)


def worktree_snapshot(root: Path) -> Snapshot:
    root = root.resolve()
    folder = root / ENTRIES
    if not folder.resolve().is_relative_to(root):
        raise ChangeError("History directory escapes project root")
    result: Snapshot = {}
    for path in sorted(folder.rglob("*.json")):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ChangeError(f"History file escapes root or is a symlink: {path}")
        _add(result, path.relative_to(root).as_posix(), path.read_bytes())
    return result


def create_entry(root: Path, payload: dict) -> Path:
    root = root.resolve()
    entry = {"schema_version": 1, "id": str(uuid4()),
             "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
             "breaking": False, "action_required": None, "references": [],
             "user": None, "internal_reason": None, **payload}
    if set(payload) & {"id", "created_at", "schema_version"}:
        raise ChangeError("Identity and timestamp are generated, not supplied")
    validate_entry(entry)
    content = json_bytes(entry)  # Encoding must succeed before any final file exists.
    relative = entry_path(entry)
    directory = root
    # Resolve existing directories, not a missing leaf while other writers create its parents.
    for part in relative.parent.parts:
        candidate = directory / part
        try:
            candidate.mkdir()
        except OSError:
            if not candidate.is_dir():
                raise
        directory = candidate.resolve(strict=True)
        if not directory.is_relative_to(root):
            raise ChangeError("History directory escapes project root")
    path = directory / relative.name
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".pending-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic no-clobber publication on the same filesystem. No partial JSON is visible.
        if os.name == "nt":
            os.rename(temporary, path)  # Windows rename fails if destination exists.
        else:
            os.link(temporary, path)  # POSIX rename could overwrite; link cannot.
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def git(root: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], input=input_bytes,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise ChangeError(result.stderr.decode("utf-8", errors="replace").strip() or f"Git failed: {args[0]}")
    return result.stdout


def commit(root: Path, ref: str) -> str:
    if not isinstance(ref, str) or not ref.strip() or ref != ref.strip():
        raise ChangeError("Git reference must be nonempty, without surrounding whitespace")
    actual_root = Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if actual_root != root.resolve():
        raise ChangeError("--root must name the Git repository root")
    return git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()


def git_snapshot(root: Path, revision: str) -> Snapshot:
    rows = git(root, "ls-tree", "-r", "-z", revision, "--", ENTRIES.as_posix()).split(b"\0")
    objects = []
    for row in filter(None, rows):
        metadata, path_bytes = row.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        path = path_bytes.decode("utf-8")
        if not path.endswith(".json"):
            continue
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise ChangeError(f"Entry must be a regular file: {path}")
        objects.append((oid, path))
    result: Snapshot = {}
    if not objects:
        return result
    raw = git(root, "cat-file", "--batch", input_bytes=b"".join(oid + b"\n" for oid, _ in objects))
    offset = 0
    for oid, path in objects:
        end = raw.index(b"\n", offset)
        found, kind, size_bytes = raw[offset:end].split()
        size = int(size_bytes)
        if found != oid or kind != b"blob":
            raise ChangeError("Unexpected Git object response")
        start = end + 1
        _add(result, path, raw[start:start + size])
        offset = start + size + 1
    return result


def select_entries(base: Snapshot, head: Snapshot) -> Snapshot:
    for uid, (_, old) in base.items():
        if uid not in head:
            raise ChangeError(f"Previously recorded entry was deleted: {uid}")
        if fingerprint(old) != fingerprint(head[uid][1]):
            raise ChangeError(f"Previously recorded entry was changed: {uid}; add a new correction instead")
    return {uid: pair for uid, pair in head.items() if uid not in base}


def compare(root: Path, base_ref: str | None, head_ref: str, *, preview: bool = False) -> tuple[Snapshot, str | None, str | None]:
    base_id = commit(root, base_ref) if base_ref is not None else None
    head_id = None if preview else commit(root, head_ref)
    if base_id and head_id:
        git(root, "merge-base", "--is-ancestor", base_id, head_id)
    base = git_snapshot(root, base_id) if base_id else {}
    head = worktree_snapshot(root) if preview else git_snapshot(root, head_id)
    return select_entries(base, head), base_id, head_id


def _markdown(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    literal = "".join("\\" + ch if ch in string.punctuation else ch for ch in normalized)
    # Preserve leading whitespace as text, without creating indented code blocks.
    lines = ["&#32;" + line[1:] if line.startswith(" ") else
             "&#9;" + line[1:] if line.startswith("\t") else line
             for line in literal.split("\n")]
    return "\n  ".join(lines)


def render(entries: Snapshot, release: str, release_date: str, audience: str, *, draft: bool) -> str:
    title = "Изменения" if audience == "user" else "Изменения для технической поддержки"
    lines = [f"# {title}: {_markdown(release)}", "", f"Дата выпуска: {release_date}", ""]
    if draft:
        lines += ["> ЧЕРНОВИК: рабочее дерево, не состав опубликованной сборки.", ""]
    total = 0
    for kind, label in KINDS.items():
        rows = [entry for _, entry in entries.values() if entry["kind"] == kind and (audience == "support" or entry["user"] is not None)]
        if not rows:
            continue
        lines += [f"## {label}", ""]
        for entry in sorted(rows, key=lambda e: (e["component"].casefold(), e["created_at"], e["id"])):
            total += 1
            text = entry["user"] if audience == "user" else entry["support"]
            lines += [f"- **{_markdown(entry['component'])}.** {_markdown(text)}"]
            if entry["breaking"]:
                lines += ["  - **Изменение совместимости.**"]
            if entry["action_required"]:
                lines += [f"  - Действие: {_markdown(entry['action_required'])}"]
            if audience == "support":
                lines += [f"  - ID: `{entry['id']}`; записано: {entry['created_at']}"]
                if entry["internal_reason"]:
                    lines += [f"  - Внутреннее: {_markdown(entry['internal_reason'])}"]
                if entry["references"]:
                    lines += ["  - Связи: " + "; ".join(_markdown(x) for x in entry["references"])]
        lines.append("")
    if not total:
        lines += ["Записей для этой аудитории нет.", ""]
    return "\n".join(lines)


def build_output(entries: Snapshot, output: Path, release: str, release_date: str,
                 base_id: str | None, head_id: str | None, *, draft: bool = False) -> None:
    _text(release, "release")
    if "\n" in release or "\r" in release:
        raise ChangeError("release must be one line")
    if date.fromisoformat(release_date).isoformat() != release_date:
        raise ChangeError("date must be YYYY-MM-DD")
    manifest = {"schema_version": 1, "release": release, "date": release_date,
                "draft": draft, "base_commit": base_id, "source_commit": head_id,
                "entries": [{"id": uid, "file": path, "sha256": fingerprint(entry)}
                            for uid, (path, entry) in sorted(entries.items())]}
    files = {"CHANGELOG.md": render(entries, release, release_date, "user", draft=draft).encode("utf-8"),
             "SUPPORT-CHANGELOG.md": render(entries, release, release_date, "support", draft=draft).encode("utf-8"),
             "manifest.json": json_bytes(manifest)}
    output.mkdir(parents=True, exist_ok=False)
    # Manifest is written last. A failed build is not a release artifact.
    for name, content in files.items():
        with (output / name).open("xb") as stream:
            stream.write(content)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="Record one completed logical change")
    new.add_argument("--kind", choices=KINDS, required=True)
    new.add_argument("--component", required=True)
    audience = new.add_mutually_exclusive_group(required=True)
    audience.add_argument("--user")
    audience.add_argument("--internal-reason")
    new.add_argument("--support", required=True)
    new.add_argument("--ref", action="append", default=[])
    new.add_argument("--breaking", action="store_true")
    new.add_argument("--action-required")
    check = sub.add_parser("check", help="Validate working entries, or a committed comparison for CI")
    check.add_argument("--base")
    check.add_argument("--head", default="HEAD")
    check.add_argument("--require-entry", action="store_true")
    for command in ("build", "preview"):
        build = sub.add_parser(command, help="Build from commits" if command == "build" else "Draft from working files")
        baseline = build.add_mutually_exclusive_group(required=True)
        baseline.add_argument("--base")
        baseline.add_argument("--all", action="store_true", help="Explicit first release / full-history report")
        if command == "build":
            build.add_argument("--head", default="HEAD")
        build.add_argument("--release", required=True, help="Label supplied by your build, never computed here")
        build.add_argument("--date", required=True)
        build.add_argument("--output", type=Path, required=True, help="New output directory; never overwritten")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "new":
            path = create_entry(root, {"kind": args.kind, "component": args.component,
                "user": args.user, "support": args.support, "internal_reason": args.internal_reason,
                "references": args.ref, "breaking": args.breaking, "action_required": args.action_required})
            print(path)
        elif args.command == "check":
            if args.require_entry and args.base is None:
                raise ChangeError("--require-entry needs --base and a committed comparison")
            if args.base is not None:
                selected, base_id, head_id = compare(root, args.base, args.head)
                changed = git(root, "diff", "--no-renames", "--name-only", "-z", base_id, head_id, "--").split(b"\0")
                if args.require_entry and any(p and not p.startswith(b".changes/entries/") for p in changed) and not selected:
                    raise ChangeError("Changed project files require at least one new change entry")
            else:
                selected = worktree_snapshot(root)
            print(f"PASS: {len(selected)} entries")
        else:
            draft = args.command == "preview"
            selected, base_id, head_id = compare(root, args.base, getattr(args, "head", "HEAD"), preview=draft)
            output = args.output.resolve()
            if output == root or output.is_relative_to((root / ENTRIES).resolve()):
                raise ChangeError("Output must not be the repository root or the entries directory")
            build_output(selected, output, args.release, args.date, base_id, head_id, draft=draft)
            print(f"{'DRAFT' if draft else 'BUILD'}: {len(selected)} entries -> {output}")
        return 0
    except (ChangeError, OSError, ValueError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
