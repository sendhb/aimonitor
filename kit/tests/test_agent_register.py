"""TASK-042/043 — kit/tools/agent/ 注册状态机与轮询单测。

TASK-042 覆盖：
- RegistrationState.load() 正确读取 agent.json
- RegistrationState.save() 正确写入 agent.json（保留其他字段）
- 状态转换：unregistered → pending（写入 req_id/request_key）
- 状态转换：pending → active（写入 token，清除 req_id/request_key）
- 状态转换不合法时抛异常
- build_register_payload() 返回格式正确的请求体
- generate_request_key() 返回 ≥ 32 字节随机字符串
- host_info 包含 hostname

TASK-043 覆盖（POLL-001/002/004）：
- RegistrationPoller.poll() 真实 HTTP 端到端：各状态解析、URL 推导、404/4xx 错误分类

REVIEW-r3 返工回归（TASK-042 r4）：
- --status active 不泄露 token 前缀（ISSUE-01）
- unregistered --register 不写 pending/不伪造 req_id（ISSUE-02）
- --register --reset 恢复 pending（ISSUE-02b）
- pending 输出顺序：stdout flush 先于 stderr（ISSUE-10）
"""
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_register as reg_lib  # noqa: E402
from test_agent_loop import MockRegisterServer  # noqa: E402


def valid_cfg(**overrides):
    cfg = {
        "server_url": "https://aimonitor.example.com/api/ingest",
        "token": "secret-token",
        "projects": [{"id": "proj-1", "path": "/srv/proj-1"}],
        "poll_interval_seconds": 30,
        "state": "active",
        "req_id": None,
        "request_key": None,
    }
    cfg.update(overrides)
    return cfg


class RegistrationStateTests(unittest.TestCase):
    """RegistrationState 状态机单元测试。"""

    def test_init_from_config_active(self):
        reg = reg_lib.RegistrationState(valid_cfg())
        self.assertEqual(reg.state, "active")
        self.assertIsNone(reg.req_id)
        self.assertIsNone(reg.request_key)

    def test_init_from_config_unregistered(self):
        reg = reg_lib.RegistrationState(valid_cfg(
            state="unregistered", token=None
        ))
        self.assertEqual(reg.state, "unregistered")
        self.assertIsNone(reg.req_id)

    def test_init_from_config_pending(self):
        reg = reg_lib.RegistrationState(valid_cfg(
            state="pending", token=None,
            req_id="req-abc", request_key="key-xyz"
        ))
        self.assertEqual(reg.state, "pending")
        self.assertEqual(reg.req_id, "req-abc")
        self.assertEqual(reg.request_key, "key-xyz")

    def test_is_registered_true(self):
        reg = reg_lib.RegistrationState(valid_cfg())
        self.assertTrue(reg.is_registered())

    def test_is_registered_false(self):
        reg = reg_lib.RegistrationState(valid_cfg(state="unregistered", token=None))
        self.assertFalse(reg.is_registered())
        reg = reg_lib.RegistrationState(valid_cfg(state="pending", token=None))
        self.assertFalse(reg.is_registered())

    # --- 状态转换 ---

    def test_transition_unregistered_to_pending(self):
        reg = reg_lib.RegistrationState(valid_cfg(state="unregistered", token=None))
        reg.transition_to("pending", req_id="req-001", request_key="key-001")
        self.assertEqual(reg.state, "pending")
        self.assertEqual(reg.req_id, "req-001")
        self.assertEqual(reg.request_key, "key-001")

    def test_transition_pending_to_active(self):
        reg = reg_lib.RegistrationState(valid_cfg(
            state="pending", token=None,
            req_id="req-001", request_key="key-001"
        ))
        reg.transition_to("active", token="new-token-42")
        self.assertEqual(reg.state, "active")
        self.assertIsNone(reg.req_id)  # 清除
        self.assertIsNone(reg.request_key)  # 清除

    def test_transition_invalid_unregistered_to_active(self):
        reg = reg_lib.RegistrationState(valid_cfg(state="unregistered", token=None))
        with self.assertRaises(reg_lib.RegistrationError) as cm:
            reg.transition_to("active", token="t")
        self.assertIn("非法状态转换", str(cm.exception))

    def test_transition_invalid_pending_to_unregistered(self):
        reg = reg_lib.RegistrationState(valid_cfg(state="pending", token=None))
        with self.assertRaises(reg_lib.RegistrationError) as cm:
            reg.transition_to("unregistered")
        self.assertIn("非法状态转换", str(cm.exception))

    def test_transition_active_to_anything(self):
        reg = reg_lib.RegistrationState(valid_cfg())
        for target in ("unregistered", "pending"):
            with self.assertRaises(reg_lib.RegistrationError):
                reg.transition_to(target)

    def test_transition_to_pending_missing_req_id(self):
        reg = reg_lib.RegistrationState(valid_cfg(state="unregistered", token=None))
        with self.assertRaises(reg_lib.RegistrationError) as cm:
            reg.transition_to("pending", request_key="key")
        self.assertIn("req_id", str(cm.exception))

    def test_transition_to_pending_missing_request_key(self):
        reg = reg_lib.RegistrationState(valid_cfg(state="unregistered", token=None))
        with self.assertRaises(reg_lib.RegistrationError) as cm:
            reg.transition_to("pending", req_id="req")
        self.assertIn("request_key", str(cm.exception))

    def test_transition_to_active_missing_token(self):
        reg = reg_lib.RegistrationState(valid_cfg(state="pending", token=None))
        with self.assertRaises(reg_lib.RegistrationError) as cm:
            reg.transition_to("active")
        self.assertIn("token", str(cm.exception))

    # --- 读写文件 ---

    def test_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(valid_cfg(state="unregistered", token=None), f)
            reg = reg_lib.RegistrationState.load(path)
            self.assertEqual(reg.state, "unregistered")

    def test_save_preserves_other_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(valid_cfg(), f)
            reg = reg_lib.RegistrationState(valid_cfg(state="unregistered", token=None))
            reg.save(path)
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["state"], "unregistered")
            self.assertEqual(saved["server_url"], "https://aimonitor.example.com/api/ingest")
            self.assertEqual(len(saved["projects"]), 1)

    def test_save_clears_req_id_on_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(valid_cfg(state="unregistered", token=None), f)
            reg = reg_lib.RegistrationState.load(path)
            reg.transition_to("pending", req_id="req-abc", request_key="key-xyz")
            reg.save(path)
            reg.transition_to("active", token="new-token")
            reg.save(path, token="new-token")
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["state"], "active")
            self.assertNotIn("req_id", saved)
            self.assertNotIn("request_key", saved)
            self.assertEqual(saved["token"], "new-token")

    def test_save_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "no-such.json")
            reg = reg_lib.RegistrationState(valid_cfg())
            with self.assertRaises(reg_lib.RegistrationError):
                reg.save(path)


class RegistrationPayloadTests(unittest.TestCase):
    """注册请求构造与密钥生成测试。"""

    def test_generate_request_key_length(self):
        key = reg_lib.generate_request_key()
        # token_urlsafe(24) → 32 字符（24 字节熵），字符串长度 ≥ 32（AGENT-003）
        self.assertGreaterEqual(len(key), 32)
        self.assertIsInstance(key, str)

    def test_generate_request_key_unique(self):
        keys = {reg_lib.generate_request_key() for _ in range(100)}
        self.assertEqual(len(keys), 100)

    def test_build_register_payload_structure(self):
        project = {"id": "proj-1", "path": "/srv/proj-1"}
        payload = reg_lib.build_register_payload(project, "test-key-abc")
        self.assertIn("project_id", payload)
        self.assertIn("path", payload)          # 契约：path（非 project_path）
        self.assertNotIn("project_path", payload)
        self.assertIn("request_key", payload)
        self.assertIn("host_info", payload)
        self.assertEqual(payload["project_id"], "proj-1")
        self.assertEqual(payload["path"], "/srv/proj-1")
        self.assertEqual(payload["request_key"], "test-key-abc")

    def test_build_register_payload_host_info(self):
        payload = reg_lib.build_register_payload(
            {"id": "p", "path": "/p"}, "key"
        )
        host_info = payload["host_info"]
        # 契约：host_info 为字符串（如 "hostname:dev-box, ip:192.168.1.5"）
        self.assertIsInstance(host_info, str)
        self.assertGreater(len(host_info), 0)
        self.assertIn("hostname:", host_info)

    def test_build_register_payload_with_enrollment_code(self):
        payload = reg_lib.build_register_payload(
            {"id": "p", "path": "/p"}, "key", enrollment_code="EC-001"
        )
        self.assertEqual(payload["enrollment_code"], "EC-001")

    def test_build_register_payload_without_enrollment_code(self):
        payload = reg_lib.build_register_payload(
            {"id": "p", "path": "/p"}, "key"
        )
        self.assertNotIn("enrollment_code", payload)


# ---------------------------------------------------------------------------
# RegistrationPoller 单元测试
# ---------------------------------------------------------------------------


class RegistrationPollerDeriveUrlTests(unittest.TestCase):
    """URL 推导测试（POLL-002）。"""

    def test_standard_url(self):
        url = reg_lib.RegistrationPoller.derive_status_url(
            "http://host:3113/api/ingest", "req-abc", "key-xyz"
        )
        self.assertEqual(
            url,
            "http://host:3113/api/register/req-abc/status?request_key=key-xyz"
        )

    def test_url_without_api_ingest(self):
        url = reg_lib.RegistrationPoller.derive_status_url(
            "http://host:3113/ingest", "req-001", "key-002"
        )
        self.assertEqual(
            url,
            "http://host:3113/ingest/api/register/req-001/status?request_key=key-002"
        )

    def test_https_with_trailing_slash(self):
        url = reg_lib.RegistrationPoller.derive_status_url(
            "https://monitor.example.com/api/ingest/", "req-1", "k-1"
        )
        self.assertEqual(
            url,
            "https://monitor.example.com/api/register/req-1/status?request_key=k-1"
        )

    def test_url_without_path(self):
        url = reg_lib.RegistrationPoller.derive_status_url(
            "http://host:3113", "req-1", "k-1"
        )
        self.assertEqual(
            url,
            "http://host:3113/api/register/req-1/status?request_key=k-1"
        )

    def test_request_key_special_chars(self):
        url = reg_lib.RegistrationPoller.derive_status_url(
            "http://host:3113/api/ingest", "req-1", "key+with/special?chars"
        )
        self.assertIn("request_key=key%2Bwith%2Fspecial%3Fchars", url)


class RegistrationPollerParseResponseTests(unittest.TestCase):
    """状态响应解析测试（POLL-001）。"""

    def setUp(self):
        self.poller = reg_lib.RegistrationPoller()

    def test_parse_pending(self):
        result = self.poller._parse_status_response(
            '{"status": "pending"}'
        )
        self.assertEqual(result, {"status": "pending"})

    def test_parse_approved(self):
        result = self.poller._parse_status_response(
            '{"status": "approved", "token": "tok-42", "project_id": "proj-1"}'
        )
        self.assertEqual(result, {
            "status": "approved",
            "token": "tok-42",
            "project_id": "proj-1",
        })

    def test_parse_approved_minimal(self):
        result = self.poller._parse_status_response(
            '{"status": "approved", "token": "tok-42"}'
        )
        self.assertEqual(result, {"status": "approved", "token": "tok-42"})

    def test_parse_rejected(self):
        result = self.poller._parse_status_response(
            '{"status": "rejected"}'
        )
        self.assertEqual(result, {"status": "rejected"})

    def test_parse_expired(self):
        result = self.poller._parse_status_response(
            '{"status": "expired"}'
        )
        self.assertEqual(result, {"status": "expired"})

    def test_parse_revoked(self):
        result = self.poller._parse_status_response(
            '{"status": "revoked"}'
        )
        self.assertEqual(result, {"status": "revoked"})

    def test_parse_invalid_json(self):
        with self.assertRaises(reg_lib.PollError):
            self.poller._parse_status_response("not json")

    def test_parse_missing_token_on_approved(self):
        with self.assertRaises(reg_lib.PollError) as cm:
            self.poller._parse_status_response(
                '{"status": "approved"}'
            )
        self.assertIn("token", str(cm.exception))

    def test_parse_unknown_status(self):
        with self.assertRaises(reg_lib.PollError) as cm:
            self.poller._parse_status_response(
                '{"status": "unknown"}'
            )
        self.assertIn("未知状态", str(cm.exception))


class RegistrationPollerErrorTests(unittest.TestCase):
    """错误处理测试（POLL-004）。"""

    def test_poll_rejected_error_message(self):
        err = reg_lib.PollRejectedError("服务端拒绝请求（HTTP 401）")
        self.assertIn("401", str(err))

    def test_poll_retryable_error_message(self):
        err = reg_lib.PollRetryableError("轮询网络失败: Connection refused")
        self.assertIn("Connection refused", str(err))

    def test_poll_error_base(self):
        err = reg_lib.PollError("基础错误")
        self.assertIn("基础错误", str(err))


class RegistrationPollerHttpTests(unittest.TestCase):
    """poll() 通过真实 mock 服务端的端到端测试（POLL-001/004）。"""

    def setUp(self):
        # 防止环境 http_proxy 劫持 127.0.0.1 本地 mock（同 test_agent_http）
        self._old_no_proxy = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def tearDown(self):
        if self._old_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._old_no_proxy

    def test_poll_pending_over_http(self):
        """pending：GET 状态 URL，返回 pending，不阻塞。"""
        with MockRegisterServer() as server:
            server.set_responses([{"status": "pending"}])
            result = reg_lib.RegistrationPoller().poll(
                server.ingest_url(), "req-http-1", "key-http-1")
            self.assertEqual(result, {"status": "pending"})
            self.assertIn("/api/register/req-http-1/status?request_key=key-http-1",
                          server.requests[0]["path"])

    def test_poll_approved_over_http(self):
        """approved：提取 token 与 project_id。"""
        with MockRegisterServer() as server:
            server.set_responses([
                {"status": "approved", "token": "tok-http-1", "project_id": "p-1"},
            ])
            result = reg_lib.RegistrationPoller().poll(
                server.ingest_url(), "req-http-1", "key-http-1")
            self.assertEqual(result, {
                "status": "approved", "token": "tok-http-1", "project_id": "p-1",
            })

    def test_poll_terminal_states_over_http(self):
        """rejected/expired/revoked：正确返回终态。"""
        for status in ("rejected", "expired", "revoked"):
            with MockRegisterServer() as server:
                server.set_responses([{"status": status}])
                result = reg_lib.RegistrationPoller().poll(
                    server.ingest_url(), "req-http-1", "key-http-1")
                self.assertEqual(result, {"status": status})

    def test_poll_404_raises_retryable(self):
        """404：可重试错误。"""
        with MockRegisterServer() as server:
            server.set_next_http_error(404, "not found")
            with self.assertRaises(reg_lib.PollRetryableError) as cm:
                reg_lib.RegistrationPoller().poll(
                    server.ingest_url(), "req-http-1", "key-http-1")
            self.assertIn("404", str(cm.exception))

    def test_poll_4xx_raises_rejected(self):
        """4xx（不含 404）：不可重试错误。"""
        with MockRegisterServer() as server:
            server.set_next_http_error(401, "unauthorized")
            with self.assertRaises(reg_lib.PollRejectedError) as cm:
                reg_lib.RegistrationPoller().poll(
                    server.ingest_url(), "req-http-1", "key-http-1")
            self.assertIn("401", str(cm.exception))


# ---------------------------------------------------------------------------
# CLI --register 子命令测试（AGENT-004）
# ---------------------------------------------------------------------------


class AgentRegisterCliTests(unittest.TestCase):
    """AGENT-004: --register / --register --status 输出验证（子进程 + 本地 mock 服务端）。"""

    AGENT = os.path.join(AGENT_DIR, "agent.py")

    def write(self, tmp, cfg):
        path = os.path.join(tmp, "agent.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return path

    def run_cli(self, path, *extra):
        return subprocess.run(
            [sys.executable, self.AGENT, "--register", "--config", path, *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )

    def test_register_status_active(self):
        """--register --status：state=active 显示当前状态。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, valid_cfg())
            proc = self.run_cli(path, "--status")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("注册状态: active", proc.stdout)
            self.assertIn("已配置 token", proc.stdout)

    def test_register_status_pending_shows_req_id(self):
        """--register --status：state=pending 显示 req_id。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, valid_cfg(
                state="pending", token=None,
                req_id="req-cli-1", request_key="key-cli-1",
            ))
            proc = self.run_cli(path, "--status")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("注册状态: pending", proc.stdout)
            self.assertIn("req-cli-1", proc.stdout)

    def test_register_active_noop(self):
        """--register：state=active 输出"已注册，无需重复注册"并退出 0。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, valid_cfg())
            proc = self.run_cli(path)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("已注册，无需重复注册", proc.stdout)

    def test_register_status_active_no_token_leak(self):
        """--register --status：active 状态不输出 token 或 token 前缀（ISSUE-01）。"""
        with tempfile.TemporaryDirectory() as tmp:
            token = "supersecret-token-value"
            path = self.write(tmp, valid_cfg(token=token))
            proc = self.run_cli(path, "--status")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("已配置 token", proc.stdout)
            self.assertNotIn(token, proc.stdout)
            self.assertNotIn(token[:8], proc.stdout)

    def test_register_unregistered_submits_and_persists(self):
        """--register：state=unregistered 提交 POST /api/register，成功写 pending
        （req_id/request_key 入 agent.json），然后进入轮询（TASK-014 接线）。

        服务端返回 pending → 提交成功；轮询首轮返回 rejected → 退出 1
        （避免无限轮询，验证到 pending 持久化即可）。
        """
        with tempfile.TemporaryDirectory() as tmp, \
                MockRegisterServer() as server:
            cfg = valid_cfg(state="unregistered", token=None)
            # 干净的未注册配置：不含 req_id/request_key（模拟全新 agent.json）
            cfg.pop("req_id", None)
            cfg.pop("request_key", None)
            cfg["server_url"] = server.ingest_url()
            path = self.write(tmp, cfg)
            # 第一次响应 = POST /api/register 成功（req_id）；
            # 第二次响应 = 轮询首轮（rejected 终态，避免无限轮询）
            server.set_responses([
                {"req_id": "req-sub-1", "status": "pending", "pending_since": 1786892400.0},
                {"status": "rejected"},
            ])
            proc = self.run_cli(path)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("提交注册申请至 aimonitor 服务端", proc.stdout)
            self.assertIn("注册申请已提交（req_id: req-sub-1）", proc.stdout)
            # agent.json 已写 pending（req_id/request_key 持久化）
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["state"], "pending")
            self.assertEqual(saved["req_id"], "req-sub-1")
            self.assertIn("request_key", saved)
            self.assertTrue(saved["request_key"])
            # 确实发生了 POST /api/register 提交
            reg_posts = [r for r in server.requests if r["path"] == "/api/register"]
            self.assertEqual(len(reg_posts), 1, server.requests)
            self.assertIn("proj-1", json.loads(reg_posts[0]["body"])["project_id"])

    def test_register_unregistered_409_no_persist(self):
        """--register：state=unregistered，服务端 409（已注册/已有申请）→
        提示冲突、退出 1，agent.json 不得被写为 pending（ISSUE-02 语义保留）。
        """
        with tempfile.TemporaryDirectory() as tmp, \
                MockRegisterServer() as server:
            cfg = valid_cfg(state="unregistered", token=None)
            cfg.pop("req_id", None)
            cfg.pop("request_key", None)
            cfg["server_url"] = server.ingest_url()
            path = self.write(tmp, cfg)
            server.set_next_http_error(409, '{"existing": "active"}')
            proc = self.run_cli(path)
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("注册冲突（HTTP 409）", proc.stderr)
            # 失败不得伪造 pending / req_id
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["state"], "unregistered")
            self.assertNotIn("req_id", saved)
            self.assertNotIn("request_key", saved)

    def test_register_reset_pending_to_unregistered(self):
        """--register --reset：pending → unregistered，清除 req_id/request_key（ISSUE-02b）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, valid_cfg(
                state="pending", token=None,
                req_id="req-ghost", request_key="key-ghost",
            ))
            proc = self.run_cli(path, "--reset")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("已重置为 unregistered", proc.stdout)
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["state"], "unregistered")
            self.assertNotIn("req_id", saved)
            self.assertNotIn("request_key", saved)

    def test_register_reset_active_refused(self):
        """--register --reset：active 拒绝重置，配置不变（ISSUE-02b）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, valid_cfg())
            proc = self.run_cli(path, "--reset")
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("不允许 reset", proc.stderr)
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["state"], "active")

    def test_register_pending_waits(self):
        """--register：state=pending 输出"正在等待审批（req_id: xxx）"并进入轮询。

        ISSUE-10：合并 stdout+stderr 后，首行必须是 stdout 的等待消息（flush 生效），
        轮询拒绝错误（stderr）出现在其后。
        """
        with tempfile.TemporaryDirectory() as tmp, \
                MockRegisterServer() as server:
            cfg = valid_cfg(
                state="pending", token=None,
                req_id="req-cli-2", request_key="key-cli-2",
            )
            cfg["server_url"] = server.ingest_url()
            path = self.write(tmp, cfg)
            server.set_next_response({"status": "rejected"})
            proc = subprocess.run(
                [sys.executable, self.AGENT, "--register", "--config", path],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", timeout=60,
            )
            merged = proc.stdout
            self.assertEqual(proc.returncode, 1, merged)
            self.assertIn("正在等待审批（req_id: req-cli-2）", merged)
            first_line = merged.splitlines()[0]
            self.assertIn("正在等待审批", first_line)


class AgentRegisterSignalTests(unittest.TestCase):
    """FIND-001 回归：--register 轮询中 SIGTERM 干净退出（exit 0、无 traceback）。

    REVIEW-2026-08-20 FIND-001：信号处理器原在 --register 分支之后才安装且
    _cmd_register 不传 stop_fn → 轮询阶段 SIGINT 抛 KeyboardInterrupt traceback
    （returncode -2）；本测试验证修复后 SIGTERM → exit 0、无 traceback。
    """

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
    def test_register_polling_sigterm_clean_exit(self):
        """--register 轮询等待审批中收到 SIGTERM → exit 0、无 traceback。"""
        with tempfile.TemporaryDirectory() as tmp, \
                MockRegisterServer() as server:
            cfg = valid_cfg(
                state="pending", token=None,
                req_id="req-sig-1", request_key="key-sig-1",
            )
            cfg["server_url"] = server.ingest_url()
            path = os.path.join(tmp, "agent.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
            server.set_next_response({"status": "pending"})
            proc = subprocess.Popen(
                [sys.executable, self.AGENT, "--register", "--config", path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
            try:
                # 等待进入轮询（首个状态查询请求到达 mock 服务端）
                deadline = time.time() + 15
                while time.time() < deadline:
                    if len(server.requests) >= 1:
                        break
                    time.sleep(0.05)
                self.assertGreaterEqual(len(server.requests), 1,
                                        "未进入轮询（无状态查询请求）")
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            # 干净退出：exit 0（与常驻模式契约一致），无 KeyboardInterrupt/traceback
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertIn("正在等待审批", stdout)
            self.assertNotIn("Traceback", stderr)
            self.assertNotIn("KeyboardInterrupt", stderr)


if __name__ == "__main__":
    unittest.main()