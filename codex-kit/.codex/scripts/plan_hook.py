"""Codex PostToolUse handler for plan edits. Reads the patch event from stdin."""

import argparse
import hashlib
import json
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = Path(__file__).with_name("plans.py")


def patch_destinations(patch):
    # A Unicode line separator inside a diff line is file content, not a patch header.
    lines = [line.rstrip() for line in patch.strip().split("\n")]
    if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        return
    for index, line in enumerate(lines[1:-1], start=1):
        if index == 1:  # Before the first hunk, indentation cannot be diff context.
            line = line.lstrip()
        if line.startswith("*** Add File: "):
            yield line.removeprefix("*** Add File: ")
        elif line.startswith("*** Update File: "):
            if lines[index + 1].startswith("*** Move to: "):
                yield lines[index + 1].removeprefix("*** Move to: ")
            else:
                yield line.removeprefix("*** Update File: ")


def plan_receipt(path, data, validator_digest):
    name = path.relative_to(ROOT).as_posix().encode("utf-8")
    return hashlib.sha256(validator_digest + hashlib.sha256(data).digest() + name).hexdigest()


def handle(event):
    if event.get("hook_event_name") != "PostToolUse" or event.get("tool_name") not in {"apply_patch", "Edit", "Write"}:
        return
    patch = event.get("tool_input", {}).get("command")
    if not isinstance(patch, str):
        return
    folder = ROOT / ".plans"
    cwd = Path(event["cwd"])
    seen = set()
    rows = []
    validate = None
    for destination in patch_destinations(patch):
        path = (cwd / destination).resolve()
        if path.suffix.lower() != ".md" or not path.is_relative_to(folder) or path in seen:
            continue
        seen.add(path)
        row = {"plan": path.relative_to(ROOT).as_posix()}
        try:
            data = path.read_bytes()
            if validate is None:
                validator_digest = hashlib.sha256(VALIDATOR.read_bytes()).digest()
                validate = runpy.run_path(str(VALIDATOR))["validate"]
                if hashlib.sha256(VALIDATOR.read_bytes()).digest() != validator_digest:
                    raise RuntimeError("Validator changed during hook startup; use plans.py")
            result = validate(data.decode("utf-8-sig"))
            row["status"] = result["status"]
            for key in ("progress", "errors", "warnings"):
                if result.get(key):
                    row[key] = result[key]
            if result["status"] == "valid":
                row["receipt"] = plan_receipt(path, data, validator_digest)
        except (OSError, UnicodeError) as exc:
            row.update(status="error", errors=[str(exc)])
        rows.append(json.dumps(row, ensure_ascii=False))
    if rows:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": "\n".join(rows)}}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matches", nargs=2, metavar=("PLAN", "RECEIPT"),
                        help="Check whether a previous valid result still applies, without running validation")
    args = parser.parse_args()
    if args.matches:
        filename, receipt = args.matches
        try:
            path = Path(filename).resolve()
            if not path.is_relative_to(ROOT / ".plans"):
                return 1
            digest = hashlib.sha256(VALIDATOR.read_bytes()).digest()
            return 0 if plan_receipt(path, path.read_bytes(), digest) == receipt else 1
        except OSError as exc:
            print(exc, file=sys.stderr)
            return 1
    handle(json.load(sys.stdin.buffer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
