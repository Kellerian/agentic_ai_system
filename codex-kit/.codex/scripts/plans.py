"""Read-only validation and progress for Markdown plans, format 1 (Python 3.11+)."""

import argparse
from collections import Counter, defaultdict
from datetime import date
from graphlib import CycleError, TopologicalSorter
import json
from pathlib import Path
import re
import sys


PLAN_STATES = {"draft", "ready", "in_progress", "blocked", "paused", "completed", "cancelled"}
TASK_STATES = {"todo", "in_progress", "review", "blocked", "done", "cancelled"}
FIELDS = {"Статус", "Зависимости", "Результат", "Блокер", "Разблокировка", "Причина"}


INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)")


def visible_lines(text, errors):
    """Skip examples and comments so their task headings cannot affect progress."""
    fence = None
    in_comment = False
    for line in text.splitlines():
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker and not in_comment:
            fence = marker[1]
            continue
        visible = []
        while line:
            if in_comment:
                _, close, line = line.partition("-->")
                in_comment = not close
            else:
                start = line.find("<!--")
                if start >= 0 and "`" in line:
                    masked = INLINE_CODE.sub(lambda match: " " * len(match[0]), line)
                    start = masked.find("<!--")
                if start < 0:
                    visible.append(line)
                    break
                visible.append(line[:start])
                line = line[start + 4:]
                in_comment = True
        yield "".join(visible).rstrip()
    if in_comment or fence:
        errors.append("plan: unclosed comment or code fence")


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
    lines = list(visible_lines(text.lstrip("\ufeff"), errors))
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
    in_tasks = False
    for line in lines[end + 1:]:
        heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.*)$", line)
        if heading:
            marks, title = heading.groups()
            task = re.fullmatch(r"(T[0-9]{2,}) — \S.*", title)
            if len(marks) > 3 and not task:
                current_lines.append(line)
                continue
            if task_id is not None:
                sections.append((task_id, current_lines))
            task_id = None
            current_lines = []
            if task:
                task_id = task[1]
                if marks != "###":
                    errors.append(f"{task_id}: expected task heading '### {title}', found '{line}'")
            elif len(marks) < 3:
                in_tasks = marks == "##" and title == "Задачи"
            elif in_tasks or title.startswith("T"):
                errors.append(f"Malformed task heading: {line}")
        current_lines.append(line)
    if task_id is not None:
        sections.append((task_id, current_lines))
    title_groups = defaultdict(list)
    for identifier, body in sections:
        if identifier in tasks:
            errors.append(f"{identifier}: duplicate task ID")
            continue
        data = fields(body, FIELDS, errors, identifier)
        if state in {"draft", "ready"} and data.get("Статус") in {"todo", "blocked"}:
            title = body[0].partition(" — ")[2]
            title_groups[" ".join(title.casefold().split())].append(identifier)
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

    try:
        TopologicalSorter({key: data["dependencies"] for key, data in tasks.items()}).prepare()
    except CycleError as exc:
        errors.append("dependency cycle detected: " + " -> ".join(exc.args[1]))

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
    if state == "ready" and not ready:
        errors.append("plan: ready requires a task that can start")
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
    warnings = []
    for identifiers in title_groups.values():
        if len(identifiers) > 1:
            examples = ", ".join(identifiers[:3])
            warnings.append(f"{examples}: одинаковое название у {len(identifiers)} задач. Сверь ожидаемые результаты.")
    return {"status": "invalid" if errors else "valid", "plan_status": state, "errors": errors, "warnings": warnings,
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
        for warning in result.get("warnings", []):
            print(f"Warning: {warning}")
    return {"valid": 0, "invalid": 1}.get(result["status"], 2)


if __name__ == "__main__":
    sys.exit(main())
