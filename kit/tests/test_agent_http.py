"""TASK-025 — kit/tools/agent/ HTTP 推送客户端单测。

覆盖：
- 请求形状：POST /api/ingest、Authorization: Bearer <token>、Content-Type: application/json、
  User-Agent、body 字节与 payload 一致
- 状态码分类：200/2xx 成功返回 PushResult；400/401/409/413 与其它 4xx → PushRejectedError
  （不可重试）；429 → PushRateLimitedError（可重试 + Retry-After 解析）；5xx → PushServerError
  （可重试）
- 超时：read 超时（真实慢服务器）→ PushNetworkError(timeout=True)；connect 超时映射
  （模拟 connect 抛 TimeoutError）→ PushNetworkError(timeout=True)；connect_timeout /
  read_timeout 正确接线到连接对象
- 网络失败：连接被拒 → PushNetworkError(timeout=False)；is_retryable 分类
- 入参校验：非法 URL（scheme/主机/端口）、空 token、body 非字符串、超时非法 → PushError
- 响应体上限：超大响应被截断到 MAX_RESPONSE_BODY
"""
import http.client
import os
import sys
import threading
import time
import unittest
import unittest.mock as mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "agent"
)
sys.path.insert(0, AGENT_DIR)
import agent_http  # noqa: E402

PAYLOAD = '{"project_id":"proj-1","ts":1786892400.0,"files":{"focus":null}}'
TOKEN = "secret-token"


class MockIngestServer:
    """线程化本地 mock ingest 服务器（127.0.0.1 临时端口）。

    每个请求：记录 (method/path/headers/body) 到 requests；按 handler 或固定
    response 返回。slow_seconds > 0 时先睡眠再响应（用于 read 超时测试）；
    body_slow_seconds > 0 时发送状态头后睡眠再写响应体（用于 FIND-002
    错误响应体读超时测试）。
    """

    def __init__(self, handler=None, response=(200, "ok", {}), slow_seconds=0.0,
                 body_slow_seconds=0.0):
        self.handler = handler
        self.response = response
        self.slow_seconds = slow_seconds
        self.body_slow_seconds = body_slow_seconds
        self.requests = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RequestHandler)
        self.httpd.daemon_threads = True
        self.httpd.mock_server = self
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def ingest_url(self):
        return self.base_url + "/api/ingest"


class _RequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        server = self.server.mock_server
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        record = {
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers.items()),
            "body": body,
        }
        server.requests.append(record)
        if server.handler is not None:
            status, resp_body, extra_headers = server.handler(record)
        else:
            status, resp_body, extra_headers = server.response
        if server.slow_seconds:
            time.sleep(server.slow_seconds)
        try:
            self.send_response(status)
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body.encode("utf-8"))))
            self.end_headers()
            if server.body_slow_seconds:
                time.sleep(server.body_slow_seconds)
            self.wfile.write(resp_body.encode("utf-8"))
        except OSError:
            pass  # 客户端超时断开后的写失败忽略

    def log_message(self, *args):
        pass


def fixed_server(status, body="", headers=None):
    return MockIngestServer(response=(status, body, headers or {}))


class AgentHttpTestCase(unittest.TestCase):
    def setUp(self):
        # 防止环境 http_proxy 劫持 127.0.0.1 本地 mock（urllib ProxyHandler 读环境变量）
        self._old_no_proxy = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def tearDown(self):
        if self._old_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._old_no_proxy

    def push(self, url=None, token=TOKEN, body=PAYLOAD, **kwargs):
        return agent_http.push_payload(url or fixed_server(200).ingest_url(),
                                       token, body, **kwargs)


class RequestShapeTests(AgentHttpTestCase):
    def test_post_shape_and_success(self):
        with fixed_server(200, "ok") as server:
            result = agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(result, agent_http.PushResult(200, "ok"))
        req = server.requests[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/api/ingest")
        self.assertEqual(req["headers"].get("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(req["headers"].get("Content-Type"), "application/json")
        self.assertEqual(req["headers"].get("User-Agent"), agent_http.USER_AGENT)
        self.assertEqual(req["body"], PAYLOAD)

    def test_other_2xx_success(self):
        with fixed_server(204) as server:
            result = agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(result.status, 204)
        self.assertEqual(result.body, "")

    def test_utf8_body_sent_as_bytes(self):
        payload = '{"files":{"focus":"# 中文任务\\n正文"}}'
        with fixed_server(200) as server:
            agent_http.push_payload(server.ingest_url(), TOKEN, payload)
        self.assertEqual(server.requests[0]["body"], payload)


class StatusCodeTests(AgentHttpTestCase):
    def _assert_rejected(self, status, body="err"):
        with fixed_server(status, body) as server:
            with self.assertRaises(agent_http.PushRejectedError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(ctx.exception.status, status)
        self.assertEqual(ctx.exception.body, body)
        self.assertFalse(agent_http.is_retryable(ctx.exception))

    def test_400_rejected(self):
        self._assert_rejected(400)

    def test_401_rejected(self):
        self._assert_rejected(401)

    def test_409_rejected(self):
        self._assert_rejected(409)

    def test_413_rejected(self):
        self._assert_rejected(413)

    def test_other_4xx_rejected(self):
        self._assert_rejected(404)

    def test_rate_limited_429_retryable(self):
        with fixed_server(429, "slow down") as server:
            with self.assertRaises(agent_http.PushRateLimitedError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(ctx.exception.status, 429)
        self.assertIsNone(ctx.exception.retry_after)
        self.assertTrue(agent_http.is_retryable(ctx.exception))

    def test_rate_limited_retry_after_parsed(self):
        with fixed_server(429, "slow down", {"Retry-After": "42"}) as server:
            with self.assertRaises(agent_http.PushRateLimitedError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(ctx.exception.retry_after, 42)

    def test_rate_limited_invalid_retry_after_none(self):
        with fixed_server(429, "slow down", {"Retry-After": "Thu, 01 Jan 2026 00:00:00 GMT"}) as server:
            with self.assertRaises(agent_http.PushRateLimitedError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertIsNone(ctx.exception.retry_after)

    def test_server_error_500_retryable(self):
        with fixed_server(500, "boom") as server:
            with self.assertRaises(agent_http.PushServerError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(ctx.exception.status, 500)
        self.assertTrue(agent_http.is_retryable(ctx.exception))

    def test_server_error_503_retryable(self):
        with fixed_server(503) as server:
            with self.assertRaises(agent_http.PushServerError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertTrue(agent_http.is_retryable(ctx.exception))

    def test_redirect_302_cross_origin_not_followed(self):
        """FIND-001 回归：跨源 302 不跟随——Authorization 不泄露给异源；3xx 归 PushRejectedError。"""
        with fixed_server(200) as target, \
             fixed_server(302, "moved", {"Location": target.ingest_url()}) as redirector:
            with self.assertRaises(agent_http.PushRejectedError) as ctx:
                agent_http.push_payload(redirector.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(ctx.exception.status, 302)
        self.assertFalse(agent_http.is_retryable(ctx.exception))
        self.assertEqual(target.requests, [])  # 目标服务器未收到任何请求（含 Authorization）

    def test_redirect_same_host_not_followed_single_request(self):
        """FIND-001 回归：同主机 302 也不跟随——仅 1 个 POST 请求，无静默降级。"""
        with fixed_server(302, "moved", {"Location": "/api/ingest2"}) as server:
            with self.assertRaises(agent_http.PushRejectedError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(ctx.exception.status, 302)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["method"], "POST")

    def test_redirect_308_not_followed(self):
        with fixed_server(308, "moved", {"Location": "http://127.0.0.1:9/api/ingest"}) as server:
            with self.assertRaises(agent_http.PushRejectedError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(ctx.exception.status, 308)
        self.assertFalse(agent_http.is_retryable(ctx.exception))

    def test_redirect_3xx_invalid_location_no_crash(self):
        """3xx 带畸形 Location（urlsplit 会抛 ValueError）也不跟随、不崩溃。"""
        with fixed_server(302, "moved", {"Location": "http://[::1"}) as server:
            with self.assertRaises(agent_http.PushRejectedError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(ctx.exception.status, 302)

    def test_large_error_body_truncated_in_message(self):
        with fixed_server(400, "x" * 5000) as server:
            with self.assertRaises(agent_http.PushRejectedError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(ctx.exception.body, "x" * 5000)
        self.assertLess(len(str(ctx.exception)), 5000)


class TimeoutTests(AgentHttpTestCase):
    def test_read_timeout_real_slow_server(self):
        """真实慢服务器：sleep 超过 read_timeout → PushNetworkError(timeout=True)。"""
        with MockIngestServer(response=(200, "ok", {}), slow_seconds=1.0) as server:
            t0 = time.time()
            with self.assertRaises(agent_http.PushNetworkError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD,
                                        connect_timeout=2.0, read_timeout=0.2)
            elapsed = time.time() - t0
        self.assertTrue(ctx.exception.timeout)
        self.assertTrue(agent_http.is_retryable(ctx.exception))
        self.assertLess(elapsed, 0.9)  # 未等满 slow_seconds=1.0，超时确实生效

    def test_connect_timeout_maps_to_network_error(self):
        """connect 阶段抛 TimeoutError（do_open 包装为 URLError）→ PushNetworkError(timeout=True)。"""
        with mock.patch.object(agent_http._SplitTimeoutHTTPConnection, "connect",
                               side_effect=TimeoutError("connect timed out")):
            with self.assertRaises(agent_http.PushNetworkError) as ctx:
                agent_http.push_payload("http://127.0.0.1:9/api/ingest", TOKEN, PAYLOAD)
        self.assertTrue(ctx.exception.timeout)
        self.assertTrue(agent_http.is_retryable(ctx.exception))

    def test_connect_timeout_wired_to_connection(self):
        """connect_timeout 被传给 http.client 连接构造（最终生效的 timeout kwarg）。

        在 http.client.HTTPConnection.__init__ 层探测（urllib do_open 默认传
        _GLOBAL_DEFAULT_TIMEOUT 哨兵，经 mixin 覆盖为 connect_timeout 后才到这一层）。
        """
        captured = {}
        orig_init = http.client.HTTPConnection.__init__

        def spy_init(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return orig_init(self, *args, **kwargs)

        with fixed_server(200) as server:
            with mock.patch.object(http.client.HTTPConnection, "__init__", spy_init):
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD,
                                        connect_timeout=1.5, read_timeout=3.0)
        self.assertEqual(captured["timeout"], 1.5)

    def test_read_timeout_applied_after_connect(self):
        """connect 完成后 socket 超时被切换为 read_timeout。"""
        seen = {}
        real_connect = agent_http._SplitTimeoutHTTPConnection.connect

        def spy_connect(self):
            real_connect(self)
            seen["sock_timeout"] = self.sock.gettimeout()

        with fixed_server(200) as server:
            with mock.patch.object(agent_http._SplitTimeoutHTTPConnection,
                                   "connect", spy_connect):
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD,
                                        connect_timeout=1.0, read_timeout=2.5)
        self.assertEqual(seen["sock_timeout"], 2.5)

    def test_connection_refused_maps_to_network_error(self):
        """连接被拒（刚关闭的端口）→ PushNetworkError(timeout=False)，可重试。"""
        with fixed_server(200) as server:
            port = server.httpd.server_port
        with self.assertRaises(agent_http.PushNetworkError) as ctx:
            agent_http.push_payload(f"http://127.0.0.1:{port}/api/ingest", TOKEN, PAYLOAD)
        self.assertFalse(ctx.exception.timeout)
        self.assertTrue(agent_http.is_retryable(ctx.exception))

    def test_error_body_read_timeout_maps_to_network_error(self):
        """FIND-002 回归：500 状态头发送后响应体 stall → PushNetworkError(timeout=True)。"""
        with MockIngestServer(response=(500, "boom", {}), body_slow_seconds=1.0) as server:
            with self.assertRaises(agent_http.PushNetworkError) as ctx:
                agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD,
                                        connect_timeout=2.0, read_timeout=0.2)
        self.assertTrue(ctx.exception.timeout)
        self.assertTrue(agent_http.is_retryable(ctx.exception))
        self.assertNotIsInstance(ctx.exception, agent_http.PushServerError)


class ResponseBodyTests(AgentHttpTestCase):
    def test_large_response_capped(self):
        with fixed_server(200, "x" * (agent_http.MAX_RESPONSE_BODY + 100)) as server:
            result = agent_http.push_payload(server.ingest_url(), TOKEN, PAYLOAD)
        self.assertEqual(len(result.body), agent_http.MAX_RESPONSE_BODY)


class ValidationTests(AgentHttpTestCase):
    def test_invalid_scheme(self):
        for url in ("ftp://host/api/ingest", "file:///tmp/x", "not-a-url"):
            with self.assertRaises(agent_http.PushError, msg=f"url={url!r}"):
                agent_http.push_payload(url, TOKEN, PAYLOAD)

    def test_missing_host(self):
        with self.assertRaises(agent_http.PushError):
            agent_http.push_payload("http:///api/ingest", TOKEN, PAYLOAD)

    def test_non_numeric_port(self):
        with self.assertRaises(agent_http.PushError):
            agent_http.push_payload("http://host:abc/api/ingest", TOKEN, PAYLOAD)

    def test_userinfo_url_rejected(self):
        """NOTE-001：带 userinfo 的 URL 拒绝，且异常消息不泄露密码。"""
        for url in ("https://user:secret@127.0.0.1:9/api/ingest",
                    "http://user:secret@host/api/ingest"):
            with self.subTest(url=url):
                with self.assertRaises(agent_http.PushError) as ctx:
                    agent_http.push_payload(url, TOKEN, PAYLOAD)
                self.assertNotIn("secret", str(ctx.exception))

    def test_invalid_token(self):
        for bad in ("", None, 123):
            with self.assertRaises(agent_http.PushError, msg=f"token={bad!r}"):
                agent_http.push_payload("http://127.0.0.1:9/api/ingest", bad, PAYLOAD)

    def test_invalid_body(self):
        for bad in (b"bytes", None, 42, {"a": 1}):
            with self.assertRaises(agent_http.PushError, msg=f"body={bad!r}"):
                agent_http.push_payload("http://127.0.0.1:9/api/ingest", TOKEN, bad)

    def test_invalid_timeouts(self):
        for name in ("connect_timeout", "read_timeout"):
            for bad in (0, -1, "5", True, None):
                with self.assertRaises(agent_http.PushError, msg=f"{name}={bad!r}"):
                    agent_http.push_payload("http://127.0.0.1:9/api/ingest", TOKEN, PAYLOAD,
                                            **{name: bad})


class RetryableTests(AgentHttpTestCase):
    def test_is_retryable_classification(self):
        self.assertTrue(agent_http.is_retryable(agent_http.PushRateLimitedError(429)))
        self.assertTrue(agent_http.is_retryable(agent_http.PushServerError(500)))
        self.assertTrue(agent_http.is_retryable(agent_http.PushNetworkError("net")))
        self.assertFalse(agent_http.is_retryable(agent_http.PushRejectedError(400)))
        self.assertFalse(agent_http.is_retryable(agent_http.PushError("base")))

    def test_push_error_is_base_of_all(self):
        for exc in (agent_http.PushRejectedError(400),
                    agent_http.PushRateLimitedError(429),
                    agent_http.PushServerError(500),
                    agent_http.PushNetworkError("net")):
            self.assertIsInstance(exc, agent_http.PushError)


if __name__ == "__main__":
    unittest.main()
