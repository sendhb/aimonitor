# -*- coding: utf-8 -*-
"""TASK-029 集成冒烟：agent --once 对本项目（aibase 自身）生成可打印 payload。

以本仓库为被监控项目（dogfood），跑**真实** `agent.py --once --config`（子进程），
把 payload POST 到本地 mock ingest 服务器（仅监听 127.0.0.1 临时端口，零外部网络）。

断言（对应 TASK-029 验收标准"冒烟：--once 对本项目生成 payload 可打印"）：
- 子进程 exit 0，恰好推送 1 次；
- 请求形状：POST /api/ingest、Authorization: Bearer、Content-Type: application/json、
  User-Agent: aibase-agent/0.1（与 agent_http 契约一致）；
- payload JSON 合法、project_id/ts/files 六字段齐备、tasks 非空（本仓库 runtime/tasks 有实例）；
- payload 可打印：紧凑 JSON 序列化后往返解析一致，且 mock 端打印输出（测试 stdout）。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def _find_repo_root(start):
    """向上寻找含 aios.config.yaml 的项目根（兼容 kit/ 子目录布局与平铺布局）。"""
    d = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, "aios.config.yaml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return d


REPO_ROOT = _find_repo_root(os.path.dirname(os.path.abspath(__file__)))
AGENT_SCRIPT = os.path.join(REPO_ROOT, "kit", "tools", "agent", "agent.py")

EXPECTED_FILES_KEYS = ("tasks", "focus", "heartbeats", "events",
                       "verification_count", "review_count")


class MockIngestServer:
    """线程化本地 mock ingest 服务器：记录请求并在 stdout 打印收到的 payload。"""

    def __init__(self):
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
    def ingest_url(self):
        return f"http://127.0.0.1:{self.httpd.server_port}/api/ingest"


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
        # 冒烟核心：payload 可打印（输出到测试日志，人类可读）
        print(f"[SMOKE] 收到 payload（{len(body.encode('utf-8'))} bytes）:\n{body}")
        resp = b'{"ok":true}'
        try:
            self.send_response(200)
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except OSError:
            pass  # 客户端断开后的写失败忽略

    def log_message(self, *args):
        pass


class AgentOnceSmokeTest(unittest.TestCase):
    def test_once_against_this_project_payload_printable(self):
        # 防环境 http_proxy 劫持本地 127.0.0.1 mock（与 test_agent_http 同约定）
        old_no_proxy = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        try:
            with tempfile.TemporaryDirectory() as tmp, MockIngestServer() as server:
                cfg_path = os.path.join(tmp, "agent.json")
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "server_url": server.ingest_url,
                        "token": "smoke-token",
                        "projects": [{"id": "smoke-aibase", "path": REPO_ROOT}],
                        "poll_interval_seconds": 1,
                    }, f, ensure_ascii=False)

                proc = subprocess.run(
                    [sys.executable, AGENT_SCRIPT, "--once", "--config", cfg_path],
                    cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
                    encoding="utf-8", errors="replace",
                )

                self.assertEqual(
                    proc.returncode, 0,
                    f"agent --once 失败（exit {proc.returncode}）:\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
                )
                self.assertEqual(len(server.requests), 1, "--once 应恰好推送 1 次")

                req = server.requests[0]
                self.assertEqual(req["method"], "POST")
                self.assertEqual(req["path"], "/api/ingest")
                self.assertEqual(req["headers"].get("Authorization"), "Bearer smoke-token")
                self.assertEqual(req["headers"].get("Content-Type"), "application/json")
                self.assertEqual(req["headers"].get("User-Agent"), "aibase-agent/0.1")

                payload = json.loads(req["body"])
                self.assertEqual(payload["project_id"], "smoke-aibase")
                self.assertIsInstance(payload["ts"], (int, float))
                files = payload["files"]
                for key in EXPECTED_FILES_KEYS:
                    self.assertIn(key, files, f"files 缺字段 {key}")

                # 本仓库 runtime/tasks 非空 → tasks 至少 1 条且带 name/content
                self.assertIsInstance(files["tasks"], list)
                self.assertGreater(len(files["tasks"]), 0)
                self.assertIn("name", files["tasks"][0])
                self.assertIn("content", files["tasks"][0])

                # payload 可打印：紧凑 JSON 序列化 → 往返解析一致
                dumped = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                self.assertEqual(json.loads(dumped), payload)
        finally:
            if old_no_proxy is None:
                os.environ.pop("no_proxy", None)
            else:
                os.environ["no_proxy"] = old_no_proxy


if __name__ == "__main__":
    unittest.main()
