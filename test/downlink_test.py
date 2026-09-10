#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
downlink_test.py — 下行指令通道端点测试（AGENT-DOWNLINK-CONTRACT v1.0）

覆盖 aibase docs/AGENT-DOWNLINK-CONTRACT.md §一~§五：
- TEST-001：入队端点（200 dl-NNNNNN/400 schema/400 注册表闸门/409 幂等/401 无 token）
- TEST-002：拾取端点（白名单 fail-closed/FIFO 领取/空队列/领取即 running）
- TEST-003：回报端点（脱敏截断/404/403 非拾取者/409 幂等忽略）
- TEST-004：状态轮询端点（含 result 回读）
- TEST-005：pickup 超时状态机（store 级注入时钟：重投 ≤2 → failed(pickup-timeout)、busy 暂停）
- TEST-006：全链集成（入队→拾取→回报→状态）

已接入 aios.config.yaml commands.test（TASK-086：此前未接入 → 契约测试从未被执行）。

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
        # transport 缺省 = local（MONITOR-SPEC §3.1）：agent 传输必须显式声明（契约 §一/§五）
        {"id": "x1prototype", "name": "x1prototype", "path": "/tmp/x1proto",
         "transport": "agent"},
        {"id": "westhill", "name": "westhill", "path": "/tmp/westhill",
         "transport": "agent"},
        {"id": "localproj", "name": "localproj", "path": "/tmp/localproj",
         "transport": "local"},
        {"id": "noproj", "name": "noproj", "path": "/tmp/noproj"},  # 缺省 local
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
        downlink_db_path=os.path.join(tmpdir, "downlink.db"),
        start_poller=False)  # TASK-083：同步测试不用后台 poller（消竞态/孤儿线程）
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
    _check(st == 200 and str(body["command_id"]).startswith("dl-") and body["seq"] >= 1,
           "合法入队 → 200（command_id=dl-NNNNNN / seq≥1，契约 §二）")
    st, body_seq = _enqueue(port, dedup="TEST1:A2")
    _check(body_seq["seq"] > body["seq"], "seq 单调递增")
    st, _ = _enqueue(port, name="rm -rf", dedup="TEST1:B")
    _check(st == 400, "白名单外命令 → 400")
    st, _ = _enqueue(port, project="ghost", dedup="TEST1:C")
    _check(st == 400, "未登记 project → 400")
    st, _ = _enqueue(port, project="localproj", dedup="TEST1:D")
    _check(st == 400, "transport=local 条目 → 400")
    st, _ = _enqueue(port, project="noproj", dedup="TEST1:E")
    _check(st == 400, "transport 缺省（local）→ 400（契约 §一/§五）")
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
        _req(port, "POST", "/api/downlink/commands/%s/result" % cid,
             {"status": "done", "exit_code": 0}, token="tok-agent-001")


# ===================================================================
# TEST-002 — 拾取端点
# ===================================================================
def test_002(port):
    print("TEST-002 拾取端点")
    _enqueue(port, dedup="T2:west", project="westhill")
    st, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    cid = body["command"]["command_id"]
    _check(st == 200 and body["command"] is not None
           and body["command"]["project_id"] == "westhill"
           and body["command"]["status"] == "running", "拾取 → running（领取即置态）")
    st, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-002")
    _check(st == 200 and body["command"] is None, "空白名单 agent fail-closed → command=null")
    st, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    _check(st == 200 and body["command"] is None, "队列已领取完 → command=null")
    _req(port, "POST", "/api/downlink/commands/%s/result" % cid,
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
    st, _ = _req(port, "POST", "/api/downlink/commands/%s/result" % cid,
                 {"status": "done", "exit_code": 0,
                  "stdout_tail": secret + long_out, "stderr_tail": "token=sk-xyz"}, token="tok-agent-001")
    _check(st == 200, "合法回报 → 200")
    row = ms.ApiHandler.state.downlink.get(cid)
    out_lines = row["result"]["stdout_tail"].splitlines()
    _check(not any("Bearer" in ln or "token" in ln.lower() for ln in out_lines), "tail 脱敏：凭据行被剔除")
    _check(len(out_lines) <= 200, "tail 截断 ≤200 行")
    _check(row["status"] == "done" and row["result"]["exit_code"] == 0, "终态落库 done/0")
    st, _ = _req(port, "POST", "/api/downlink/commands/99999/result",
                 {"status": "done"}, token="tok-agent-001")
    _check(st == 404, "不存在指令 → 404")
    _, body = _enqueue(port, dedup="T3:B", project="westhill")
    cid2 = body["command_id"]
    _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    st, _ = _req(port, "POST", "/api/downlink/commands/%s/result" % cid2,
                 {"status": "done"}, token="tok-disp-001")
    _check(st == 403, "非拾取者 token 回报 → 403")
    st, body2 = _req(port, "POST", "/api/downlink/commands/%s/result" % cid2,
                     {"status": "done", "exit_code": 0}, token="tok-agent-001")
    _check(st == 200, "正常终态 → 200")
    st, body3 = _req(port, "POST", "/api/downlink/commands/%s/result" % cid2,
                     {"status": "failed", "exit_code": 1}, token="tok-agent-001")
    _check(st == 409, "重复回报 → 409 幂等忽略")
    # 清场：回报剩余 T3:A，不残留
    while True:
        _, body = _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
        if body["command"] is None:
            break
        cid = body["command"]["command_id"]
        _req(port, "POST", "/api/downlink/commands/%s/result" % cid,
             {"status": "done", "exit_code": 0}, token="tok-agent-001")


# ===================================================================
# TEST-004 — 状态轮询端点
# ===================================================================
def test_004(port):
    print("TEST-004 状态轮询端点")
    _, body = _enqueue(port, dedup="T4:A")
    cid = body["command_id"]
    st, body2 = _req(port, "GET", "/api/downlink/commands/%s" % cid, token="tok-disp-001")
    _check(st == 200 and body2["command"]["status"] == "queued"
           and body2["command"]["command"]["name"] == "autoloop_coder", "轮询 → queued + 命令回读")
    st, _ = _req(port, "GET", "/api/downlink/commands/99999", token="tok-disp-001")
    _check(st == 404, "未知指令 → 404")
    st, _ = _req(port, "GET", "/api/downlink/commands/%s" % cid)
    _check(st == 401, "无 token → 401")
    # 清场
    _req(port, "GET", "/api/downlink/pickup", token="tok-agent-001")
    _req(port, "POST", "/api/downlink/commands/%s/result" % cid,
         {"status": "done", "exit_code": 0}, token="tok-agent-001")


# ===================================================================
# TEST-005 — pickup 超时状态机（store 级，注入确定性时钟）
# ===================================================================
def _reset_queued(db_path, cid):
    """模拟"未拾取"：直接改库回 queued（pickup_deadline 保持已过窗口）。"""
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE command SET status='queued', picked_at=NULL, picked_by=NULL,"
                 " picked_epoch=NULL WHERE command_id=?", (cid,))
    conn.commit()
    conn.close()


def test_005(tmpdir):
    print("TEST-005 pickup 超时状态机")
    now = [1000.0]
    db = os.path.join(tmpdir, "dl5.db")
    store = ms.DownlinkStore(db, clock=lambda: now[0])
    row, reused = store.enqueue("x1prototype", "T5:A", "autoloop_coder", ["TASK-001"],
                                1800, "dispatcher")
    cid = row["command_id"]
    _check(row["status"] == "queued" and reused is False, "入队 → queued（未复用）")
    # 窗口内领取（T0+60 < 90s）
    now[0] = 1060.0
    got = store.pickup("agent-1", ["x1prototype"])
    _check(got is not None and got["status"] == "running", "窗口内拾取 → running")
    # T0+100 > 90s：首次超时 → 重投（redeliveries+1、seq+1、窗口重置）
    _reset_queued(db, cid)
    now[0] = 1100.0
    got = store.pickup("agent-1", ["x1prototype"])
    row = store.get(cid)
    _check(got is not None and row["redeliveries"] == 1 and row["seq"] > 1,
           "首次 pickup 超时 → 重投（redeliveries+1、seq 递增）")
    # 第二次超时 → 仍在重投上限内
    _reset_queued(db, cid)
    now[0] = 1300.0
    store.pickup("agent-1", ["x1prototype"])
    row = store.get(cid)
    _check(row["redeliveries"] == 2, "第二次超时 → 重投上限内（redeliveries=2）")
    # 第三次超时 → 超重投上限 → failed(pickup-timeout, human)
    _reset_queued(db, cid)
    now[0] = 1500.0
    store.pickup("agent-1", ["x1prototype"])
    row = store.get(cid)
    _check(row["status"] == "failed" and row["result"]["reason"] == "pickup-timeout",
           "连续超时 >2 次 → failed(pickup-timeout, human)")
    # busy 暂停语义（契约 §三 R2-001）：空白名单 pickup 不推进他人 pickup 窗口
    row2, _ = store.enqueue("westhill", "T5:B", "autoloop_coder", [], 1800, "dispatcher")
    now[0] = 1700.0
    store.pickup("agent-other", [])
    _check(store.get(row2["command_id"])["redeliveries"] == 0,
           "空白名单 pickup 不推进他人 pickup 窗口（busy 暂停语义）")


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
    st, _ = _req(port, "POST", "/api/downlink/commands/%s/result" % cid,
                 {"status": "done", "exit_code": 0, "stdout_tail": "ok"}, token="tok-agent-001")
    st, body = _req(port, "GET", "/api/downlink/commands/%s" % cid, token="tok-disp-001")
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
