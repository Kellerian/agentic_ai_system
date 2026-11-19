"""Validate the copyable kit using Python 3.11+ and the standard library only."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path


EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
ROLES = {"kit_explorer", "kit_planner", "kit_developer", "kit_reviewer", "kit_documenter", "kit_expert",
         "kit_ui_designer", "kit_ui_developer"}
RULES = {"engineering", "contracts", "distributed", "data", "security", "operations", "client", "history", "performance", "ux-ui"}


def has_plan_hook(groups: list) -> bool:
    for group in groups:
        for hook in group.get("hooks", []):
            command = hook.get("command", "")
            windows = hook.get("commandWindows", command)
            if hook.get("type") == "command" and "plan_hook.py" in command and "plan_hook.py" in windows:
                matcher = group.get("matcher")
                if matcher in (None, "", "*") or any(re.search(matcher, name) for name in ("apply_patch", "Edit", "Write")):
                    return True
    return False


def validate(root: Path, models_cache: Path | None = None) -> list[str]:
    """Check package coherence, local references and optional model compatibility."""
    root = root.resolve()
    errors: list[str] = []
    parsed: dict[Path, dict] = {}
    for path in sorted((root / ".codex").rglob("*.toml")):
        if not path.resolve().is_relative_to(root / ".codex"):
            errors.append(f"{path.relative_to(root)}: escapes .codex")
            continue
        try:
            parsed[path] = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            errors.append(f"{path.relative_to(root)}: {exc}")

    for relative in ("AGENTS.md", ".codex/project.md", ".codex/config.toml",
                     ".changes/README.md", ".codex/scripts/changes.py", ".codex/scripts/plans.py",
                     ".codex/scripts/plan_hook.py", ".codex/hooks.json"):
        if not (root / relative).is_file():
            errors.append(f"Missing: {relative}")

    hook_file = root / ".codex/hooks.json"
    if hook_file.is_file():
        try:
            hook_config = json.loads(hook_file.read_text(encoding="utf-8-sig"))
            post_tool = hook_config["hooks"]["PostToolUse"]
            if not isinstance(post_tool, list) or not has_plan_hook(post_tool):
                raise ValueError("missing PostToolUse command for apply_patch and plan_hook.py")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, re.error) as exc:
            errors.append(f".codex/hooks.json: {exc}")

    guidance = root / "AGENTS.md"
    routed = set()
    if guidance.is_file():
        targets = re.findall(r"\[[^\]]*\]\(([^)]+)\)", guidance.read_text(encoding="utf-8-sig"))
        routed = {(root / t.split("#", 1)[0]).resolve() for t in targets if "://" not in t}
    for name in sorted(RULES):
        path = root / ".codex/rules" / f"{name}.md"
        if not path.is_file() or not path.read_text(encoding="utf-8-sig").strip():
            errors.append(f"Missing or empty rule: {name}")
        if path.resolve() not in routed:
            errors.append(f"Rule not linked from AGENTS.md: {name}")

    config = parsed.get(root / ".codex/config.toml", {})
    agents = config.get("agents", {})
    if not isinstance(agents, dict):
        errors.append("config: agents must be a table")
        agents = {}
    models = None
    if models_cache:
        try:
            cache = json.loads(models_cache.read_text(encoding="utf-8-sig"))
            models = {
                item["slug"]: {level["effort"] for level in item["supported_reasoning_levels"]}
                for item in cache["models"]
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"Cannot read model cache: {exc}")

    role_dir = root / ".codex/agents"
    seen_roles = set()
    for path, spec in parsed.items():
        name = spec.get("name")
        if path.parent == role_dir and isinstance(name, str) and name in ROLES:
            if name in seen_roles:
                errors.append(f"{name}: duplicate agent name in {path.name}")
            seen_roles.add(name)

    for role in sorted(ROLES):
        path = role_dir / f"{role}.toml"
        if role in agents:  # Earlier kit versions also declared each role in config.toml.
            declaration = agents[role]
            relative = declaration.get("config_file") if isinstance(declaration, dict) else None
            if relative != f"agents/{role}.toml":
                errors.append(f"{role}: legacy config_file escapes or differs from the kit role path")
        spec = parsed.get(path)
        if spec is None:
            errors.append(f"{role}: missing or invalid agent file")
            continue
        if spec.get("name") != role:
            errors.append(f"{role}: name mismatch")
        invalid_fields = False
        for key in ("description", "developer_instructions", "model", "model_reasoning_effort"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                label = "missing" if key not in spec else "invalid"
                errors.append(f"{role}: {label} {key}; expected a nonempty string")
                invalid_fields = True
        if invalid_fields:
            continue
        effort = spec.get("model_reasoning_effort")
        if effort not in EFFORTS:
            errors.append(f"{role}: unsupported effort {effort!r}")
        if models is not None:
            model = spec.get("model")
            if model not in models:
                errors.append(f"{role}: model absent from supplied cache: {model}")
            elif effort not in models[model]:
                errors.append(f"{role}: effort absent from supplied model capabilities")

    for skill in ("dev-cycle", "dev-plan", "dev-review", "dev-docs", "git-workflow"):
        path = root / ".agents/skills" / skill / "SKILL.md"
        if not path.is_file():
            errors.append(f"Missing skill: {skill}")
            continue
        text = path.read_text(encoding="utf-8-sig")
        parts = text.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            errors.append(f"{skill}: missing frontmatter")
        else:
            # The kit deliberately uses only single-line name/description YAML fields.
            fields = dict(re.findall(r"^(name|description):\s*(.+)$", parts[1], re.MULTILINE))
            if fields.get("name") != skill or not fields.get("description", "").strip():
                errors.append(f"{skill}: invalid name/description")

    for path in list((root / ".agents").rglob("*.md")) + list((root / ".codex").rglob("*.md")) + [root / "AGENTS.md"]:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig")
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            if not resolved.is_relative_to(root) or not resolved.exists():
                errors.append(f"{path.relative_to(root)}: broken/escaping link {target}")
        project_context = path == root / ".codex/project.md"
        if ".cursor/" in text or (not project_context and re.search(r"[A-Za-z]:[\\/]", text)):
            errors.append(f"{path.relative_to(root)}: nonportable project dependency")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--models-cache", type=Path, help="Optional local Codex models_cache.json; no network")
    args = parser.parse_args()
    errors = validate(args.root, args.models_cache)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("PASS: TOML, roles, skills, rule routing and local links")
    print("Model cache: checked" if args.models_cache else "Model availability: not checked")
    print("Native client loading and task quality require a live smoke test.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
