"""TASK-065 — cli/task 事件流落地单测。

覆盖：
- append_event：文件缺失容错、seq 从 1 单调递增、必填字段齐全、task 短 id、追加写
- _event_last_seq：空/缺失文件返回 0、损坏尾行跳过、取最后一条合法 seq
- validate_events：正常文件通过、非法 JSON 检出、seq 不单调检出、缺失文件 0 条 0 错
- set_status 钩子：状态流转自动追加事件（from/to 可追溯）
"""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TASK_PATH = "kit/cli/task" if os.path.isfile("kit/cli/task") else "cli/task"
loader = importlib.machinery.SourceFileLoader("task_cli", _TASK_PATH)
spec = importlib.util.spec_from_loader(loader.name, loader)
task = importlib.util.module_from_spec(spec)
loader.exec_module(task)


class AppendEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task.LOG_DIR = str(self.root / "logs")

    def tearDown(self):
        self.tmp.cleanup()

    def read_events(self):
        path = Path(task.LOG_DIR, "task-events.jsonl")
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # 容错：与生产读取一致，跳过损坏行
        return out

    def test_append_creates_file_with_seq1_and_required_fields(self):
        task.append_event("task.created", "TASK-001-test", to_status="open")
        events = self.read_events()
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["seq"], 1)
        self.assertEqual(ev["ev"], "task.created")
        self.assertEqual(ev["task"], "TASK-001")  # 短 id
        self.assertEqual(ev["to"], "open")
        self.assertIsNone(ev["from"])
        for key in ("seq", "ts", "ev", "task", "from", "to", "actor", "commit"):
            self.assertIn(key, ev)

    def test_append_increments_seq_monotonically(self):
        task.append_event("task.started", "TASK-001", from_status="open", to_status="in-progress")
        task.append_event("task.done", "TASK-001", from_status="in-progress", to_status="done")
        events = self.read_events()
        self.assertEqual([e["seq"] for e in events], [1, 2])
        self.assertEqual([e["from"] for e in events], ["open", "in-progress"])
        self.assertEqual([e["to"] for e in events], ["in-progress", "done"])

    def test_append_with_reason_and_dispatch_ref(self):
        task.append_event("task.blocked", "TASK-001", from_status="in-progress",
                          to_status="blocked", reason="P0 等人工", dispatch_ref="cmd-0001")
        ev = self.read_events()[0]
        self.assertEqual(ev["reason"], "P0 等人工")
        self.assertEqual(ev["dispatch_ref"], "cmd-0001")

    def test_append_missing_logs_dir_creates_it(self):
        task.append_event("task.cancelled", "TASK-001", to_status="cancelled")
        self.assertTrue(Path(task.LOG_DIR, "task-events.jsonl").exists())

    def test_append_does_not_raise_on_bad_last_seq(self):
        """文件损坏（尾行非 JSON）时追加不抛异常，seq 跳过损坏行继续。"""
        Path(task.LOG_DIR).mkdir(parents=True)
        path = Path(task.LOG_DIR, "task-events.jsonl")
        path.write_text('{"seq": 3, "ev": "task.created"}\nnot-json\n', encoding="utf-8")
        task.append_event("task.started", "TASK-001")
        events = self.read_events()
        self.assertEqual(events[-1]["seq"], 4)


class EventLastSeqTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name, "task-events.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_0(self):
        self.assertEqual(task._event_last_seq(str(self.path)), 0)

    def test_empty_file_returns_0(self):
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(task._event_last_seq(str(self.path)), 0)

    def test_returns_last_valid_seq(self):
        self.path.write_text('{"seq": 1}\n{"seq": 2}\n{"seq": 3}\n', encoding="utf-8")
        self.assertEqual(task._event_last_seq(str(self.path)), 3)

    def test_skips_corrupt_tail_lines(self):
        self.path.write_text('{"seq": 5}\ngarbage\n{"seq": 9}\n', encoding="utf-8")
        self.assertEqual(task._event_last_seq(str(self.path)), 9)


class ValidateEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task.LOG_DIR = str(self.root / "logs")
        Path(task.LOG_DIR).mkdir(parents=True)
        self.path = Path(task.LOG_DIR, "task-events.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, lines):
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def good(self, seq):
        return (f'{{"seq": {seq}, "ts": "2026-08-27T00:00:00", '
                f'"ev": "task.created", "task": "TASK-001"}}')

    def test_missing_file_returns_zero_zero(self):
        if self.path.exists():
            self.path.unlink()
        self.assertEqual(task.validate_events(), (0, 0))

    def test_valid_file_passes(self):
        self.write([self.good(1), self.good(2), self.good(3)])
        self.assertEqual(task.validate_events(), (0, 3))

    def test_bad_json_detected(self):
        self.write([self.good(1), "not-json", self.good(2)])
        errors, total = task.validate_events()
        self.assertEqual(total, 3)
        self.assertGreaterEqual(errors, 1)

    def test_non_monotonic_seq_detected(self):
        self.write([self.good(1), self.good(1)])
        errors, total = task.validate_events()
        self.assertEqual(total, 2)
        self.assertGreaterEqual(errors, 1)

    def test_missing_required_field_detected(self):
        self.write(['{"seq": 1, "ts": "2026-08-27T00:00:00", "ev": "task.created"}'])
        errors, _ = task.validate_events()
        self.assertGreaterEqual(errors, 1)


class CmdNewEventTests(unittest.TestCase):
    """cmd_new 钩子：创建任务即追加 task.created 事件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task.TASKS_DIR = str(self.root / "tasks")
        task.STATE_DIR = str(self.root / "states")
        task.LOG_DIR = str(self.root / "logs")
        Path(task.TASKS_DIR).mkdir(parents=True)
        Path(task.STATE_DIR).mkdir(parents=True)
        Path(task.LOG_DIR).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cmd_new_appends_created_event(self):
        task.cmd_new(["测试任务"])
        path = Path(task.LOG_DIR, "task-events.jsonl")
        self.assertTrue(path.exists())
        lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["ev"], "task.created")
        self.assertEqual(lines[0]["to"], "open")
        self.assertIsNone(lines[0]["from"])
        # 任务文件已创建
        tasks = list(Path(task.TASKS_DIR).glob("TASK-001-*.md"))
        self.assertEqual(len(tasks), 1)


class SetStatusEventHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task.TASKS_DIR = str(self.root / "tasks")
        task.STATE_DIR = str(self.root / "states")
        task.LOG_DIR = str(self.root / "logs")
        Path(task.TASKS_DIR).mkdir(parents=True)
        Path(task.STATE_DIR).mkdir(parents=True)
        Path(task.LOG_DIR).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_task(self, status="open"):
        body = f"""---
name: TASK-001-test
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
---
# TASK-001

## 验收标准
- [ ] ok
"""
        path = Path(task.TASKS_DIR, "TASK-001-test.md")
        path.write_text(body, encoding="utf-8")
        return str(path)

    def read_events(self):
        path = Path(task.LOG_DIR, "task-events.jsonl")
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # 容错：与生产读取一致，跳过损坏行
        return out

    def test_set_status_appends_transition_event(self):
        path = self.write_task("open")
        task.set_status(path, "in-progress", ev="task.started")
        events = self.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ev"], "task.started")
        self.assertEqual(events[0]["from"], "open")
        self.assertEqual(events[0]["to"], "in-progress")
        self.assertEqual(events[0]["task"], "TASK-001")

    def test_no_event_when_status_unchanged(self):
        path = self.write_task("in-progress")
        task.set_status(path, "in-progress")
        self.assertEqual(self.read_events(), [])


if __name__ == "__main__":
    unittest.main()
