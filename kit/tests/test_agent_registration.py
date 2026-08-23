"""TASK-044 — Agent 注册集成测试（真实 agent 模块 + 简化 aimonitor 服务端全链路）。

覆盖 INT-001 ~ INT-006：
- INT-001 注册全流程：unregistered → submit_register → pending（agent.json 写
  req_id/request_key）→ 管理员 approve → 轮询领 token → active → 自动推送 →
  GET /api/status 可见推送数据
- INT-002 拒绝流程：reject → 轮询到 rejected → 打印错误 → 退出（exit 1 / stderr 有内容）
- INT-003 吊销流程：approve → 开始推送 → revoke → 下次推送 401 → 重新轮询 status →
  revoked → 退出（agent_loop 新增行为）
- INT-004 冲突场景：active 409 → 提示已注册；pending 409 → 继续轮询旧 req_id
- INT-005 网络错误：服务端不可达 → 可退避分类；5xx → 退避重试 → 恢复继续；404 → 3 次退出
- INT-006 存量兼容：无 state 字段 / 有 token 无 state → 正常推送

测试服务端 TestAimonitorServer：内存 SQLite + ThreadingHTTPServer，对齐
MONITOR-SPEC §3.2.6 契约子集（register/status/approve/reject/revoke/ingest/status）。
文件落位 kit/tests/ 而非任务文本的 test/：verify 的 `unittest discover -s kit/tests`
只发现 test*.py，且 repo 约定 agent 测试均在 kit/tests/（见 TASK-044 计划）。

提交步骤说明：CLI --register 的 POST /api/register 提交胶水按 TASK-042 ISSUE-02/README
仍延迟；本测试用真实 agent 代码 agent_register.submit_register() 驱动提交，其余环节
（config/状态机/轮询/推送）全部走真实模块。
"""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "agent"
)
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_config  # noqa: E402
import agent_http  # noqa: E402
import agent_loop  # noqa: E402
import agent_register  # noqa: E402
from test_agent_loop import FakeClock, FakeSleeper  # noqa: E402

ADMIN_TOKEN = "test-admin-token"


class TestAimonitorServer:
    """简化版 aimonitor 服务端（内存 SQLite + ThreadingHTTPServer）。

    对齐 MONITOR-SPEC §3.2.6 契约子集：
    - POST /api/register → 201 {req_id, status: pending, pending_since}；409 {existing}
    - GET /api/register/<req_id>/status?request_key=... → pending/approved/rejected/
      expired/revoked；request_key 不匹配或不存在 → 404
    - POST /api/register/<req_id>/approve|reject|revoke（Authorization: Bearer <admin>）
    - POST /api/ingest → Bearer token 有效且未吊销 → 200；否则 401
    - GET /api/status → 聚合各项目推送数据
    """

    def __init__(self, admin_token=ADMIN_TOKEN):
        self.admin_token = admin_token
        self.requests = []          # 全部请求日志（method/path/status/body）
        self.ingest_records = []    # 成功 ingest 记录（project_id/ts/payload）
        self.fail_status_codes = []  # 故障注入：按序弹出的 status 响应 HTTP 码
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_db()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _AimonitorHandler)
        self.httpd.daemon_threads = True
        self.httpd.test_server = self
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(2)
        with self._lock:
            self._db.close()
        return False

    # ------------------------------------------------------------------ DB

    def _init_db(self):
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE registration (
                  req_id       TEXT PRIMARY KEY,
                  project_id   TEXT NOT NULL,
                  request_key  TEXT NOT NULL,
                  status       TEXT NOT NULL DEFAULT 'pending',
                  token        TEXT,
                  reason       TEXT,
                  created_at   REAL NOT NULL
                );
                CREATE TABLE ingest (
                  id         INTEGER PRIMARY KEY AUTOINCREMENT,
                  project_id TEXT NOT NULL,
                  ts         REAL NOT NULL,
                  payload    TEXT NOT NULL
                );
                """
            )

    def _row(self, req_id):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM registration WHERE req_id=?", (req_id,)
            ).fetchone()
        return dict(row) if row else None

    def _find_by_project(self, project_id):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM registration WHERE project_id=?", (project_id,)
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------- 便捷方法

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def ingest_url(self):
        return self.base_url + "/api/ingest"

    def register_url(self):
        return self.base_url + "/api/register"

    def status_url(self, req_id):
        return f"{self.base_url}/api/register/{req_id}/status"

    def _http(self, method, url, body=None, headers=None):
        """真实 HTTP 调用（测试进程→服务端），返回 (status, parsed_json)。"""
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read(64 * 1024).decode("utf-8", errors="replace")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read(64 * 1024).decode("utf-8", errors="replace") if e.fp else ""
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return e.code, parsed

    def register_request(self, payload):
        """POST /api/register（等价 agent_register.submit_register 的服务端视角）。"""
        return self._http("POST", self.register_url(), payload)

    def admin_headers(self, token=None):
        return {"Authorization": "Bearer " + (token or self.admin_token)}

    def approve(self, req_id):
        return self._http(
            "POST", f"{self.base_url}/api/register/{req_id}/approve",
            {}, headers=self.admin_headers())

    def reject(self, req_id, reason="模拟管理员拒绝"):
        return self._http(
            "POST", f"{self.base_url}/api/register/{req_id}/reject",
            {"reason": reason}, headers=self.admin_headers())

    def revoke(self, req_id):
        return self._http(
            "POST", f"{self.base_url}/api/register/{req_id}/revoke",
            {}, headers=self.admin_headers())

    def status(self, req_id, request_key):
        return self._http(
            "GET", self.status_url(req_id) + "?request_key=" + urllib.parse.quote(request_key))

    def api_status(self):
        return self._http("GET", self.base_url + "/api/status")

    def seed_active(self, project_id, token, req_id="seed-0001", request_key="seed-key-16bytes"):
        """预置已注册（approved）记录：存量 agent 兼容场景（INT-006）。"""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO registration"
                " (req_id, project_id, request_key, status, token, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (req_id, project_id, request_key, "approved", token, time.time()),
            )

    def set_fail_status(self, codes):
        """故障注入：后续 status 请求按序返回这些 HTTP 码。"""
        self.fail_status_codes = list(codes)

    def push_count(self, project_id=None):
        if project_id is None:
            return len(self.ingest_records)
        return sum(1 for r in self.ingest_records if r["project_id"] == project_id)


class _AimonitorHandler(BaseHTTPRequestHandler):
    """HTTP 路由：把请求转给 TestAimonitorServer 处理并回写 JSON。"""

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------------

    def _json(self, status, obj):
        # TASK-049：先记录请求日志、再写响应——
        # 否则客户端读完响应即继续，服务端线程的 _record 可能尚未执行
        # （ThreadingHTTPServer 每请求一线程，INT-005 flake：3 次轮询偶发只记到 2 条）。
        self._record(status)
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _record(self, status):
        # 幂等：_json 已自动记录；调用点保留的显式 _record 不再重复记
        if getattr(self, "_recorded", False):
            return
        self._recorded = True
        srv = self.server.test_server
        srv.requests.append({
            "method": self.command,
            "path": self.path,
            "status": status,
        })

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        srv = self.server.test_server
        if self.path == "/api/status":
            with srv._lock:
                rows = srv._db.execute(
                    "SELECT project_id, COUNT(*) c, MAX(ts) last_ts"
                    " FROM ingest GROUP BY project_id"
                ).fetchall()
            self._json(200, {"projects": [{
                "id": r["project_id"], "ingest_count": r["c"], "last_ts": r["last_ts"],
            } for r in rows]})
            self._record(200)
            return

        # /api/register/<req_id>[/status]?request_key=...
        # 注意跨端契约差异：§3.2.10（agent 实现 derive_status_url）请求
        # /api/register/<req_id>；§3.2.6（aimonitor 服务端）为 /api/register/<req_id>/status。
        # 测试服务端两种路径都接受，保证真实 agent 请求可用。
        parts = self.path.split("?")
        path = parts[0]
        query = parts[1] if len(parts) > 1 else ""
        prefix = "/api/register/"
        if path.startswith(prefix):
            rest = path[len(prefix):]
            req_id = rest[:-len("/status")] if rest.endswith("/status") else rest
            params = urllib.parse.parse_qs(query)
            request_key = (params.get("request_key") or [None])[0]

            if srv.fail_status_codes:
                code = srv.fail_status_codes.pop(0)
                self._json(code, {"error": f"注入故障 HTTP {code}"})
                self._record(code)
                return

            if not request_key:
                self._json(404, {"error": "not found"})
                self._record(404)
                return
            row = srv._row(req_id)
            if row is None or row["request_key"] != request_key:
                self._json(404, {"error": "not found"})
                self._record(404)
                return
            if row["status"] == "pending":
                self._json(200, {"status": "pending", "pending_since": row["created_at"]})
            elif row["status"] == "approved":
                self._json(200, {
                    "status": "approved", "token": row["token"], "project_id": row["project_id"],
                })
            elif row["status"] == "rejected":
                self._json(200, {"status": "rejected", "reason": row["reason"]})
            elif row["status"] == "expired":
                self._json(200, {"status": "expired"})
            elif row["status"] == "revoked":
                self._json(200, {"status": "revoked"})
            else:
                self._json(500, {"error": f"未知状态 {row['status']}"})
            self._record(200)
            return

        self._json(404, {"error": "not found"})
        self._record(404)

    # ------------------------------------------------------------------ POST

    def do_POST(self):
        srv = self.server.test_server
        path = self.path

        if path == "/api/register":
            obj = self._read_json()
            if obj is None:
                self._json(400, {"error": "请求体不是合法 JSON"})
                self._record(400)
                return
            project_id = obj.get("project_id")
            request_key = obj.get("request_key")
            if not project_id or not request_key:
                self._json(400, {"error": "project_id/request_key 必填"})
                self._record(400)
                return
            if len(request_key.encode("utf-8")) < 16:
                self._json(400, {"error": "request_key 长度不能少于 16 字节"})
                self._record(400)
                return
            existing = srv._find_by_project(project_id)
            if existing and existing["status"] in ("pending", "approved"):
                # 契约语义：已注册/活跃 → existing=active；待审批 → existing=pending
                existing_tag = "active" if existing["status"] == "approved" else "pending"
                self._json(409, {
                    "error": "project_id 已存在",
                    "existing": existing_tag,
                })
                self._record(409)
                return
            if existing:
                # rejected/expired/revoked → 允许重新注册，替换旧记录
                with srv._lock:
                    srv._db.execute("DELETE FROM registration WHERE project_id=?", (project_id,))
            req_id = "req-" + os.urandom(8).hex()
            with srv._lock:
                srv._db.execute(
                    "INSERT INTO registration"
                    " (req_id, project_id, request_key, status, created_at)"
                    " VALUES (?,?,?,?,?)",
                    (req_id, project_id, request_key, "pending", time.time()),
                )
            self._json(201, {"req_id": req_id, "status": "pending", "pending_since": time.time()})
            self._record(201)
            return

        # 管理员端点
        for action in ("approve", "reject", "revoke"):
            prefix = f"/api/register/"
            suffix = f"/{action}"
            if path.startswith(prefix) and path.endswith(suffix):
                req_id = path[len(prefix):-len(suffix)]
                auth = self.headers.get("Authorization", "")
                if auth != "Bearer " + srv.admin_token:
                    self._json(401, {"error": "unauthorized"})
                    self._record(401)
                    return
                row = srv._row(req_id)
                if row is None:
                    self._json(404, {"error": "not found"})
                    self._record(404)
                    return
                if action == "approve":
                    if row["status"] != "pending":
                        self._json(409, {"error": f"状态 {row['status']} 不可审批"})
                        self._record(409)
                        return
                    token = f"aimon_{row['project_id']}_{req_id}_tok"
                    with srv._lock:
                        srv._db.execute(
                            "UPDATE registration SET status='approved', token=? WHERE req_id=?",
                            (token, req_id),
                        )
                    self._json(200, {
                        "status": "approved", "req_id": req_id, "project_id": row["project_id"],
                    })
                elif action == "reject":
                    if row["status"] != "pending":
                        self._json(409, {"error": f"状态 {row['status']} 不可拒绝"})
                        self._record(409)
                        return
                    obj = self._read_json() or {}
                    reason = obj.get("reason") or "管理员拒绝"
                    with srv._lock:
                        srv._db.execute(
                            "UPDATE registration SET status='rejected', reason=? WHERE req_id=?",
                            (reason, req_id),
                        )
                    self._json(200, {"status": "rejected", "req_id": req_id})
                elif action == "revoke":
                    if row["status"] != "approved":
                        self._json(409, {"error": f"状态 {row['status']} 不可吊销"})
                        self._record(409)
                        return
                    with srv._lock:
                        srv._db.execute(
                            "UPDATE registration SET status='revoked', token=NULL WHERE req_id=?",
                            (req_id,),
                        )
                    self._json(200, {"status": "revoked"})
                self._record(200)
                return

        if path == "/api/ingest":
            auth = self.headers.get("Authorization", "")
            token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
            with srv._lock:
                row = srv._db.execute(
                    "SELECT * FROM registration WHERE token=? AND status='approved'",
                    (token,),
                ).fetchone()
            if row is None:
                self._json(401, {"error": "invalid token"})
                self._record(401)
                return
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}
            project_id = payload.get("project_id", row["project_id"])
            with srv._lock:
                srv._db.execute(
                    "INSERT INTO ingest (project_id, ts, payload) VALUES (?,?,?)",
                    (project_id, time.time(), raw),
                )
            srv.ingest_records.append({
                "project_id": project_id, "ts": time.time(), "payload": raw,
            })
            self._json(200, {"status": "ok"})
            self._record(200)
            return

        self._json(404, {"error": "not found"})
        self._record(404)


class AgentRegistrationIntegrationTests(unittest.TestCase):
    """INT-001 ~ INT-006 集成测试。"""

    AGENT = os.path.join(AGENT_DIR, "agent.py")

    def setUp(self):
        # 防止环境 http_proxy 劫持 127.0.0.1 本地测试服务端（同 test_agent_http）
        self._old_no_proxy = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def tearDown(self):
        if self._old_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._old_no_proxy

    # ------------------------------------------------------------ 工具方法

    def _quiet_log(self):
        return agent_loop.AgentLog(quiet=True, stream=io.StringIO(), err_stream=io.StringIO())

    def _write_config(self, tmp, server_url, state="unregistered", token=None,
                      project_id="proj-int", poll_interval=30, extra=None):
        cfg = {
            "server_url": server_url,
            "projects": [{"id": project_id, "path": tmp}],
            "poll_interval_seconds": poll_interval,
            "state": state,
        }
        if token is not None:
            cfg["token"] = token
        cfg.update(extra or {})
        path = os.path.join(tmp, "agent.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return path

    def _register_and_pending(self, tmp, server, project_id="proj-int"):
        """真实 agent 代码：构造注册请求 → 提交 → 写入 pending 到 agent.json。

        返回 (path, cfg_pending, req_id, request_key)。
        """
        server_url = server.ingest_url()
        path = self._write_config(tmp, server_url, state="unregistered")
        cfg = agent_config.load_config(path)
        request_key = agent_register.generate_request_key()
        payload = agent_register.build_register_payload(cfg["projects"][0], request_key)
        resp = agent_register.submit_register(server_url, payload)
        self.assertEqual(resp["status"], "pending")
        req_id = resp["req_id"]
        reg = agent_register.RegistrationState(cfg)
        reg.transition_to("pending", req_id=req_id, request_key=request_key)
        reg.save(path)
        cfg_pending = agent_config.load_config(path)
        return path, cfg_pending, req_id, request_key

    # ------------------------------------------------------------ INT-001

    def test_int001_full_registration_flow(self):
        """INT-001：unregistered → 提交 → pending → approve → 轮询领 token → 推送 → status 可见。"""
        with tempfile.TemporaryDirectory() as tmp, TestAimonitorServer() as server:
            path, cfg_pending, req_id, request_key = self._register_and_pending(tmp, server)

            # agent.json 已进入 pending 且写入 req_id/request_key
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["state"], "pending")
            self.assertEqual(saved["req_id"], req_id)
            self.assertEqual(saved["request_key"], request_key)
            self.assertNotIn("token", saved)

            # 模拟管理员 approve（调服务端 approve 端点）
            status, body = server.approve(req_id)
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "approved")

            # 轮询领 token → active → 自动开始推送
            clock = FakeClock(1000.0)
            sleeper = FakeSleeper(clock)
            log = self._quiet_log()
            result = agent_loop.run_registration_polling(
                cfg_pending, path, log=log, clock=clock, sleeper=sleeper,
                stop_fn=lambda: server.push_count() >= 1,
            )
            self.assertEqual(result, "approved")

            # agent.json：state=active，token 写入，req_id/request_key 清除
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["state"], "active")
            self.assertTrue(saved["token"].startswith("aimon_"))
            self.assertNotIn("req_id", saved)
            self.assertNotIn("request_key", saved)

            # 真实 agent_http 推送到达服务端 /api/ingest（Authorization 新 token）
            self.assertEqual(server.push_count(), 1)
            ingest_reqs = [r for r in server.requests
                           if r["method"] == "POST" and r["path"] == "/api/ingest"]
            self.assertEqual(len(ingest_reqs), 1)
            self.assertEqual(ingest_reqs[0]["status"], 200)

            # GET /api/status 能看到该项目的推送数据
            status, payload = server.api_status()
            self.assertEqual(status, 200)
            projects = payload["projects"]
            self.assertTrue(any(p["id"] == "proj-int" and p["ingest_count"] >= 1
                                for p in projects), projects)

    def test_int001_legacy_active_skips_registration(self):
        """INT-001：state=active（存量已注册）→ --register 直接提示已注册，不重复提交。"""
        with tempfile.TemporaryDirectory() as tmp, TestAimonitorServer() as server:
            server.seed_active("proj-int", "aimon_existing_token")
            path = self._write_config(
                tmp, server.ingest_url(), state="active", token="aimon_existing_token")
            proc = subprocess.run(
                [sys.executable, self.AGENT, "--register", "--config", path],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("已注册，无需重复注册", proc.stdout)
            # 未发起任何注册请求
            self.assertEqual(
                [r for r in server.requests if r["path"] == "/api/register"], [])

    # ------------------------------------------------------------ INT-002

    def test_int002_reject_flow(self):
        """INT-002：reject → 轮询到 rejected → 打印错误 → 返回 rejected（CLI 退出非 0）。"""
        with tempfile.TemporaryDirectory() as tmp, TestAimonitorServer() as server:
            path, cfg_pending, req_id, request_key = self._register_and_pending(tmp, server)

            # 模拟管理员 reject（调服务端 reject 端点）
            status, body = server.reject(req_id)
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "rejected")

            clock = FakeClock(1000.0)
            sleeper = FakeSleeper(clock)
            log = self._quiet_log()
            result = agent_loop.run_registration_polling(
                cfg_pending, path, log=log, clock=clock, sleeper=sleeper)
            self.assertEqual(result, "rejected")
            # 错误输出到 stderr
            self.assertIn("拒绝", log.err_stream.getvalue())

            # CLI 层面：exit code 非 0 且 stderr 有内容（真实 CLI 子进程）
            proc = subprocess.run(
                [sys.executable, self.AGENT, "--register", "--config", path],
                capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace")
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertTrue(proc.stderr.strip())

    # ------------------------------------------------------------ INT-003

    def test_int003_revoke_flow(self):
        """INT-003：approve → 开始推送 → revoke → 下次推送 401 → 重查 status → revoked → 退出。"""
        with tempfile.TemporaryDirectory() as tmp, TestAimonitorServer() as server:
            path, cfg_pending, req_id, request_key = self._register_and_pending(tmp, server)

            status, body = server.approve(req_id)
            self.assertEqual(status, 200)

            clock = FakeClock(1000.0)
            log = self._quiet_log()

            # 推送循环：第一次推送成功后（ingest 1 条），revoke；
            # 下一次推送收到 401 → 重查注册状态 → revoked → 退出
            revoked_done = [False]

            def sleeper(seconds):
                clock.advance(seconds)
                if server.push_count() >= 1 and not revoked_done[0]:
                    revoked_done[0] = True
                    server.revoke(req_id)

            result = agent_loop.run_registration_polling(
                cfg_pending, path, log=log, clock=clock, sleeper=sleeper,
                stop_fn=lambda: False,
            )
            self.assertEqual(result, "revoked")

            # 至少一次成功推送；revoke 后推送被拒（401）
            self.assertGreaterEqual(server.push_count(), 1)
            ingest_reqs = [r for r in server.requests
                           if r["method"] == "POST" and r["path"] == "/api/ingest"]
            self.assertGreaterEqual(len(ingest_reqs), 2)
            self.assertTrue(any(r["status"] == 401 for r in ingest_reqs), ingest_reqs)
            self.assertIn("revoked", log.err_stream.getvalue())

    # ------------------------------------------------------------ INT-004

    def test_int004_conflict_already_registered(self):
        """INT-004：已注册（active）的 project_id 再次注册 → 409 → 提示已注册。"""
        with tempfile.TemporaryDirectory() as tmp, TestAimonitorServer() as server:
            path, cfg_pending, req_id, request_key = self._register_and_pending(tmp, server)
            server.approve(req_id)  # 首次注册完成 → active

            # 同 project_id 再次注册 → 409 existing=active
            payload = agent_register.build_register_payload(
                {"id": "proj-int", "path": tmp}, agent_register.generate_request_key())
            with self.assertRaises(agent_register.RegisterConflictError) as cm:
                agent_register.submit_register(server.ingest_url(), payload)
            self.assertEqual(cm.exception.existing, "active")
            self.assertIn("已注册", str(cm.exception))

    def test_int004_conflict_pending_keeps_polling_old_req(self):
        """INT-004：同 project_id 的 pending 申请再次注册 → 409 → 继续轮询旧 req_id。"""
        with tempfile.TemporaryDirectory() as tmp, TestAimonitorServer() as server:
            path, cfg_pending, req_id, request_key = self._register_and_pending(tmp, server)

            # 同 project_id 再次提交 → 409 existing=pending
            payload = agent_register.build_register_payload(
                {"id": "proj-int", "path": tmp}, agent_register.generate_request_key())
            with self.assertRaises(agent_register.RegisterConflictError) as cm:
                agent_register.submit_register(server.ingest_url(), payload)
            self.assertEqual(cm.exception.existing, "pending")

            # agent 继续轮询旧 req_id → approve 旧申请 → 正常领 token 推送
            server.approve(req_id)
            clock = FakeClock(1000.0)
            sleeper = FakeSleeper(clock)
            log = self._quiet_log()
            result = agent_loop.run_registration_polling(
                cfg_pending, path, log=log, clock=clock, sleeper=sleeper,
                stop_fn=lambda: server.push_count() >= 1,
            )
            self.assertEqual(result, "approved")
            # 轮询目标始终是旧 req_id
            status_reqs = [r for r in server.requests
                           if r["method"] == "GET" and req_id in r["path"]]
            self.assertGreaterEqual(len(status_reqs), 1)

    # ------------------------------------------------------------ INT-005

    def test_int005_server_unreachable_retryable(self):
        """INT-005：服务端不可达 → PollRetryableError（可退避分类，恢复后可继续）。"""
        poller = agent_register.RegistrationPoller()
        with self.assertRaises(agent_register.PollRetryableError):
            poller.poll("http://127.0.0.1:1/api/ingest", "req-x", "key-x-16bytes")

    def test_int005_5xx_backoff_then_recover(self):
        """INT-005：服务端 5xx → 退避重试 → 恢复后继续（真实 HTTP + FakeClock）。"""
        with tempfile.TemporaryDirectory() as tmp, TestAimonitorServer() as server:
            path, cfg_pending, req_id, request_key = self._register_and_pending(tmp, server)
            # 先审批：前 2 次 status 返回 500（可退避），第 3 次正常返回 approved → 领 token
            status, body = server.approve(req_id)
            self.assertEqual(status, 200)
            server.set_fail_status([500, 500])

            clock = FakeClock(1000.0)
            sleeper = FakeSleeper(clock)
            log = self._quiet_log()
            result = agent_loop.run_registration_polling(
                cfg_pending, path, log=log, clock=clock, sleeper=sleeper,
                stop_fn=lambda: server.push_count() >= 1,
            )
            # 2 次 5xx 退避后恢复：第 3 次 poll 拿到 approved → 推送成功
            self.assertEqual(result, "approved")
            status_reqs = [r for r in server.requests
                           if r["method"] == "GET" and "/api/register/" in r["path"]]
            self.assertGreaterEqual(len(status_reqs), 3)
            self.assertEqual(status_reqs[0]["status"], 500)
            self.assertEqual(status_reqs[1]["status"], 500)
            self.assertIn("HTTP 500", log.err_stream.getvalue())

    def test_int005_404_three_times_exit(self):
        """INT-005：404（req_id 未同步）→ 退避重试，3 次后退出。"""
        with tempfile.TemporaryDirectory() as tmp, TestAimonitorServer() as server:
            server_url = server.ingest_url()
            path = self._write_config(
                tmp, server_url, state="pending",
                extra={"req_id": "req-not-synced", "request_key": "key-x-16bytes"})
            cfg_pending = agent_config.load_config(path)

            clock = FakeClock(1000.0)
            sleeper = FakeSleeper(clock)
            log = self._quiet_log()
            result = agent_loop.run_registration_polling(
                cfg_pending, path, log=log, clock=clock, sleeper=sleeper)
            self.assertEqual(result, "error")
            # 恰好 3 次轮询后退出（agent 请求路径无 /status 后缀）
            status_reqs = [r for r in server.requests
                           if r["method"] == "GET" and "/api/register/" in r["path"]]
            self.assertEqual(len(status_reqs), 3)
            self.assertIn("404", log.err_stream.getvalue())

    # ------------------------------------------------------------ INT-006

    def test_int006_legacy_no_state_field(self):
        """INT-006：存量 agent.json（无 state 字段）→ 正常推送（state 缺省 active）。"""
        with tempfile.TemporaryDirectory() as tmp, TestAimonitorServer() as server:
            server.seed_active("proj-legacy", "aimon_legacy_token")
            cfg = {
                "server_url": server.ingest_url(),
                "token": "aimon_legacy_token",
                "projects": [{"id": "proj-legacy", "path": tmp}],
                "poll_interval_seconds": 30,
            }
            path = os.path.join(tmp, "agent.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)

            loaded = agent_config.load_config(path)
            self.assertEqual(loaded["state"], "active")  # 缺省 active

            clock = FakeClock(1000.0)
            log = self._quiet_log()
            pushed, skipped, failed = agent_loop.poll_once(
                loaded, states={}, log=log, clock=clock)
            self.assertEqual((pushed, skipped, failed), (1, 0, 0))
            self.assertEqual(server.push_count("proj-legacy"), 1)

    def test_int006_legacy_token_no_state(self):
        """INT-006：存量 agent.json（有 token 无 state）→ 正常推送。"""
        with tempfile.TemporaryDirectory() as tmp, TestAimonitorServer() as server:
            server.seed_active("proj-legacy2", "aimon_legacy_token2")
            cfg = {
                "server_url": server.ingest_url(),
                "token": "aimon_legacy_token2",
                "projects": [{"id": "proj-legacy2", "path": tmp}],
            }
            path = os.path.join(tmp, "agent.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f)

            loaded = agent_config.load_config(path)
            self.assertEqual(loaded["state"], "active")

            clock = FakeClock(1000.0)
            log = self._quiet_log()
            pushed, skipped, failed = agent_loop.poll_once(
                loaded, states={}, log=log, clock=clock)
            self.assertEqual((pushed, skipped, failed), (1, 0, 0))
            self.assertEqual(server.push_count("proj-legacy2"), 1)


if __name__ == "__main__":
    unittest.main()
