"""TASK-074 — kit/tools/dispatcher/monitor.py 单测 + CLI monitor 接线测试。

覆盖：
- 心跳文件写入（mtime/内容）
- 事件流读取（None/空/损坏行跳过）+ 游标 roundtrip
- payload 形状（project_id=dispatcher，files.tasks=分配快照，events/cursor）
- 增量事件（游标推进后只推新增；None 事件流 → payload 不携带事件）
- monitor_once：dry-run（无 cfg）不推送；有 cfg 推送成功并推进游标
- CLI monitor：dry-run 输出 + 心跳落盘；配置错误 exit 1
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

AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "telemetry"
)
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, DISPATCHER_DIR)
import agent_payload  # noqa: E402
import monitor as monitor_lib  # noqa: E402
import state as state_lib  # noqa: E402


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_state(state_dir, clock, events=True):
    """构造 1 条 running 分配 + 事件流（3 条事件）并落盘。"""
    st = state_lib.SchedulerState(state_dir, clock=clock)
    st.allocate("proj-a", "TASK-001", worker="dispatcher", ts=100.0)
    st.mark_running("proj-a", "TASK-001", ts=100.0)
    st.mark_done("proj-a", "TASK-001", ts=101.0)
    st.save()
    return st


class MonitorUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self._tmp.name, "state")
        self.clock = FakeClock(1000.0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_heartbeat_creates_file(self):
        path = monitor_lib.write_heartbeat(self.state_dir, ts=1234.0)
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            self.assertIn("1234", f.read())
        self.assertEqual(os.path.basename(path), state_lib.HEARTBEAT_FILE)

    def test_read_heartbeat_mtime(self):
        self.assertIsNone(monitor_lib.read_heartbeat(self.state_dir))
        monitor_lib.write_heartbeat(self.state_dir, ts=1234.0)
        os.utime(os.path.join(self.state_dir, state_lib.HEARTBEAT_FILE),
                 (1234.0, 1234.0))
        self.assertEqual(monitor_lib.read_heartbeat(self.state_dir), 1234.0)

    def test_read_events_missing_empty_and_parsed(self):
        self.assertIsNone(monitor_lib.read_events(self.state_dir))
        path = os.path.join(self.state_dir, state_lib.EVENTS_FILE)
        os.makedirs(self.state_dir, exist_ok=True)
        open(path, "w", encoding="utf-8").close()
        self.assertEqual(monitor_lib.read_events(self.state_dir), [])
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"seq": 1, "ev": "a"}\n')
            f.write("not-json\n")
            f.write('{"seq": 2, "ev": "b"}\n')
        events = monitor_lib.read_events(self.state_dir)
        self.assertEqual([e["seq"] for e in events], [1, 2])

    def test_cursor_roundtrip(self):
        self.assertIsNone(monitor_lib.read_cursor(self.state_dir))
        monitor_lib.write_cursor(self.state_dir, 7)
        self.assertEqual(monitor_lib.read_cursor(self.state_dir), 7)

    def test_build_payload_shape(self):
        st = make_state(self.state_dir, self.clock)
        events = monitor_lib.read_events(self.state_dir)
        payload = monitor_lib.build_payload(
            st.allocations.values(), events, cursor=3, ts=1234.0,
        )
        self.assertEqual(payload["project_id"], "dispatcher")
        self.assertEqual(payload["ts"], 1234.0)
        tasks = payload["files"]["tasks"]
        self.assertEqual(tasks[0]["name"], "proj-a|TASK-001")
        content = json.loads(tasks[0]["content"])
        self.assertEqual(content["status"], "done")
        self.assertEqual(payload["cursor"], 3)
        self.assertEqual(len(payload["events"]), 3)

    def test_incremental_events_cursor_semantics(self):
        all_ev = [{"seq": 1, "ev": "a"}, {"seq": 2, "ev": "b"}, {"seq": 3, "ev": "c"}]
        batch, cursor = monitor_lib.incremental_events(all_ev, None)
        self.assertEqual([e["seq"] for e in batch], [1, 2, 3])
        self.assertEqual(cursor, 3)
        batch, cursor = monitor_lib.incremental_events(all_ev, 2)
        self.assertEqual([e["seq"] for e in batch], [3])
        self.assertEqual(cursor, 3)
        batch, cursor = monitor_lib.incremental_events(all_ev, 99)
        self.assertEqual([e["seq"] for e in batch], [1, 2, 3])  # 游标超尾 → 全量重推
        self.assertEqual(cursor, 99)  # 游标保留（与 agent_loop 语义一致：本轮重推一次）
        batch, cursor = monitor_lib.incremental_events(None, None)
        self.assertEqual((batch, cursor), (None, None))

    def test_monitor_once_dry_run_does_not_push(self):
        make_state(self.state_dir, self.clock)
        pushed = []
        result = monitor_lib.monitor_once(
            self.state_dir, cfg=None, clock=self.clock,
            push_fn=lambda *a, **k: pushed.append(a),
        )
        self.assertEqual(result, (0, 1, 0))
        self.assertEqual(pushed, [])
        self.assertTrue(os.path.isfile(
            os.path.join(self.state_dir, state_lib.HEARTBEAT_FILE)))

    def test_monitor_once_pushes_and_advances_cursor(self):
        st = make_state(self.state_dir, self.clock)
        pushed_bodies = []

        def fake_push(server_url, token, body):
            pushed_bodies.append(body)

        cfg = {"server_url": "https://aimonitor.example/api/ingest",
               "token": "secret"}
        pushed, skipped, failed = monitor_lib.monitor_once(
            self.state_dir, state=st, cfg=cfg, clock=self.clock,
            push_fn=fake_push,
        )
        self.assertEqual((pushed, skipped, failed), (1, 0, 0))
        self.assertEqual(len(pushed_bodies), 1)
        body = json.loads(pushed_bodies[0])
        self.assertEqual(body["project_id"], "dispatcher")
        self.assertEqual(body["cursor"], 3)  # 3 条事件已推送
        self.assertEqual(monitor_lib.read_cursor(self.state_dir), 3)

    def test_monitor_once_second_round_only_incremental(self):
        st = make_state(self.state_dir, self.clock)
        pushed_bodies = []
        cfg = {"server_url": "https://aimonitor.example/api/ingest",
               "token": "secret"}

        def fake_push(server_url, token, body):
            pushed_bodies.append(body)

        monitor_lib.monitor_once(self.state_dir, state=st, cfg=cfg,
                                 clock=self.clock, push_fn=fake_push)
        # 第二单事件：新增 1 条
        st.allocate("proj-b", "TASK-002", worker="dispatcher", ts=110.0)
        st.save()
        self.clock.advance(10)
        monitor_lib.monitor_once(self.state_dir, state=st, cfg=cfg,
                                 clock=self.clock, push_fn=fake_push)
        body2 = json.loads(pushed_bodies[1])
        self.assertEqual(len(body2["events"]), 1)
        self.assertEqual(body2["events"][0]["ev"], "dispatcher.allocated")
        self.assertEqual(body2["events"][0]["project"], "proj-b")
        self.assertEqual(body2["cursor"], 4)

    def test_serialize_payload_within_capacity(self):
        st = make_state(self.state_dir, self.clock)
        events = monitor_lib.read_events(self.state_dir)
        payload = monitor_lib.build_payload(
            st.allocations.values(), events, cursor=3, ts=self.clock(),
        )
        body = agent_payload.serialize_payload(payload)
        self.assertIsInstance(body, str)
        self.assertLessEqual(len(body.encode("utf-8")), agent_payload.MAX_PAYLOAD_BYTES)


class MonitorCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = os.path.join(self._tmp.name, "state")
        self.cfg = os.path.join(self._tmp.name, "projects.json")
        os.makedirs(os.path.join(self._tmp.name, "proj-a"), exist_ok=True)
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"projects": [
                {"id": "proj-a", "name": "proj-a",
                 "path": os.path.join(self._tmp.name, "proj-a")},
            ]}, f, ensure_ascii=False, indent=2)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, DISPATCHER_PY, *extra, "--config", self.cfg,
             "--state-dir", self.state_dir],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
        )

    def test_cli_monitor_dry_run_writes_heartbeat(self):
        proc = self._run("monitor")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dry-run", proc.stdout)
        self.assertTrue(os.path.isfile(
            os.path.join(self.state_dir, state_lib.HEARTBEAT_FILE)))

    def test_cli_monitor_bad_config_returns_1(self):
        bad = os.path.join(self._tmp.name, "bad-agent.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{}")
        proc = self._run("monitor", "--monitor-config", bad)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("monitor 配置错误", proc.stderr)


if __name__ == "__main__":
    unittest.main()
