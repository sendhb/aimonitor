#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
downlink_test.py — 下行指令队列端点测试（TASK-035）

覆盖 AGENT-DOWNLINK-CONTRACT（docs/AGENT-DOWNLINK-CONTRACT.md）§一~§五：
- TEST-001：入队端点（200/400 schema/400 注册表闸门/409 幂等/401 无 token）
- TEST-002：拾取端点（白名单 fail-closed/FIFO 领取/空队列/领取即 running）
- TEST-003：回报端点（脱敏截断/404/403/409 幂等忽略）
- TEST-004：状态轮询端点（含 result 回读）
- TEST-005：pickup 超时状态机（store 级：重投 ≤2 → failed(pickup-timeout)）
- TEST-006：全链集成（入队→拾取→回报→状态）

用法:
  python3 test/downlink_test.py
"""
import http.client
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import monitor_server as ms

AGENTS = {
    "dispatcher": {"token": "tok-disp-001", "projects": []},
    "agent-win01": {"token": "tok-agent-001", "projects": ["x1prototype", "westhill"]},
    "agent-noproj": {"token": "tok-agent-002", "projects": []},
}

PROJECTS = {
    "poll_interval_seconds": 30,
    "projects": [
        {"id": "x1prototype", "name": "x1prototype", "path": "/tmp/x1proto"},
        {"id": "westhill", "name": "westhill", "path": "/tmp/westhill"},
        {"id": "localproj", "name": "localproj", "path": "/tmp/localproj", "transport": "local"},
    ],
}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _boot(tmpdir):
    with open(os.path.join(tmpdir, "projects.json"), "w", encoding="utf-8") as f:
        json.dump(PROJECTS, f)
    agents_path = os.path.join(tmpdir, "agents.json")
    with open(agents_path, "w", encoding="utf-8") as f:
        json.dump(AGENTS, f)
    # Windows 兼容：NTFS 无 POSIX 位（服务端已平台感知跳过检查）；POSIX 平台仍置 600
    if os.name != "nt":
        os.chmod(agents_path, 0o600)
    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)
    ms.ApiHandler.state = ms.State(
        config, quiet=True,
        db_path=os.path.join(tmpdir, "history.db"),
        ingest_db_path=os.path.join(tmpdir, "ingest.db"),
        registration_db_path=os.path.join(tmpdir, "registration.db"),
        projects_path=os.path.join(tmpdir, "projects.json"),
        agents_path=agents_path,
        downlink_db_path=os.path.join(tmpdir, "downlink.db"))
    port = _free_port()
    httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    return port, httpd


def _req(port, method, path, body=None, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    try:
        parsed = json.loads(data.decode("utf-8")) if data else None
    except ValueError:
        parsed = None
    return resp.status, parsed


def _enqueue(port, project="x1prototype", dedup=None, name="autoloop_coder",
             args=None, token="tok-disp-001", timeout_secs=1800):
    body = {"project_id": project, "dedup_key": dedup or "p:t:r",
            "command": {"name": name, "args": args if args is not None else ["TASK-001"]},
            "timeout_secs": timeout_secs}
    return _req(port, "POST", "/api/downlink/commands", body, token=token)


_PASS = []


def _check(cond, label):
    if cond:
        _PASS.append(label)
        print("  ✓ %s" % label)
    else:
        raise AssertionError("FAIL: %s" % label)


# ===================================================================
# TEST-001 — 入队端点
# ===================================================================
def test_001(port):
    print("TEST-001 入队端点")
    st, body = _enqueue(port, dedup="TEST1:A")
    _check(st == 200 and body["command_id"] >= 1 and body["status"] == "queued", "合法入队 → 200 queued")
    _check(body["seq"] == body["command_id"], "seq == command_id（单调）")
    st, _ = _enqueue(port, name="rm -rf", dedup="TEST1:B")
    _check(st == 400, "白名单外命令 → 400")
    st, _ = _enqueue(port, project="ghost", dedup="TEST1:C")
    _check(st == 400, "未登记 project → 400")
    st, _ = _enqueue(port, project="localproj", dedup="TEST1:D")
    _check(st == 400, "transport=local 条目 → 400")
    st, body2 = _enqueue(port, dedup="TEST1:A")
    _check(st == 409 and body2.get("command_id") == body["command_id"], "dedup 未终态重复 → 409 带既有 id")
    st, _ = _enqueue(port, token="wrong")
    _check(st == 401, "无效 token → 401")
    st, _ = _req(port, "POST", "/api/downlink/commands", {"project_id": "x1prototype",
                "command": {"name": "autoloop_coder", "args": "notalist"}}, token="tok-disp-001")
    _check(st == 400, "args 非字符串数组 → 400")
    # 清场：TEST-001 创建的指令全部领完报完，不残留队列干扰后续 FIFO 断言
    while True:
        _, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
        if body["command"] is None:
            break
        cid = body["command"]["command_id"]
        _req(port, "POST", "/api/downlink/commands/%d/result" % cid,
             {"status": "done", "exit_code": 0}, token="tok-agent-001")


# ===================================================================
# TEST-002 — 拾取端点
# ===================================================================
def test_002(port):
    print("TEST-002 拾取端点")
    _enqueue(port, dedup="T2:west", project="westhill")
    st, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    _check(st == 200 and body["command"] is not None
           and body["command"]["project_id"] == "westhill"
           and body["command"]["status"] == "running", "拾取 → running（领取即置态）")
    st, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-002")
    _check(st == 200 and body["command"] is None, "空白名单 agent fail-closed → command=null")
    st, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    _check(st == 200 and body["command"] is None, "队列已领取完 → command=null")
    _req(port, "POST", "/api/downlink/commands/%d/result" % 1,
         {"status": "done", "exit_code": 0}, token="tok-agent-001")
    st, body = _enqueue(port, dedup="T2:proto", project="x1prototype")
    st, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    _check(body["command"]["project_id"] == "x1prototype", "FIFO：领取次旧指令")


# ===================================================================
# TEST-003 — 回报端点
# ===================================================================
def test_003(port):
    print("TEST-003 回报端点")
    _, body = _enqueue(port, dedup="T3:A")
    cid = body["command_id"]
    _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    secret = "Authorization: Bearer sk-abc123\ndone building\n" * 1
    long_out = "\n".join("line%d" % i for i in range(250))
    st, _ = _req(port, "POST", "/api/downlink/commands/%d/result" % cid,
                 {"status": "done", "exit_code": 0,
                  "stdout_tail": secret + long_out, "stderr_tail": "token=sk-xyz"}, token="tok-agent-001")
    _check(st == 200, "合法回报 → 200")
    row = ms.ApiHandler.state.downlink.get(cid)
    out_lines = row["result"]["stdout_tail"].splitlines()
    _check(not any("Bearer" in ln or "token" in ln.lower() for ln in out_lines), "tail 脱敏：凭据行被剔除")
    _check(len(out_lines) <= 200, "tail 截断 ≤200 行")
    _check(row["status"] == "done" and row["result"]["exit_code"] == 0, "终态落库 done/0")
    st, _ = _req(port, "POST", "/api/downlink/commands/%d/result" % 99999,
                 {"status": "done"}, token="tok-agent-001")
    _check(st == 404, "不存在指令 → 404")
    _, body = _enqueue(port, dedup="T3:B", project="westhill")
    cid2 = body["command_id"]
    _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    st, _ = _req(port, "POST", "/api/downlink/commands/%d/result" % cid2,
                 {"status": "done"}, token="tok-disp-001")
    _check(st == 403, "非授权 token 回报 → 403")
    st, body2 = _req(port, "POST", "/api/downlink/commands/%d/result" % cid2,
                     {"status": "done", "exit_code": 0}, token="tok-agent-001")
    _check(st == 200, "正常终态 → 200")
    st, body3 = _req(port, "POST", "/api/downlink/commands/%d/result" % cid2,
                     {"status": "failed", "exit_code": 1}, token="tok-agent-001")
    _check(st == 409, "重复回报 → 409 幂等忽略")
    # 清场：回报剩余 T3:A，不残留
    while True:
        _, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
        if body["command"] is None:
            break
        cid = body["command"]["command_id"]
        _req(port, "POST", "/api/downlink/commands/%d/result" % cid,
             {"status": "done", "exit_code": 0}, token="tok-agent-001")


# ===================================================================
# TEST-004 — 状态轮询端点
# ===================================================================
def test_004(port):
    print("TEST-004 状态轮询端点")
    _, body = _enqueue(port, dedup="T4:A")
    cid = body["command_id"]
    st, body2 = _req(port, "GET", "/api/downlink/commands/%d" % cid, token="tok-disp-001")
    _check(st == 200 and body2["command"]["status"] == "queued"
           and body2["command"]["command"]["name"] == "autoloop_coder", "轮询 → queued + 命令回读")
    st, _ = _req(port, "GET", "/api/downlink/commands/99999", token="tok-disp-001")
    _check(st == 404, "未知指令 → 404")
    st, _ = _req(port, "GET", "/api/downlink/commands/%d" % cid)
    _check(st == 401, "无 token → 401")
    # 清场
    _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    _req(port, "POST", "/api/downlink/commands/%d/result" % cid,
         {"status": "done", "exit_code": 0}, token="tok-agent-001")


# ===================================================================
# TEST-005 — pickup 超时状态机（store 级）
# ===================================================================
def test_005(tmpdir):
    print("TEST-005 pickup 超时状态机")
    store = ms.DownlinkStore(os.path.join(tmpdir, "dl5.db"))
    row, reused = store.enqueue("x1prototype", "T5:A", {"name": "autoloop_coder", "args": []},
                                1800, "dispatcher", now=1000.0)
    cid = row["command_id"]
    # T0+60：未超时，正常领取
    got = store.pickup({"x1prototype"}, now=1060.0, pickup_timeout=90)
    _check(got is not None and got["status"] == "running", "窗口内拾取 → running")
    # 放回 queued（模拟未拾取）：直接改库
    conn = sqlite3.connect(os.path.join(tmpdir, "dl5.db"))
    conn.execute("UPDATE downlink_commands SET status='queued', created_at=1000.0, attempt=0 WHERE command_id=?", (cid,))
    conn.commit(); conn.close()
    # T0+100（>90s）：第一次超时 → 重投（attempt=1，仍 queued）
    got = store.pickup({"x1prototype"}, now=1100.0, pickup_timeout=90)
    _check(got is not None, "首次超时 → 重投后仍可领取")
    conn = sqlite3.connect(os.path.join(tmpdir, "dl5.db"))
    conn.execute("UPDATE downlink_commands SET status='queued', created_at=1100.0, attempt=1 WHERE command_id=?", (cid,))
    conn.commit(); conn.close()
    store.pickup({"x1prototype"}, now=1300.0, pickup_timeout=90)
    conn = sqlite3.connect(os.path.join(tmpdir, "dl5.db"))
    conn.execute("UPDATE downlink_commands SET status='queued', created_at=1300.0, attempt=2 WHERE command_id=?", (cid,))
    conn.commit(); conn.close()
    store.pickup({"x1prototype"}, now=1500.0, pickup_timeout=90)
    row = store.get(cid)
    _check(row["status"] == "failed" and row["result"]["reason"] == "pickup-timeout",
           "连续超时 ≥3 次 → failed(pickup-timeout, human)")


# ===================================================================
# TEST-006 — 全链集成
# ===================================================================
def test_006(port):
    print("TEST-006 全链集成（入队→拾取→回报→状态）")
    st, body = _enqueue(port, dedup="T6:A", args=["TASK-009"])
    cid = body["command_id"]
    st, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    cmd = body["command"]
    _check(cmd["command_id"] == cid and cmd["status"] == "running", "agent 领到指定指令")
    st, _ = _req(port, "POST", "/api/downlink/commands/%d/result" % cid,
                 {"status": "done", "exit_code": 0, "stdout_tail": "ok"}, token="tok-agent-001")
    st, body = _req(port, "GET", "/api/downlink/commands/%d" % cid, token="tok-disp-001")
    _check(st == 200 and body["command"]["status"] == "done"
           and body["command"]["result"]["stdout_tail"] == "ok"
           and body["command"]["finished_at"] is not None, "dispatcher 轮询读到终态 + result")
    # 幂等重入队：同 key 终态后可再次入队（重试场景）
    st, body = _enqueue(port, dedup="T6:A")
    _check(st == 200 and body["command_id"] != cid, "同 dedup_key 终态后可重新入队")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # 临时目录落在仓库 data/ 下（同盘）：避免 Windows 告警路径 relpath 跨盘崩溃（C:/D:）
    data_dir = os.path.join(ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="aimonitor-downlink-", dir=data_dir)
    port, httpd = _boot(tmpdir)
    failures = 0
    try:
        for fn, arg in [(test_001, port), (test_002, port), (test_003, port),
                        (test_004, port), (test_005, tmpdir), (test_006, port)]:
            try:
                fn(arg)
            except AssertionError as e:
                failures += 1
                print("  ✗ %s" % e)
    finally:
        httpd.shutdown()
        # SMELL-005（TASK-035 返工）：临时目录用毕即焚，不再逐次累积
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("\n%d 项断言通过，%d 组失败" % (len(_PASS), failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
