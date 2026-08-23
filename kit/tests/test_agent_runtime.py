"""TASK-023 — kit/tools/agent/ runtime 读取模块单测。

覆盖：
- 完整项目：tasks（文件名+原文）/focus/heartbeat mtime/events 原文/VERIFY/REVIEW 计数
- 缺失容错：runtime/ 整体缺失、项目路径不存在、单项文件缺失 → None/空列表，不抛异常
- 空目录：目录存在但无匹配文件 → 空列表 / 0
- 多项目遍历：各自读取互不串扰
- mtime 精确、非 TASK 文件忽略、非 UTF-8 容错、计数互不串扰、排序稳定
"""
import os
import sys
import tempfile
import unittest

AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "agent"
)
sys.path.insert(0, AGENT_DIR)
import agent_runtime  # noqa: E402


def make_file(path, content, encoding="utf-8"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(content, bytes):
        with open(path, "wb") as f:
            f.write(content)
    else:
        with open(path, "w", encoding=encoding) as f:
            f.write(content)
    return path


class AgentRuntimeTests(unittest.TestCase):
    def make_full_project(self, root):
        """构造一个字段齐全的项目 runtime/，返回 (root, 期望快照)。"""
        tasks = [
            "TASK-001-first.md", "TASK-002-second.md",
        ]
        for name in tasks:
            make_file(os.path.join(root, "runtime", "tasks", name),
                      f"# {name}\n\n原文内容-{name}")
        make_file(os.path.join(root, "runtime", "states", "CURRENT_FOCUS.md"),
                  "# Current Focus — 2026-08-16\n\n## 当前任务\nTASK-023\n")
        make_file(os.path.join(root, "runtime", "logs", "autoloop-coder.heartbeat"),
                  "1786892280\n")
        make_file(os.path.join(root, "runtime", "logs", "autoloop-reviewer.heartbeat"),
                  "1786892100\n")
        make_file(os.path.join(root, "runtime", "logs", "autoloop-coder-events.jsonl"),
                  '{"ts": 1, "task": "TASK-005", "outcome": "ok"}\n'
                  '{"ts": 2, "task": "-", "outcome": "no_task"}\n')
        for i in range(2):
            make_file(os.path.join(root, "runtime", "verification",
                                   f"VERIFY-2026-08-16-task-0{i}.md"),
                      f"# VERIFY 0{i}")
        make_file(os.path.join(root, "runtime", "reviews",
                               "REVIEW-2026-08-16-task-022.md"), "# REVIEW")
        return root

    def test_full_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_full_project(os.path.join(tmp, "proj"))
            snap = agent_runtime.read_project_runtime(root)

            self.assertEqual([t["name"] for t in snap["tasks"]],
                             ["TASK-001-first.md", "TASK-002-second.md"])
            self.assertIn("原文内容-TASK-001-first.md", snap["tasks"][0]["content"])
            self.assertIn("TASK-002-second.md", snap["tasks"][1]["content"])

            self.assertIsNotNone(snap["focus"])
            self.assertIn("Current Focus", snap["focus"])

            self.assertEqual([h["file"] for h in snap["heartbeats"]],
                             ["autoloop-coder.heartbeat", "autoloop-reviewer.heartbeat"])
            self.assertIsInstance(snap["heartbeats"][0]["mtime"], float)
            self.assertGreater(snap["heartbeats"][0]["mtime"], 0)

            self.assertEqual([e["name"] for e in snap["events"]],
                             ["autoloop-coder-events.jsonl"])
            self.assertIn('"outcome": "ok"', snap["events"][0]["content"])

            self.assertEqual(snap["verification_count"], 2)
            self.assertEqual(snap["review_count"], 1)

    def test_missing_runtime_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")  # 项目存在但没有 runtime/
            os.makedirs(root)
            snap = agent_runtime.read_project_runtime(root)
            self.assertIsNone(snap["tasks"])
            self.assertIsNone(snap["focus"])
            self.assertIsNone(snap["heartbeats"])
            self.assertIsNone(snap["events"])
            self.assertIsNone(snap["verification_count"])
            self.assertIsNone(snap["review_count"])

    def test_missing_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = agent_runtime.read_project_runtime(os.path.join(tmp, "no-such-project"))
            self.assertIsNone(snap["tasks"])
            self.assertIsNone(snap["focus"])
            self.assertIsNone(snap["heartbeats"])
            self.assertIsNone(snap["events"])
            self.assertIsNone(snap["verification_count"])
            self.assertIsNone(snap["review_count"])

    def test_empty_present_dirs(self):
        """目录存在但为空 → 空列表 / 0（有数据但为空），与缺失（None）区分。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            os.makedirs(os.path.join(root, "runtime", "tasks"))
            os.makedirs(os.path.join(root, "runtime", "logs"))
            os.makedirs(os.path.join(root, "runtime", "verification"))
            os.makedirs(os.path.join(root, "runtime", "reviews"))
            snap = agent_runtime.read_project_runtime(root)
            self.assertEqual(snap["tasks"], [])
            self.assertEqual(snap["heartbeats"], [])
            self.assertEqual(snap["events"], [])
            self.assertEqual(snap["verification_count"], 0)
            self.assertEqual(snap["review_count"], 0)
            self.assertIsNone(snap["focus"])

    def test_partial_missing(self):
        """只缺某些项：tasks 在、focus 缺、logs 缺、verification 缺。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            make_file(os.path.join(root, "runtime", "tasks", "TASK-001-a.md"), "# TASK-001")
            snap = agent_runtime.read_project_runtime(root)
            self.assertEqual(len(snap["tasks"]), 1)
            self.assertIsNone(snap["focus"])
            self.assertIsNone(snap["heartbeats"])
            self.assertIsNone(snap["events"])
            self.assertIsNone(snap["verification_count"])
            self.assertIsNone(snap["review_count"])

    def test_multi_project_traversal(self):
        """两个项目各自读取，互不串扰。"""
        with tempfile.TemporaryDirectory() as tmp:
            root_a = os.path.join(tmp, "proj-a")
            root_b = os.path.join(tmp, "proj-b")
            make_file(os.path.join(root_a, "runtime", "tasks", "TASK-001-a.md"), "# A-001")
            make_file(os.path.join(root_a, "runtime", "verification", "VERIFY-2026-08-16-task-001.md"),
                      "# V")
            make_file(os.path.join(root_b, "runtime", "tasks", "TASK-009-b.md"), "# B-009")
            make_file(os.path.join(root_b, "runtime", "tasks", "TASK-010-b.md"), "# B-010")
            make_file(os.path.join(root_b, "runtime", "states", "CURRENT_FOCUS.md"), "# focus B")

            snap_a = agent_runtime.read_project_runtime(root_a)
            snap_b = agent_runtime.read_project_runtime(root_b)

            self.assertEqual([t["name"] for t in snap_a["tasks"]], ["TASK-001-a.md"])
            self.assertEqual(snap_a["verification_count"], 1)
            self.assertIsNone(snap_a["focus"])
            self.assertEqual([t["name"] for t in snap_b["tasks"]],
                             ["TASK-009-b.md", "TASK-010-b.md"])
            self.assertIn("focus B", snap_b["focus"])
            self.assertIsNone(snap_b["verification_count"])

    def test_heartbeat_mtime_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            path = make_file(os.path.join(root, "runtime", "logs", "proj.heartbeat"), "123\n")
            mtime = 1_700_000_000.0
            os.utime(path, (mtime, mtime))
            snap = agent_runtime.read_project_runtime(root)
            self.assertEqual(snap["heartbeats"], [{"file": "proj.heartbeat", "mtime": mtime}])

    def test_ignores_non_task_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            make_file(os.path.join(root, "runtime", "tasks", "TASK-001-a.md"), "# 1")
            make_file(os.path.join(root, "runtime", "tasks", "INDEX.md"), "# index")
            make_file(os.path.join(root, "runtime", "tasks", "TASK.template.md"), "# tmpl")
            make_file(os.path.join(root, "runtime", "tasks", "README.md"), "# readme")
            snap = agent_runtime.read_project_runtime(root)
            self.assertEqual([t["name"] for t in snap["tasks"]], ["TASK-001-a.md"])

    def test_non_utf8_content_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            make_file(os.path.join(root, "runtime", "tasks", "TASK-001-bad.md"),
                      b"\xff\xfeTASK-001 raw \x80")
            snap = agent_runtime.read_project_runtime(root)
            self.assertEqual(len(snap["tasks"]), 1)
            self.assertEqual(snap["tasks"][0]["name"], "TASK-001-bad.md")
            self.assertIsInstance(snap["tasks"][0]["content"], str)

    def test_counts_do_not_leak(self):
        """verification 目录里的 REVIEW 文件不算 verification_count，反之亦然。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            make_file(os.path.join(root, "runtime", "verification", "VERIFY-2026-08-16-task-001.md"), "# v1")
            make_file(os.path.join(root, "runtime", "verification", "VERIFY-2026-08-16-task-002.md"), "# v2")
            make_file(os.path.join(root, "runtime", "verification", "REVIEW-2026-08-16-task-001.md"), "# r")
            make_file(os.path.join(root, "runtime", "reviews", "REVIEW-2026-08-16-task-001.md"), "# r1")
            make_file(os.path.join(root, "runtime", "reviews", "VERIFY-2026-08-16-task-001.md"), "# v")
            snap = agent_runtime.read_project_runtime(root)
            self.assertEqual(snap["verification_count"], 2)
            self.assertEqual(snap["review_count"], 1)

    def test_sorted_output(self):
        """输出按文件名排序（创建顺序故意乱序）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "proj")
            for name in ("TASK-003-c.md", "TASK-001-a.md", "TASK-002-b.md"):
                make_file(os.path.join(root, "runtime", "tasks", name), f"# {name}")
            snap = agent_runtime.read_project_runtime(root)
            self.assertEqual([t["name"] for t in snap["tasks"]],
                             ["TASK-001-a.md", "TASK-002-b.md", "TASK-003-c.md"])


if __name__ == "__main__":
    unittest.main()
