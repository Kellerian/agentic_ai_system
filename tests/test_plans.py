"""Plan validation through the portable command-line entry point."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "codex-kit/.codex/scripts/plans.py"


def task(identifier, status="todo", deps="нет", extra=""):
    return f"\n### {identifier} — Result\nСтатус: {status}\nЗависимости: {deps}\n{extra}\n"


def plan(tasks, status="in_progress", extra=""):
    return f'''---
plan_format: 1
title: "Example"
status: {status}
created: 2026-11-16
updated: 2026-11-16
---
{tasks}
## Текущее состояние
{extra}
'''


class PlanTests(unittest.TestCase):
    def test_filled_template_and_reopened_dependency(self):
        template = SCRIPT.parents[2] / ".agents/skills/dev-plan/references/plan-template.md"
        content = template.read_text(encoding="utf-8").replace("YYYY-MM-DD", "2026-11-16")
        code, result = self.run_plan(content)
        self.assertEqual(0, code)
        self.assertEqual(["T01"], result["ready"])
        tasks = task("T01", "done", extra="Результат: verified") + task("T02", "done", "T01", "Результат: verified")
        self.assertEqual(0, self.run_plan(plan(tasks, "completed"))[0])
        reopened = tasks.replace("Статус: done", "Статус: todo", 1)
        self.assertEqual(1, self.run_plan(plan(reopened))[0])

    def test_missing_file_returns_json_error(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(SCRIPT), str(Path(directory)/"missing.md"), "--json"],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(2, result.returncode)
            self.assertEqual("error", json.loads(result.stdout)["status"])

    def run_plan(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan with spaces.md"
            path.write_text(text, encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), str(path), "--json"],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(text, path.read_text(encoding="utf-8"))
            return result.returncode, json.loads(result.stdout)

    def test_progress_and_ready_tasks(self):
        code, result = self.run_plan(plan(task("T01", "done", extra="Результат: tests PASS")
                                         + task("T02", "review", "T01") + task("T03")
                                         + task("T04", "cancelled", extra="Причина: removed scope")))
        self.assertEqual(0, code)
        self.assertEqual({"done": 1, "total": 3, "percent": 33.3}, result["progress"])
        self.assertEqual(["T03"], result["ready"])

    def test_duplicate_unknown_self_and_cycles(self):
        for tasks, error in [(task("T01") * 2, "duplicate"),
                             (task("T01", deps="T99"), "unknown"),
                             (task("T01", deps="T01"), "cycle"),
                             (task("T01", deps="T02") + task("T02", deps="T01"), "cycle")]:
            with self.subTest(error=error):
                code, result = self.run_plan(plan(tasks))
                self.assertEqual(1, code)
                self.assertTrue(any(error in e for e in result["errors"]))

    def test_unfinished_dependency_rejects_started_work(self):
        for status in ("in_progress", "review", "done"):
            code, result = self.run_plan(plan(task("T01") + task("T02", status, "T01", "Результат: checked")))
            self.assertEqual(1, code)
            self.assertTrue(any("unfinished" in e for e in result["errors"]))

    def test_cancelled_dependency_requires_resolution(self):
        code, result = self.run_plan(plan(task("T01", "cancelled", extra="Причина: obsolete")
                                         + task("T02", deps="T01")))
        self.assertEqual(1, code)
        self.assertTrue(any("cancelled dependency" in e for e in result["errors"]))

    def test_completion_and_zero_denominator(self):
        self.assertEqual(1, self.run_plan(plan(task("T01"), "completed"))[0])
        self.assertEqual(0, self.run_plan(plan(task("T01", "done", extra="Результат: checked"), "completed"))[0])
        code, result = self.run_plan(plan(task("T01", "cancelled", extra="Причина: obsolete"),
                                         "cancelled", "Причина: withdrawn"))
        self.assertEqual(0, code)
        self.assertIsNone(result["progress"]["percent"])

    def test_blocking_and_pause(self):
        tasks = task("T01", "blocked", extra="Блокер: access\nРазблокировка: grant access")
        self.assertEqual(0, self.run_plan(plan(tasks, "blocked", "Блокер: access\nРазблокировка: grant access"))[0])
        self.assertEqual(1, self.run_plan(plan(tasks + task("T02"), "blocked",
                                              "Блокер: access\nРазблокировка: grant access"))[0])
        self.assertEqual(1, self.run_plan(plan(tasks, "paused"))[0])
        self.assertEqual(0, self.run_plan(plan(tasks, "paused", "Причина: user postponed"))[0])

    def test_invalid_fields_and_evidence(self):
        for content in [plan(task("T01", "green")), plan(task("T01"), "green"),
                        plan(task("T01", "done")), plan(task("T01", "blocked")),
                        plan(task("T01")).replace("updated: 2026-11-16", "updated: 2026-11-15"),
                        plan(task("T01")).replace("Зависимости: нет", "Зависимости: maybe"),
                        plan(task("T01")).replace("Статус: todo", "Статус: todo\nСтатус: done")]:
            with self.subTest(content=content):
                self.assertEqual(1, self.run_plan(content)[0])

    def test_examples_in_fences_and_comments_are_ignored(self):
        content = plan(task("T01") + "\n```md\n" + task("T02", "bad") + "\n```\n"
                       + "<!--\n" + task("T03", "bad") + "\n-->\n")
        code, result = self.run_plan(content)
        self.assertEqual(0, code)
        self.assertEqual(1, result["progress"]["total"])

    def test_legacy_is_explicitly_unsupported(self):
        code, result = self.run_plan("# Old plan\n- [x] Done\n- [ ] Pending\n")
        self.assertEqual(2, code)
        self.assertEqual("unsupported", result["status"])

    def test_draft_can_be_empty_but_ready_cannot(self):
        self.assertEqual(0, self.run_plan(plan("", "draft"))[0])
        self.assertEqual(1, self.run_plan(plan("", "ready"))[0])


if __name__ == "__main__":
    unittest.main()
