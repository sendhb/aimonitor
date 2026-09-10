"""TASK-075 — 治理挂钩（P0 阻塞、返工上限、告警）单测。

覆盖（验收标准）：
- P0 无 approval-ref 任务 → policy 跳过（evaluate 可见 p0-blocked），state
  分配拒绝（blocked 语义，不执行不实现）
- P0 有 approval-ref → 正常放行/分配
- rework-count=3 任务 → policy 拒绝、state 分配拒绝（转人工）
- dispatcher dispatch/run --dry-run 只输出判定，不执行任何命令
- monitor 派生 governance：blocked 任务/项目计数、blocked_ratio、stale 告警
- monitor payload 携带 governance 派生字段（aimonitor 可读）
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

DISPATCHER_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "dispatcher"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISPATCHER_PY = os.path.join(DISPATCHER_DIR, "dispatcher.py")

sys.path.insert(0, DISPATCHER_DIR)
import governance  # noqa: E402
import monitor as monitor_lib  # noqa: E402
import policy  # noqa: E402
import registry  # noqa: E402
import state as state_lib  # noqa: E402

TASK_TPL = """---
name: {name}
description: governance fixture
metadata:
  type: task
  status: {status}
  created: 2026-08-28
  updated: 2026-08-28
  priority: {priority}
  risk: {risk}
  approval-ref: {approval}
  rework-count: {rework}
---
# {name}
"""


def write_file(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)


def make_task(root, project, task_id, status="open", priority="P2", risk="P2",
              approval="none", rework=0):
    name = f"{task_id}-{project}"
    path = os.path.join(root, project, "runtime", "tasks", f"{name}.md")
    write_file(path, TASK_TPL.format(
        name=name, status=status, priority=priority, risk=risk,
        approval=approval, rework=rework,
    ))


def build_fixture(root):
    """治理 fixture：

    - proj-p0: TASK-001(open, P0, 无 approval-ref) → p0-blocked
    - proj-p0ok: TASK-001(open, P0, approval-ref=REV-001) → ok
    - proj-rework: TASK-001(open, P2, rework-count=3) → rework-rejected
    - proj-rework2: TASK-001(open, P2, rework-count=2) → ok
    - proj-normal: TASK-001(open, P2, rework-count=0) → ok
    """
    make_task(root, "proj-p0", "TASK-001", priority="P0", risk="P1", approval="none")
    make_task(root, "proj-p0ok", "TASK-001", priority="P0", risk="P0",
              approval="REV-2026-08-01-xxx")
    make_task(root, "proj-rework", "TASK-001", priority="P2", risk="P2", rework=3)
    make_task(root, "proj-rework2", "TASK-001", priority="P2", risk="P2", rework=2)
    make_task(root, "proj-normal", "TASK-001", priority="P1", risk="P1")
    data = {
        "projects": [
            {"id": p, "name": p, "path": os.path.join(root, p)}
            for p in ("proj-p0", "proj-p0ok", "proj-rework", "proj-rework2",
                      "proj-normal")
        ]
    }
    cfg = os.path.join(root, "projects.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return registry.load_registry(cfg)


class GovernanceUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.entries = build_fixture(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # ---------- policy 层 ----------

    def test_policy_skips_p0_without_approval_ref(self):
        cands = policy.select_candidates(self.entries, max_workers=10)
        ids = [(c.entry.id, c.task_id) for c in cands]
        self.assertNotIn(("proj-p0", "TASK-001"), ids)

    def test_policy_evaluate_exposes_p0_blocked_decision(self):
        considered = policy.evaluate_candidates(self.entries, max_workers=10)
        by_proj = {c.entry.id: c for c in considered}
        self.assertEqual(by_proj["proj-p0"].decision, "p0-blocked")
        self.assertIn("approval-ref", by_proj["proj-p0"].reason)
        # P0 有批准 → 放行
        self.assertEqual(by_proj["proj-p0ok"].decision, "ok")

    def test_policy_skips_rework_count_three(self):
        cands = policy.select_candidates(self.entries, max_workers=10)
        ids = [(c.entry.id, c.task_id) for c in cands]
        self.assertNotIn(("proj-rework", "TASK-001"), ids)
        self.assertIn(("proj-rework2", "TASK-001"), ids)  # 2 次仍可自动返工

    def test_policy_evaluate_exposes_rework_rejected_decision(self):
        considered = policy.evaluate_candidates(self.entries, max_workers=10)
        by_proj = {c.entry.id: c for c in considered}
        self.assertEqual(by_proj["proj-rework"].decision, "rework-rejected")
        self.assertIn("rework-count=3", by_proj["proj-rework"].reason)

    def test_policy_max_workers_counts_only_ok_candidates(self):
        # proj-p0 / proj-rework 被拦截，不占 max-workers 额度
        considered = policy.evaluate_candidates(self.entries, max_workers=2)
        ok_ids = [c.entry.id for c in considered if c.decision == "ok"]
        self.assertEqual(ok_ids, ["proj-p0ok", "proj-rework2"])

    # ---------- state 层 ----------

    def test_state_allocate_rejects_p0_without_approval_ref(self):
        st = state_lib.SchedulerState(self._tmp.name, clock=lambda: 1000.0)
        content = TASK_TPL.format(
            name="TASK-001-x", status="open", priority="P0", risk="P1",
            approval="none", rework=0,
        )
        a = st.allocate("proj-p0", "TASK-001", worker="dispatcher",
                        task_content=content)
        self.assertIsNone(a)  # 治理闸门：不分配
        self.assertEqual(st.active_count(), 0)
        # 事件流记录 governance-blocked
        evs = monitor_lib.read_events(self._tmp.name)
        self.assertIsNotNone(evs)
        self.assertEqual(evs[-1]["ev"], "dispatcher.governance-blocked")
        self.assertIn("p0-blocked", evs[-1]["comment"])

    def test_state_allocate_rejects_rework_count_three(self):
        st = state_lib.SchedulerState(self._tmp.name, clock=lambda: 1000.0)
        content = TASK_TPL.format(
            name="TASK-001-x", status="open", priority="P2", risk="P2",
            approval="none", rework=3,
        )
        a = st.allocate("proj-rework", "TASK-001", worker="dispatcher",
                        task_content=content)
        self.assertIsNone(a)
        evs = monitor_lib.read_events(self._tmp.name)
        self.assertEqual(evs[-1]["ev"], "dispatcher.governance-blocked")
        self.assertIn("rework-rejected", evs[-1]["comment"])

    def test_state_allocate_allows_p0_with_approval_ref(self):
        st = state_lib.SchedulerState(self._tmp.name, clock=lambda: 1000.0)
        content = TASK_TPL.format(
            name="TASK-001-x", status="open", priority="P0", risk="P0",
            approval="REV-2026-08-01-xxx", rework=0,
        )
        a = st.allocate("proj-p0ok", "TASK-001", worker="dispatcher",
                        task_content=content)
        self.assertIsNotNone(a)
        self.assertEqual(st.active_count(), 1)


class GovernanceMonitorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self._tmp.name, "state")
        self.clock = _FakeClock(1000.0)
        self.entries = build_fixture(self._tmp.name)
        self.snapshots = {}
        for e in self.entries:
            if not os.path.isdir(e.path):
                continue
            tasks = []
            tasks_dir = os.path.join(e.path, "runtime", "tasks")
            if os.path.isdir(tasks_dir):
                for fn in sorted(os.listdir(tasks_dir)):
                    p = os.path.join(tasks_dir, fn)
                    if os.path.isfile(p):
                        with open(p, encoding="utf-8") as f:
                            tasks.append({"name": fn, "content": f.read()})
            self.snapshots[e.id] = {"tasks": tasks}

    def tearDown(self):
        self._tmp.cleanup()

    def test_derive_governance_blocked_counts_and_ratio(self):
        snapshots = self.snapshots
        # 5 个项目各 1 个 open 候选；blocked = proj-p0 (p0-blocked) + proj-rework
        gov = monitor_lib.derive_governance([], snapshots=snapshots)
        self.assertEqual(len(gov["blocked_tasks"]), 2)
        self.assertEqual(
            {(b["project"], b["decision"]) for b in gov["blocked_tasks"]},
            {("proj-p0", "p0-blocked"), ("proj-rework", "rework-rejected")},
        )
        self.assertEqual(len(gov["blocked_projects"]), 2)
        self.assertAlmostEqual(gov["blocked_ratio"], 2 / 5)
        self.assertEqual(len(gov["alerts"]), 2)
        self.assertTrue(all(a["type"] == "blocked" for a in gov["alerts"]))

    def test_derive_governance_stale_allocations_alert(self):
        st = state_lib.SchedulerState(self.state_dir, clock=self.clock)
        st.allocate("proj-normal", "TASK-001", worker="dispatcher", ts=100.0)
        st.mark_running("proj-normal", "TASK-001", ts=100.0)
        st.check_timeouts(timeout=0, now=2000.0)  # → stale
        gov = monitor_lib.derive_governance(st.allocations.values(),
                                            snapshots=self.snapshots)
        self.assertEqual(len(gov["stale_allocations"]), 1)
        sa = gov["stale_allocations"][0]
        self.assertEqual(sa["project"], "proj-normal")
        self.assertEqual(sa["task"], "TASK-001")
        self.assertIn("stale", {a["type"] for a in gov["alerts"]})

    def test_derive_governance_without_snapshots_only_stale(self):
        st = state_lib.SchedulerState(self.state_dir, clock=self.clock)
        st.allocate("proj-normal", "TASK-001", worker="dispatcher", ts=100.0)
        st.mark_running("proj-normal", "TASK-001", ts=100.0)
        st.check_timeouts(timeout=0, now=2000.0)
        gov = monitor_lib.derive_governance(st.allocations.values(), snapshots=None)
        self.assertEqual(gov["blocked_tasks"], [])
        self.assertEqual(gov["blocked_ratio"], 0.0)
        self.assertEqual(len(gov["stale_allocations"]), 1)

    def test_build_payload_includes_governance_when_provided(self):
        gov = monitor_lib.derive_governance([], snapshots=self.snapshots)
        payload = monitor_lib.build_payload([], [], cursor=None, ts=1000.0,
                                            governance=gov)
        self.assertEqual(payload["governance"]["blocked_projects"],
                         ["proj-p0", "proj-rework"])
        # 不传 governance → 不携带该键（向后兼容）
        payload2 = monitor_lib.build_payload([], [], cursor=None, ts=1000.0)
        self.assertNotIn("governance", payload2)

    def test_monitor_once_payload_contains_governance(self):
        st = state_lib.SchedulerState(self.state_dir, clock=self.clock)
        st.allocate("proj-normal", "TASK-001", worker="dispatcher", ts=100.0)
        st.save()
        bodies = []
        cfg = {"server_url": "https://aimonitor.example/api/ingest",
               "token": "secret"}

        def fake_push(server_url, token, body):
            bodies.append(body)

        monitor_lib.monitor_once(self.state_dir, state=st, cfg=cfg,
                                 clock=self.clock, push_fn=fake_push,
                                 snapshots=self.snapshots)
        body = json.loads(bodies[0])
        self.assertIn("governance", body)
        self.assertEqual(len(body["governance"]["blocked_tasks"]), 2)


class _FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class GovernanceCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self._tmp.name, "projects.json")
        # 可执行项目 + 治理阻塞项目
        self.proj_ok = os.path.join(self._tmp.name, "proj-ok")
        self.proj_blocked = os.path.join(self._tmp.name, "proj-blocked")
        for p in (self.proj_ok, self.proj_blocked):
            os.makedirs(os.path.join(p, "kit", "cli"), exist_ok=True)
            write_file(os.path.join(p, "kit", "cli", "task"),
                       "#!/usr/bin/env python3\n"
                       "import sys\n"
                       "print('fake-task', *sys.argv[1:])\n", mode=0o755)
            write_file(os.path.join(p, "kit", "cli", "autoloop-coder"),
                       "#!/usr/bin/env python3\n"
                       "import sys\n"
                       "print('SHOULD-NOT-RUN', *sys.argv[1:])\n", mode=0o755)
        make_task(self._tmp.name, "proj-ok", "TASK-001", priority="P2", risk="P2")
        make_task(self._tmp.name, "proj-blocked", "TASK-001", priority="P0",
                  risk="P0", approval="none")
        write_file(self.cfg, json.dumps({"projects": [
            {"id": "proj-ok", "name": "proj-ok", "path": self.proj_ok},
            {"id": "proj-blocked", "name": "proj-blocked",
             "path": self.proj_blocked},
        ]}, ensure_ascii=False, indent=2))
        self.state_dir = os.path.join(self._tmp.name, "state")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, DISPATCHER_PY, *extra, "--config", self.cfg,
             "--state-dir", self.state_dir],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
        )

    def test_dispatch_dry_run_reports_decisions_and_does_not_execute(self):
        proc = self._run("dispatch", "--once", "--dry-run", "--max-workers", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[ok] proj-ok", proc.stdout)
        self.assertIn("[governance] proj-blocked", proc.stdout)
        self.assertIn("p0-blocked", proc.stdout)
        self.assertIn("未执行任何命令", proc.stdout)
        self.assertNotIn("SHOULD-NOT-RUN", proc.stdout)
        self.assertNotIn("SHOULD-NOT-RUN", proc.stderr)

    def test_run_dry_run_does_not_execute(self):
        proc = self._run("run", "--dry-run", "--max-workers", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[ok] proj-ok", proc.stdout)
        self.assertNotIn("SHOULD-NOT-RUN", proc.stdout)
        self.assertNotIn("SHOULD-NOT-RUN", proc.stderr)

    def test_run_executes_only_ok_candidates_skips_p0(self):
        proc = self._run("run", "--max-workers", "2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("▶ proj-ok", proc.stdout)
        self.assertNotIn("▶ proj-blocked", proc.stdout)
        # 治理拦截候选：非 dry-run 也打印 [governance]（跳过并记录告警，
        # FIND-001 修复）且写 dispatcher.governance-blocked 事件，不静默丢弃
        self.assertIn("[governance] proj-blocked", proc.stdout)
        self.assertIn("p0-blocked", proc.stdout)
        # proj-ok 会真实执行下行链（task start + autoloop-coder，打印
        # SHOULD-NOT-RUN 证明被执行）；proj-blocked 被治理拦截，绝不出现
        # 其任务的执行行（▶ proj-blocked 不存在）。
        # 事件流已记录 governance-blocked（blocked 语义，供 monitor/审计）
        evs = monitor_lib.read_events(self.state_dir)
        self.assertIsNotNone(evs)
        self.assertTrue(any(
            e["ev"] == "dispatcher.governance-blocked" and "p0-blocked" in
            (e.get("comment") or "") for e in evs
        ))


if __name__ == "__main__":
    unittest.main()
