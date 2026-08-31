"""TASK-027 — kit/tools/agent/ 主循环与常驻单测。

覆盖：
- poll_once 一轮：全部成功 / 单项目失败（可重试与不可重试）→ 退避计数 /
  退避中项目本轮跳过 / 项目间隔离（A 失败退避不阻塞 B）/ payload 构造失败与
  PayloadTooLargeError → 记失败不推送
- run_forever 常驻：按 interval 轮询、睡眠按 POLL_SLEEP_STEP 分片、
  睡眠中停止标志置位 → 当前轮完成后不再 poll（干净退出）、启动前已置位 → 零 poll、
  interval 非法 → ValueError
- AgentLog：quiet 抑制 info 保留 error；默认两者都输出
- StopFlag + 信号：request_stop 置位并记录 signum；install_signal_handlers 注册
  SIGINT/SIGTERM 置位标志（测试后恢复处理器，不污染测试进程）
- CLI 子进程 E2E：--once + 本地 mock ingest 成功（收到 1 个请求、Authorization 正确）、
  --once --quiet 连接失败（exit 0 + 错误在 stderr + stdout 无常规日志）、
  --interval 非法 → exit 2、常驻 SIGTERM → exit 0 无 traceback
- TASK-043 轮询循环：approved 写 token 并切推送 / rejected/expired/revoked 终态 /
  网络错误退避 3 次 / 404 退避 3 次 / 4xx 立即退出 / pending TTL 超时（POLL-001/003/004）
"""
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "agent"
)
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 复用 test_agent_http 的 mock 服务器
import agent_loop  # noqa: E402
import agent_http  # noqa: E402
import agent_payload  # noqa: E402
from test_agent_http import MockIngestServer  # noqa: E402


class FakeClock:
    """可手动推进的假时钟（time_fn / clock 注入用），替代真实时间。"""

    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeSleeper:
    """睡眠替身：记录请求秒数并推进假时钟（不真实 sleep）。"""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)
        self.clock.advance(seconds)


class FakePusher:
    """推送替身：记录调用；errors 序列按调用顺序依次弹出抛错，空则成功。"""

    def __init__(self, errors=None):
        self.errors = list(errors or [])
        self.calls = []

    def __call__(self, server_url, token, body):
        self.calls.append({"server_url": server_url, "token": token, "body": body})
        if self.errors:
            raise self.errors.pop(0)


def ok_cfg():
    """两个项目的规范化配置；server_url 指向不可达端口 1（单测注入推送替身，不会真连）。"""
    return {
        "server_url": "http://127.0.0.1:1/api/ingest",
        "token": "secret-token",
        "projects": [
            {"id": "proj-a", "path": "/nonexistent/a"},
            {"id": "proj-b", "path": "/nonexistent/b"},
        ],
        "poll_interval_seconds": 30,
    }


class PollOnceTests(unittest.TestCase):
    def test_all_success(self):
        clock = FakeClock(1000.0)
        pusher = FakePusher()
        states = {}
        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (2, 0, 0))
        self.assertEqual(len(pusher.calls), 2)
        for call, pid in zip(pusher.calls, ("proj-a", "proj-b")):
            self.assertEqual(call["server_url"], ok_cfg()["server_url"])
            self.assertEqual(call["token"], "secret-token")
            body = json.loads(call["body"])
            self.assertEqual(body["project_id"], pid)
            self.assertIsNone(body["files"]["tasks"])  # runtime 缺失容错 → payload 正常
        for pid in ("proj-a", "proj-b"):
            self.assertEqual(states[pid].consecutive_failures, 0)
            self.assertTrue(states[pid].can_push())  # 成功未进入退避

    def test_single_failure_backoff_and_isolation(self):
        clock = FakeClock(1000.0)
        pusher = FakePusher(errors=[agent_http.PushServerError(500, body="boom")])
        states = {}
        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (1, 0, 1))
        self.assertEqual(states["proj-a"].consecutive_failures, 1)
        self.assertFalse(states["proj-a"].can_push())  # A 退避中
        self.assertEqual(states["proj-a"].next_retry_at, 1001.0)  # 失败后退避 1s（now + 1）
        self.assertEqual(states["proj-b"].consecutive_failures, 0)
        self.assertTrue(states["proj-b"].can_push())  # B 不受影响

    def test_non_retryable_failure_still_backs_off(self):
        clock = FakeClock(1000.0)
        pusher = FakePusher(errors=[agent_http.PushRejectedError(401)])
        states = {}
        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (1, 0, 1))
        self.assertEqual(states["proj-a"].consecutive_failures, 1)
        self.assertFalse(states["proj-a"].can_push())

    def test_backoff_project_skipped(self):
        clock = FakeClock(1000.0)
        states = {}
        # 第一轮：proj-a 失败进入退避（next_retry_at = 1001）
        agent_loop.poll_once(ok_cfg(), states=states, clock=clock,
                             push_fn=FakePusher(errors=[agent_http.PushServerError(503)]))
        self.assertFalse(states["proj-a"].can_push())
        # 第二轮：proj-a 跳过，proj-b 正常推送
        pusher = FakePusher()
        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (1, 1, 0))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(json.loads(pusher.calls[0]["body"])["project_id"], "proj-b")

    def test_backoff_expires_then_pushes(self):
        clock = FakeClock(1000.0)
        states = {}
        agent_loop.poll_once(ok_cfg(), states=states, clock=clock,
                             push_fn=FakePusher(errors=[agent_http.PushNetworkError("boom")]))
        self.assertFalse(states["proj-a"].can_push())
        # 时间推进超过退避 → 恢复推送
        clock.advance(1.0)
        pusher = FakePusher()
        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (2, 0, 0))
        self.assertEqual(states["proj-a"].consecutive_failures, 0)  # 成功清零

    def test_payload_error_records_failure_no_push(self):
        clock = FakeClock(1000.0)
        pusher = FakePusher()
        states = {}

        def bad_payload(pid, snapshot, **kwargs):
            raise agent_payload.PayloadError("模拟构造失败")

        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, payload_fn=bad_payload, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (0, 0, 2))
        self.assertEqual(pusher.calls, [])
        self.assertEqual(states["proj-a"].consecutive_failures, 1)

    def test_payload_too_large_records_failure_no_push(self):
        clock = FakeClock(1000.0)
        pusher = FakePusher()
        states = {}

        def bad_serialize(payload):
            raise agent_payload.PayloadTooLargeError("模拟超大 payload")

        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, serialize_fn=bad_serialize, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (0, 0, 2))
        self.assertEqual(pusher.calls, [])


class RunForeverTests(unittest.TestCase):
    def _run(self, interval, stop_fn, clock, sleeper, poller):
        return agent_loop.run_forever(
            ok_cfg(), interval, log=agent_loop.AgentLog(quiet=True),
            stop_fn=stop_fn, clock=clock, sleeper=sleeper, poller=poller)

    def test_polls_at_interval_and_chunks_sleep(self):
        clock = FakeClock(0.0)
        sleeper = FakeSleeper(clock)
        poll_counts = []

        def poller(cfg, states=None, log=None, clock=None):
            poll_counts.append(clock())

        def stop_fn():
            return len(poll_counts) >= 2

        result = self._run(2.0, stop_fn, clock, sleeper, poller)
        self.assertEqual(result, "stopped")
        self.assertEqual(poll_counts, [0.0, 2.0])  # 间隔 2s 轮询
        self.assertEqual(sleeper.calls, [0.5, 0.5, 0.5, 0.5])  # 2s 按 0.5s 分片

    def test_stop_during_sleep_exits_after_current_round(self):
        clock = FakeClock(0.0)
        sleeper = FakeSleeper(clock)
        poll_counts = []
        stop = agent_loop.StopFlag()

        def poller(cfg, states=None, log=None, clock=None):
            poll_counts.append(clock())

        # 睡眠 3 个分片后（t=1.5）请求停止 → 第 2 轮不应发生
        def stop_fn():
            if clock() >= 1.5:
                stop.request_stop(signal.SIGINT)
            return stop()

        self._run(2.0, stop_fn, clock, sleeper, poller)
        self.assertEqual(poll_counts, [0.0])
        self.assertEqual(sleeper.calls, [0.5, 0.5, 0.5])

    def test_stop_before_start_no_poll(self):
        clock = FakeClock(0.0)
        sleeper = FakeSleeper(clock)
        poll_counts = []
        stop = agent_loop.StopFlag()
        stop.request_stop(signal.SIGTERM)

        def poller(cfg, states=None, log=None, clock=None):
            poll_counts.append(clock())

        result = self._run(1.0, stop, clock, sleeper, poller)
        self.assertEqual(result, "stopped")
        self.assertEqual(poll_counts, [])
        self.assertEqual(sleeper.calls, [])

    def test_invalid_interval_rejected(self):
        clock = FakeClock(0.0)
        sleeper = FakeSleeper(clock)
        for bad in (0, -1, "5", True, None):
            with self.assertRaises(ValueError, msg=f"interval={bad!r}"):
                self._run(bad, lambda: True, clock, sleeper, lambda **kw: None)


class AgentLogTests(unittest.TestCase):
    def test_quiet_suppresses_info_keeps_error(self):
        out, err = io.StringIO(), io.StringIO()
        log = agent_loop.AgentLog(quiet=True, stream=out, err_stream=err)
        log.info("常规")
        log.error("故障")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("故障", err.getvalue())

    def test_default_outputs_both(self):
        out, err = io.StringIO(), io.StringIO()
        log = agent_loop.AgentLog(quiet=False, stream=out, err_stream=err)
        log.info("常规")
        log.error("故障")
        self.assertIn("常规", out.getvalue())
        self.assertIn("故障", err.getvalue())


class StopFlagTests(unittest.TestCase):
    def test_stop_flag_callable_and_signum(self):
        flag = agent_loop.StopFlag()
        self.assertFalse(flag())
        flag.request_stop(signal.SIGTERM)
        self.assertTrue(flag())
        self.assertEqual(flag.signum, signal.SIGTERM)
        # 首个信号被记录，后续请求不覆盖
        flag.request_stop(signal.SIGINT)
        self.assertEqual(flag.signum, signal.SIGTERM)

    @unittest.skipIf(os.name == "nt", "Windows 管道环境下 os.kill(SIGINT) 会直接终止进程，自激不可行（TASK-008）")
    def test_install_signal_handlers_sets_flag(self):
        flag = agent_loop.StopFlag()
        installed = agent_loop.install_signal_handlers(flag)
        self.assertIn(signal.SIGINT, installed)
        self.assertIn(signal.SIGTERM, installed)
        try:
            os.kill(os.getpid(), signal.SIGINT)
            deadline = time.time() + 2.0
            while not flag.stopped and time.time() < deadline:
                time.sleep(0.01)
        finally:
            for sig in installed:  # 恢复默认处理器，不污染测试进程
                signal.signal(sig, signal.SIG_DFL)
        self.assertTrue(flag.stopped)
        self.assertEqual(flag.signum, signal.SIGINT)

    @unittest.skipUnless(os.name == "nt", "仅 Windows：验证 install_signal_handlers 容错注册（SIGTERM 不可装时跳过不抛异常）")
    def test_install_signal_handlers_windows_graceful(self):
        # TASK-008：Windows 上 SIGTERM 不可注册（install_signal_handlers 内部 catch
        # ValueError/OSError 跳过），且不能 os.kill(SIGINT) 自激——仅断言可注册部分。
        flag = agent_loop.StopFlag()
        installed = agent_loop.install_signal_handlers(flag)
        self.assertIn(signal.SIGINT, installed)
        for sig in installed:  # 恢复默认处理器，不污染测试进程
            signal.signal(sig, signal.SIG_DFL)


def _write_config(tmp, server_url, project_path, poll=30):
    cfg = {
        "server_url": server_url,
        "token": "secret-token",
        "projects": [{"id": "proj-e2e", "path": project_path}],
        "poll_interval_seconds": poll,
    }
    path = os.path.join(tmp, "agent.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


class AgentCliOnceTests(unittest.TestCase):
    """子进程 E2E：--once 推送一轮（真实本地 mock ingest）。"""

    AGENT = os.path.join(AGENT_DIR, "agent.py")

    def setUp(self):
        # 防止环境 http_proxy 劫持 127.0.0.1 本地 mock（同 test_agent_http）
        self._old_no_proxy = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def tearDown(self):
        if self._old_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._old_no_proxy

    def test_once_pushes_one_round(self):
        with tempfile.TemporaryDirectory() as tmp, \
                MockIngestServer(response=(200, "ok", {})) as server:
            cfg_path = _write_config(tmp, server.ingest_url(), tmp)
            proc = subprocess.run(
                [sys.executable, self.AGENT, "--once", "--config", cfg_path],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 1)  # 单轮 = 1 个请求
            req = server.requests[0]
            self.assertEqual(req["path"], "/api/ingest")
            self.assertEqual(req["headers"].get("Authorization"), "Bearer secret-token")
            body = json.loads(req["body"])
            self.assertEqual(body["project_id"], "proj-e2e")
            self.assertIn("单轮结束", proc.stdout)

    def test_once_quiet_failure_error_visible_exit_0(self):
        # 指向不可达端口 1：轮内推送失败（可重试网络错误）→ exit 0，错误在 stderr
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = _write_config(tmp, "http://127.0.0.1:1/api/ingest", tmp)
            proc = subprocess.run(
                [sys.executable, self.AGENT, "--once", "--quiet", "--config", cfg_path],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("推送失败", proc.stderr)
            self.assertEqual(proc.stdout, "")  # quiet 抑制所有常规日志

    def test_interval_invalid_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = _write_config(tmp, "http://127.0.0.1:1/api/ingest", tmp)
            for bad in ("0", "-5", "abc"):
                proc = subprocess.run(
                    [sys.executable, self.AGENT, "--interval", bad,
                     "--check-config", "--config", cfg_path],
                    capture_output=True, text=True, timeout=30,
                    encoding="utf-8", errors="replace")
                self.assertEqual(proc.returncode, 2, f"interval={bad}")
                self.assertIn("interval", proc.stderr)

    def test_interval_valid_with_check_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = _write_config(tmp, "http://127.0.0.1:1/api/ingest", tmp)
            proc = subprocess.run(
                [sys.executable, self.AGENT, "--interval", "10",
                 "--check-config", "--config", cfg_path],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("配置 OK", proc.stdout)


class AgentCliDaemonSignalTests(unittest.TestCase):
    """子进程 E2E：常驻模式收到 SIGTERM 干净退出（exit 0、无 traceback）。"""

    AGENT = os.path.join(AGENT_DIR, "agent.py")

    def setUp(self):
        self._old_no_proxy = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def tearDown(self):
        if self._old_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._old_no_proxy

    @unittest.skipIf(os.name == "nt", "Windows 无 SIGTERM 语义：send_signal(SIGTERM)=TerminateProcess 硬杀，无法验证优雅退出（TASK-008）")
    def test_sigterm_clean_exit(self):
        with tempfile.TemporaryDirectory() as tmp, \
                MockIngestServer(response=(200, "ok", {})) as server:
            cfg_path = _write_config(tmp, server.ingest_url(), tmp, poll=30)
            proc = subprocess.Popen(
                [sys.executable, self.AGENT, "--config", cfg_path, "--quiet"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace")
            try:
                # 等待第一轮推送到达 mock 服务器（启动即 poll 一轮）
                deadline = time.time() + 15
                while time.time() < deadline:
                    if len(server.requests) >= 1:
                        break
                    time.sleep(0.05)
                self.assertGreaterEqual(len(server.requests), 1, "第一轮推送未到达")
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertNotIn("KeyboardInterrupt", stderr)


# ---------------------------------------------------------------------------
# 注册轮询循环测试
# ---------------------------------------------------------------------------


class MockRegisterServer:
    """模拟注册审批服务端（context manager）。

    用法:
        with MockRegisterServer() as server:
            server.set_next_response({"status": "pending"})
            # ... 测试逻辑
    """

    def __init__(self, host="127.0.0.1", port=0):
        self.host = host
        self.port = port
        self._server = None
        self._thread = None
        self.responses = []  # 按顺序返回响应
        self.requests = []

    def set_next_response(self, data):
        self.responses.append(data)

    def set_responses(self, responses):
        self.responses = list(responses)

    def set_next_http_error(self, status, body=""):
        """追加一个 HTTP 错误响应（如 404/401），用于错误处理路径测试。"""
        self.responses.append((status, body))

    def status_url(self):
        return f"http://{self.host}:{self.port}/api/register"

    def ingest_url(self):
        return f"http://{self.host}:{self.port}/api/ingest"

    def __enter__(self):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            server_ref = self

            def log_message(self, fmt, *args):
                pass  # 抑制日志

            def do_GET(self):
                self.server_ref.requests.append({
                    "path": self.path,
                    "headers": dict(self.headers),
                })
                if self.server_ref.responses:
                    entry = self.server_ref.responses.pop(0)
                else:
                    entry = {"status": "pending"}
                if isinstance(entry, tuple):
                    # (status, body_text)：HTTP 错误响应
                    status, body_text = entry
                    body = str(body_text).encode("utf-8")
                else:
                    # dict：JSON 状态响应
                    status, body = 200, json.dumps(entry).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                # 模拟 ingest 端点（用于 approved 后的推送测试）
                # 或 /api/register 提交（TASK-014：支持 set_next_response 提供 req_id）
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len) if content_len else b"{}"
                path = self.path
                self.server_ref.requests.append({
                    "path": path,
                    "headers": dict(self.headers),
                    "body": body.decode("utf-8", errors="replace"),
                })
                if path == "/api/register" and self.server_ref.responses:
                    entry = self.server_ref.responses.pop(0)
                    if isinstance(entry, tuple):
                        status, body_text = entry
                        resp_body = str(body_text).encode("utf-8")
                    else:
                        status, resp_body = 201, json.dumps(entry).encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.end_headers()
                    self.wfile.write(resp_body)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok"}')

        self._server = http.server.HTTPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args):
        if self._server:
            self._server.shutdown()
            self._server.server_close()  # 关闭监听 socket，避免 ResourceWarning
            self._thread.join(2)


class RegistrationPollingTests(unittest.TestCase):
    """run_registration_polling 单元测试（POLL-001/003/004）。"""

    def setUp(self):
        self.clock = FakeClock(1000.0)
        self.sleeper = FakeSleeper(self.clock)

    def _pending_cfg(self, server_url, **overrides):
        cfg = {
            "server_url": server_url,
            "token": None,
            "projects": [{"id": "proj-a", "path": "/tmp/proj-a"}],
            "poll_interval_seconds": 30,
            "state": "pending",
            "req_id": "req-test-001",
            "request_key": "key-test-001",
        }
        cfg.update(overrides)
        return cfg

    def test_polling_approved_triggers_push(self):
        """POLL-003: 收到 token 后写入 agent.json 并启动推送。"""
        with tempfile.TemporaryDirectory() as tmp, \
                MockRegisterServer() as server:
            server.set_responses([
                {"status": "pending"},
                {"status": "approved", "token": "tok-42", "project_id": "proj-a"},
            ])
            cfg = self._pending_cfg(server.ingest_url())
            path = os.path.join(tmp, "agent.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)

            result = agent_loop.run_registration_polling(
                cfg, path, log=agent_loop.AgentLog(quiet=True),
                clock=self.clock, sleeper=self.sleeper,
                stop_fn=lambda: len(server.requests) >= 3,  # 2 轮 poll + 1 轮推送
            )

            self.assertEqual(result, "approved")
            # 验证 agent.json 已更新
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["state"], "active")
            self.assertEqual(saved["token"], "tok-42")
            self.assertNotIn("req_id", saved)
            self.assertNotIn("request_key", saved)

            # FIND-001 强化：approved 后确实发生了 POST /api/ingest 推送
            # （请求序列 = 2 次 GET 状态轮询 + 1 次 POST 推送）
            post_paths = [r for r in server.requests if r["path"] == "/api/ingest"]
            self.assertEqual(len(post_paths), 1, server.requests)
            self.assertEqual(post_paths[0]["headers"].get("Authorization"),
                             "Bearer tok-42")  # 推送使用新 token
            push_body = json.loads(post_paths[0]["body"])
            self.assertEqual(push_body["project_id"], "proj-a")

    def test_polling_rejected_returns_rejected(self):
        """POLL-001: rejected 状态正确返回。"""
        with MockRegisterServer() as server:
            server.set_responses([{"status": "rejected"}])
            cfg = self._pending_cfg(server.ingest_url())

            result = agent_loop.run_registration_polling(
                cfg, "/tmp/agent.json", log=agent_loop.AgentLog(quiet=True),
                clock=self.clock, sleeper=self.sleeper,
            )
            self.assertEqual(result, "rejected")

    def test_polling_expired_returns_expired(self):
        """POLL-001: expired 状态正确返回。"""
        with MockRegisterServer() as server:
            server.set_responses([{"status": "expired"}])
            cfg = self._pending_cfg(server.ingest_url())

            result = agent_loop.run_registration_polling(
                cfg, "/tmp/agent.json", log=agent_loop.AgentLog(quiet=True),
                clock=self.clock, sleeper=self.sleeper,
            )
            self.assertEqual(result, "expired")

    def test_polling_revoked_returns_revoked(self):
        """POLL-001: revoked 状态正确返回。"""
        with MockRegisterServer() as server:
            server.set_responses([{"status": "revoked"}])
            cfg = self._pending_cfg(server.ingest_url())

            result = agent_loop.run_registration_polling(
                cfg, "/tmp/agent.json", log=agent_loop.AgentLog(quiet=True),
                clock=self.clock, sleeper=self.sleeper,
            )
            self.assertEqual(result, "revoked")

    def test_polling_missing_req_id_returns_error(self):
        """缺少 req_id/request_key 时返回 error。"""
        cfg = self._pending_cfg("http://127.0.0.1:1/api/ingest")
        cfg["req_id"] = None

        result = agent_loop.run_registration_polling(
            cfg, "/tmp/agent.json", log=agent_loop.AgentLog(quiet=True),
            clock=self.clock, sleeper=self.sleeper,
        )
        self.assertEqual(result, "error")

    def test_polling_stop_flag_returns_stopped(self):
        """stop_fn 置位时干净退出。"""
        with MockRegisterServer() as server:
            server.set_responses([{"status": "pending"}])
            cfg = self._pending_cfg(server.ingest_url())
            stop = agent_loop.StopFlag()
            stop.request_stop()

            result = agent_loop.run_registration_polling(
                cfg, "/tmp/agent.json", log=agent_loop.AgentLog(quiet=True),
                clock=self.clock, sleeper=self.sleeper, stop_fn=stop,
            )
            self.assertEqual(result, "stopped")

    def _quiet_err_log(self):
        """quiet 日志器：捕获 stderr 供断言，常规日志不输出。"""
        return agent_loop.AgentLog(quiet=True, stream=io.StringIO(),
                                   err_stream=io.StringIO())

    def test_polling_network_error_backoff_then_error(self):
        """POLL-004: 网络错误指数退避（复用 agent_retry），3 次后提示无法连接并退出。"""
        cfg = self._pending_cfg("http://127.0.0.1:1/api/ingest")
        log = self._quiet_err_log()
        result = agent_loop.run_registration_polling(
            cfg, "/tmp/agent.json", log=log,
            clock=self.clock, sleeper=self.sleeper,
        )
        self.assertEqual(result, "error")
        self.assertIn("无法连接服务端", log.err_stream.getvalue())

    def test_polling_404_retries_then_error(self):
        """POLL-004: 404 退避重试 3 次后退出。"""
        with MockRegisterServer() as server:
            server.set_responses([(404, "not found"), (404, "not found"), (404, "not found")])
            cfg = self._pending_cfg(server.ingest_url())
            log = self._quiet_err_log()
            result = agent_loop.run_registration_polling(
                cfg, "/tmp/agent.json", log=log,
                clock=self.clock, sleeper=self.sleeper,
            )
            self.assertEqual(result, "error")
            self.assertEqual(len(server.requests), 3)  # 3 次轮询后退出
            self.assertIn("404", log.err_stream.getvalue())

    def test_polling_4xx_immediate_error(self):
        """POLL-004: 4xx（不含 404）不重试，打印错误立即退出。"""
        with MockRegisterServer() as server:
            server.set_responses([(401, "unauthorized")])
            cfg = self._pending_cfg(server.ingest_url())
            log = self._quiet_err_log()
            result = agent_loop.run_registration_polling(
                cfg, "/tmp/agent.json", log=log,
                clock=self.clock, sleeper=self.sleeper,
            )
            self.assertEqual(result, "error")
            self.assertEqual(len(server.requests), 1)  # 仅轮询一次
            self.assertIn("401", log.err_stream.getvalue())

    def test_polling_ttl_timeout(self):
        """POLL-004: pending 超过 7 天未审批 → timeout 提示重新注册。"""
        class BigSleeper:
            """每次睡眠都大幅推进假时钟（模拟长时间流逝），不真实 sleep。"""

            def __init__(self, clock):
                self.clock = clock

            def __call__(self, seconds):
                self.clock.advance(agent_loop.REGISTER_POLL_MAX_SECONDS + 1)

        with MockRegisterServer() as server:
            server.set_responses([{"status": "pending"}])
            cfg = self._pending_cfg(server.ingest_url())
            clock = FakeClock(0.0)
            log = self._quiet_err_log()
            result = agent_loop.run_registration_polling(
                cfg, "/tmp/agent.json", log=log,
                clock=clock, sleeper=BigSleeper(clock),
            )
            self.assertEqual(result, "timeout")
            self.assertEqual(len(server.requests), 1)  # 仅轮询一次，TTL 在第二次外层循环触发
            self.assertIn("请重新注册", log.err_stream.getvalue())


class IncrementalEventsTests(unittest.TestCase):
    """TASK-066 — _incremental_events 增量筛选与游标推进语义。"""

    def ev(self, seq):
        return {"seq": seq, "ev": "task.created", "task": "TASK-001"}

    def test_none_events_returns_none_none(self):
        self.assertEqual(agent_loop._incremental_events(None, None), (None, None))
        self.assertEqual(agent_loop._incremental_events(None, 5), (None, None))

    def test_no_cursor_returns_full_batch(self):
        events = [self.ev(1), self.ev(2), self.ev(3)]
        batch, cursor_out = agent_loop._incremental_events(events, None)
        self.assertEqual([e["seq"] for e in batch], [1, 2, 3])
        self.assertEqual(cursor_out, 3)

    def test_incremental_after_cursor(self):
        events = [self.ev(1), self.ev(2), self.ev(3), self.ev(4)]
        batch, cursor_out = agent_loop._incremental_events(events, 2)
        self.assertEqual([e["seq"] for e in batch], [3, 4])
        self.assertEqual(cursor_out, 4)

    def test_no_new_events_keeps_cursor(self):
        """游标已到文件最大 seq（全部已确认）→ 本轮无新事件，游标保持。"""
        events = [self.ev(1), self.ev(2)]
        batch, cursor_out = agent_loop._incremental_events(events, 2)
        self.assertEqual(batch, [])
        self.assertEqual(cursor_out, 2)

    def test_file_reset_resends_all(self):
        """事件文件被截断/重建（cursor > 文件最大 seq）→ 全量重推，不静默丢。"""
        events = [self.ev(1), self.ev(2)]
        batch, cursor_out = agent_loop._incremental_events(events, 10)
        self.assertEqual([e["seq"] for e in batch], [1, 2])
        self.assertEqual(cursor_out, 10)  # 游标不回退（服务端按 seq 去重）

    def test_empty_events_returns_empty_with_cursor(self):
        batch, cursor_out = agent_loop._incremental_events([], None)
        self.assertEqual(batch, [])
        self.assertIsNone(cursor_out)
        batch, cursor_out = agent_loop._incremental_events([], 3)
        self.assertEqual(batch, [])
        self.assertEqual(cursor_out, 3)

    def test_batch_capped_and_cursor_linked_bug001(self):
        """BUG-001 回归：>MAX_TASK_EVENTS 积压时截断与游标联动，尾部下轮重推不丢。"""
        limit = agent_payload.MAX_TASK_EVENTS
        events = [self.ev(i) for i in range(1, limit + 51)]  # 250 条（1..250）
        batch, cursor_out = agent_loop._incremental_events(events, None)
        self.assertEqual(len(batch), limit)
        self.assertEqual(cursor_out, limit)  # 游标只反映实际送达的最大 seq
        # 下一轮：从 cursor 续推尾部（201..250，共 50 条）
        batch2, cursor_out2 = agent_loop._incremental_events(events, cursor_out)
        self.assertEqual([e["seq"] for e in batch2], list(range(limit + 1, limit + 51)))
        self.assertEqual(cursor_out2, limit + 50)
        # 第三轮：全部送达，无新事件
        batch3, cursor_out3 = agent_loop._incremental_events(events, cursor_out2)
        self.assertEqual(batch3, [])
        self.assertEqual(cursor_out3, limit + 50)

    def test_negative_seq_cursor_out_never_negative(self):
        """SMELL-001 兜底：即使负 seq 混入，游标也不会算出负值；
        负 seq 的真正过滤在读取层（read_task_events 只收 seq>=1）。"""
        events = [{"seq": -5, "ev": "bad"}, {"seq": 1, "ev": "ok"}]
        batch, cursor_out = agent_loop._incremental_events(events, None)
        self.assertEqual([e["seq"] for e in batch], [-5, 1])
        self.assertEqual(cursor_out, 1)


class PollOnceEventCursorTests(unittest.TestCase):
    """TASK-066 — poll_once 携带事件增量并在推送成功后推进游标。"""

    def make_project(self):
        root = tempfile.mkdtemp()
        logs = os.path.join(root, "runtime", "logs")
        os.makedirs(logs, exist_ok=True)
        with open(os.path.join(logs, "task-events.jsonl"), "w", encoding="utf-8") as f:
            f.write('{"seq": 1, "ev": "task.created", "task": "TASK-001"}\n')
            f.write('{"seq": 2, "ev": "task.started", "task": "TASK-001"}\n')
            f.write('{"seq": 3, "ev": "task.done", "task": "TASK-001"}\n')
        return root, os.path.join(logs, ".push-cursor")

    def test_pushes_events_and_advances_cursor_on_success(self):
        root, cursor_path = self.make_project()
        cfg = {"server_url": "http://127.0.0.1:1/api/ingest", "token": "t",
               "projects": [{"id": "proj-events", "path": root}],
               "poll_interval_seconds": 30}
        pusher = FakePusher()
        pushed, skipped, failed = agent_loop.poll_once(
            cfg, states={}, log=agent_loop.AgentLog(quiet=True),
            clock=FakeClock(0.0), push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (1, 0, 0))
        body = json.loads(pusher.calls[0]["body"])
        self.assertEqual(body["project_id"], "proj-events")
        self.assertEqual([e["seq"] for e in body["events"]], [1, 2, 3])
        self.assertEqual(body["cursor"], 3)
        with open(cursor_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["seq"], 3)

    def test_cursor_not_advanced_on_failure(self):
        root, cursor_path = self.make_project()
        cfg = {"server_url": "http://127.0.0.1:1/api/ingest", "token": "t",
               "projects": [{"id": "proj-events", "path": root}],
               "poll_interval_seconds": 30}
        pusher = FakePusher(errors=[agent_http.PushServerError(500, body="boom")])
        pushed, skipped, failed = agent_loop.poll_once(
            cfg, states={}, log=agent_loop.AgentLog(quiet=True),
            clock=FakeClock(0.0), push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (0, 0, 1))
        self.assertFalse(os.path.exists(cursor_path))

    def test_cursor_write_failure_logs_not_crash(self):
        """SMELL-001：游标写失败（ValueError/OSError）只告警，不使 agent 循环崩溃。"""
        root, cursor_path = self.make_project()
        cfg = {"server_url": "http://127.0.0.1:1/api/ingest", "token": "t",
               "projects": [{"id": "proj-events", "path": root}],
               "poll_interval_seconds": 30}
        pusher = FakePusher()

        def bad_cursor_write(path, seq):
            raise ValueError("模拟游标写入失败")

        err = io.StringIO()
        pushed, skipped, failed = agent_loop.poll_once(
            cfg, states={}, log=agent_loop.AgentLog(quiet=True, err_stream=err),
            clock=FakeClock(0.0), push_fn=pusher, cursor_write_fn=bad_cursor_write)
        self.assertEqual((pushed, skipped, failed), (1, 0, 0))
        self.assertIn("游标写入失败", err.getvalue())

    def test_second_poll_only_pushes_incremental(self):
        root, cursor_path = self.make_project()
        cfg = {"server_url": "http://127.0.0.1:1/api/ingest", "token": "t",
               "projects": [{"id": "proj-events", "path": root}],
               "poll_interval_seconds": 30}
        agent_loop.poll_once(cfg, states={}, log=agent_loop.AgentLog(quiet=True),
                             clock=FakeClock(0.0), push_fn=FakePusher())
        # 追加一条新事件后第二轮：只推新增的 seq=4
        with open(os.path.join(root, "runtime", "logs", "task-events.jsonl"),
                  "a", encoding="utf-8") as f:
            f.write('{"seq": 4, "ev": "task.blocked", "task": "TASK-001"}\n')
        pusher = FakePusher()
        agent_loop.poll_once(cfg, states={}, log=agent_loop.AgentLog(quiet=True),
                             clock=FakeClock(0.0), push_fn=pusher)
        body = json.loads(pusher.calls[0]["body"])
        self.assertEqual([e["seq"] for e in body["events"]], [4])
        self.assertEqual(body["cursor"], 4)


if __name__ == "__main__":
    unittest.main()
