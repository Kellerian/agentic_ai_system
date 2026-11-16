"""Read-only validation and progress for Markdown plans, format 1 (Python 3.11+)."""

import argparse
from collections import Counter
from datetime import date
import json
from pathlib import Path
import re
import sys


PLAN_STATES = {"draft", "ready", "in_progress", "blocked", "paused", "completed", "cancelled"}
TASK_STATES = {"todo", "in_progress", "review", "blocked", "done", "cancelled"}
FIELDS = {"Статус", "Зависимости", "Результат", "Блокер", "Разблокировка", "Причина"}


def visible_lines(text):
    """Skip examples and comments so their task headings cannot affect progress."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        yield line


def fields(lines, allowed, errors, scope):
    result = {}
    for line in lines:
        key, sep, value = line.partition(":")
        if not sep or key not in allowed:
            continue
        if key in result:
            errors.append(f"{scope}: duplicate field {key}")
        result[key] = value.strip()
    return result


def validate(text):
    errors = []
    lines = list(visible_lines(text.lstrip("\ufeff")))
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        return {"status": "unsupported", "errors": ["Expected plan_format: 1 frontmatter"]}
    end = lines.index("---", 1)
    meta = fields(lines[1:end], {"plan_format", "title", "status", "created", "updated"}, errors, "plan")
    if meta.get("plan_format") != "1":
        return {"status": "unsupported", "errors": ["Expected plan_format: 1; legacy plan is unchanged"]}
    state = meta.get("status")
    if state not in PLAN_STATES:
        errors.append(f"plan: invalid status {state!r}")
    if not meta.get("title", "").strip('"\' '):
        errors.append("plan: missing title")
    dates = {}
    for key in ("created", "updated"):
        try:
            value = meta.get(key, "")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError()
            dates[key] = date.fromisoformat(value)
        except ValueError:
            errors.append(f"plan: invalid {key} date, expected YYYY-MM-DD")
    if len(dates) == 2 and dates["updated"] < dates["created"]:
        errors.append("plan: updated precedes created")

    tasks = {}
    sections = []
    current_lines = []
    task_id = None
    for line in lines[end + 1:]:
        if re.match(r"^#{1,3} ", line):
            if task_id is not None:
                sections.append((task_id, current_lines))
            task_id = None
            current_lines = []
            if line.startswith("### T"):
                match = re.fullmatch(r"### (T[0-9]{2,}) — (\S.*)", line)
                if not match:
                    errors.append(f"Malformed task heading: {line}")
                else:
                    task_id = match[1]
        current_lines.append(line)
    if task_id is not None:
        sections.append((task_id, current_lines))
    for identifier, body in sections:
        if identifier in tasks:
            errors.append(f"{identifier}: duplicate task ID")
            continue
        data = fields(body, FIELDS, errors, identifier)
        if data.get("Статус") not in TASK_STATES:
            errors.append(f"{identifier}: invalid status")
        dependency_text = data.get("Зависимости", "")
        if dependency_text == "нет":
            dependencies = []
        elif re.fullmatch(r"T[0-9]{2,}(?:,\s*T[0-9]{2,})*", dependency_text):
            dependencies = [part.strip() for part in dependency_text.split(",")]
            if len(set(dependencies)) != len(dependencies):
                errors.append(f"{identifier}: duplicate dependencies")
        else:
            errors.append(f"{identifier}: invalid dependencies, use IDs separated by commas or нет")
            dependencies = []
        data["dependencies"] = dependencies
        required = {"done": ["Результат"], "blocked": ["Блокер", "Разблокировка"],
                    "cancelled": ["Причина"]}.get(data.get("Статус"), [])
        for key in required:
            if not data.get(key):
                errors.append(f"{identifier}: missing {key}")
        tasks[identifier] = data

    for identifier, data in tasks.items():
        for dependency in data["dependencies"]:
            if dependency not in tasks:
                errors.append(f"{identifier}: unknown dependency {dependency}")
                continue
            dep_state = tasks[dependency].get("Статус")
            if dep_state == "cancelled" and data.get("Статус") != "cancelled":
                errors.append(f"{identifier}: cancelled dependency {dependency}; revise scope/dependencies")
            if data.get("Статус") in {"in_progress", "review", "done"} and dep_state != "done":
                errors.append(f"{identifier}: unfinished dependency {dependency}")

    # Topological elimination avoids recursion limits on large plans.
    pending = {key: set(data["dependencies"]) & tasks.keys() for key, data in tasks.items()}
    while pending:
        roots = {key for key, deps in pending.items() if not deps}
        if not roots:
            errors.append("dependency cycle detected")
            break
        pending = {key: deps - roots for key, deps in pending.items() if key not in roots}

    counts = Counter(data.get("Статус") for data in tasks.values())
    total = len(tasks) - counts["cancelled"]
    ready = [key for key, data in tasks.items() if data.get("Статус") == "todo"
             and all(tasks.get(dep, {}).get("Статус") == "done" for dep in data["dependencies"])]
    if state not in {"draft", "cancelled"} and total == 0:
        errors.append("plan: requires at least one active task")
    if state == "completed" and counts["done"] != total:
        errors.append("plan: completed with unfinished tasks")
    if state in {"draft", "ready"} and any(counts[s] for s in ("in_progress", "review", "done")):
        errors.append("plan: started tasks require an execution status")
    if state in {"blocked", "cancelled"} and any(counts[s] for s in ("in_progress", "review")):
        errors.append("plan: active execution conflicts with plan status")
    if state == "blocked" and (ready or not counts["blocked"]):
        errors.append("plan: blocked requires blocked work and no ready tasks")
    current = []
    recording = False
    for line in lines[end + 1:]:
        if re.match(r"^#{1,2} ", line):
            recording = line == "## Текущее состояние"
        if recording:
            current.append(line)
    context = fields(current, FIELDS, errors, "plan state")
    required = {"blocked": ["Блокер", "Разблокировка"], "paused": ["Причина"],
                "cancelled": ["Причина"]}.get(state, [])
    for key in required:
        if not context.get(key):
            errors.append(f"plan state: missing {key}")
    return {"status": "invalid" if errors else "valid", "plan_status": state, "errors": errors,
            "counts": dict(counts), "ready": ready,
            "progress": {"done": counts["done"], "total": total,
                         "percent": round(100 * counts["done"] / total, 1) if total else None}}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--json", action="store_true", help="Machine-readable result")
    args = parser.parse_args()
    try:
        result = validate(args.plan.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        result = {"status": "error", "errors": [str(exc)]}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"{result['status'].upper()}: {args.plan}")
        if "progress" in result:
            progress = result["progress"]
            print(f"Done: {progress['done']}/{progress['total']}; percent: {progress['percent']}")
            print("States: " + json.dumps(result["counts"], ensure_ascii=False))
            print("Dependency-ready: " + ", ".join(result["ready"]))
        for error in result["errors"]:
            print(error)
    return {"valid": 0, "invalid": 1}.get(result["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
