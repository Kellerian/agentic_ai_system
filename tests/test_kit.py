"""Behavioral checks for the kit validator and relocation support."""

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "codex-kit"
spec = importlib.util.spec_from_file_location("validate_kit", KIT / ".codex/scripts/validate_kit.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class KitValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "different project"
        shutil.copytree(KIT, self.root)

    def test_relocated_kit_is_self_contained(self):
        self.assertEqual([], validator.validate(self.root))

    def test_broken_role_registration_is_rejected(self):
        (self.root / ".codex/agents/kit_reviewer.toml").unlink()
        self.assertTrue(any("kit_reviewer" in e for e in validator.validate(self.root)))

    def test_missing_ui_roles_are_rejected(self):
        for role in ("kit_ui_designer", "kit_ui_developer"):
            with self.subTest(role=role):
                (self.root / f".codex/agents/{role}.toml").unlink(missing_ok=True)
                self.assertTrue(any(role in e for e in validator.validate(self.root)))

    def test_path_escape_is_rejected(self):
        p = self.root / ".codex/config.toml"
        p.write_text(p.read_text(encoding="utf-8").replace("agents/kit_reviewer.toml", "../../outside.toml"), encoding="utf-8")
        self.assertTrue(any("escapes" in e for e in validator.validate(self.root)))

    def test_unsupported_model_effort_is_rejected(self):
        models = []
        for model in ("gpt-6-luna", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra"):
            models.append({"slug": model, "supported_reasoning_levels": [{"effort": "medium"}]})
        cache = Path(self.temp.name) / "models.json"
        cache.write_text(json.dumps({"models": models}), encoding="utf-8")
        self.assertTrue(any("effort absent" in e for e in validator.validate(self.root, cache)))

    def test_missing_instructions_are_rejected(self):
        p = self.root / ".codex/agents/kit_developer.toml"
        p.write_text(p.read_text(encoding="utf-8").split("developer_instructions")[0], encoding="utf-8")
        self.assertTrue(any("missing developer_instructions" in e for e in validator.validate(self.root)))

    def test_invalid_profile_types_report_errors(self):
        p = self.root / ".codex/agents/kit_developer.toml"
        original = p.read_text(encoding="utf-8")
        cache = Path(self.temp.name) / "models.json"
        cache.write_text(json.dumps({"models": []}), encoding="utf-8")
        for field, old in (("model", '"gpt-6.1-terra"'),
                           ("model_reasoning_effort", '"medium"')):
            with self.subTest(field=field):
                p.write_text(original.replace(f"{field} = {old}", f"{field} = []"), encoding="utf-8")
                errors = validator.validate(self.root, cache)
                self.assertTrue(any(f"kit_developer: invalid {field}" in e for e in errors))

    def test_broken_reference_is_rejected(self):
        (self.root / ".agents/skills/dev-cycle/references/runtime.md").unlink()
        self.assertTrue(any("broken/escaping link" in e for e in validator.validate(self.root)))

    def test_missing_git_workflow_is_rejected(self):
        (self.root / ".agents/skills/git-workflow/SKILL.md").unlink()
        self.assertIn("Missing skill: git-workflow", validator.validate(self.root))

    def test_lost_rule_routes_after_merge_are_rejected(self):
        (self.root / "AGENTS.md").write_text("# Existing project guidance\n", encoding="utf-8")
        self.assertTrue(any("Rule not linked" in e for e in validator.validate(self.root)))

    def test_missing_rule_is_rejected_even_without_its_link(self):
        (self.root / ".codex/rules/data.md").unlink()
        p = self.root / "AGENTS.md"
        p.write_text(p.read_text(encoding="utf-8").replace("[Данные](.codex/rules/data.md)", "Данные"), encoding="utf-8")
        self.assertTrue(any("Missing or empty rule: data" in e for e in validator.validate(self.root)))


if __name__ == "__main__":
    unittest.main()
