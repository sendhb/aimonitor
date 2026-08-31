"""TASK-073 — kit/tools/dispatcher/policy.py 单测。

覆盖：
- 注册表顺序 round-robin：逐项目轮询，每项目最多 1 候选
- 项目内按 TASK 编号升序取最先（open/in-progress）
- 全局并发上限 --max-workers：1 时不选第二个项目、2 时每项目 1 候选
- agent 传输条目跳过
- 无 open/in-progress / 无 runtime/tasks → 空候选
- Candidate 的 priority/updated 字段解析
"""
import json
import os
import sys
import tempfile
import unittest

DISPATCHER_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "dispatcher"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, DISPATCHER_DIR)
import policy  # noqa: E402
import registry  # noqa: E402

TASK_TPL = """---
name: {name}
description: policy fixture
metadata:
  type: task
  status: {status}
  created: 2026-08-01
  updated: {updated}
  priority: {priority}
  risk: P2
  approval-ref: none
---
# {name}
"""


def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def make_task(root, project, task_id, status, priority="P2", updated="2026-08-01"):
    name = f"{task_id}-{project}"
    path = os.path.join(root, project, "runtime", "tasks", f"{name}.md")
    write_file(path, TASK_TPL.format(
        name=name, status=status, priority=priority, updated=updated,
    ))


def build_fixture(tmp):
    """返回 (registry_entries, snapshots)。

    - proj-a: TASK-001(open) TASK-002(in-progress) TASK-003(done)
    - proj-b: TASK-005(open) TASK-010(done)
    - proj-c: 无 runtime/tasks（空）
    - hb-share-x: agent 传输（跳过）
    注册表顺序：a, b, c, agent。
    """
    make_task(tmp, "proj-a", "TASK-001", "open", priority="P1", updated="2026-08-10")
    make_task(tmp, "proj-a", "TASK-002", "in-progress")
    make_task(tmp, "proj-a", "TASK-003", "done")
    make_task(tmp, "proj-b", "TASK-005", "open", priority="P2")
    make_task(tmp, "proj-b", "TASK-010", "done")

    data = {
        "projects": [
            {"id": "proj-a", "name": "proj-a", "path": os.path.join(tmp, "proj-a")},
            {"id": "proj-b", "name": "proj-b", "path": os.path.join(tmp, "proj-b")},
            {"id": "proj-c", "name": "proj-c", "path": os.path.join(tmp, "proj-c")},
            {"id": "hb-share-x", "name": "hb-share-x",
             "path": "D:/share/x", "transport": "agent"},
        ]
    }
    cfg = os.path.join(tmp, "projects.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return registry.load_registry(cfg)


class PolicySelectTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entries = build_fixture(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_select_picks_first_open_or_in_progress_ascending(self):
        cands = policy.select_candidates(self.entries, max_workers=1)
        self.assertEqual(len(cands), 1)
        c = cands[0]
        self.assertEqual(c.entry.id, "proj-a")
        # 项目内升序：TASK-001(open) 先于 TASK-002(in-progress)
        self.assertEqual(c.task_id, "TASK-001")
        self.assertEqual(c.status, "open")

    def test_max_workers_one_does_not_select_second_project(self):
        cands = policy.select_candidates(self.entries, max_workers=1)
        self.assertEqual([c.entry.id for c in cands], ["proj-a"])

    def test_max_workers_two_selects_one_per_project(self):
        cands = policy.select_candidates(self.entries, max_workers=2)
        self.assertEqual([(c.entry.id, c.task_id) for c in cands],
                         [("proj-a", "TASK-001"), ("proj-b", "TASK-005")])

    def test_agent_entries_skipped_even_with_high_workers(self):
        cands = policy.select_candidates(self.entries, max_workers=10)
        self.assertTrue(all(c.entry.id != "hb-share-x" for c in cands))
        self.assertTrue(all(c.entry.transport != "agent" for c in cands))

    def test_round_robin_registry_order_when_first_project_empty(self):
        # 移除 proj-a 的所有任务 → proj-b 成为首个有候选的项目
        for fn in os.listdir(os.path.join(self._tmp.name, "proj-a", "runtime", "tasks")):
            os.remove(os.path.join(self._tmp.name, "proj-a", "runtime", "tasks", fn))
        cands = policy.select_candidates(self.entries, max_workers=1)
        self.assertEqual([(c.entry.id, c.task_id) for c in cands],
                         [("proj-b", "TASK-005")])

    def test_no_candidates_when_no_open_or_in_progress(self):
        make_task(self._tmp.name, "proj-c", "TASK-001", "done")
        # proj-c 只有 done → 仍无候选（proj-a/b 有候选，但用只含 proj-c 的注册表）
        entries = [
            registry.RegistryEntry(
                id="proj-c", name="proj-c",
                path=os.path.join(self._tmp.name, "proj-c"), transport="local",
            )
        ]
        cands = policy.select_candidates(entries, max_workers=1)
        self.assertEqual(cands, [])

    def test_missing_runtime_tasks_yields_no_candidates(self):
        # proj-c 没有 runtime/tasks → 空候选
        entries = [
            registry.RegistryEntry(
                id="proj-c", name="proj-c",
                path=os.path.join(self._tmp.name, "proj-c"), transport="local",
            )
        ]
        cands = policy.select_candidates(entries, max_workers=1)
        self.assertEqual(cands, [])

    def test_candidate_fields_priority_updated(self):
        cands = policy.select_candidates(self.entries, max_workers=1)
        c = cands[0]
        self.assertEqual(c.priority, "P1")
        self.assertEqual(c.updated, "2026-08-10")

    def test_max_workers_zero_or_negative_selects_nothing(self):
        self.assertEqual(policy.select_candidates(self.entries, max_workers=0), [])
        self.assertEqual(policy.select_candidates(self.entries, max_workers=-1), [])


if __name__ == "__main__":
    unittest.main()
