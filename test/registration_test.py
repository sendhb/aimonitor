#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
registration_test.py — 服务端注册流程测试（TASK-060）

覆盖 TASK-050~TASK-054 所有端点的单元测试和组织测试：
- TEST-001：注册端点（TASK-050）
- TEST-002：状态轮询端点（TASK-051）
- TEST-003：审批端点（TASK-052）
- TEST-004：吊销/轮换端点（TASK-053）
- TEST-005：全流程集成
- TEST-006：边界情况
- TEST-007：注册码管理端点全链路（TASK-057，REVIEW F8；范围扩展见 TASK 文件）
- TEST-008：故障注入与 admin 配置（5xx 分支 + 401 fail-closed；REVIEW MED-002）

覆盖率（VERIFY-001 / REVIEW MED-001）：
- 默认运行只执行测试；`--coverage` 额外用 stdlib `trace` 测量
  注册子系统（TASK-050~057 功能面）行覆盖率并断言 ≥ 85%，
  同时输出全模块覆盖率供透明参考。零第三方依赖。

用法:
  python3 test/registration_test.py
  python3 test/registration_test.py --coverage
"""
import contextlib
import http.client
import io
import json
import os
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))


# ===================================================================
# 覆盖率测量（VERIFY-001 / REVIEW MED-001）— stdlib trace，零第三方依赖
# ===================================================================
# 目标文件：server/monitor_server.py（被测服务端唯一模块）。
# 测量口径：code-object 行号表（co_lines，与 coverage.py 同基准）上的可执行行；
# 执行集合来自 sys.settrace + threading.settrace（HTTP handler 线程/轮询线程同样计入）。
# 目标范围：注册子系统（TASK-050~057 功能面）——
#   RegistrationStore / EnrollmentCodeStore / agents.json I/O / TokenIssuer（1519-2048）
#   load_agents_config / admin 鉴权辅助（2425-2642）
#   ApiHandler._read_body + _json/_json_error + 全部注册端点（3221-3243、3345-3945）
#   不含 do_GET/do_POST 分派、_ingest（TASK-034~036 自有任务/测试覆盖）、
#   HistoryStore/IngestStore/轮询/解析器/静态资源（各自任务覆盖）。
# 全模块覆盖率同时输出，仅作透明参考，不作验收门槛。

COVERAGE_TARGET_FILE = os.path.join(ROOT, "server", "monitor_server.py")
# 注册子系统功能面的行区间（依据当前 monitor_server.py 函数边界；
# TASK-071 返工 r3（FIND-002/003：upsert/claim_or_update 保留 task_cursor、
# store_task_events/validate 超批 fail loud）在 IngestStore/validate 段新增代码使
# 行号再次下移——改动 monitor_server.py 结构时须同步刷新本区间，否则覆盖率断言失真。
# TASK-072：load_notify_config/NotificationSender/State 通知钩子在 derive_project_alerts
# 后新增 ~170 行，注册子系统整体下移——已按当前函数边界刷新）
# TASK-073：validate sessions 分支 + parse_session_line + IngestStore session 三方法
# （~260 行）+ _sessions/_ingest 接线（~100 行）使注册子系统再下移——已按当前函数边界刷新
COVERAGE_SCOPE_RANGES = [
    (1519, 2048),  # RegistrationStore / EnrollmentCodeStore / agents.json I/O / TokenIssuer（不含 State；TASK-073 后下移刷新）
    (2425, 2642),  # load_agents_config / ensure_admin_config / load_admin_config / load_projects_config / register_project_in_config / 鉴权辅助（TASK-069；TASK-073 后刷新，止于 is_project_registered）
    (3221, 3243),  # ApiHandler._read_body（注册端点读体依赖）
    (3345, 3945),  # _json / _json_error / _register ~ _read_body_json（全部注册端点 + TASK-069 自动登记）
]
COVERAGE_THRESHOLD = 85.0  # VERIFY-001：注册子系统行覆盖率 ≥ 85%


def _code_executable_lines(path):
    """code-object 行号表上的可执行行集合（co_lines，Python 3.10+）。

    与 coverage.py 同基准：只统计字节码可归属的行（排除注释/空行/纯文档串/
    def 头），是“行覆盖率”分母的公平口径。
    """
    import types

    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    code = compile(src, path, "exec")
    lines = set()

    def walk(c):
        for _s, _e, lineno in c.co_lines():
            if lineno is not None:
                lines.add(lineno)
        for const in c.co_consts:
            if isinstance(const, types.CodeType):
                walk(const)

    walk(code)
    return lines


def _run_with_coverage():
    """--coverage 模式：stdlib trace 实测注册子系统行覆盖率并断言 ≥ 85%。

    用 sys.settrace + threading.settrace 覆盖所有线程（HTTP handler 线程与
    后台轮询线程同样计入执行集合）。结束后输出分区间/合计/全模块覆盖率，
    并把报告写入 coverage/TASK-060-coverage.txt（gitignored，证据留存）。
    """
    executed = set()

    def tracer(frame, event, _arg):
        if event == "line" and frame.f_code.co_filename == COVERAGE_TARGET_FILE:
            executed.add(frame.f_lineno)
        return tracer

    sys.settrace(tracer)
    threading.settrace(tracer)
    try:
        run_all_tests()
    finally:
        sys.settrace(None)
        threading.settrace(None)

    all_lines = _code_executable_lines(COVERAGE_TARGET_FILE)
    scoped = set()
    for lo, hi in COVERAGE_SCOPE_RANGES:
        scoped.update(l for l in all_lines if lo <= l <= hi)
    hit_scoped = len(scoped & executed)
    hit_all = len(all_lines & executed)
    pct_scoped = hit_scoped / len(scoped) * 100 if scoped else 100.0
    pct_all = hit_all / len(all_lines) * 100 if all_lines else 100.0

    lines = []
    lines.append("===== COVERAGE (stdlib trace) — 注册子系统（VERIFY-001） =====")
    for lo, hi in COVERAGE_SCOPE_RANGES:
        sl = {l for l in all_lines if lo <= l <= hi}
        hl = len(sl & executed)
        lines.append(f"  L{lo}-{hi}: {hl:4d}/{len(sl):<4d} {hl / len(sl) * 100 if sl else 100:6.2f}%")
    lines.append(f"  注册子系统合计: {hit_scoped:4d}/{len(scoped):<4d} {pct_scoped:6.2f}%")
    lines.append(f"  全模块（参考）: {hit_all:4d}/{len(all_lines):<4d} {pct_all:6.2f}%")
    report = "\n".join(lines)
    print(report)
    print(f"\n{'✓' if pct_scoped >= COVERAGE_THRESHOLD else '✗'} 注册子系统行覆盖率 "
          f"{pct_scoped:.2f}% （阈值 {COVERAGE_THRESHOLD}%）")

    # 证据留存（gitignored：.gitignore 含 coverage/）
    cov_dir = os.path.join(ROOT, "coverage")
    os.makedirs(cov_dir, exist_ok=True)
    with open(os.path.join(cov_dir, "TASK-060-coverage.txt"), "w", encoding="utf-8") as fh:
        fh.write(report + "\n")

    if pct_scoped < COVERAGE_THRESHOLD:
        raise SystemExit(
            f"✗ 注册子系统行覆盖率 {pct_scoped:.2f}% < {COVERAGE_THRESHOLD}%（VERIFY-001）")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write_agents_file(tmpdir, agents):
    """写临时 config/agents.json（权限 600）→ 返回路径。"""
    agents_path = os.path.join(tmpdir, "agents.json")
    with open(agents_path, "w", encoding="utf-8") as f:
        json.dump(agents, f)
    try:
        os.chmod(agents_path, 0o600)
    except OSError:
        pass
    return agents_path


# ===================================================================
# TEST-001 — 注册端点（TASK-050）
# ===================================================================

def test_001_register_endpoint():
    """TEST-001：注册端点全覆盖。

    - 正常注册 → 201 + req_id
    - 缺少必填字段 → 400
    - project_id 非法字符 → 400
    - request_key 太短 → 400
    - project_id 已存在（projects.json）→ 409
    - project_id 有活跃 pending → 409
    - 有效 enrollment_code → 注册成功，码被消费
    - 无效 enrollment_code → 400
    - 限流超限 → 429
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-001-")
    try:
        reg_db = os.path.join(tmpdir, "registration.db")
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       registration_db_path=reg_db,
                                       projects_path=os.path.join(tmpdir, "projects.json"))
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def post(body):
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode("utf-8")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/register", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, raw

        valid = {
            "project_id": "reg-test-proj",
            "path": "/home/user/code/my-project",
            "host_info": "hostname:dev-box, ip:192.168.1.5",
            "request_key": "a" * 16,
        }

        # 1. 正常注册 → 201 + req_id
        status, raw = post(dict(valid, project_id="normal-proj"))
        assert status == 201, f"正常注册应 201：{status} {raw[:100]}"
        data = json.loads(raw)
        assert "req_id" in data, f"响应应含 req_id：{data}"
        assert data["status"] == "pending"
        assert "pending_since" in data
        print("✓ 正常注册 → 201 + req_id")

        # 2. 缺少必填字段 → 400
        for field in ("project_id", "path", "host_info", "request_key"):
            bad = dict(valid)
            del bad[field]
            status, raw = post(bad)
            assert status == 400, f"缺 {field} 应 400：{status} {raw[:100]}"
            assert json.loads(raw).get("error"), "400 响应应含 error 字段"
        print("✓ 缺少必填字段 → 400")

        # 3. project_id 非法字符 → 400
        for bad_id in ("project@123", "project id", "project/id", "project.id"):
            status, raw = post(dict(valid, project_id=bad_id))
            assert status == 400, f"project_id={bad_id!r} 应 400：{status}"
        print("✓ project_id 非法字符 → 400")

        # 4. request_key 太短 → 400
        for short_key in ("", "a" * 15, "a" * 8):
            status, raw = post(dict(valid, request_key=short_key))
            assert status == 400, f"request_key={short_key!r} 应 400：{status}"
        print("✓ request_key 太短 → 400")

        # 5. project_id 已存在（projects.json）→ 409
        status, raw = post(dict(valid, project_id="aimonitor"))
        assert status == 409, f"已注册 project_id 应 409：{status} {raw[:100]}"
        data = json.loads(raw)
        assert data.get("existing") == "active", f"existing 应为 active：{data}"
        print("✓ project_id 已存在（projects.json）→ 409")

        # 6. project_id 有活跃 pending → 409
        status, raw = post(dict(valid, project_id="dup-pending"))
        assert status == 201, f"首次注册应 201：{status}"
        status, raw = post(dict(valid, project_id="dup-pending"))
        assert status == 409, f"重复 pending 应 409：{status} {raw[:100]}"
        data = json.loads(raw)
        assert data.get("existing") == "pending", f"existing 应为 pending：{data}"
        print("✓ project_id 有活跃 pending → 409")

        # 7. 有效 enrollment_code → 注册成功，码被消费
        code = ms.ApiHandler.state.enrollment.generate(
            description="test-001-code", max_uses=1,
            allowed_project_pattern="enroll-*")
        status, raw = post(dict(valid, project_id="enroll-001",
                                enrollment_code=code))
        assert status == 201, f"有效 enrollment_code 应 201：{status} {raw[:100]}"
        code_row = ms.ApiHandler.state.enrollment.get(code)
        assert code_row["use_count"] == 1, f"注册码应已被消费：{code_row}"
        print("✓ 有效 enrollment_code → 注册成功，码被消费")

        # 8. 无效 enrollment_code → 400
        status, raw = post(dict(valid, project_id="enroll-bad",
                                enrollment_code="INVALID-CODE"))
        assert status == 400, f"无效 enrollment_code 应 400：{status} {raw[:100]}"
        data = json.loads(raw)
        assert "invalid enrollment_code" in data.get("error", "")
        print("✓ 无效 enrollment_code → 400")

        # 9. 限流超限 → 429
        old_limiter = ms.ApiHandler.state.register_limiter
        ms.ApiHandler.state.register_limiter = ms.IngestRateLimiter(2, clock=time.time)
        try:
            for i in range(2):
                status, raw = post(dict(valid, project_id=f"rate-001-{i}"))
                assert status == 201, f"第 {i+1} 次应 201：{status}"
            status, raw = post(dict(valid, project_id="rate-001-3"))
            assert status == 429, f"超限应 429：{status} {raw[:100]}"
            data = json.loads(raw)
            assert "rate limit" in data.get("error", "").lower() or "频繁" in data.get("error", "")
            print("✓ 限流超限 → 429")
        finally:
            ms.ApiHandler.state.register_limiter = old_limiter

        # 10. payload 超限 → 413（VERIFY-001 2xx/4xx/5xx 分支）
        # 服务端在读到体之前即按 Content-Length 声明拒绝（快路径）并关闭连接，
        # 客户端若继续 sendall 大体会 BrokenPipe；改用原始 socket 只发请求头
        # （声明超限 Content-Length）即可触发 413。
        over_body = json.dumps(dict(valid, project_id="big-payload",
                                    host_info="x" * (ms.MAX_INGEST_PAYLOAD_BYTES + 1)))
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            req_head = (f"POST /api/register HTTP/1.1\r\n"
                        f"Host: 127.0.0.1:{port}\r\n"
                        f"Content-Type: application/json\r\n"
                        f"Content-Length: {len(over_body.encode('utf-8'))}\r\n"
                        f"Connection: close\r\n\r\n").encode("utf-8")
            s.sendall(req_head)
            resp_raw = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp_raw += chunk
        finally:
            s.close()
        status_line = resp_raw.split(b"\r\n", 1)[0]
        assert b"413" in status_line, f"payload 超限应 413：{resp_raw[:200]!r}"
        print("✓ payload 超限 → 413")

        # 11. 非法 JSON 请求体 → 400
        status, raw = post(b"{not-valid-json")
        assert status == 400, f"非法 JSON 应 400：{status} {raw[:100]}"
        print("✓ 非法 JSON → 400")

        # 12. JSON 非对象（数组）→ 400
        status, raw = post(b"[1,2,3]")
        assert status == 400, f"JSON 非对象应 400：{status} {raw[:100]}"
        print("✓ JSON 非对象 → 400")

        # 13. enrollment_code 类型非字符串 → 400
        status, raw = post(dict(valid, project_id="enroll-type", enrollment_code=12345))
        assert status == 400, f"enrollment_code 非字符串应 400：{status} {raw[:100]}"
        print("✓ enrollment_code 非字符串 → 400")

        # 14. project_id 在 ingest_state 有活跃记录 → 409 active
        ingest_proj = "ingest-active-proj"
        ms.ApiHandler.state.ingest.upsert(
            ingest_proj, {"tasks": []}, "some-agent")
        status, raw = post(dict(valid, project_id=ingest_proj))
        assert status == 409, f"ingest 活跃 project_id 应 409：{status} {raw[:100]}"
        data = json.loads(raw)
        assert data.get("existing") == "active", f"existing 应为 active：{data}"
        print("✓ project_id 在 ingest_state 活跃 → 409 active")

        print("✓ TEST-001 全部通过")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# TEST-002 — 状态轮询端点（TASK-051）
# ===================================================================

def test_002_status_endpoint():
    """TEST-002：状态轮询端点全覆盖。

    - 各状态响应正确
    - request_key 错误 → 404
    - token 单次交付：首次有 token，第二次无
    - req_id 不存在 → 404
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-002-")
    try:
        reg_db = os.path.join(tmpdir, "registration.db")
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       registration_db_path=reg_db,
                                       projects_path=os.path.join(tmpdir, "projects.json"))
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, raw

        reg = ms.ApiHandler.state.registration

        # 准备测试数据
        req_id_p = reg.create("st-proj-p", "cp", '{}', "key-p")
        req_id_a = reg.create("st-proj-a", "ca", '{}', "key-a")
        token_a = "tok-a-001"
        reg.approve(req_id_a, token_a)
        req_id_d = reg.create("st-proj-d", "cd", '{}', "key-d")
        reg.approve(req_id_d, "tok-d")
        reg.mark_token_delivered(req_id_d)
        req_id_r = reg.create("st-proj-r", "cr", '{}', "key-r")
        reg.reject(req_id_r, "不合规")
        req_id_e = reg.create("st-proj-e", "ce", '{}', "key-e")
        conn = sqlite3.connect(reg_db)
        conn.execute("UPDATE registration_request SET expire_at=? WHERE req_id=?",
                     (time.time() - 100, req_id_e))
        conn.commit()
        conn.close()
        reg.expire_stale()
        req_id_v = reg.create("st-proj-v", "cv", '{}', "key-v")
        reg.approve(req_id_v, "tok-v")
        reg.revoke(req_id_v)

        # 1. pending → { status: "pending", pending_since }
        status, raw = get(f"/api/register/{req_id_p}/status?request_key=key-p")
        assert status == 200, f"pending 应 200：{status}"
        data = json.loads(raw)
        assert data["status"] == "pending"
        assert "pending_since" in data
        print("✓ pending → { status: 'pending', pending_since }")

        # 2. approved（首次）→ { status, token, project_id }
        status, raw = get(f"/api/register/{req_id_a}/status?request_key=key-a")
        assert status == 200, f"approved 首次应 200：{status}"
        data = json.loads(raw)
        assert data["status"] == "approved"
        assert data["token"] == token_a
        assert data["project_id"] == "st-proj-a"
        print("✓ approved（首次）→ { status, token, project_id }")

        # 3. approved（已交付）→ { status: "approved" }（不含 token）
        status, raw = get(f"/api/register/{req_id_d}/status?request_key=key-d")
        assert status == 200, f"approved 已交付应 200：{status}"
        data = json.loads(raw)
        assert data == {"status": "approved"}
        assert "token" not in data
        print("✓ approved（已交付）→ { status: 'approved' }（无 token）")

        # 4. rejected → { status: "rejected", reason }
        status, raw = get(f"/api/register/{req_id_r}/status?request_key=key-r")
        assert status == 200, f"rejected 应 200：{status}"
        data = json.loads(raw)
        assert data["status"] == "rejected"
        assert "reason" in data
        print("✓ rejected → { status: 'rejected', reason }")

        # 5. expired → { status: "expired" }
        status, raw = get(f"/api/register/{req_id_e}/status?request_key=key-e")
        assert status == 200, f"expired 应 200：{status}"
        data = json.loads(raw)
        assert data == {"status": "expired"}
        print("✓ expired → { status: 'expired' }")

        # 6. revoked → { status: "revoked" }
        status, raw = get(f"/api/register/{req_id_v}/status?request_key=key-v")
        assert status == 200, f"revoked 应 200：{status}"
        data = json.loads(raw)
        assert data == {"status": "revoked"}
        print("✓ revoked → { status: 'revoked' }")

        # 7. request_key 错误 → 404
        status, raw = get(f"/api/register/{req_id_p}/status?request_key=wrong-key")
        assert status == 404, f"request_key 错误应 404：{status}"
        assert json.loads(raw) == {"error": "not found"}
        print("✓ request_key 错误 → 404")

        # 7b. 缺少 request_key 参数 → 404（不区分缺参与错参）
        status, raw = get(f"/api/register/{req_id_p}/status")
        assert status == 404, f"缺少 request_key 应 404：{status}"
        assert json.loads(raw) == {"error": "not found"}
        print("✓ 缺少 request_key 参数 → 404")

        # 8. token 单次交付：首次有 token，第二次无
        req_id_once = reg.create("st-once", "co", '{}', "key-once")
        reg.approve(req_id_once, "single-use-tok")
        status, raw = get(f"/api/register/{req_id_once}/status?request_key=key-once")
        assert status == 200
        data1 = json.loads(raw)
        assert data1["token"] == "single-use-tok", "首次应有 token"
        status, raw = get(f"/api/register/{req_id_once}/status?request_key=key-once")
        data2 = json.loads(raw)
        assert "token" not in data2, "第二次不应含 token"
        assert data2 == {"status": "approved"}
        print("✓ token 单次交付：首次有 token，第二次无")

        # 9. req_id 不存在 → 404
        status, raw = get("/api/register/no-such-req/status?request_key=k")
        assert status == 404, f"req_id 不存在应 404：{status}"
        assert json.loads(raw) == {"error": "not found"}
        print("✓ req_id 不存在 → 404")

        print("✓ TEST-002 全部通过")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# TEST-003 — 审批端点（TASK-052）
# ===================================================================

def test_003_approve_reject_endpoint():
    """TEST-003：审批端点全覆盖。

    - 正常 approve → 200 + token 签发
    - 正常 reject → 200
    - 已处理 → 409
    - admin 密码错误 → 401
    - 缺少 auth header → 401
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-003-")
    try:
        admin_path = os.path.join(tmpdir, "admin.json")
        admin_password = secrets.token_hex(16)
        with open(admin_path, "w", encoding="utf-8") as fh:
            json.dump({"admin_password": admin_password}, fh)
        try:
            os.chmod(admin_path, 0o600)
        except OSError:
            pass

        reg_db = os.path.join(tmpdir, "registration.db")
        agents_path = os.path.join(tmpdir, "agents.json")

        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))
        try:
            ms.ApiHandler.state = ms.State(config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                           agents_path=agents_path,
                                           registration_db_path=reg_db,
                                           projects_path=os.path.join(tmpdir, "projects.json"))
            ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

            port = free_port()
            httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)

            def post(path, auth=None, body=None):
                if body is None:
                    body = b"{}"
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                if auth is not None:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            def get(path, auth=None):
                headers = {}
                if auth is not None:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            auth = f"Bearer {admin_password}"
            reg = ms.ApiHandler.state.registration

            # 准备测试数据
            req_id_app = reg.create("app-proj", "ca", '{}', "key-app")
            req_id_rej = reg.create("rej-proj", "cr", '{}', "key-rej")

            # 1. 正常 approve → 200 + token 签发
            status, raw = post(f"/api/register/{req_id_app}/approve", auth=auth)
            assert status == 200, f"正常 approve 应 200：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data["status"] == "approved"
            assert data["req_id"] == req_id_app
            assert data["project_id"] == "app-proj"
            # agents.json 已写入
            assert os.path.isfile(agents_path)
            with open(agents_path, encoding="utf-8") as fh:
                ag = json.load(fh)
            assert "app-proj" in ag
            assert ag["app-proj"].startswith("aimon_app-proj_")
            # RegistrationStore 记录已更新
            row = reg.get(req_id_app)
            assert row["status"] == "approved"
            assert row["issued_token"] == ag["app-proj"]
            assert row["decided_at"] is not None
            print("✓ 正常 approve → 200 + token 签发")

            # 2. 正常 reject → 200
            status, raw = post(f"/api/register/{req_id_rej}/reject", auth=auth)
            assert status == 200, f"正常 reject 应 200：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data["status"] == "rejected"
            assert data["req_id"] == req_id_rej
            row = reg.get(req_id_rej)
            assert row["status"] == "rejected"
            assert row["decided_at"] is not None
            print("✓ 正常 reject → 200")

            # 2b. reject 携带原因 → 持久化并随列表返回（TASK-056 F2 / UI-003）
            req_id_rej2 = reg.create("rej-proj2", "cr2", '{}', "key-rej2")
            status, raw = post(f"/api/register/{req_id_rej2}/reject",
                              auth=auth, body={"reason": "机器信息不合规"})
            assert status == 200, f"带原因 reject 应 200：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data["status"] == "rejected"
            row = reg.get(req_id_rej2)
            assert row["reject_reason"] == "机器信息不合规", "reject_reason 应持久化"
            # 列表接口应返回原因（前端 UI-003 列表渲染依赖）
            status, raw = get("/api/register/list", auth=auth)
            assert status == 200, f"列表应 200：{status}"
            rows = json.loads(raw)
            hit = [r for r in rows if r.get("req_id") == req_id_rej2]
            assert hit and hit[0].get("reject_reason") == "机器信息不合规", \
                f"列表应包含 reject_reason：{raw[:200]}"
            print("✓ 带原因 reject → 持久化 + 列表返回")

            # 3. 已处理 → 409（approve 已处理）
            status, raw = post(f"/api/register/{req_id_app}/approve", auth=auth)
            assert status == 409, f"已处理 approve 应 409：{status}"
            assert json.loads(raw) == {"error": "already processed"}
            # reject 已处理
            status, raw = post(f"/api/register/{req_id_rej}/reject", auth=auth)
            assert status == 409, f"已处理 reject 应 409：{status}"
            assert json.loads(raw) == {"error": "already processed"}
            print("✓ 已处理 → 409")

            # 4. admin 密码错误 → 401
            status, raw = post(f"/api/register/{req_id_app}/approve",
                              auth="Bearer wrong-password-1234567890")
            assert status == 401, f"错密码应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}
            status, raw = post(f"/api/register/{req_id_rej}/reject",
                              auth="Bearer wrong-password-1234567890")
            assert status == 401, f"错密码 reject 应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}
            print("✓ admin 密码错误 → 401")

            # 5. 缺少 auth header → 401
            status, raw = post(f"/api/register/{req_id_app}/approve", auth=None)
            assert status == 401, f"缺 auth 应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}
            status, raw = post(f"/api/register/{req_id_rej}/reject", auth=None)
            assert status == 401, f"缺 auth reject 应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}
            print("✓ 缺少 auth header → 401")

            print("✓ TEST-003 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# TEST-004 — 吊销/轮换端点（TASK-053）
# ===================================================================

def test_004_revoke_renew_endpoint():
    """TEST-004：吊销/轮换端点全覆盖。

    - 正常 revoke → 200，token 从 agents.json 移除
    - 正常 renew → 200，旧 token 失效，新 token 可用
    - 非 approved 状态 revoke → 409
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-004-")
    try:
        config["projects"] = list(config["projects"]) + [
            {"id": "renew-proj", "name": "renew", "path": tmpdir},
        ]

        admin_path = os.path.join(tmpdir, "admin.json")
        admin_password = secrets.token_hex(16)
        with open(admin_path, "w", encoding="utf-8") as fh:
            json.dump({"admin_password": admin_password}, fh)
        try:
            os.chmod(admin_path, 0o600)
        except OSError:
            pass

        reg_db = os.path.join(tmpdir, "registration.db")
        agents_path = os.path.join(tmpdir, "agents.json")

        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))
        try:
            ms.ApiHandler.state = ms.State(config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                           agents_path=agents_path,
                                           registration_db_path=reg_db,
                                           projects_path=os.path.join(tmpdir, "projects.json"))
            ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

            port = free_port()
            httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)

            def post(path, auth=None, body=None):
                if body is None:
                    body = b"{}"
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                if auth is not None:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            auth = f"Bearer {admin_password}"
            reg = ms.ApiHandler.state.registration

            # 准备：创建并 approve 两个请求
            req_id_rv = reg.create("rev-proj", None, '{}', "key-rv")
            assert req_id_rv is not None
            req_id_rn = reg.create("renew-proj", None, '{}', "key-rn")
            assert req_id_rn is not None

            status, raw = post(f"/api/register/{req_id_rv}/approve", auth=auth)
            assert status == 200
            status, raw = post(f"/api/register/{req_id_rn}/approve", auth=auth)
            assert status == 200

            old_token_rv = reg.get(req_id_rv)["issued_token"]
            old_token_rn = reg.get(req_id_rn)["issued_token"]

            # 1. 正常 revoke → 200，token 从 agents.json 移除
            status, raw = post(f"/api/register/{req_id_rv}/revoke", auth=auth)
            assert status == 200, f"正常 revoke 应 200：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data == {"status": "revoked"}
            with open(agents_path, encoding="utf-8") as fh:
                ag = json.load(fh)
            assert "rev-proj" not in ag, "token 应从 agents.json 移除"
            row = reg.get(req_id_rv)
            assert row["status"] == "revoked"
            assert row["decided_at"] is not None
            print("✓ 正常 revoke → 200，token 从 agents.json 移除")

            # 2. 正常 renew → 200，旧 token 失效，新 token 可用
            status, raw = post(f"/api/register/{req_id_rn}/renew", auth=auth)
            assert status == 200, f"正常 renew 应 200：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data["status"] == "approved"
            # REVIEW F7：note 与 MONITOR-SPEC 权威契约对齐
            assert data["note"] == "新 token 已签发，agent 下次推送时收到 401 后自动轮询领取"
            with open(agents_path, encoding="utf-8") as fh:
                ag = json.load(fh)
            assert "renew-proj" in ag
            new_token = ag["renew-proj"]
            assert new_token != old_token_rn, "新 token 应与旧 token 不同"
            assert old_token_rn not in ag.values(), "旧 token 应从 agents.json 移除"
            row = reg.get(req_id_rn)
            assert row["issued_token"] == new_token
            assert row["token_delivered"] is False, "renew 后 token_delivered 应重置为 0"
            print("✓ 正常 renew → 200，旧 token 失效，新 token 可用")

            # 3. 非 approved 状态 revoke → 409（revoked 状态）
            status, raw = post(f"/api/register/{req_id_rv}/revoke", auth=auth)
            assert status == 409, f"非 approved revoke 应 409：{status}"
            assert json.loads(raw) == {"error": "already processed"}

            # 非 approved 状态 renew → 409（被 revoke 的请求）
            status, raw = post(f"/api/register/{req_id_rv}/renew", auth=auth)
            assert status == 409, f"非 approved renew 应 409：{status}"
            assert json.loads(raw) == {"error": "already processed"}
            print("✓ 非 approved 状态 revoke/renew → 409")

            # 4. 重复 renew → 409（renew_count 守卫，TASK-053 / REVIEW F1）
            req_id_rn2 = reg.create("renew-proj2", None, '{}', "key-rn2")
            assert req_id_rn2 is not None
            status, raw = post(f"/api/register/{req_id_rn2}/approve", auth=auth)
            assert status == 200, f"前置 approve 应 200：{status} {raw[:100]}"
            status, raw = post(f"/api/register/{req_id_rn2}/renew", auth=auth)
            assert status == 200, f"首次 renew 应 200：{status} {raw[:100]}"
            status, raw = post(f"/api/register/{req_id_rn2}/renew", auth=auth)
            assert status == 409, f"重复 renew 应 409：{status} {raw[:100]}"
            assert json.loads(raw) == {"error": "already processed"}
            print("✓ 重复 renew → 409（renew_count 守卫）")

            print("✓ TEST-004 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# TEST-005 — 全流程集成
# ===================================================================

def test_005_full_integration():
    """TEST-005：全流程集成。

    - 注册 → pending → approve → 轮询拿到 token → 用 token 推 ingest 成功
    - 注册 → pending → reject → 轮询到 rejected
    - 注册 → approve → revoke → 旧 token 推 ingest 失败（401）
    - 注册 → approve → renew → 旧 token 401，新 token 200
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-005-")
    try:
        admin_path = os.path.join(tmpdir, "admin.json")
        admin_password = "test-admin-pwd-32-char-len-xxxx"
        with open(admin_path, "w", encoding="utf-8") as fh:
            json.dump({"admin_password": admin_password}, fh)
        try:
            os.chmod(admin_path, 0o600)
        except OSError:
            pass

        reg_db = os.path.join(tmpdir, "registration.db")
        agents_path = os.path.join(tmpdir, "agents.json")

        # 注册测试项目到 config（用于 ingest 鉴权检查）
        # 注意：注册流程本身使用不在 config 中的 project_id，
        # 审批后才把 project_id 加入 config 以支持 ingest 推送
        config["projects"] = list(config["projects"])

        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))
        try:
            ms.ApiHandler.state = ms.State(config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                           agents_path=agents_path,
                                           registration_db_path=reg_db,
                                           projects_path=os.path.join(tmpdir, "projects.json"))
            ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

            port = free_port()
            httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)

            def post(path, auth=None, body=None):
                if body is None:
                    body = b"{}"
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                if auth is not None:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            def get(path):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", path)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            auth = f"Bearer {admin_password}"
            reg = ms.ApiHandler.state.registration

            # ===== 流程 1: 注册 → pending → approve → 轮询拿到 token → ingest 成功 =====
            # 使用不在 config 中的 project_id 进行注册
            valid = {
                "project_id": "flow-approve",
                "path": "/tmp/flow-approve",
                "host_info": "host:flow-approve",
                "request_key": "x" * 16,
            }
            status, raw = post("/api/register", body=valid)
            assert status == 201, f"注册应 201：{status} {raw[:100]}"
            req_id = json.loads(raw)["req_id"]

            status, raw = get(f"/api/register/{req_id}/status?request_key={'x' * 16}")
            assert status == 200
            assert json.loads(raw)["status"] == "pending"

            status, raw = post(f"/api/register/{req_id}/approve", auth=auth)
            assert status == 200

            status, raw = get(f"/api/register/{req_id}/status?request_key={'x' * 16}")
            assert status == 200
            data = json.loads(raw)
            assert data["status"] == "approved"
            token = data["token"]
            assert token.startswith("aimon_flow-approve_")

            # 将 project_id 加入 config 以便 ingest 鉴权通过
            config["projects"] = list(config["projects"]) + [
                {"id": "flow-approve", "name": "flow-approve", "path": tmpdir},
            ]

            # 用 token 推 ingest
            ingest_body = {
                "project_id": "flow-approve",
                "ts": int(time.time()),
                "files": {"tasks": [{"name": "TASK-001.md", "content": "# TASK\n"}]},
            }
            status, raw = post("/api/ingest", auth=f"Bearer {token}", body=ingest_body)
            assert status == 200, f"用 token 推 ingest 应 200：{status} {raw[:100]}"
            print("✓ 流程1: 注册→pending→approve→轮询拿 token→ingest 成功")

            # ===== 流程 2: 注册 → pending → reject → 轮询到 rejected =====
            status, raw = post("/api/register", body={
                "project_id": "flow-reject", "path": "/tmp/flow-reject",
                "host_info": "host:flow-reject", "request_key": "y" * 16,
            })
            assert status == 201
            req_id_rj = json.loads(raw)["req_id"]

            status, raw = post(f"/api/register/{req_id_rj}/reject", auth=auth)
            assert status == 200

            status, raw = get(f"/api/register/{req_id_rj}/status?request_key={'y' * 16}")
            assert status == 200
            data = json.loads(raw)
            assert data["status"] == "rejected"
            print("✓ 流程2: 注册→pending→reject→轮询到 rejected")

            # ===== 流程 3: 注册 → approve → revoke → 旧 token 推 ingest 失败（401） =====
            status, raw = post("/api/register", body={
                "project_id": "flow-revoke", "path": "/tmp/flow-revoke",
                "host_info": "host:flow-revoke", "request_key": "z" * 16,
            })
            assert status == 201
            req_id_rv = json.loads(raw)["req_id"]

            status, raw = post(f"/api/register/{req_id_rv}/approve", auth=auth)
            assert status == 200
            old_token = reg.get(req_id_rv)["issued_token"]

            # 加入 config 用于 ingest 鉴权
            config["projects"] = list(config["projects"]) + [
                {"id": "flow-revoke", "name": "flow-revoke", "path": tmpdir},
            ]

            status, raw = post(f"/api/register/{req_id_rv}/revoke", auth=auth)
            assert status == 200

            # 旧 token 推 ingest → 401
            status, raw = post("/api/ingest", auth=f"Bearer {old_token}", body={
                "project_id": "flow-revoke", "ts": int(time.time()),
                "files": {"tasks": [{"name": "TASK.md", "content": "# TASK\n"}]},
            })
            assert status == 401, f"revoke 后旧 token 推 ingest 应 401：{status} {raw[:100]}"
            print("✓ 流程3: 注册→approve→revoke→旧 token 推 ingest 失败（401）")

            # ===== 流程 4: 注册 → approve → renew → 旧 token 401，新 token 200 =====
            status, raw = post("/api/register", body={
                "project_id": "flow-renew", "path": "/tmp/flow-renew",
                "host_info": "host:flow-renew", "request_key": "w" * 16,
            })
            assert status == 201
            req_id_rn = json.loads(raw)["req_id"]

            status, raw = post(f"/api/register/{req_id_rn}/approve", auth=auth)
            assert status == 200
            old_token_rn = reg.get(req_id_rn)["issued_token"]

            # 加入 config 用于 ingest 鉴权
            config["projects"] = list(config["projects"]) + [
                {"id": "flow-renew", "name": "flow-renew", "path": tmpdir},
            ]

            status, raw = post(f"/api/register/{req_id_rn}/renew", auth=auth)
            assert status == 200
            with open(agents_path, encoding="utf-8") as fh:
                ag = json.load(fh)
            new_token = ag["flow-renew"]

            # 旧 token 401
            status, raw = post("/api/ingest", auth=f"Bearer {old_token_rn}", body={
                "project_id": "flow-renew", "ts": int(time.time()),
                "files": {"tasks": [{"name": "TASK.md", "content": "# TASK\n"}]},
            })
            assert status == 401, f"renew 后旧 token 推 ingest 应 401：{status} {raw[:100]}"

            # 新 token 200
            status, raw = post("/api/ingest", auth=f"Bearer {new_token}", body={
                "project_id": "flow-renew", "ts": int(time.time()),
                "files": {"tasks": [{"name": "TASK.md", "content": "# TASK\n"}]},
            })
            assert status == 200, f"renew 后新 token 推 ingest 应 200：{status} {raw[:100]}"
            print("✓ 流程4: 注册→approve→renew→旧 token 401，新 token 200")

            print("✓ TEST-005 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# TEST-006 — 边界情况
# ===================================================================

def test_006_edge_cases():
    """TEST-006：边界情况。

    - 空表查询
    - 大量注册请求（限流阈值验证）
    - 并发请求同 project_id（多线程 barrier 同时发出，恰一个 201 一个 409）
    - agents.json 文件不存在 → 自动创建
    - agents.json 文件损坏 → 读侧 fail-closed + 数据丢失风险标注（LOW-003）
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-006-")
    try:
        admin_path = os.path.join(tmpdir, "admin.json")
        admin_password = secrets.token_hex(16)
        with open(admin_path, "w", encoding="utf-8") as fh:
            json.dump({"admin_password": admin_password}, fh)
        try:
            os.chmod(admin_path, 0o600)
        except OSError:
            pass

        reg_db = os.path.join(tmpdir, "registration.db")
        agents_path = os.path.join(tmpdir, "agents.json")

        config["projects"] = list(config["projects"]) + [
            {"id": "edge-test-proj", "name": "edge", "path": tmpdir},
        ]

        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))
        try:
            ms.ApiHandler.state = ms.State(config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                           agents_path=agents_path,
                                           registration_db_path=reg_db,
                                           projects_path=os.path.join(tmpdir, "projects.json"))
            ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

            port = free_port()
            httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)

            def post(path, auth=None, body=None):
                if body is None:
                    body = b"{}"
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                if auth is not None:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            auth = f"Bearer {admin_password}"
            valid = {
                "project_id": "edge-test",
                "path": "/tmp/edge-test",
                "host_info": "host:edge",
                "request_key": "e" * 16,
            }

            # ===== 1. 空表查询 =====
            # RegistrationStore 无记录时 list_by_status 返回空列表
            rows = ms.ApiHandler.state.registration.list_by_status()
            # 有已有记录（aimonitor 等），但应可正常返回空筛选
            rows_revoked = ms.ApiHandler.state.registration.list_by_status("revoked")
            assert isinstance(rows_revoked, list)

            def get(path):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", path, headers={"Authorization": auth})
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            # GET /api/register/list?status=revoked → 200 + []（LOW-002：删除了被丢弃的 POST 死代码）
            status, raw = get("/api/register/list?status=revoked")
            assert status == 200
            data = json.loads(raw)
            assert isinstance(data, list), f"空表应返回 []：{data}"
            print("✓ 空表查询 → 200 + []")

            # ===== 2. 大量注册请求（限流阈值验证） =====
            # 限流器默认 60 次/分钟，快速发 3 次验证不超限
            for i in range(3):
                v = dict(valid, project_id=f"edge-bulk-{i}")
                status, raw = post("/api/register", body=v)
                assert status == 201, f"批量第 {i} 次应 201：{status}"
            print("✓ 大量注册请求（限流阈值验证：3 次全部成功）")

            # ===== 3. 并发请求同 project_id（恰一个 201，其余 409） =====
            # REVIEW LOW-001：真正的多线程并发（threading.Barrier 同步同时发出），
            # 而非顺序请求。ThreadingHTTPServer 每请求一线程；RegistrationStore.create
            # 在进程内锁内做 SELECT+INSERT 二次冲突检查（TASK-047），并发下恰一个
            # 创建成功（201），其余命中冲突（409）——store 锁把 TOCTOU 窗口收口；
            # 单进程 ThreadingHTTPServer 部署下成立（跨进程多 worker 不在本测试范围）。
            concurrent_proj = "edge-concurrent"
            results = []
            barrier = threading.Barrier(3)  # 2 请求线程 + 主线程同时放行

            def fire():
                barrier.wait()
                st, raw_body = post("/api/register", body=dict(valid, project_id=concurrent_proj))
                results.append((st, raw_body))

            threads = [threading.Thread(target=fire) for _ in range(2)]
            for th in threads:
                th.start()
            barrier.wait()  # 两个请求线程几乎同时发出
            for th in threads:
                th.join(timeout=10)

            statuses = sorted(st for st, _ in results)
            assert statuses == [201, 409], f"并发应恰一个 201 一个 409：{results}"
            status, raw = post("/api/register", body=dict(valid, project_id=concurrent_proj))
            assert status == 409, f"后续重复注册应 409：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data.get("existing") == "pending", f"existing 应为 pending：{data}"
            print("✓ 并发请求同 project_id（barrier 同时发出）→ 恰一个 201 一个 409")

            # 3b. DB 唯一约束兜底（确定性）：模拟“预检查未发现冲突”的竞态——
            # monkeypatch list_by_status → []（等价于两个请求同时越过 HTTP 层预检查），
            # 冲突由 RegistrationStore.create 的进程内锁二次检查兜底（第二个 → None → 409）。
            orig_list = ms.ApiHandler.state.registration.list_by_status
            ms.ApiHandler.state.registration.list_by_status = lambda status=None: []
            try:
                status, raw = post("/api/register", body=dict(valid, project_id="edge-race"))
                assert status == 201, f"首次应 201：{status} {raw[:100]}"
                status, raw = post("/api/register", body=dict(valid, project_id="edge-race"))
                assert status == 409, f"store 锁兜底应 409：{status} {raw[:100]}"
                assert json.loads(raw).get("existing") == "pending"
            finally:
                ms.ApiHandler.state.registration.list_by_status = orig_list
            print("✓ DB 唯一约束兜底（list_by_status 预检查失效 → store 锁二次检查 409）")

            # ===== 4. agents.json 文件不存在 → 自动创建 =====
            # 删除 agents.json 后 approve 应自动创建
            if os.path.isfile(agents_path):
                os.remove(agents_path)
            assert not os.path.isfile(agents_path), "agents.json 应已被删除"

            reg = ms.ApiHandler.state.registration
            req_id_no_agent = reg.create("edge-no-agent", None, '{}', "key-no-agent")
            assert req_id_no_agent is not None
            status, raw = post(f"/api/register/{req_id_no_agent}/approve", auth=auth)
            assert status == 200, f"agents.json 不存在时 approve 应 200：{status} {raw[:100]}"
            assert os.path.isfile(agents_path), "approve 后 agents.json 应被自动创建"
            with open(agents_path, encoding="utf-8") as fh:
                ag = json.load(fh)
            assert "edge-no-agent" in ag, "新 project 应写入 agents.json"
            try:
                mode = os.stat(agents_path).st_mode & 0o777
                assert mode == 0o600, f"agents.json 权限应为 600：{oct(mode)}"
            except OSError:
                pass
            print("✓ agents.json 文件不存在 → approve 自动创建")

            # ===== 5. agents.json 文件损坏 → 读侧 fail-closed（TASK-054 TOKEN-002） =====
            # 已知风险（REVIEW LOW-003）：TokenIssuer.issue 对损坏文件先 fail-closed 读空
            # （read_agents_config → {}，TASK-054 TOKEN-002 验收语义），随后
            # write_agents_config 整体覆盖文件——若损坏前文件中还有其他项目的有效 token，
            # 一次合法审批会静默丢弃它们（数据丢失风险）。本测试如实断言该既有语义并
            # 显式标注风险；修复方向（损坏时拒绝写入或合并保留）属生产行为变更，应另立
            # 任务（登记见 TASK-060 附注），不在本测试任务内改动 server 代码。
            with open(agents_path, "w", encoding="utf-8") as fh:
                fh.write("not-valid-json{{{")
            try:
                os.chmod(agents_path, 0o600)
            except OSError:
                pass

            # 读侧 fail-closed：损坏文件读空（TASK-054 TOKEN-002）
            assert ms.read_agents_config(agents_path) == {}, \
                "损坏 agents.json 应 fail-closed 读空 {}"
            # 鉴权侧 fail-closed：损坏文件 → load_agents_config {} → 任何 token 无法解析
            assert ms.load_agents_config(agents_path) == {}
            assert ms.resolve_agent_id(ms.load_agents_config(agents_path), "any-token") is None

            req_id_bad_agent = reg.create("edge-bad-agent", None, '{}', "key-bad-agent")
            assert req_id_bad_agent is not None
            status, raw = post(f"/api/register/{req_id_bad_agent}/approve", auth=auth)
            assert status == 200, f"agents.json 损坏时 approve 应 200：{status} {raw[:100]}"
            # 如实断言数据丢失风险：损坏文件被新文件整体覆盖，只剩新项目
            with open(agents_path, encoding="utf-8") as fh:
                ag = json.load(fh)
            assert set(ag.keys()) == {"edge-bad-agent"}, (
                f"损坏文件被覆盖后仅剩新项目（既有 token 丢失风险）：{sorted(ag.keys())}")
            print("✓ agents.json 损坏 → 读侧 fail-closed；approve 覆盖修复（数据丢失风险已标注）")

            # ===== 6. agents.json 权限过宽 → load_agents_config fail-closed =====
            # 创建权限 644 的文件
            wide_path = os.path.join(tmpdir, "agents-wide.json")
            with open(wide_path, "w", encoding="utf-8") as fh:
                json.dump({"test": "tok"}, fh)
            try:
                os.chmod(wide_path, 0o644)
            except OSError:
                pass
            result = ms.load_agents_config(wide_path)
            assert result == {}, f"权限过宽应 fail-closed 返回 {{}}：{result}"
            print("✓ agents.json 权限过宽 → load_agents_config fail-closed ({})")

            # ===== 7. 追加写入不覆盖已有 token（TOKEN-002 显式多项目断言） =====
            # 预置 2 个项目 → issue 新项目 → 断言 3 个 token 并存且旧 token 不变
            append_path = os.path.join(tmpdir, "agents-append.json")
            pre_tokens = {
                "pre-proj-a": "aimon_pre-proj-a_aaaa_aaaa",
                "pre-proj-b": "aimon_pre-proj-b_bbbb_bbbb",
            }
            with open(append_path, "w", encoding="utf-8") as fh:
                json.dump(pre_tokens, fh)
            try:
                os.chmod(append_path, 0o600)
            except OSError:
                pass

            issuer_append = ms.TokenIssuer(agents_path=append_path)
            res = issuer_append.issue("edge-append")
            assert res["project_id"] == "edge-append"
            assert res["scope"] == "agent"

            with open(append_path, encoding="utf-8") as fh:
                ag = json.load(fh)
            assert set(ag.keys()) == {"pre-proj-a", "pre-proj-b", "edge-append"}, (
                f"追加写入应保留 2 个旧 token 并新增 1 个：{sorted(ag.keys())}")
            assert ag["pre-proj-a"] == pre_tokens["pre-proj-a"], "旧 token A 不应被覆盖"
            assert ag["pre-proj-b"] == pre_tokens["pre-proj-b"], "旧 token B 不应被覆盖"
            assert ag["edge-append"] == res["token"], "新 token 应为刚签发的值"
            try:
                mode = os.stat(append_path).st_mode & 0o777
                assert mode == 0o600, f"追加后 agents.json 权限应为 600：{oct(mode)}"
            except OSError:
                pass
            print("✓ 追加写入不覆盖已有 token：2 旧 + 1 新 = 3 个 token 并存")

            print("✓ TEST-006 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# TEST-007 — 注册码管理端点全链路（TASK-057 / REVIEW F8）
# ===================================================================

def test_007_enrollment_codes_flow():
    """TEST-007：注册码管理端点（正确 admin 鉴权）HTTP 全链路。

    覆盖 REVIEW F8 建议的 generate→list→revoke 专项自动化用例，同时验证：
    - 正确 admin 密码下 generate → 200 + code
    - list → 200，新 code 出现在列表中且字段齐全
    - revoke → 200，列表 revoked=true
    - 参数校验：description 必填、max_uses<1 → 400；
      REVIEW F5：max_uses/expire_at 传 JSON 布尔 → 400
    - REVIEW F6：generate 响应 created_at 与 store 记录一致
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-007-")
    try:
        admin_path = os.path.join(tmpdir, "admin.json")
        admin_password = secrets.token_hex(16)
        with open(admin_path, "w", encoding="utf-8") as fh:
            json.dump({"admin_password": admin_password}, fh)
        try:
            os.chmod(admin_path, 0o600)
        except OSError:
            pass

        reg_db = os.path.join(tmpdir, "registration.db")
        agents_path = os.path.join(tmpdir, "agents.json")

        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))
        try:
            ms.ApiHandler.state = ms.State(config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                           agents_path=agents_path,
                                           registration_db_path=reg_db,
                                           projects_path=os.path.join(tmpdir, "projects.json"))
            ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

            port = free_port()
            httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)

            def post(path, auth=None, body=None):
                if body is None:
                    body = b"{}"
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                if auth is not None:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            def get(path, auth=None):
                headers = {}
                if auth is not None:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            auth = f"Bearer {admin_password}"
            bad_auth = "Bearer wrong-password-123"

            # 1. 参数校验：description 必填 → 400
            status, raw = post("/api/register/codes/generate", auth=auth,
                               body={"max_uses": 1})
            assert status == 400, f"description 缺失应 400：{status} {raw[:100]}"
            print("✓ generate 缺 description → 400")

            # 2. REVIEW F5：max_uses 传布尔 → 400
            status, raw = post("/api/register/codes/generate", auth=auth,
                               body={"description": "f5-bool", "max_uses": True})
            assert status == 400, f"max_uses 布尔应 400：{status} {raw[:100]}"
            print("✓ generate max_uses 布尔 → 400（REVIEW F5）")

            # 3. REVIEW F5：expire_at 传布尔 → 400
            status, raw = post("/api/register/codes/generate", auth=auth,
                               body={"description": "f5-bool-exp", "expire_at": True})
            assert status == 400, f"expire_at 布尔应 400：{status} {raw[:100]}"
            print("✓ generate expire_at 布尔 → 400（REVIEW F5）")

            # 3b. allowed_project 非字符串 → 400
            status, raw = post("/api/register/codes/generate", auth=auth,
                               body={"description": "f5-ap", "allowed_project": ["a"]})
            assert status == 400, f"allowed_project 非字符串应 400：{status} {raw[:100]}"
            print("✓ generate allowed_project 非字符串 → 400")

            # 3c. expire_at 非数字（字符串）→ 400
            status, raw = post("/api/register/codes/generate", auth=auth,
                               body={"description": "f5-exp-str", "expire_at": "tomorrow"})
            assert status == 400, f"expire_at 字符串应 400：{status} {raw[:100]}"
            print("✓ generate expire_at 字符串 → 400")

            # 4. max_uses < 1 → 400
            status, raw = post("/api/register/codes/generate", auth=auth,
                               body={"description": "f5-zero", "max_uses": 0})
            assert status == 400, f"max_uses=0 应 400：{status} {raw[:100]}"
            print("✓ generate max_uses=0 → 400")

            # 5. 正常 generate → 200 + code
            future = int(time.time()) + 86400
            status, raw = post("/api/register/codes/generate", auth=auth, body={
                "description": "TEST-007 code",
                "allowed_project": "proj-*",
                "max_uses": 3,
                "expire_at": future,
            })
            assert status == 200, f"正常 generate 应 200：{status} {raw[:200]}"
            data = json.loads(raw)
            code = data.get("code")
            assert code and "-" in code, f"code 缺失或格式异常：{data}"
            assert data["description"] == "TEST-007 code"
            assert data["allowed_project_pattern"] == "proj-*"
            assert data["max_uses"] == 3
            assert data["expire_at"] == future

            # REVIEW F6：created_at 与 store 记录一致
            row = ms.ApiHandler.state.enrollment.get(code)
            assert row is not None, "generate 后 store 应能查到记录"
            assert abs(data["created_at"] - row["created_at"]) < 1e-6, (
                f"created_at 应与 store 一致：{data['created_at']} vs {row['created_at']}"
            )
            print("✓ 正常 generate → 200 + code（created_at 与 store 一致，REVIEW F6）")

            # 6. list → 200，包含新 code
            status, raw = get("/api/register/codes", auth=auth)
            assert status == 200, f"list 应 200：{status} {raw[:200]}"
            codes = json.loads(raw)
            assert isinstance(codes, list), f"list 应为数组：{type(codes)}"
            rec = next((c for c in codes if c.get("code") == code), None)
            assert rec is not None, "list 应包含刚生成的 code"
            assert rec["description"] == "TEST-007 code"
            assert rec["allowed_project_pattern"] == "proj-*"
            assert rec["max_uses"] == 3
            assert rec["use_count"] == 0
            assert rec["revoked"] is False
            assert rec["expire_at"] == future
            print("✓ list → 200，新 code 字段齐全")

            # 7. 无鉴权 list → 401
            status, raw = get("/api/register/codes")
            assert status == 401, f"无 auth list 应 401：{status}"
            status, raw = get("/api/register/codes", auth=bad_auth)
            assert status == 401, f"错密码 list 应 401：{status}"
            print("✓ list 无鉴权/错密码 → 401")

            # 7b. generate/revoke 无鉴权/错密码 → 401
            status, raw = post("/api/register/codes/generate",
                               body={"description": "noauth"})
            assert status == 401, f"generate 无鉴权应 401：{status}"
            status, raw = post("/api/register/codes/generate", auth=bad_auth,
                               body={"description": "badauth"})
            assert status == 401, f"generate 错密码应 401：{status}"
            status, raw = post("/api/register/codes/ANY-CODE/revoke", auth=bad_auth)
            assert status == 401, f"revoke 错密码应 401：{status}"
            print("✓ generate/revoke 无鉴权/错密码 → 401")

            # 8. revoke → 200，列表更新为 revoked
            status, raw = post(f"/api/register/codes/{code}/revoke", auth=auth)
            assert status == 200, f"revoke 应 200：{status} {raw[:200]}"
            data = json.loads(raw)
            assert data.get("status") == "revoked"
            assert data.get("code") == code

            status, raw = get("/api/register/codes", auth=auth)
            assert status == 200
            codes = json.loads(raw)
            rec = next((c for c in codes if c.get("code") == code), None)
            assert rec is not None and rec["revoked"] is True, (
                f"revoke 后列表 revoked 应为 True：{rec}"
            )
            print("✓ revoke → 200，列表更新为 revoked")

            # 9. 重复 revoke（幂等）→ 200 不报错
            status, raw = post(f"/api/register/codes/{code}/revoke", auth=auth)
            assert status == 200, f"重复 revoke 应 200（幂等）：{status} {raw[:200]}"
            print("✓ 重复 revoke → 200（幂等）")

            print("✓ TEST-007 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# TEST-008 — 故障注入与 admin 配置（REVIEW MED-002 / NEW-001）
# ===================================================================

def test_008_fault_injection_and_admin_config():
    """TEST-008：故障注入与 admin 配置 fail-closed 覆盖（VERIFY-001 5xx 分支）。

    - MED-002：agents.json 写入故障 → approve/revoke/renew 500（文档化的 5xx 路径）
    - GET /api/register/list 无鉴权/错密码 → 401
    - admin.json 缺失/损坏/权限过宽/缺 admin_password 字段 → 审批 401（fail-closed）
    - load_admin_config / ensure_admin_config 辅助函数分支直测
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-008-")
    try:
        admin_path = os.path.join(tmpdir, "admin.json")
        admin_password = secrets.token_hex(16)
        with open(admin_path, "w", encoding="utf-8") as fh:
            json.dump({"admin_password": admin_password}, fh)
        try:
            os.chmod(admin_path, 0o600)
        except OSError:
            pass

        reg_db = os.path.join(tmpdir, "registration.db")
        agents_path = os.path.join(tmpdir, "agents.json")

        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))
        try:
            ms.ApiHandler.state = ms.State(config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                           agents_path=agents_path,
                                           registration_db_path=reg_db,
                                           projects_path=os.path.join(tmpdir, "projects.json"))
            ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

            port = free_port()
            httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)

            def post(path, auth=None, body=None):
                if body is None:
                    body = b"{}"
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                if auth is not None:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            def get(path, auth=None):
                headers = {}
                if auth is not None:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            auth = f"Bearer {admin_password}"
            reg = ms.ApiHandler.state.registration

            # 准备 3 个 pending 请求（approve/revoke/renew 各一）
            req_a = reg.create("fault-app", None, '{}', "fault-key-app")
            req_rv = reg.create("fault-rev", None, '{}', "fault-key-rev")
            req_rn = reg.create("fault-rn", None, '{}', "fault-key-rn")
            assert req_a and req_rv and req_rn

            # ===== 1. MED-002：agents.json 写入故障 → approve 500 =====
            orig_write = ms.write_agents_config

            def boom_write(config, agents_path=None):
                raise OSError("模拟磁盘故障（写 agents.json 失败）")

            ms.write_agents_config = boom_write
            try:
                status, raw = post(f"/api/register/{req_a}/approve", auth=auth)
                assert status == 500, f"写故障 approve 应 500：{status} {raw[:100]}"
                assert json.loads(raw) == {"error": "internal error"}
                print("✓ 写故障 → approve 500（internal error）")
            finally:
                ms.write_agents_config = orig_write

            # ===== 2. MED-002：agents.json 写入故障 → revoke 500 =====
            # 先正常 approve（否则非 approved 状态直接 409）
            status, raw = post(f"/api/register/{req_rv}/approve", auth=auth)
            assert status == 200, f"前置 approve 应 200：{status} {raw[:100]}"
            ms.write_agents_config = boom_write
            try:
                status, raw = post(f"/api/register/{req_rv}/revoke", auth=auth)
                assert status == 500, f"写故障 revoke 应 500：{status} {raw[:100]}"
                assert json.loads(raw) == {"error": "internal error"}
                print("✓ 写故障 → revoke 500（internal error）")
            finally:
                ms.write_agents_config = orig_write

            # ===== 3. MED-002：agents.json 写入故障 → renew 500（issue 阶段） =====
            status, raw = post(f"/api/register/{req_rn}/approve", auth=auth)
            assert status == 200, f"前置 approve 应 200：{status} {raw[:100]}"
            ms.write_agents_config = boom_write
            try:
                status, raw = post(f"/api/register/{req_rn}/renew", auth=auth)
                assert status == 500, f"写故障 renew 应 500：{status} {raw[:100]}"
                assert json.loads(raw) == {"error": "internal error"}
                print("✓ 写故障 → renew 500（internal error）")
            finally:
                ms.write_agents_config = orig_write

            # ===== 4. GET /api/register/list 无鉴权/错密码 → 401 =====
            status, raw = get("/api/register/list")
            assert status == 401, f"list 无鉴权应 401：{status}"
            status, raw = get("/api/register/list", auth="Bearer wrong-password-1234567890")
            assert status == 401, f"list 错密码应 401：{status}"
            status, raw = get("/api/register/list?status=pending", auth=auth)
            assert status == 200, f"list 正确鉴权应 200：{status}"
            print("✓ list 鉴权：无/错 → 401，正确 → 200")

            # ===== 5. admin.json fail-closed → 审批 401 =====
            # 5a. 缺失
            missing_path = os.path.join(tmpdir, "admin-missing.json")
            if os.path.isfile(missing_path):
                os.remove(missing_path)
            ms.ADMIN_CONFIG_REL = (tmpdir, "admin-missing.json")
            status, raw = post(f"/api/register/{req_a}/approve", auth=auth)
            assert status == 401, f"admin.json 缺失应 401：{status}"
            print("✓ admin.json 缺失 → 审批 401")

            # 5b. 损坏 JSON
            bad_path = os.path.join(tmpdir, "admin-bad.json")
            with open(bad_path, "w", encoding="utf-8") as fh:
                fh.write("{not-valid-json")
            try:
                os.chmod(bad_path, 0o600)
            except OSError:
                pass
            ms.ADMIN_CONFIG_REL = (tmpdir, "admin-bad.json")
            status, raw = post(f"/api/register/{req_a}/approve", auth=auth)
            assert status == 401, f"admin.json 损坏应 401：{status}"
            print("✓ admin.json 损坏 → 审批 401")

            # 5c. 权限过宽（644）
            wide_path = os.path.join(tmpdir, "admin-wide.json")
            with open(wide_path, "w", encoding="utf-8") as fh:
                json.dump({"admin_password": admin_password}, fh)
            try:
                os.chmod(wide_path, 0o644)
            except OSError:
                pass
            ms.ADMIN_CONFIG_REL = (tmpdir, "admin-wide.json")
            status, raw = post(f"/api/register/{req_a}/approve", auth=auth)
            assert status == 401, f"admin.json 权限过宽应 401：{status}"
            print("✓ admin.json 权限过宽 → 审批 401")

            # 5d. 缺 admin_password 字段
            nofield_path = os.path.join(tmpdir, "admin-nofield.json")
            with open(nofield_path, "w", encoding="utf-8") as fh:
                json.dump({"other": "x"}, fh)
            try:
                os.chmod(nofield_path, 0o600)
            except OSError:
                pass
            ms.ADMIN_CONFIG_REL = (tmpdir, "admin-nofield.json")
            status, raw = post(f"/api/register/{req_a}/approve", auth=auth)
            assert status == 401, f"admin.json 缺字段应 401：{status}"
            print("✓ admin.json 缺 admin_password 字段 → 审批 401")

            # ===== 6. load_admin_config / ensure_admin_config 辅助函数分支直测 =====
            assert ms.load_admin_config(os.path.join(tmpdir, "no-such.json")) is None
            assert ms.load_admin_config(bad_path) is None
            assert ms.load_admin_config(wide_path) is None
            assert ms.load_admin_config(nofield_path) is None
            good = ms.load_admin_config(admin_path)
            assert good == admin_password, f"正常 admin.json 应返回密码：{good!r}"

            created_path = os.path.join(tmpdir, "admin-auto.json")
            if os.path.isfile(created_path):
                os.remove(created_path)
            # ensure_admin_config 首次创建会向 stdout 打印随机密码（生产行为）；
            # 测试内用 redirect_stdout 静默，避免 verify 记录出现随机密钥噪音
            with contextlib.redirect_stdout(io.StringIO()):
                ms.ensure_admin_config(created_path)
            assert os.path.isfile(created_path), "ensure_admin_config 应创建文件"
            with open(created_path, encoding="utf-8") as fh:
                created = json.load(fh)
            pw = created.get("admin_password")
            assert isinstance(pw, str) and len(pw) == 32, f"自动密码应为 32 字符：{pw!r}"
            try:
                assert (os.stat(created_path).st_mode & 0o777) == 0o600
            except OSError:
                pass
            # 幂等：已存在不覆盖
            with contextlib.redirect_stdout(io.StringIO()):
                ms.ensure_admin_config(created_path)
            with open(created_path, encoding="utf-8") as fh:
                assert json.load(fh)["admin_password"] == pw
            print("✓ load_admin_config fail-closed 分支 + ensure_admin_config 创建/幂等")

            # 恢复有效 admin.json 供后续鉴权用例
            ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))

            # ===== 7. 端点 404/401 分支：不存在的 req_id / 错误鉴权 =====
            for ep in ("approve", "reject", "revoke", "renew"):
                status, raw = post(f"/api/register/no-such-req/{ep}", auth=auth)
                assert status == 404, f"{ep} 不存在 req_id 应 404：{status} {raw[:80]}"
                assert json.loads(raw) == {"error": "not found"}
            print("✓ approve/reject/revoke/renew 不存在 req_id → 404")

            status, raw = post(f"/api/register/{req_rv}/revoke",
                               auth="Bearer wrong-password-1234567890")
            assert status == 401, f"revoke 错密码应 401：{status}"
            status, raw = post(f"/api/register/{req_rn}/renew",
                               auth="Bearer wrong-password-1234567890")
            assert status == 401, f"renew 错密码应 401：{status}"
            print("✓ revoke/renew 错密码 → 401")

            # ===== 8. store 守卫故障注入：approve/reject 返回 False → 409 =====
            # approve 守卫失败：步骤 4 已签发 token，步骤 5 失败 → 回滚 agents.json + 409
            req_guard = reg.create("guard-app", None, '{}', "guard-key-app")
            assert req_guard is not None
            orig_approve = reg.approve
            reg.approve = lambda req_id, token: False
            try:
                status, raw = post(f"/api/register/{req_guard}/approve", auth=auth)
                assert status == 409, f"approve 守卫失败应 409：{status} {raw[:100]}"
                assert json.loads(raw) == {"error": "already processed"}
            finally:
                reg.approve = orig_approve
            with open(agents_path, encoding="utf-8") as fh:
                ag = json.load(fh)
            assert "guard-app" not in ag, "approve 回滚后 agents.json 不应残留 token"
            print("✓ approve store 守卫失败 → 409 + agents.json 回滚")

            # reject 守卫失败 → 409
            req_guard_rj = reg.create("guard-rej", None, '{}', "guard-key-rej")
            assert req_guard_rj is not None
            orig_reject = reg.reject
            reg.reject = lambda req_id, reason=None: False
            try:
                status, raw = post(f"/api/register/{req_guard_rj}/reject", auth=auth,
                                   body={"reason": "x"})
                assert status == 409, f"reject 守卫失败应 409：{status} {raw[:100]}"
                assert json.loads(raw) == {"error": "already processed"}
            finally:
                reg.reject = orig_reject
            print("✓ reject store 守卫失败 → 409")

            # ===== 9. enrollment consume 失败不阻断审批（best-effort，步骤 6） =====
            code_c = ms.ApiHandler.state.enrollment.generate(
                description="consume-fail", max_uses=1, allowed_project_pattern="consume-*")
            req_cons = reg.create("consume-proj", code_c, '{}', "consume-key")
            assert req_cons is not None
            orig_consume = ms.ApiHandler.state.enrollment.consume

            def raise_consume(code):
                raise RuntimeError("模拟注册码消费失败")

            ms.ApiHandler.state.enrollment.consume = raise_consume
            try:
                status, raw = post(f"/api/register/{req_cons}/approve", auth=auth)
                assert status == 200, f"consume 失败应仍 200：{status} {raw[:100]}"
            finally:
                ms.ApiHandler.state.enrollment.consume = orig_consume
            print("✓ enrollment consume 失败 → 审批仍 200（best-effort）")

            # ===== 10. renew 部分失败窗口（REVIEW F2）：remove 成功、issue 失败 → 500 =====
            req_f2 = reg.create("f2-proj", None, '{}', "f2-key")
            assert req_f2 is not None
            status, raw = post(f"/api/register/{req_f2}/approve", auth=auth)
            assert status == 200, f"前置 approve 应 200：{status} {raw[:100]}"
            write_calls = {"n": 0}

            def fail_second_write(cfg, agents_path=None):
                write_calls["n"] += 1
                if write_calls["n"] >= 2:
                    raise OSError("模拟 issue 写失败（remove 成功、issue 失败窗口）")
                return orig_write(cfg, agents_path)

            ms.write_agents_config = fail_second_write
            try:
                status, raw = post(f"/api/register/{req_f2}/renew", auth=auth)
                assert status == 500, f"issue 失败窗口应 500：{status} {raw[:100]}"
                assert json.loads(raw) == {"error": "internal error"}
            finally:
                ms.write_agents_config = orig_write
            print("✓ renew 部分失败窗口（remove 成功、issue 失败）→ 500")

            # ===== 11. 未知 status → status 端点 404（else 分支） =====
            req_bogus = reg.create("bogus-proj", None, '{}', "bogus-key")
            assert req_bogus is not None
            reg.approve(req_bogus, "bogus-token")
            conn = sqlite3.connect(reg_db)
            conn.execute("UPDATE registration_request SET status='weird' WHERE req_id=?",
                         (req_bogus,))
            conn.commit()
            conn.close()
            status, raw = get(f"/api/register/{req_bogus}/status?request_key=bogus-key")
            assert status == 404, f"未知状态应 404：{status} {raw[:100]}"
            print("✓ 未知 status → status 端点 404")

            # ===== 12. 辅助函数 fail-closed 补充分支 =====
            # load_agents_config / load_admin_config 顶层非对象 → fail-closed
            arr_path = os.path.join(tmpdir, "arr.json")
            with open(arr_path, "w", encoding="utf-8") as fh:
                json.dump([1, 2, 3], fh)
            try:
                os.chmod(arr_path, 0o600)
            except OSError:
                pass
            assert ms.load_agents_config(arr_path) == {}
            assert ms.load_admin_config(arr_path) is None
            # extract_bearer_token 非法格式 → None
            assert ms.extract_bearer_token("") is None
            assert ms.extract_bearer_token("Basic abc") is None
            assert ms.extract_bearer_token("Bearer") is None
            assert ms.extract_bearer_token("Bearer   ") is None
            print("✓ 顶层非对象 fail-closed + extract_bearer_token 非法格式 → None")

            # ===== 13. _read_body 对非法 Content-Length 容错（declared=0 走保护性读取） =====
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            try:
                body = json.dumps({"project_id": "clen-proj", "path": "/tmp/clen",
                                   "host_info": "h", "request_key": "k" * 16})
                req_head = (f"POST /api/register HTTP/1.1\r\n"
                            f"Host: 127.0.0.1:{port}\r\n"
                            f"Content-Type: application/json\r\n"
                            f"Content-Length: abc\r\n"
                            f"Connection: close\r\n\r\n").encode("utf-8")
                s.sendall(req_head + body.encode("utf-8"))
                s.shutdown(socket.SHUT_WR)  # 半关闭：让服务端读到 EOF 结束保护性读取
                resp_raw = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    resp_raw += chunk
            finally:
                s.close()
            status_line = resp_raw.split(b"\r\n", 1)[0]
            assert b"201" in status_line, \
                f"非法 Content-Length 应仍可处理（declared=0）：{resp_raw[:200]!r}"
            print("✓ 非法 Content-Length → declared=0 保护性读取，正常 201")

            print("✓ TEST-008 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_009_approve_auto_register_projects():
    """TEST-009：审批通过自动登记 projects.json（TASK-069）。

    - 注册（带 path）→ 审批 → projects.json 自动登记 {id, name, path, transport: agent}
    - 内存 state.config 同步（ingest 注册校验与轮询线程读同一份，无需重启即生效）
    - 幂等：吊销后重新注册同一 project_id 再审批 → 不重复追加
    - RegistrationStore path 存取往返（create → get）
    - 旧记录（path=None）自动登记时 path 兜底为 project_id
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-009-")
    try:
        admin_path = os.path.join(tmpdir, "admin.json")
        admin_password = secrets.token_hex(16)
        with open(admin_path, "w", encoding="utf-8") as fh:
            json.dump({"admin_password": admin_password}, fh)
        try:
            os.chmod(admin_path, 0o600)
        except OSError:
            pass

        reg_db = os.path.join(tmpdir, "registration.db")
        agents_path = os.path.join(tmpdir, "agents.json")
        projects_path = os.path.join(tmpdir, "projects.json")

        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))
        try:
            ms.ApiHandler.state = ms.State(config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                           agents_path=agents_path,
                                           registration_db_path=reg_db,
                                           projects_path=projects_path)
            ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

            port = free_port()
            httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)

            def post(path, auth=None, body=None):
                if body is None:
                    body = b"{}"
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode("utf-8")
                headers = {"Content-Type": "application/json"}
                if auth is not None:
                    headers["Authorization"] = auth
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            auth = f"Bearer {admin_password}"
            reg = ms.ApiHandler.state.registration

            # 0. RegistrationStore path 存取往返（直接单测，TASK-069）
            rid_rt = reg.create("roundtrip-proj", None, '{}', "key-rt",
                                path="D:/win/code/aibase")
            assert reg.get(rid_rt)["path"] == "D:/win/code/aibase"
            print("✓ RegistrationStore path 存取往返")

            # 1. 注册（带 path）→ 审批 → projects.json 自动登记
            body = {"project_id": "auto-reg-1",
                    "path": "D:/share/the5/aibase",
                    "host_info": "hostname:win01, ip:192.168.2.16",
                    "request_key": "k" * 32}
            status, raw = post("/api/register", body=body)
            assert status == 201, f"注册应 201：{status} {raw[:100]}"
            req_id = json.loads(raw)["req_id"]

            status, raw = post(f"/api/register/{req_id}/approve", auth=auth)
            assert status == 200, f"审批应 200：{status} {raw[:100]}"
            assert json.loads(raw)["status"] == "approved"

            with open(projects_path, encoding="utf-8") as fh:
                pj = json.load(fh)
            entries = [p for p in pj.get("projects", []) if p.get("id") == "auto-reg-1"]
            assert len(entries) == 1, f"projects.json 应仅 1 条 auto-reg-1：{pj}"
            assert entries[0]["name"] == "auto-reg-1"
            assert entries[0]["path"] == "D:/share/the5/aibase"
            assert entries[0]["transport"] == "agent"
            print("✓ 审批通过 → projects.json 自动登记 {id,name,path,transport:agent}")

            # 2. 内存 state.config 同步（无重启即对 ingest 校验/轮询生效）
            assert ms.is_project_registered(ms.ApiHandler.state.config, "auto-reg-1"), \
                "内存 config 应已含 auto-reg-1（ingest 400 修复的关键）"
            print("✓ 内存 state.config 同步")

            # 3. 自动登记生效后，同 project_id 重新注册 → 409 active（不应允许重复登记）
            body["request_key"] = "k2" * 16
            status, raw = post("/api/register", body=body)
            assert status == 409, f"已登记的 project_id 重新注册应 409：{status} {raw[:100]}"
            data_conflict = json.loads(raw)
            assert data_conflict.get("existing") == "active"
            print("✓ 自动登记生效：同 project_id 重新注册 409 active")

            # 4. register_project_in_config 直接调用幂等（已存在 → None）
            again = ms.register_project_in_config(projects_path, "auto-reg-1",
                                                  name="x", path_value="/tmp/x")
            assert again is None, "已存在的 project_id 应返回 None"
            print("✓ register_project_in_config 幂等（已存在 → None）")

            # 5. 旧记录（path=None）登记时 path 兜底为 project_id
            entry_legacy = ms.register_project_in_config(
                projects_path, "legacy-proj", name="legacy-proj", path_value=None)
            assert entry_legacy is not None and entry_legacy["path"] == "legacy-proj", \
                f"path=None 应兜底为 project_id：{entry_legacy}"
            print("✓ path=None 兜底为 project_id")

            # 6. 并发登记互斥（PROJECTS_CONFIG_LOCK）：N 线程写同一文件不同 id → 不丢条目
            results = []
            def _reg_one(i):
                results.append(ms.register_project_in_config(
                    projects_path, f"conc-proj-{i}", name=f"c{i}", path_value=f"/tmp/c{i}"))
            threads = [threading.Thread(target=_reg_one, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert all(r is not None for r in results), "并发登记应全部成功"
            with open(projects_path, encoding="utf-8") as fh:
                pj3 = json.load(fh)
            conc_ids = [p["id"] for p in pj3.get("projects", [])
                        if p.get("id", "").startswith("conc-proj-")]
            assert len(conc_ids) == 8, f"并发 8 个登记不应丢条目：{conc_ids}"
            print("✓ 并发登记互斥：8 线程无丢失")

            print("✓ TEST-009 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# 主入口
# ===================================================================

def run_all_tests():
    test_001_register_endpoint()
    test_002_status_endpoint()
    test_003_approve_reject_endpoint()
    test_004_revoke_renew_endpoint()
    test_005_full_integration()
    test_006_edge_cases()
    test_007_enrollment_codes_flow()
    test_008_fault_injection_and_admin_config()
    test_009_approve_auto_register_projects()
    print("\n✓ registration_test 全部通过")


def main():
    if "--coverage" in sys.argv:
        _run_with_coverage()
    else:
        run_all_tests()


if __name__ == "__main__":
    main()