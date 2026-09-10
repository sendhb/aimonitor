"""TASK-074 — kit/tools/dispatcher/state.py + CLI status/run 状态机接线 单测。

覆盖（验收标准）：
- 分配指纹：project/task/worker/started_at 落盘；同任务活跃分配拒绝重复
- 超时回收：in-progress 超过 --task-timeout → 标记 stale → 可重新分配
- 重试上限：连续失败 3 次 → human（不再自动重试，状态/事件流可见）
- 并发上限：--max-workers N 全局限制生效（活跃 + 本轮新启动 ≤ N）
- 状态重建：kill 后重启（新 SchedulerState/新 CLI 进程）从 runtime/tasks/ 恢复认知
- status CLI 显示 project/task/worker/started_at/状态
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

DISPATCHER_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "dispatcher"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISPATCHER_PY = os.path.join(DISPATCHER_DIR, "dispatcher.py")

sys.path.insert(0, DISPATCHER_DIR)
import registry  # noqa: E402
import state as state_lib  # noqa: E402

TASK_TPL = """---
name: {name}
description: state fixture
metadata:
  type: task
  status: {status}
  created: 2026-08-28
  updated: 2026-08-28
  priority: P2
  risk: P2
  approval-ref: none
---
# {name}
"""

FAKE_TASK_PY = """#!/usr/bin/env python3
import sys
print("fake-task", *sys.argv[1:])
sys.exit(0)
"""
# autoloop-coder 入口自 TASK-026 起是 Python 薄 shim，dispatcher 以
# sys.executable 调用（不再经 bash），假脚本同步为 Python。
FAKE_CODER_PY = """#!/usr/bin/env python3
import sys
print("fake-autoloop-coder", *sys.argv[1:])
sys.exit(0)
"""


class FakeClock:
    """可注入时钟：手动推进，支持递增。"""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def write_file(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)


def make_task(root, project, task_id, status):
    name = f"{task_id}-{project}"
    write_file(os.path.join(root, project, "runtime", "tasks", f"{name}.md"),
               TASK_TPL.format(name=name, status=status))


def make_local_project(root, project, status="open"):
    """建一个带任务 + fake 工具链的本地项目，返回路径。"""
    proj = os.path.join(root, project)
    make_task(root, project, "TASK-001", status)
    write_file(os.path.join(proj, "kit", "cli", "task"), FAKE_TASK_PY)
    write_file(os.path.join(proj, "kit", "cli", "autoloop-coder"), FAKE_CODER_PY,
               mode=0o755)
    return proj


def write_registry(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def local_registry(root, projects):
    return {
        "poll_interval_seconds": 30,
        "projects": [
            {"id": p, "name": p, "path": os.path.join(root, p)} for p in projects
        ],
    }


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self._tmp.name, "state")
        self.clock = FakeClock(1000.0)
        self.st = state_lib.SchedulerState(self.state_dir, clock=self.clock)

    def tearDown(self):
        self._tmp.cleanup()

    def _allocate(self, task="TASK-001", project="proj-a", worker="dispatcher"):
        return self.st.allocate(project, task, worker=worker)

    def test_allocate_records_fingerprint(self):
        a = self._allocate()
        self.assertIsNotNone(a)
        self.assertEqual(a.project_id, "proj-a")
        self.assertEqual(a.task_id, "TASK-001")
        self.assertEqual(a.worker, "dispatcher")
        self.assertEqual(a.started_at, 1000.0)
        self.assertEqual(a.status, "allocated")
        self.assertEqual(a.retry_count, 0)
        self.assertEqual(self.st.active_count(), 1)

    def test_allocate_refuses_duplicate_active(self):
        self._allocate()
        dup = self._allocate()
        self.assertIsNone(dup)  # 分配指纹：同任务已有活跃分配
        self.assertEqual(self.st.active_count(), 1)

    def test_reallocate_after_done_and_stale(self):
        a = self._allocate()
        self.st.mark_done("proj-a", "TASK-001")
        self.assertEqual(self.st.get("proj-a", "TASK-001").status, "done")
        again = self._allocate()
        self.assertIsNotNone(again)
        self.assertEqual(again.retry_count, 0)
        # stale 后可重新分配
        self.st.mark_running("proj-a", "TASK-001")
        self.st.check_timeouts(timeout=0, now=2000.0)
        self.assertEqual(self.st.get("proj-a", "TASK-001").status, "stale")
        self.assertNotIn(self.st.get("proj-a", "TASK-001").status,
                         state_lib.ACTIVE_STATUSES)
        realloc = self._allocate()
        self.assertIsNotNone(realloc)
        self.assertEqual(realloc.status, "allocated")
        self.assertIn("重新分配", realloc.comment)

    def test_failed_carries_retry_count_and_limit_turns_human(self):
        self._allocate()
        for i in range(1, 4):
            self.st.mark_failed("proj-a", "TASK-001")
        a = self.st.get("proj-a", "TASK-001")
        self.assertEqual(a.retry_count, 3)
        self.assertEqual(a.status, "human")  # 连续失败 3 次 → 转人工
        # 人工后不再自动重试（allocate 拒绝）
        self.assertIsNone(self._allocate())
        # 事件流可见（dispatcher.human）
        with open(os.path.join(self.state_dir, state_lib.EVENTS_FILE),
                  encoding="utf-8") as f:
            lines = f.read().splitlines()
        evs = [json.loads(l)["ev"] for l in lines]
        self.assertEqual(evs[-1], "dispatcher.human")
        self.assertEqual(json.loads(lines[-1])["retry_count"], 3)

    def test_failed_below_limit_is_failed_and_reallocatable(self):
        self._allocate()
        self.st.mark_failed("proj-a", "TASK-001")
        self.st.mark_failed("proj-a", "TASK-001")
        a = self.st.get("proj-a", "TASK-001")
        self.assertEqual(a.status, "failed")
        self.assertEqual(a.retry_count, 2)
        realloc = self._allocate()  # failed 后可重试
        self.assertIsNotNone(realloc)
        self.assertEqual(realloc.retry_count, 2)  # 连续失败计数延续

    def test_check_timeouts_only_marks_active(self):
        self._allocate()
        self.st.mark_done("proj-a", "TASK-001")
        stale = self.st.check_timeouts(timeout=0, now=2000.0)
        self.assertEqual(stale, [])  # done 不超时
        self._allocate()
        stale = self.st.check_timeouts(timeout=0, now=2000.0)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].status, "stale")

    def test_active_count_concurrency_units(self):
        self._allocate("TASK-001")
        self._allocate("TASK-002", project="proj-b")
        self.assertEqual(self.st.active_count(), 2)
        self.st.mark_done("proj-a", "TASK-001")
        self.assertEqual(self.st.active_count(), 1)

    def test_save_load_roundtrip(self):
        self._allocate()
        self.st.mark_running("proj-a", "TASK-001")
        self.st.save()
        st2 = state_lib.SchedulerState.load(self.state_dir)
        a = st2.get("proj-a", "TASK-001")
        self.assertIsNotNone(a)
        self.assertEqual(a.status, "running")
        self.assertEqual(a.worker, "dispatcher")
        self.assertEqual(a.started_at, 1000.0)

    def test_load_missing_dir_is_empty(self):
        st = state_lib.SchedulerState.load(os.path.join(self._tmp.name, "nope"))
        self.assertEqual(st.allocations, {})

    def test_events_stream_seq_monotonic(self):
        self._allocate()
        self.st.mark_running("proj-a", "TASK-001")
        self.st.mark_failed("proj-a", "TASK-001")
        with open(os.path.join(self.state_dir, state_lib.EVENTS_FILE),
                  encoding="utf-8") as f:
            lines = f.read().splitlines()
        parsed = [json.loads(l) for l in lines]
        self.assertEqual([e["seq"] for e in parsed], [1, 2, 3])
        self.assertEqual(parsed[0]["ev"], "dispatcher.allocated")
        self.assertEqual(parsed[1]["ev"], "dispatcher.running")
        self.assertEqual(parsed[2]["ev"], "dispatcher.failed")

    def test_rebuild_from_projects_recovers_in_progress(self):
        proj_a = os.path.join(self._tmp.name, "proj-a")
        proj_b = os.path.join(self._tmp.name, "proj-b")
        make_task(self._tmp.name, "proj-a", "TASK-001", "in-progress")
        make_task(self._tmp.name, "proj-a", "TASK-002", "open")
        make_task(self._tmp.name, "proj-b", "TASK-003", "in-progress")
        entries = registry.load_registry(self._write_cfg(
            local_registry(self._tmp.name, ["proj-a", "proj-b"])
        ))
        # 模拟 kill 后重启：全新状态对象重建
        st = state_lib.SchedulerState(self.state_dir, clock=self.clock)
        st.rebuild_from_projects(entries)
        self.assertEqual(st.active_count(), 2)
        a = st.get("proj-a", "TASK-001")
        self.assertEqual(a.status, "running")
        self.assertEqual(a.worker, "recovered")
        # open 任务无活跃分配
        self.assertIsNone(st.get("proj-a", "TASK-002"))

    def test_rebuild_uses_heartbeat_mtime_as_started_at(self):
        proj_a = os.path.join(self._tmp.name, "proj-a")
        make_task(self._tmp.name, "proj-a", "TASK-001", "in-progress")
        write_file(os.path.join(proj_a, "runtime", "logs",
                                "autoloop-coder.heartbeat"), "1234\n")
        os.utime(os.path.join(proj_a, "runtime", "logs",
                              "autoloop-coder.heartbeat"), (1234.0, 1234.0))
        entries = registry.load_registry(self._write_cfg(
            local_registry(self._tmp.name, ["proj-a"])
        ))
        st = state_lib.SchedulerState(self.state_dir, clock=self.clock)
        st.rebuild_from_projects(entries)
        self.assertEqual(st.get("proj-a", "TASK-001").started_at, 1234.0)

    def test_rebuild_clears_active_records_no_longer_in_runtime(self):
        make_task(self._tmp.name, "proj-a", "TASK-001", "in-progress")
        entries = registry.load_registry(self._write_cfg(
            local_registry(self._tmp.name, ["proj-a"])
        ))
        self._allocate("TASK-001")
        self.st.mark_running("proj-a", "TASK-001")
        self.st.save()
        # 任务已 done → 重建后旧 active 记录清除
        make_task(self._tmp.name, "proj-a", "TASK-001", "done")
        st2 = state_lib.SchedulerState.load(self.state_dir, clock=self.clock)
        st2.rebuild_from_projects(entries)
        self.assertEqual(st2.active_count(), 0)
        self.assertIsNone(st2.get("proj-a", "TASK-001"))

    def _write_cfg(self, data):
        cfg = os.path.join(self._tmp.name, "projects.json")
        write_registry(cfg, data)
        return cfg


class DispatcherStatusCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj_a = make_local_project(self._tmp.name, "proj-a")
        self.state_dir = os.path.join(self._tmp.name, "state")
        self.cfg = os.path.join(self._tmp.name, "projects.json")
        write_registry(self.cfg, local_registry(self._tmp.name, ["proj-a"]))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, DISPATCHER_PY, *extra, "--config", self.cfg,
             "--state-dir", self.state_dir],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
        )

    def _make_state(self, status="running", started_at=None, retry_count=0):
        # started_at 缺省用真实当前时间附近（避免 CLI 真实时钟把 fixture 判成 stale）
        if started_at is None:
            started_at = time.time() - 10
        st = state_lib.SchedulerState(self.state_dir, clock=lambda: time.time())
        st.allocate("proj-a", "TASK-001", worker="dispatcher",
                    ts=started_at, comment="fixture")
        if status == "running":
            st.mark_running("proj-a", "TASK-001", ts=started_at)
        st.save()

    def test_status_shows_allocations_table(self):
        self._make_state(status="running")
        proc = self._run("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PROJECT", proc.stdout)
        self.assertIn("proj-a", proc.stdout)
        self.assertIn("TASK-001", proc.stdout)
        self.assertIn("dispatcher", proc.stdout)  # worker
        self.assertIn("running", proc.stdout)     # 状态
        self.assertIn("STARTED_AT", proc.stdout)

    def test_status_empty_state(self):
        proc = self._run("status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("（无分配记录）", proc.stdout)

    def test_status_timeout_marks_stale_and_run_reallocates(self):
        # 构造超时场景：分配 started_at=老时间 + --task-timeout 0 → stale
        self._make_state(status="running", started_at=100.0)
        proc = self._run("status", "--task-timeout", "0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stale", proc.stdout)
        self.assertIn("超时回收", proc.stderr)
        # stale 后可重新分配：run 会把 proj-a 重新分配并执行
        proc2 = self._run("run", "--task-timeout", "0", "--max-workers", "1")
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn("fake-task", proc2.stdout)
        self.assertIn("fake-autoloop-coder", proc2.stdout)

    def test_status_rebuild_recovers_from_runtime_tasks(self):
        # 状态目录为空（kill 后重启）+ 项目内有 in-progress → --rebuild 恢复认知
        make_task(self._tmp.name, "proj-a", "TASK-002", "in-progress")
        proc = self._run("status", "--rebuild")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("recovered", proc.stdout)
        self.assertIn("TASK-002", proc.stdout)
        self.assertIn("running", proc.stdout)


class DispatcherConcurrencyCliTests(unittest.TestCase):
    """全局并发上限（状态机层）：活跃分配 + 本轮新启动 ≤ --max-workers N。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj_a = make_local_project(self._tmp.name, "proj-a")
        self.proj_b = make_local_project(self._tmp.name, "proj-b")
        self.state_dir = os.path.join(self._tmp.name, "state")
        self.cfg = os.path.join(self._tmp.name, "projects.json")
        write_registry(self.cfg, local_registry(self._tmp.name, ["proj-a", "proj-b"]))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, DISPATCHER_PY, *extra, "--config", self.cfg,
             "--state-dir", self.state_dir],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
        )

    def _make_running_state(self, project="proj-a", task="TASK-001"):
        """构造一条 running 分配（真实时钟附近，CLI 运行时不误判 stale）。"""
        st = state_lib.SchedulerState(self.state_dir, clock=lambda: time.time())
        st.allocate(project, task, worker="dispatcher", ts=time.time() - 10)
        st.mark_running(project, task, ts=time.time() - 10)
        st.save()

    def test_active_allocation_blocks_new_start_with_max_workers_1(self):
        self._make_running_state()
        proc = self._run("run", "--max-workers", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 活跃 1 条 ≥ 上限 1 → 本轮不新启动任何任务
        self.assertIn("无候选任务", proc.stdout)
        self.assertNotIn("fake-task", proc.stdout)

    def test_max_workers_two_starts_second_project_only(self):
        self._make_running_state()  # proj-a 活跃占 1 个 slot
        proc = self._run("run", "--max-workers", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 剩余额度 1 → 只启动注册表下一个候选 proj-b（proj-a 活跃不重复分配）
        self.assertIn("▶ proj-b TASK-001 (open)", proc.stdout)
        self.assertNotIn("▶ proj-a", proc.stdout)
        self.assertIn("fake-task", proc.stdout)

    def test_run_writes_done_and_failed_to_state(self):
        proc = self._run("run", "--max-workers", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        st = state_lib.SchedulerState.load(self.state_dir)
        a = st.get("proj-a", "TASK-001")
        self.assertEqual(a.status, "done")
        self.assertEqual(a.retry_count, 0)


if __name__ == "__main__":
    unittest.main()
