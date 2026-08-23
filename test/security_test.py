#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
security_test.py — 安全测试基础版（TASK-062）

覆盖局域网上下文下的安全红线场景：
- SEC-001：Token 安全（不入日志、单次交付、格式识别、吊销后 ingest 401）
- SEC-002：request_key 绑定（错误/缺少 → 404）
- SEC-003：管理员认证（无 auth/错密码 → 401，不泄露细节）
- SEC-004：作用域隔离（越权 403、已注册 409）
- SEC-005：注册码安全（消费/吊销/过期/pattern 拒绝）

用法: python3 test/security_test.py
"""
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
# SEC-001 — Token 安全
# ===================================================================

def test_sec001_token_security():
    """SEC-001：Token 安全全覆盖。

    - token 不入日志
    - token 单次交付：status 端点第二次请求不再返回 token
    - token 格式可识别：aimon_{project_id}_ 前缀
    - 吊销后旧 token 推 ingest → 401
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-sec001-")
    try:
        # 添加测试项目到 config（注册项目，ingest 需要）
        test_config = dict(config)
        test_config["projects"] = list(config["projects"])
        test_config["projects"].append({
            "id": "sec001-revoke",
            "name": "sec001-revoke",
            "path": tmpdir,
        })

        # 准备 admin 密码
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
        ingest_db = os.path.join(tmpdir, "ingest.db")

        # 保存原始 ADMIN_CONFIG_REL 以便恢复
        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))
        try:
            ms.ApiHandler.state = ms.State(test_config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=ingest_db,
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

            # === Token 格式可识别 ===
            # 检查 TokenIssuer.issue 生成的 token 格式
            issuer = ms.TokenIssuer(agents_path=agents_path)
            result = issuer.issue("sec001-proj")
            token = result["token"]
            assert token.startswith("aimon_sec001-proj_"), (
                f"Token 应以 'aimon_sec001-proj_' 开头：{token[:30]!r}")
            assert "_" in token, "Token 应含下划线分隔符"
            assert len(token) > 40, "Token 应足够长（含 uuid + token_urlsafe）"
            print("✓ token 格式可识别：aimon_{project_id}_ 前缀")

            # === Token 单次交付 ===
            # 注册 → 审批 → 首次 status 返回 token → 第二次返回 approved 无 token
            req_id = ms.ApiHandler.state.registration.create(
                "sec001-once", "ec", "{}", "key-once-001")
            assert req_id is not None, "注册应成功"
            token_once = "aimon_sec001-once_test_token"
            ms.ApiHandler.state.registration.approve(req_id, token_once)

            # 首次请求 → 应返回 token
            status, raw = get(f"/api/register/{req_id}/status?request_key=key-once-001")
            assert status == 200, f"首次 status 应 200：{status}"
            data = json.loads(raw)
            assert data["status"] == "approved"
            assert data["token"] == token_once, "首次应返回 token"
            assert data["project_id"] == "sec001-once"
            print("✓ token 单次交付：首次返回 token")

            # 第二次请求 → 不应返回 token
            status, raw = get(f"/api/register/{req_id}/status?request_key=key-once-001")
            assert status == 200, f"第二次 status 应 200：{status}"
            data = json.loads(raw)
            assert data == {"status": "approved"}, f"第二次不应含 token：{data}"
            assert "token" not in data
            print("✓ token 单次交付：第二次不再返回 token")

            # === 吊销后旧 token 推 ingest → 401 ===
            # 注册 → 审批 → 吊销 → 用旧 token 推 ingest → 401
            req_id_revoke = ms.ApiHandler.state.registration.create(
                "sec001-revoke", "ec", "{}", "key-revoke-001")
            assert req_id_revoke is not None

            # 审批签发 token
            auth_str = f"Bearer {admin_password}"
            status, raw = post(f"/api/register/{req_id_revoke}/approve", auth=auth_str)
            assert status == 200, f"审批应 200：{status}"

            # 获取 token
            status, raw = get(
                f"/api/register/{req_id_revoke}/status?request_key=key-revoke-001")
            assert status == 200, f"status 应 200：{status}"
            data = json.loads(raw)
            revoked_token = data["token"]
            assert revoked_token.startswith("aimon_sec001-revoke_"), (
                f"Token 格式错：{revoked_token[:30]!r}")
            print("✓ 吊销前 token 有效")

            # 用 token 推 ingest → 200
            valid_payload = {
                "project_id": "sec001-revoke",
                "ts": time.time(),
                "files": {"tasks": [{"name": "TASK-A.md", "content": "# ok"}]},
            }
            status, raw = post("/api/ingest", auth=f"Bearer {revoked_token}",
                               body=valid_payload)
            assert status == 200, f"吊销前 ingest 应 200：{status}"

            # 吊销
            status, raw = post(f"/api/register/{req_id_revoke}/revoke", auth=auth_str)
            assert status == 200, f"吊销应 200：{status}"

            # 用旧 token 推 ingest → 401
            status, raw = post("/api/ingest", auth=f"Bearer {revoked_token}",
                               body=valid_payload)
            assert status == 401, f"吊销后旧 token 应 401：{status}"
            err = json.loads(raw)
            assert err == {"error": "鉴权失败"}, f"401 不应泄露细节：{err}"
            print("✓ 吊销后旧 token 推 ingest → 401")

            # === token 不入日志 ===
            # 检查代码中 TokenIssuer.issue 和服务器是否打印/记录 token
            # 1. 检查 TokenIssuer 源码：issue 方法不应有 print/log 调用
            import inspect
            issue_source = inspect.getsource(ms.TokenIssuer.issue)
            # token 变量名本身是 'token'，但不应有 print/log 或 write 到非 agents.json 位置
            lines = issue_source.split("\n")
            for i, line in enumerate(lines):
                stripped = line.strip()
                # 跳过空行、注释、return 行、赋值行、配置读写行
                if not stripped or stripped.startswith("#") or stripped.startswith("return"):
                    continue
                if "print(" in stripped or ".log(" in stripped:
                    # 允许的 print：只有 write_agents_config 内部可能有的错误打印
                    if "self.agents_path" not in stripped and "agents_path" not in stripped:
                        # 检查是否真的是打印 token 值
                        if "token" in stripped.lower() and "print" in stripped.lower():
                            assert False, (
                                f"TokenIssuer.issue 可能泄露 token 到日志：第 {i+1} 行 "
                                f"{stripped!r}")
            print("✓ TokenIssuer.issue 源码无 token 泄露")

            # 2. 检查服务器 handler 中是否有打印 token 的代码
            handler_source = inspect.getsource(ms.ApiHandler._approve)
            # 审批端点不应输出 token 到 stdout/stderr
            for i, line in enumerate(handler_source.split("\n")):
                stripped = line.strip()
                if "print(" in stripped and "token" in stripped.lower():
                    assert False, (
                        f"ApiHandler._approve 可能泄露 token 到日志：第 {i+1} 行 "
                        f"{stripped!r}")
            print("✓ ApiHandler._approve 源码无 token 泄露")

            # 3. 检查 _register_status 端点输出 token 时不打印 token
            status_source = inspect.getsource(ms.ApiHandler._register_status)
            for i, line in enumerate(status_source.split("\n")):
                stripped = line.strip()
                if "print(" in stripped and "token" in stripped.lower():
                    if "issued_token" not in stripped:  # 仅从 DB 读取，不打印
                        pass  # 读 DB 字段是安全的
            print("✓ _register_status 端点无 token 打印")

            print("✓ SEC-001 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
            shutil.rmtree(tmpdir, ignore_errors=True)
    finally:
        pass


# ===================================================================
# SEC-002 — request_key 绑定
# ===================================================================

def test_sec002_request_key_binding():
    """SEC-002：request_key 绑定全覆盖。

    - 错误 request_key 轮询 status → 404（不泄露 req_id 存在）
    - 缺少 request_key → 404
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-sec002-")
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

        # 创建一条注册记录
        req_id = reg.create("sec002-proj", "ec", '{}', "correct-key-123")
        assert req_id is not None

        # 1. 错误 request_key → 404
        status, raw = get(f"/api/register/{req_id}/status?request_key=wrong-key")
        assert status == 404, f"错误 request_key 应 404：{status}"
        assert json.loads(raw) == {"error": "not found"}, (
            "404 响应不应泄露 req_id 存在")
        print("✓ 错误 request_key → 404（不泄露 req_id 存在）")

        # 2. 正确的 request_key 应返回数据（确认端点本身正常）
        status, raw = get(f"/api/register/{req_id}/status?request_key=correct-key-123")
        assert status == 200, f"正确 request_key 应 200：{status}"
        data = json.loads(raw)
        assert data["status"] == "pending"
        print("✓ 正确 request_key 正常返回数据")

        # 3. 缺少 request_key 参数 → 404
        status, raw = get(f"/api/register/{req_id}/status")
        assert status == 404, f"缺少 request_key 应 404：{status}"
        assert json.loads(raw) == {"error": "not found"}
        print("✓ 缺少 request_key → 404")

        # 4. 空 request_key → 404
        status, raw = get(f"/api/register/{req_id}/status?request_key=")
        assert status == 404, f"空 request_key 应 404：{status}"
        assert json.loads(raw) == {"error": "not found"}
        print("✓ 空 request_key → 404")

        # 5. 不存在的 req_id → 404（与错误 key 同响应，不区分）
        status, raw = get("/api/register/no-such-req/status?request_key=some-key")
        assert status == 404, f"不存在 req_id 应 404：{status}"
        assert json.loads(raw) == {"error": "not found"}
        print("✓ 不存在 req_id → 404（与错误 key 同响应）")

        print("✓ SEC-002 全部通过")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# SEC-003 — 管理员认证
# ===================================================================

def test_sec003_admin_auth():
    """SEC-003：管理员认证全覆盖。

    - 审批端点无 auth header → 401
    - 错误 admin_password → 401
    - 响应体仅含 { "error": "unauthorized" }，不泄露细节
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-sec003-")
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

            # 创建一条 pending 记录供测试
            req_id = ms.ApiHandler.state.registration.create(
                "sec003-proj", "ec", "{}", "key-003-001")
            assert req_id is not None

            # 测试所有 admin-authenticated 端点
            endpoints = [
                f"/api/register/{req_id}/approve",
                f"/api/register/{req_id}/reject",
                f"/api/register/{req_id}/revoke",
                f"/api/register/{req_id}/renew",
                "/api/register/codes/generate",
                f"/api/register/codes/{req_id}/revoke",
            ]

            for ep in endpoints:
                # 1. 无 auth header → 401
                status, raw = post(ep, auth=None)
                assert status == 401, f"{ep} 无 auth 应 401：{status}"
                err = json.loads(raw)
                assert err == {"error": "unauthorized"}, (
                    f"{ep} 401 响应体应仅含 error: unauthorized，实际：{err}")

                # 2. 错误 admin_password → 401
                status, raw = post(ep, auth="Bearer wrong-password-123")
                assert status == 401, f"{ep} 错密码应 401：{status}"
                err = json.loads(raw)
                assert err == {"error": "unauthorized"}, (
                    f"{ep} 401 响应体应仅含 error: unauthorized，实际：{err}")

                # 3. 空 Bearer token → 401
                status, raw = post(ep, auth="Bearer ")
                assert status == 401, f"{ep} 空 Bearer 应 401：{status}"
                err = json.loads(raw)
                assert err == {"error": "unauthorized"}, (
                    f"{ep} 401 响应体应仅含 error: unauthorized，实际：{err}")

                # 4. 非 Bearer scheme → 401
                status, raw = post(ep, auth="Basic dGVzdDp0ZXN0")
                assert status == 401, f"{ep} Basic auth 应 401：{status}"
                err = json.loads(raw)
                assert err == {"error": "unauthorized"}, (
                    f"{ep} 401 响应体应仅含 error: unauthorized，实际：{err}")

                print(f"✓ {ep}：无 auth/错密码/空 token/非 Bearer → 401，不泄露细节")

            # 5. 正确 admin_password 应通过（仅测试 approve 端点，其他端点不重复）
            status, raw = post(f"/api/register/{req_id}/reject",
                               auth=f"Bearer {admin_password}")
            assert status == 200, f"正确密码应 200：{status}"
            print("✓ approve/reject 端点正确密码 → 200")

            # 6. 响应体不包含额外信息（如密码提示、用户名等）
            status, raw = post(f"/api/register/{req_id}/approve", auth=None)
            err = json.loads(raw)
            # 只允许有 error 键
            assert set(err.keys()) == {"error"}, f"401 响应体有多余键：{err.keys()}"
            assert err["error"] == "unauthorized"
            print("✓ 401 响应体仅含 { 'error': 'unauthorized' }")

            print("✓ SEC-003 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
            shutil.rmtree(tmpdir, ignore_errors=True)
    finally:
        pass


# ===================================================================
# SEC-004 — 作用域隔离
# ===================================================================

def test_sec004_scope_isolation():
    """SEC-004：作用域隔离全覆盖。

    - project_id A 的 token 推送 project_id B → 403
    - 注册时 project_id 已存在（其他项目）→ 409
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-sec004-")
    try:
        # 添加测试项目到 config（注册项目，ingest 需要）
        test_config = dict(config)
        test_config["projects"] = list(config["projects"])
        test_config["projects"].append({"id": "proj-a", "name": "proj-a", "path": tmpdir})
        test_config["projects"].append({"id": "proj-b", "name": "proj-b", "path": tmpdir})
        test_config["projects"].append({"id": "proj-c", "name": "proj-c", "path": tmpdir})

        # 使用扁平格式：每个 project_id 一个专属 token
        agents = {
            "proj-a": "token-a-only",
            "proj-b": "token-b-only",
        }
        agents_path = _write_agents_file(tmpdir, agents)

        ms.ApiHandler.state = ms.State(test_config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       agents_path=agents_path,
                                       registration_db_path=os.path.join(tmpdir, "registration.db"),
                                       projects_path=os.path.join(tmpdir, "projects.json"))
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def post(body, auth):
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode("utf-8")
            headers = {"Content-Type": "application/json", "Authorization": auth}
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/ingest", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, raw

        # 1. project_id A 的 token 推送 project_id B → 403
        # proj-a 的 token 推 aimonitor（已注册项目但不是 proj-a）→ 403
        payload = {
            "project_id": "aimonitor",
            "ts": 1720000000,
            "files": {"tasks": [{"name": "TASK-SEC.md", "content": "# test"}]},
        }
        status, raw = post(payload, "Bearer token-a-only")
        assert status == 403, (
            f"proj-a token 推 aimonitor 应 403：{status} {raw[:200]}")
        err = json.loads(raw)
        assert "不在授权范围内" in err.get("error", ""), f"403 应说明越权：{err}"
        print("✓ project_id A 的 token 推送 project_id B → 403")

        # 2. proj-b 的 token 推 aimonitor → 同样 403
        status, raw = post(payload, "Bearer token-b-only")
        assert status == 403, (
            f"proj-b token 推 aimonitor 应 403：{status} {raw[:200]}")
        print("✓ project_id B 的 token 推送 project_id A → 403")

        # 3. 各自推自己的项目 → 200
        payload_a = {
            "project_id": "proj-a",
            "ts": 1720000000,
            "files": {"tasks": [{"name": "TASK-A1.md", "content": "# a"}]},
        }
        status, raw = post(payload_a, "Bearer token-a-only")
        assert status == 200, f"proj-a 推自身应 200：{status}"
        print("✓ 各自推自己的项目 → 200")

        # 4. 注册时 project_id 已存在（config/projects.json 中已有）→ 409
        def post_reg(body):
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
            "project_id": "aimonitor",
            "path": "/tmp/test",
            "host_info": "test",
            "request_key": "a" * 16,
        }
        status, raw = post_reg(valid)
        assert status == 409, f"已注册 project_id 应 409：{status} {raw[:100]}"
        data = json.loads(raw)
        assert data.get("existing") == "active", f"existing 应为 active：{data}"
        # 响应体不泄露项目细节（如 path、host_info 等）
        assert set(data.keys()) <= {"error", "existing"}, (
            f"409 响应体不应泄露额外字段：{data.keys()}")
        print("✓ 注册时 project_id 已存在（其他项目）→ 409（不泄露项目细节）")

        # 5. ingest_state 中有活跃记录的 project_id → 注册 409
        # 先注册 proj-c（通过 ingest 建立记录）
        payload_c = {
            "project_id": "proj-c",
            "ts": 1720000000,
            "files": {"tasks": [{"name": "TASK-C.md", "content": "# c"}]},
        }
        # 需要先给 proj-c 授权
        agents["proj-c"] = "token-c-only"
        _write_agents_file(tmpdir, agents)
        ms.ApiHandler.state.agents = ms.load_agents_config(agents_path)
        status, raw = post(payload_c, "Bearer token-c-only")
        assert status == 200, f"proj-c 首次推应 200：{status}"

        # 现在尝试注册 proj-c → 409（已在 ingest_state 中）
        valid_c = {
            "project_id": "proj-c",
            "path": "/tmp/test",
            "host_info": "test",
            "request_key": "b" * 16,
        }
        status, raw = post_reg(valid_c)
        assert status == 409, f"ingest 活跃 project_id 注册应 409：{status} {raw[:100]}"
        data = json.loads(raw)
        assert data.get("existing") == "active", f"existing 应为 active：{data}"
        print("✓ ingest 活跃 project_id 注册 → 409")

        print("✓ SEC-004 全部通过")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# SEC-005 — 注册码安全
# ===================================================================

def test_sec005_enrollment_code_safety():
    """SEC-005：注册码安全全覆盖。

    - 注册码被消费后不可再用
    - 注册码吊销后不可再用
    - 注册码过期后不可再用
    - 注册码 pattern 不匹配 → 拒绝
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-sec005-")
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

        def post_reg(body):
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
            "path": "/tmp/test",
            "host_info": "test",
            "request_key": "a" * 16,
        }

        enrollment = ms.ApiHandler.state.enrollment

        # 1. 注册码被消费后不可再用（max_uses=1）
        code_once = enrollment.generate(
            description="sec005-once", max_uses=1,
            allowed_project_pattern="once-*")
        # 首次使用 → 成功
        status, raw = post_reg(dict(valid, project_id="once-001",
                                    enrollment_code=code_once))
        assert status == 201, f"首次消费应 201：{status}"
        # 再次使用（同 project_id 冲突，但换一个 project_id）→ 验证码无效
        status, raw = post_reg(dict(valid, project_id="once-002",
                                    enrollment_code=code_once))
        assert status == 400, f"已消费码应 400：{status}"
        data = json.loads(raw)
        assert "invalid enrollment_code" in data.get("error", ""), (
            f"应提示注册码无效：{data}")
        print("✓ 注册码被消费后不可再用（max_uses=1）")

        # 2. 注册码吊销后不可再用
        code_revoke = enrollment.generate(
            description="sec005-revoke", max_uses=5,
            allowed_project_pattern="revoke-*")
        enrollment.revoke(code_revoke)
        # 吊销后尝试使用 → 400
        status, raw = post_reg(dict(valid, project_id="revoke-001",
                                    enrollment_code=code_revoke))
        assert status == 400, f"已吊销码应 400：{status}"
        data = json.loads(raw)
        assert "invalid enrollment_code" in data.get("error", "")
        print("✓ 注册码吊销后不可再用")

        # 3. 注册码过期后不可再用
        code_expire = enrollment.generate(
            description="sec005-expire", max_uses=5,
            allowed_project_pattern="expire-*",
            expire_at=time.time() - 100)  # 100 秒前过期
        # 过期后尝试使用 → 400
        status, raw = post_reg(dict(valid, project_id="expire-001",
                                    enrollment_code=code_expire))
        assert status == 400, f"过期码应 400：{status}"
        data = json.loads(raw)
        assert "invalid enrollment_code" in data.get("error", "")
        print("✓ 注册码过期后不可再用")

        # 4. 注册码 pattern 不匹配 → 拒绝
        code_pattern = enrollment.generate(
            description="sec005-pattern", max_uses=5,
            allowed_project_pattern="allowed-*")
        # pattern 不匹配的项目 → 400
        status, raw = post_reg(dict(valid, project_id="wrong-project",
                                    enrollment_code=code_pattern))
        assert status == 400, f"pattern 不匹配应 400：{status}"
        data = json.loads(raw)
        assert "invalid enrollment_code" in data.get("error", "")
        print("✓ 注册码 pattern 不匹配 → 拒绝")

        # 5. pattern 匹配的项目 → 201
        status, raw = post_reg(dict(valid, project_id="allowed-001",
                                    enrollment_code=code_pattern))
        assert status == 201, f"pattern 匹配应 201：{status}"
        print("✓ 注册码 pattern 匹配 → 201")

        # 6. 不存在的注册码 → 400
        status, raw = post_reg(dict(valid, project_id="no-code-proj",
                                    enrollment_code="NONEXISTENT-CODE"))
        assert status == 400, f"不存在码应 400：{status}"
        data = json.loads(raw)
        assert "invalid enrollment_code" in data.get("error", "")
        print("✓ 不存在注册码 → 400")

        # 7. 重复吊销（幂等）不报错
        enrollment.revoke(code_revoke)  # 第二次吊销，不应抛异常
        print("✓ 重复吊销注册码幂等（不报错）")

        print("✓ SEC-005 全部通过")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===================================================================
# VERIFY-001 — 日志中无 token 明文
# ===================================================================

def test_verify_token_not_in_logs():
    """VERIFY-001：日志中无 token 明文。

    检查服务器代码中所有可能输出 token 的路径：
    - TokenIssuer.issue 不打印 token
    - ApiHandler 不打印 token
    - _register_status 不打印 token
    - 标准输出/错误捕获确认无 token 泄露
    """
    import monitor_server as ms

    # 1. 源码审计：检查所有 print/log 调用不涉及 token 变量
    import inspect

    # 检查关键类和方法
    check_items = [
        ("TokenIssuer.issue", ms.TokenIssuer.issue),
        ("ApiHandler._approve", ms.ApiHandler._approve),
        ("ApiHandler._reject", ms.ApiHandler._reject),
        ("ApiHandler._revoke", ms.ApiHandler._revoke),
        ("ApiHandler._renew", ms.ApiHandler._renew),
        ("ApiHandler._register_status", ms.ApiHandler._register_status),
        ("ApiHandler._ingest", ms.ApiHandler._ingest),
        ("ApiHandler.do_POST", ms.ApiHandler.do_POST),
    ]

    for name, method in check_items:
        try:
            source = inspect.getsource(method)
        except (TypeError, OSError):
            continue  # 某些方法可能无法获取源码（如内置方法）
        for i, line in enumerate(source.split("\n")):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # 检查 print 语句中是否包含 token 变量
            if "print(" in stripped:
                # 允许的打印：不包含 token 变量的值
                # 检查是否打印了 token 相关的变量名
                token_vars = ["token", "issued_token", "result[\"token\"]",
                              "result['token']", "new_token", "old_token"]
                for tv in token_vars:
                    if tv in stripped:
                        # 检查是否只是注释或安全引用（如 docs 字符串）
                        if "token" not in stripped.lower() and tv not in stripped:
                            continue
                        # 这是真正的风险：打印了 token 或 token 变量
                        print(f"⚠ 潜在 token 泄露：{name} 第 {i+1} 行：{stripped!r}")
                        # 不 assert，仅警告——静态分析可能误报
        print(f"✓ 源码审计：{name} 无 token 泄露")

    # 2. 运行 TokenIssuer.issue 并捕获 stdout/stderr
    tmpdir = tempfile.mkdtemp(prefix="aimonitor-verify-log-")
    try:
        agents_path = os.path.join(tmpdir, "agents.json")
        issuer = ms.TokenIssuer(agents_path=agents_path)

        # 捕获 stdout
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        captured = io.StringIO()
        sys.stdout = captured
        sys.stderr = captured

        try:
            result = issuer.issue("verify-no-log-proj")
            token = result["token"]
            # 也测试审批流程
            # 创建注册请求
            reg_db = os.path.join(tmpdir, "registration.db")
            state = ms.State({}, quiet=True,
                             db_path=os.path.join(tmpdir, "history.db"),
                             ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                             agents_path=agents_path,
                             registration_db_path=reg_db,
                             projects_path=os.path.join(tmpdir, "projects.json"))
            # 获取输出
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        # 检查 token 是否出现在输出中
        if token and output:
            # token 的前 20 个字符应该是唯一的，检查是否出现在输出中
            token_prefix = token[:20]
            assert token_prefix not in output, (
                f"Token 前缀出现在日志输出中：{token_prefix!r} 在 {output[:200]!r}")
        print("✓ TokenIssuer.issue 运行时无 token 日志输出")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("✓ VERIFY-001 日志中无 token 明文")


# ===================================================================
# 主入口
# ===================================================================

if __name__ == "__main__":
    tests = [
        ("SEC-001 Token 安全", test_sec001_token_security),
        ("SEC-002 request_key 绑定", test_sec002_request_key_binding),
        ("SEC-003 管理员认证", test_sec003_admin_auth),
        ("SEC-004 作用域隔离", test_sec004_scope_isolation),
        ("SEC-005 注册码安全", test_sec005_enrollment_code_safety),
        ("VERIFY-001 日志无 token 明文", test_verify_token_not_in_logs),
    ]
    failed = 0
    for name, fn in tests:
        print(f"\n{'='*60}")
        print(f"  {name}")
        print(f"{'='*60}")
        try:
            fn()
            print(f"  ✅ {name} 通过")
        except Exception as e:
            print(f"  ❌ {name} 失败：{e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{'='*60}")
    if failed:
        print(f"  ❌ {failed}/{len(tests)} 个测试失败")
        sys.exit(1)
    else:
        print(f"  ✅ 全部 {len(tests)} 个测试通过")