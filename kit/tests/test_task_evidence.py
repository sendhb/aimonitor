import importlib.machinery
import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

import os
_TASK_PATH = "kit/cli/task" if os.path.isfile("kit/cli/task") else "cli/task"
loader = importlib.machinery.SourceFileLoader("task_cli", _TASK_PATH)
spec = importlib.util.spec_from_loader(loader.name, loader)
task = importlib.util.module_from_spec(spec)
loader.exec_module(task)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        task.VERIFY_DIR = str(root / "verification")
        task.REVIEW_DIR = str(root / "reviews")
        Path(task.VERIFY_DIR).mkdir()
        Path(task.REVIEW_DIR).mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, directory, name, body):
        # 显式 UTF-8（TASK-008）：Windows 默认 locale(GBK) 写含中文夹具会被 task 以 UTF-8 读时报错
        Path(directory, name).write_text(body, encoding="utf-8")

    def make_verify_body(self, **overrides):
        """Build a minimal passing VERIFY frontmatter."""
        fields = {
            "name": "VERIFY-2026-08-01-task-001",
            "description": "test verify",
            "type": "verify",
            "date": task.today(),
            "task-ref": "TASK-001",
            "verifier": "tester",
            "result": "pass",
            "commit": "abc1234",
        }
        fields.update(overrides)
        return (
            "---\n"
            f"name: {fields['name']}\n"
            f"description: {fields['description']}\n"
            "metadata:\n"
            f"  type: {fields['type']}\n"
            f"  date: {fields['date']}\n"
            f"  task-ref: {fields['task-ref']}\n"
            f"  verifier: {fields['verifier']}\n"
            f"  result: {fields['result']}\n"
            f"  commit: {fields['commit']}\n"
            "---\n"
        )

    def test_verify_must_be_current_and_pass(self):
        self.write(task.VERIFY_DIR, "VERIFY-old.md", "---\nmetadata:\n  type: verify\n  date: 2000-01-01\n  task-ref: TASK-001\n  result: pass\n---\n")
        self.assertFalse(task.verify_exists("TASK-001"))
        self.write(task.VERIFY_DIR, "VERIFY-pass.md", self.make_verify_body())
        self.assertTrue(task.verify_exists("TASK-001"))

    def test_review_requires_distinct_author(self):
        body = f"---\nmetadata:\n  type: review\n  date: {task.today()}\n  task-ref: TASK-001\n  result: pass\n  reviewer: codex\n  implementation-author: codex\n---\n"
        self.write(task.REVIEW_DIR, "REVIEW-same.md", body)
        self.assertFalse(task.review_exists("TASK-001", {"metadata.assignee": "codex"}))
        self.write(task.REVIEW_DIR, "REVIEW-independent.md", body.replace("reviewer: codex", "reviewer: human").replace("implementation-author: codex", "implementation-author: codex"))
        self.assertTrue(task.review_exists("TASK-001", {"metadata.assignee": "codex"}))

    # --- BUG-002 regression: required-field validation ---

    def test_verify_missing_name_rejected(self):
        self.write(task.VERIFY_DIR, "VERIFY-no-name.md", self.make_verify_body(name=""))
        self.assertFalse(task.verify_exists("TASK-001"))

    def test_verify_missing_description_rejected(self):
        self.write(task.VERIFY_DIR, "VERIFY-no-desc.md", self.make_verify_body(description=""))
        self.assertFalse(task.verify_exists("TASK-001"))

    def test_verify_missing_verifier_rejected(self):
        self.write(task.VERIFY_DIR, "VERIFY-no-verifier.md", self.make_verify_body(verifier=""))
        self.assertFalse(task.verify_exists("TASK-001"))

    def test_verify_missing_commit_rejected(self):
        self.write(task.VERIFY_DIR, "VERIFY-no-commit.md", self.make_verify_body(commit=""))
        self.assertFalse(task.verify_exists("TASK-001"))

    def test_verify_no_frontmatter_rejected(self):
        self.write(task.VERIFY_DIR, "VERIFY-no-fm.md",
                   f"name: VERIFY-no-fm\ndescription: bad\nmetadata:\n  type: verify\n  date: {task.today()}\n  task-ref: TASK-001\n  verifier: t\n  result: pass\n  commit: abc\n")
        self.assertFalse(task.verify_exists("TASK-001"))

    def test_verify_all_required_fields_accepted(self):
        self.write(task.VERIFY_DIR, "VERIFY-ok.md", self.make_verify_body())
        self.assertTrue(task.verify_exists("TASK-001"))


class CmdDoneTests(unittest.TestCase):
    """Regression: BUG-001 — cmd_done must not reference undefined force."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        task.VERIFY_DIR = str(root / "verification")
        task.REVIEW_DIR = str(root / "reviews")
        task.TASKS_DIR = str(root / "tasks")
        task.STATE_DIR = str(root / "states")
        task.LOG_DIR = str(root / "logs")  # TASK-066：隔离事件流，防污染真实 runtime/logs
        Path(task.VERIFY_DIR).mkdir()
        Path(task.REVIEW_DIR).mkdir()
        Path(task.TASKS_DIR).mkdir()
        Path(task.STATE_DIR).mkdir()
        Path(task.LOG_DIR).mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_cmd_done_does_not_raise_nameerror(self):
        # Create a task file that meets all done prerequisites
        task_body = f"""---
name: TASK-001-test-slug
description: test
tags: []
metadata:
  type: task
  status: in-progress
  created: {task.today()}
  updated: {task.today()}
  priority: P2
  risk: P2
  approval-ref: none
  assignee: any
  reviewer: any
  parent: TASK-000
  depends-on: []
---
# TASK-001 — test

## 验收标准
- [x] done
"""
        Path(task.TASKS_DIR, "TASK-001-test-slug.md").write_text(task_body, encoding="utf-8")
        # Create a valid VERIFY record
        Path(task.VERIFY_DIR, "VERIFY-ok.md").write_text(
            f"---\nname: VERIFY-ok\ndescription: ok\nmetadata:\n  type: verify\n  date: {task.today()}\n  task-ref: TASK-001\n  verifier: t\n  result: pass\n  commit: abc\n---\n", encoding="utf-8")
        # cmd_done should not raise NameError (BUG-001 regression)
        try:
            task.cmd_done(["TASK-001"])
        except NameError as e:
            self.fail(f"cmd_done raised NameError (BUG-001 regression): {e}")
        except SystemExit:
            # die() raises SystemExit for unmet preconditions — acceptable,
            # the important thing is it didn't raise NameError
            pass


class VerifyCommandTests(unittest.TestCase):
    """Regression: cli/task verify must actually run commands.*, not trust a hand-written record."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task.AI_DIR = str(self.root)
        task.VERIFY_DIR = str(self.root / "verification")
        task.TASKS_DIR = str(self.root / "tasks")
        task.LOG_DIR = str(self.root / "logs")
        Path(task.VERIFY_DIR).mkdir(parents=True)
        Path(task.TASKS_DIR).mkdir(parents=True)
        Path(task.TASKS_DIR, "TASK-001-x.md").write_text(
            f"---\nname: TASK-001-x\nmetadata:\n  type: task\n  status: in-progress\n"
            f"  created: {task.today()}\n  updated: {task.today()}\n---\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def write_config(self, check_cmd="true"):
        (self.root / "aios.config.yaml").write_text(
            "version: 1\nprofile: test\nsource_dirs:\n  - src/\ngenerated_dirs:\n  - dist/\n"
            f"commands:\n  build: true\n  lint: true\n  test: true\n  check: {check_cmd}\n",
            encoding="utf-8",
        )

    def test_verify_writes_pass_record_when_all_commands_succeed(self):
        self.write_config()
        task.cmd_verify(["TASK-001"])
        files = list(Path(task.VERIFY_DIR).glob("VERIFY-*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("result: pass", files[0].read_text(encoding="utf-8"))

    def test_verify_does_not_write_record_when_check_fails(self):
        self.write_config(check_cmd="exit 1")
        with self.assertRaises(SystemExit):
            task.cmd_verify(["TASK-001"])
        self.assertEqual(list(Path(task.VERIFY_DIR).glob("VERIFY-*.md")), [])

    def test_verify_dies_with_clear_message_when_config_missing(self):
        # no aios.config.yaml written
        with self.assertRaises(SystemExit):
            task.cmd_verify(["TASK-001"])


class ReworkLimitTests(unittest.TestCase):
    """TASK-047: task start 在 in-review→in-progress（打回）时递增 rework-count，超上限拒绝。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        task.VERIFY_DIR = str(root / "verification")
        task.REVIEW_DIR = str(root / "reviews")
        task.TASKS_DIR = str(root / "tasks")
        task.STATE_DIR = str(root / "states")
        task.LOG_DIR = str(root / "logs")  # TASK-066：隔离事件流，防污染真实 runtime/logs
        for d in (task.VERIFY_DIR, task.REVIEW_DIR, task.TASKS_DIR, task.STATE_DIR, task.LOG_DIR):
            Path(d).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_task(self, status, rework_count=None, has_rework_field=True):
        rc = f"  rework-count: {rework_count}\n" if has_rework_field else ""
        body = f"""---
name: TASK-099-test
metadata:
  type: task
  status: {status}
  created: {task.today()}
  updated: {task.today()}
  priority: P2
  risk: P2
  approval-ref: none
  assignee: any
  reviewer: any
  parent: TASK-000
  depends-on: []
{rc}---
# TASK-099

## 验收标准
- [x] ok
"""
        Path(task.TASKS_DIR, "TASK-099-test.md").write_text(body, encoding="utf-8")

    def read_rework(self):
        text = Path(task.TASKS_DIR, "TASK-099-test.md").read_text(encoding="utf-8")
        m = re.search(r"^\s*rework-count:\s*(\d+)", text, re.M)
        return int(m.group(1)) if m else None

    def put_in_review(self):
        path = Path(task.TASKS_DIR, "TASK-099-test.md")
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"^\s*status:\s*in-progress", "  status: in-review", text, count=1, flags=re.M)
        path.write_text(text, encoding="utf-8")

    def test_rework_increments_on_first_bounce(self):
        self.write_task("in-review", 0)
        task.cmd_start(["TASK-099"])
        self.assertEqual(self.read_rework(), 1)

    def test_rework_allows_second_bounce(self):
        self.write_task("in-review", 0)
        task.cmd_start(["TASK-099"])
        self.put_in_review()
        task.cmd_start(["TASK-099"])
        self.assertEqual(self.read_rework(), 2)

    def test_rework_limit_rejects_third_bounce(self):
        self.write_task("in-review", 2)
        with self.assertRaises(SystemExit):
            task.cmd_start(["TASK-099"])
        self.assertEqual(self.read_rework(), 2)  # 状态与计数均不变

    def test_initial_start_does_not_increment(self):
        self.write_task("open")
        task.cmd_start(["TASK-099"])
        self.assertIsNone(self.read_rework())  # 无 rework-count 字段，未打回不写入

    def test_rework_inserts_field_when_missing(self):
        self.write_task("in-review", has_rework_field=False)
        task.cmd_start(["TASK-099"])
        self.assertEqual(self.read_rework(), 1)


if __name__ == "__main__":
    unittest.main()
