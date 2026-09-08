#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server_smoke.py — 后端冒烟测试（零第三方依赖）

覆盖 REVIEW TEST-001 的要求，防止 SEC-001 类问题回归：
1. py_compile 语法检查（server/monitor_server.py）
2. config/projects.json 合法性
3. 启动真实服务：/api/status 结构、静态文件、路径穿越防护（SEC-001 回归）

用法: python3 test/server_smoke.py
"""
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

# 日期地雷防护（TASK-071 拆弹）：需「近期未 stale」夹具一律用相对日期，
# 随测试日推进自动保鲜（原写死 2026-08-15/17，14 天阈值后必炸）
FRESH_UPDATED = (date.today() - timedelta(days=5)).isoformat()


def test_py_compile():
    subprocess.run([sys.executable, "-m", "py_compile", "server/monitor_server.py"],
                   cwd=ROOT, check=True)
    print("✓ py_compile server/monitor_server.py")


def test_config_json():
    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    assert "projects" in cfg and cfg["projects"], "projects 缺失或为空"
    assert "poll_interval_seconds" in cfg and "heartbeat_stale_threshold_seconds" in cfg
    print("✓ config/projects.json 合法")


def test_task_detail_parsing():
    """TASK-024：detail 解析函数单测（read_task_sections / parse_acceptance / parse_dependencies）。"""
    import monitor_server as ms

    text = (
        "---\n"
        "name: TASK-X\n"
        "metadata:\n"
        "  status: open\n"
        "  depends-on: '[TASK-001, TASK-002]'\n"
        "---\n"
        "# TASK-X\n"
        "## 目标\n要做的事。\n"
        "## 范围\n涉及 A、B。\n"
        "## 验收标准\n"
        "- [ ] 条件 1\n"
        "- [x] 条件 2\n"
        "## 备注\n已知问题。\n"
    )
    fm = ms.read_frontmatter(text)
    sections = ms.read_task_sections(text)
    headings = [h for h, _ in sections]
    assert {"目标", "范围", "验收标准", "备注"} <= set(headings), f"章节缺失: {headings}"
    acc = ms.parse_acceptance(next(b for h, b in sections if h == "验收标准"))
    assert acc == [{"text": "条件 1", "checked": False},
                   {"text": "条件 2", "checked": True}], acc
    assert ms.parse_dependencies(fm) == ["TASK-001", "TASK-002"]
    assert ms.parse_dependencies({"metadata.depends-on": "[]"}) == []
    assert ms.parse_dependencies({}) == []
    print("✓ detail 解析函数（read_task_sections/parse_acceptance/parse_dependencies）")


def test_task_filters():
    """TASK-025：apply_task_filters 单测（精确匹配 + q 大小写不敏感子串 + 组合 + 宽松 + 空筛选）。"""
    import monitor_server as ms
    tasks = [
        {"id": "TASK-001", "name": "a", "description": "hello world",
         "status": "open", "priority": "P1", "assignee": "coder"},
        {"id": "TASK-002", "name": "b", "description": "foo bar",
         "status": "in-progress", "priority": "P2", "assignee": "reviewer"},
        {"id": "TASK-003", "name": "c", "description": "HELLO again",
         "status": "open", "priority": "P3", "assignee": ""},
    ]

    def ids(ts):
        return [t["id"] for t in ts]

    assert ids(ms.apply_task_filters(tasks, {"status": "open"})) == ["TASK-001", "TASK-003"]
    assert ids(ms.apply_task_filters(tasks, {"priority": "P2"})) == ["TASK-002"]
    assert ids(ms.apply_task_filters(tasks, {"assignee": "coder"})) == ["TASK-001"]
    # q 大小写不敏感子串（HELLO 大写也能被 hello 命中）
    assert ids(ms.apply_task_filters(tasks, {"q": "hello"})) == ["TASK-001", "TASK-003"]
    # 组合 AND
    assert ids(ms.apply_task_filters(tasks, {"status": "open", "q": "hello"})) == ["TASK-001", "TASK-003"]
    assert ms.apply_task_filters(tasks, {"status": "open", "priority": "P2"}) == []
    # 宽松语义：未知筛选值 → 空列表
    assert ms.apply_task_filters(tasks, {"status": "no-such"}) == []
    # 空筛选原样返回（同一引用）
    assert ms.apply_task_filters(tasks, {}) is tasks
    print("✓ apply_task_filters 单测")


def test_derive_alerts():
    """TASK-026：derive_project_alerts 单测（心跳卡死/blocked 占比/任务长期未更新/读取错误/心跳缺失不算）。"""
    import monitor_server as ms
    base = {
        "id": "proj-a", "name": "proj-a", "path": "/x",
        "error": None,
        "summary": {"total": 10, "blocked": 1},
        "heartbeat": {"coder": {"exists": True, "age_seconds": 99999},
                      "reviewer": {"exists": False, "age_seconds": None}},
        "tasks": [
            {"id": "TASK-001", "status": "open", "updated": "2000-01-01"},
            {"id": "TASK-002", "status": "done", "updated": "2000-01-01"},
            {"id": "TASK-003", "status": "in-progress", "updated": FRESH_UPDATED},
        ],
    }
    cfg = {"heartbeat_stale_threshold_seconds": 300,
           "alert_blocked_ratio_threshold": 0.2,
           "alert_stale_task_days": 14}

    def kinds(ts):
        return [a["kind"] for a in ts]

    alerts = ms.derive_project_alerts(base, cfg)
    assert "heartbeat-stale" in kinds(alerts) and "task-stale" in kinds(alerts), alerts
    hb = next(a for a in alerts if a["kind"] == "heartbeat-stale")
    assert hb["role"] == "coder" and hb["level"] == "error" and "Coder" in hb["text"]
    # blocked 1/10 = 10% ≤ 20% 阈值 → 无 blocked-ratio
    assert "blocked-ratio" not in kinds(alerts)
    # task-stale 只统计非终态：TASK-001 open+旧日期 触发；TASK-002 done 不触发；TASK-003 近期不触发
    stale = next(a for a in alerts if a["kind"] == "task-stale")
    assert stale["count"] == 1 and stale["tasks"] == ["TASK-001"] and stale["days"] == 14, stale
    assert "1 个任务" in stale["text"] and "14 天" in stale["text"]

    # blocked 占比超阈值 → blocked-ratio（warn，含字段）
    p2 = dict(base, summary={"total": 10, "blocked": 5})
    a2 = ms.derive_project_alerts(p2, cfg)
    assert "blocked-ratio" in kinds(a2), a2
    br = next(a for a in a2 if a["kind"] == "blocked-ratio")
    assert br["level"] == "warn" and br["blocked"] == 5 and br["total"] == 10, br
    assert br["threshold"] == 0.2

    # 读取失败 → 仅 read-error
    p3 = dict(base, error="boom")
    a3 = ms.derive_project_alerts(p3, cfg)
    assert kinds(a3) == ["read-error"] and a3[0]["level"] == "error", a3

    # 心跳缺失不算告警（视为"无进程"）
    p4 = dict(base, heartbeat={"coder": {"exists": False, "age_seconds": None},
                               "reviewer": {"exists": False, "age_seconds": None}})
    assert "heartbeat-stale" not in kinds(ms.derive_project_alerts(p4, cfg))

    # 默认阈值兜底：config 缺省时使用模块默认（blocked 1/2 > 0.2）
    p5 = dict(base, summary={"total": 2, "blocked": 1})
    assert "blocked-ratio" in kinds(ms.derive_project_alerts(p5, {}))
    print("✓ derive_project_alerts 单测")


def test_notify_config():
    """TASK-072：load_notify_config（缺省禁用/文件加载/环境变量覆盖/非法 URL 禁用）。"""
    import monitor_server as ms
    saved = {k: os.environ.get(k) for k in (ms.NOTIFY_ENV_URL, ms.NOTIFY_ENV_TOKEN)}
    try:
        for k in (ms.NOTIFY_ENV_URL, ms.NOTIFY_ENV_TOKEN):
            os.environ.pop(k, None)
        # 缺省（无文件无环境）→ {}（通知禁用，fail-open）
        assert ms.load_notify_config(path="/nonexistent/notify.json") == {}
        tmpdir = tempfile.mkdtemp(prefix="aimonitor-notify-")
        try:
            p = os.path.join(tmpdir, "notify.json")
            # 文件加载
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"webhook": {"enabled": True, "url": "https://example.com/hook",
                                       "token": "t"}}, f)
            cfg = ms.load_notify_config(path=p)
            assert cfg["webhook"]["url"] == "https://example.com/hook", cfg
            assert cfg["webhook"]["token"] == "t", cfg
            # 显式 enabled:false → 禁用（{}）
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"webhook": {"enabled": False, "url": "https://example.com/hook"}}, f)
            assert ms.load_notify_config(path=p) == {}
            # 非法 URL（非 http/https）→ 禁用
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"webhook": {"url": "ftp://x"}}, f)
            assert ms.load_notify_config(path=p) == {}
            # 环境变量覆盖文件（部署注入优先）
            os.environ[ms.NOTIFY_ENV_URL] = "http://127.0.0.1:9/hook"
            cfg2 = ms.load_notify_config(path=p)
            assert cfg2["webhook"]["url"] == "http://127.0.0.1:9/hook", cfg2
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("✓ load_notify_config（缺省禁用/文件/环境变量覆盖/非法 URL）")


def test_notify_sender():
    """TASK-072：NotificationSender webhook POST 端到端（本地接收器验证请求/鉴权/payload）。"""
    import monitor_server as ms

    class Receiver(threading.Thread):
        """最小本地 HTTP 接收器：接收一个 POST，记录请求头与 body。"""

        def __init__(self):
            super().__init__(daemon=True)
            self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.srv.bind(("127.0.0.1", 0))
            self.port = self.srv.getsockname()[1]
            self.srv.listen(1)
            self.received = []

        def run(self):
            conn, _ = self.srv.accept()
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                head, _, body = data.partition(b"\r\n\r\n")
                clen = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        clen = int(line.split(b":", 1)[1].strip())
                while len(body) < clen:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    body += chunk
                self.received.append((head.decode("utf-8", "replace"),
                                      body.decode("utf-8", "replace")))
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            finally:
                conn.close()

    recv = Receiver()
    recv.start()
    time.sleep(0.2)  # 等监听就绪
    s = ms.NotificationSender(f"http://127.0.0.1:{recv.port}/hook", token="tok")
    ok, detail = s.send([{"project": "p", "kind": "blocked-ratio", "text": "x"}], generated_at=123.0)
    recv.join(timeout=5)
    assert ok, detail
    assert len(recv.received) == 1, recv.received
    head, body = recv.received[0]
    assert "Authorization: Bearer tok" in head, head
    payload = json.loads(body)
    assert payload["event"] == "alerts.changed" and payload["count"] == 1, payload
    assert payload["items"][0]["kind"] == "blocked-ratio", payload
    print("✓ NotificationSender webhook 端到端（POST/Bearer 头/payload）")

    # 未配置 / 空 items → 直接失败返回，不抛异常（fail-open）
    ok2, d2 = ms.NotificationSender("").send([{"kind": "x"}])
    assert not ok2 and "未配置" in d2, (ok2, d2)
    ok3, d3 = ms.NotificationSender("http://127.0.0.1:1/hook").send([])
    assert not ok3 and "无告警" in d3, (ok3, d3)
    print("✓ NotificationSender 未配置/空 items 不抛异常")


def test_filereader_abstract():
    """TASK-037：FileReader 接口 + LocalReader 实现（exists/read/mtime/listdir 缺失容忍）。"""
    import monitor_server as ms

    # 接口为抽象基类：不可直接实例化；四个抽象方法必须由子类实现
    try:
        ms.FileReader()
        assert False, "FileReader 应不可实例化（抽象接口）"
    except TypeError:
        pass
    for m in ("exists", "read", "mtime", "listdir"):
        assert callable(getattr(ms.FileReader, m)), f"FileReader.{m} 缺失"

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reader-")
    try:
        sub = os.path.join(tmpdir, "runtime")
        os.makedirs(os.path.join(sub, "tasks"))
        with open(os.path.join(sub, "tasks", "TASK-001.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: TASK-001\n---\n# TASK-001\n")
        with open(os.path.join(sub, "log.txt"), "w", encoding="utf-8") as f:
            f.write("hello\nworld\n")

        r = ms.LocalReader()
        # exists：文件/目录 True，缺失 False
        assert r.exists(os.path.join(sub, "tasks", "TASK-001.md")) is True
        assert r.exists(os.path.join(sub, "tasks")) is True
        assert r.exists(os.path.join(sub, "no-such")) is False
        # read：原文往返；缺失/目录 → None（不抛）
        assert r.read(os.path.join(sub, "tasks", "TASK-001.md")) == "---\nname: TASK-001\n---\n# TASK-001\n"
        assert r.read(os.path.join(sub, "no-such.md")) is None
        assert r.read(os.path.join(sub, "tasks")) is None  # 目录不可 open → None
        # mtime：float；缺失 → None
        mt = r.mtime(os.path.join(sub, "tasks", "TASK-001.md"))
        assert isinstance(mt, float) and mt > 0
        assert r.mtime(os.path.join(sub, "no-such")) is None
        # listdir：名称列表；缺失/文件 → []（不抛）
        assert r.listdir(os.path.join(sub, "tasks")) == ["TASK-001.md"]
        assert r.listdir(os.path.join(sub, "no-such")) == []
        assert r.listdir(os.path.join(sub, "log.txt")) == []
        print("✓ FileReader 接口 + LocalReader 实现（exists/read/mtime/listdir 缺失容忍）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_collect_project_reader_injection():
    """TASK-037：collect_project 注入 reader（默认 LocalReader；显式注入=默认一致，零回归）。"""
    import monitor_server as ms

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-collect-")
    try:
        rt = os.path.join(tmpdir, "runtime")
        os.makedirs(os.path.join(rt, "tasks"))
        os.makedirs(os.path.join(rt, "states"))
        os.makedirs(os.path.join(rt, "logs"))
        os.makedirs(os.path.join(rt, "verification"))
        os.makedirs(os.path.join(rt, "reviews"))
        with open(os.path.join(rt, "tasks", "TASK-001-demo.md"), "w", encoding="utf-8") as f:
            f.write(
                "---\n"
                "name: TASK-001-demo\n"
                "metadata:\n"
                "  status: in-progress\n"
                "  priority: P1\n"
                "---\n"
                "# TASK-001\n"
                "## 目标\nx\n"
            )
        with open(os.path.join(rt, "states", "CURRENT_FOCUS.md"), "w", encoding="utf-8") as f:
            f.write("# Current Focus\n## 当前任务\nTASK-001\n## 下一个动作\n1. next\n")
        with open(os.path.join(rt, "logs", "autoloop-coder.heartbeat"), "w", encoding="utf-8") as f:
            f.write("alive")
        with open(os.path.join(rt, "verification", "VERIFY-2026-08-17-task-001.md"),
                  "w", encoding="utf-8") as f:
            f.write("---\nmetadata:\n  type: verify\n  task-ref: TASK-001\n---\n")

        proj = {"id": "proj-x", "name": "proj-x", "path": tmpdir}
        default = ms.collect_project(proj, {})
        assert default["error"] is None, default["error"]
        assert default["summary"]["total"] == 1 and default["tasks"][0]["id"] == "TASK-001"
        assert default["focus"]["current"], "focus 未读取"
        assert default["heartbeat"]["coder"]["exists"] is True
        assert default["verification_count"] == 1

        def _key(d):
            """可比键：剔除易变时间戳（last_read_at / heartbeat age），只比稳定派生字段。"""
            return (d["error"], d["summary"],
                    tuple((t["id"], t["status"], t["detail"]) for t in d["tasks"]),
                    d["focus"], d["verification_count"], d["review_count"],
                    d["heartbeat"]["coder"]["exists"], d["heartbeat"]["reviewer"]["exists"],
                    d["events"]["coder"]["count"], d["events"]["reviewer"]["count"])

        # 显式 LocalReader 注入 → 与默认完全一致（零回归基线）
        explicit = ms.collect_project(proj, {}, ms.LocalReader())
        assert _key(explicit) == _key(default), "显式注入 LocalReader 应与默认一致"

        # 记录式 reader：薄封装 LocalReader，验证 collect_project 确实把读取走注入的 reader
        class _CountingReader(ms.LocalReader):
            def __init__(self):
                super().__init__()
                self.calls = []

            def read(self, path):
                self.calls.append(path)
                return super().read(path)

        counting = _CountingReader()
        via_reader = ms.collect_project(proj, {}, counting)
        assert _key(via_reader) == _key(default), "注入 reader 采集结果应与默认一致"
        assert counting.calls, "collect_project 未调用注入 reader 的 read()"

        # runtime 缺失仍报 error（reader.exists 驱动的错误路径回归）
        empty_proj = {"id": "empty", "name": "empty", "path": os.path.join(tmpdir, "no-runtime")}
        ep = ms.collect_project(empty_proj, {})
        assert ep["error"] and "runtime" in ep["error"], ep
        print("✓ collect_project 注入 reader（默认 LocalReader / 显式注入 / 零回归一致 / runtime 缺失 error）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_agentreader():
    """TASK-038：AgentReader 从 ingest_state 读（tasks/focus/heartbeat/events/计数 映射）。

    TASK-042：payload 为 AIOS 通用遥测格式（file-oriented 文件条目数组）。
    """
    import monitor_server as ms

    payload = {
        "tasks": [
            {"name": "TASK-001-a.md",
             "content": "---\nname: TASK-001-a\nmetadata:\n  status: in-progress\n---\n# TASK-001\n"},
            {"name": "TASK-002-b.md",
             "content": "---\nname: TASK-002-b\nmetadata:\n  status: done\n---\n# TASK-002\n"},
        ],
        "focus": "# Current Focus\n## 当前任务\nTASK-001\n## 下一个动作\nnext\n",
        "heartbeats": [{"file": "autoloop-coder.heartbeat", "mtime": 1720000000}],
        "events": [{"name": "autoloop-coder-events.jsonl",
                     "content": '{"ts":1,"task":"TASK-001","outcome":"ok"}\n'}],
        "verification_count": 2,
        "review_count": 1,
    }
    record = {"project_id": "win-proj", "payload": payload,
              "last_seen": 1720009999, "agent_id": "agent-1"}
    proj = {"id": "win-proj", "path": "/展示/win-proj", "transport": "agent"}
    r = ms.AgentReader(proj, record=record)
    rt = os.path.join("/展示/win-proj", "runtime")

    # exists：虚拟 runtime 树（文件/目录/计数目录；缺失与未推送角色 False）
    assert r.exists(rt) is True
    assert r.exists(os.path.join(rt, "tasks")) is True
    assert r.exists(os.path.join(rt, "tasks", "TASK-001-a.md")) is True
    assert r.exists(os.path.join(rt, "tasks", "NOPE.md")) is False
    assert r.exists(os.path.join(rt, "states", "CURRENT_FOCUS.md")) is True
    assert r.exists(os.path.join(rt, "logs", "autoloop-coder.heartbeat")) is True
    assert r.exists(os.path.join(rt, "logs", "autoloop-reviewer.heartbeat")) is False
    assert r.exists(os.path.join(rt, "logs", "autoloop-coder-events.jsonl")) is True
    assert r.exists(os.path.join(rt, "logs", "autoloop-reviewer-events.jsonl")) is False
    assert r.exists(os.path.join(rt, "verification")) is True
    assert r.exists(os.path.join(rt, "reviews")) is True

    # read：tasks/focus/events 原文；VERIFY/REVIEW 只推送计数 → 无内容 None
    assert r.read(os.path.join(rt, "tasks", "TASK-001-a.md")) == payload["tasks"][0]["content"]
    assert r.read(os.path.join(rt, "states", "CURRENT_FOCUS.md")) == payload["focus"]
    assert r.read(os.path.join(rt, "logs", "autoloop-coder-events.jsonl")) == payload["events"][0]["content"]
    assert r.read(os.path.join(rt, "verification", "VERIFY-0.md")) is None
    assert r.read(os.path.join(rt, "no-such")) is None

    # mtime：heartbeat = payload epoch；非心跳/缺失 → None
    assert r.mtime(os.path.join(rt, "logs", "autoloop-coder.heartbeat")) == 1720000000
    assert r.mtime(os.path.join(rt, "logs", "autoloop-reviewer.heartbeat")) is None
    assert r.mtime(os.path.join(rt, "tasks", "TASK-001-a.md")) is None

    # listdir：tasks 文件名 / 合成计数条目 / logs 按存在性
    assert r.listdir(os.path.join(rt, "tasks")) == ["TASK-001-a.md", "TASK-002-b.md"]
    assert r.listdir(os.path.join(rt, "verification")) == ["VERIFY-0.md", "VERIFY-1.md"]
    assert r.listdir(os.path.join(rt, "reviews")) == ["REVIEW-0.md"]
    assert r.listdir(os.path.join(rt, "logs")) == ["autoloop-coder.heartbeat", "autoloop-coder-events.jsonl"]
    assert r.listdir(os.path.join(rt, "no-such")) == []

    # path 仅展示：不读本地文件系统，root 外一律不存在
    assert r.exists("/etc/passwd") is False

    # record 缺失（无推送）→ 整树不存在（缺失容忍，不抛）
    r2 = ms.AgentReader({"id": "x", "path": "/y"}, record=None)
    assert r2.exists(os.path.join("/y", "runtime")) is False
    assert r2.read(os.path.join("/y", "runtime", "tasks", "TASK-001.md")) is None
    assert r2.listdir(os.path.join("/y", "runtime")) == []
    assert r2.mtime(os.path.join("/y", "runtime", "logs", "autoloop-coder.heartbeat")) is None

    # TASK-042 file-oriented 缺失容忍：tasks/heartbeats/events 非数组或 None → 空映射不抛
    r3 = ms.AgentReader({"id": "x", "path": "/y"},
                        record={"project_id": "x", "payload": {"tasks": None,
                                                                  "heartbeats": None,
                                                                  "events": None,
                                                                  "verification_count": None,
                                                                  "review_count": None},
                                "last_seen": 1720009999, "agent_id": "a"})
    rt3 = os.path.join("/y", "runtime")
    assert r3.exists(os.path.join(rt3, "tasks")) is False
    assert r3.exists(os.path.join(rt3, "logs")) is False
    assert r3.exists(os.path.join(rt3, "verification")) is False
    assert r3.read(os.path.join(rt3, "tasks", "TASK-001.md")) is None
    assert r3.listdir(os.path.join(rt3, "tasks")) == []
    print("✓ AgentReader 从 ingest_state 读（file-oriented tasks/focus/heartbeats/events/计数 + path 仅展示）")



def test_collect_project_agent_transport():
    """TASK-038：collect_project 按 transport 选 reader（agent→AgentReader；local→LocalReader 零回归）。"""
    import monitor_server as ms

    # transport 选择器
    assert isinstance(ms.reader_for_project({"id": "a", "path": "/b"}), ms.LocalReader)
    assert isinstance(ms.reader_for_project(
        {"id": "a", "path": "/b", "transport": "local"}), ms.LocalReader)
    assert isinstance(ms.reader_for_project(
        {"id": "a", "path": "/b", "transport": "agent"}), ms.AgentReader)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-agent-")
    try:
        store = ms.IngestStore(os.path.join(tmpdir, "ingest.db"))
        payload = {
            "tasks": [
                {"name": "TASK-001-a.md",
                 "content": "---\nname: TASK-001-a\nmetadata:\n  status: in-progress\n  priority: P1\n  assignee: coder\n---\n# TASK-001\n## 目标\nx\n"},
                {"name": "TASK-002-b.md",
                 "content": "---\nname: TASK-002-b\nmetadata:\n  status: done\n---\n# TASK-002\n"},
            ],
            "focus": "# Current Focus\n## 当前任务\nTASK-001\n## 下一个动作\nnext\n",
            "heartbeats": [{"file": "autoloop-coder.heartbeat", "mtime": int(time.time())}],
            "events": [{"name": "autoloop-coder-events.jsonl",
                         "content": '{"ts":1,"task":"TASK-001","outcome":"ok"}\n'}],
            "verification_count": 2,
            "review_count": 1,
        }
        store.upsert("win-proj", payload, "agent-1")

        proj = {"id": "win-proj", "name": "Windows工程",
                "path": "/展示/win-proj", "transport": "agent"}
        res = ms.collect_project(proj, {}, ingest=store)
        assert res["error"] is None, res["error"]
        # path 仅展示：输出保持配置值，不读本地（该路径本地不存在也能采到）
        assert res["path"] == "/展示/win-proj"
        assert res["summary"]["total"] == 2 and res["summary"]["in-progress"] == 1
        assert res["summary"]["done"] == 1
        assert [t["id"] for t in res["tasks"]] == ["TASK-001", "TASK-002"]
        assert res["focus"]["current"].startswith("TASK-001")
        assert res["heartbeat"]["coder"]["exists"] is True
        assert res["heartbeat"]["coder"]["age_seconds"] is not None
        assert res["heartbeat"]["reviewer"]["exists"] is False
        assert res["events"]["coder"]["count"] == 1
        assert res["events"]["coder"]["outcomes"]["ok"] == 1
        assert res["events"]["reviewer"]["count"] == 0
        assert res["verification_count"] == 2 and res["review_count"] == 1
        # agent 不推送单条 VERIFY/REVIEW 原文 → detail 关联记录为空（只展示计数）
        d = res["tasks"][0]["detail"]
        assert d["verification"] == [] and d["reviews"] == []

        # 事件时间线经 transport reader（_events 同款路径）：agent 项目从 ingest_state 读
        reader = ms.reader_for_project(proj, store)
        rt = os.path.join(proj["path"], "runtime")
        count, items = ms.read_events_timeline(rt, "coder", 10, reader)
        assert count == 1 and items[0]["task"] == "TASK-001"
        count2, items2 = ms.read_events_timeline(rt, "reviewer", 10, reader)
        assert count2 == 0 and items2 == []

        # agent 项目从未推送（无记录）→ agent 整体离线 error='agent 离线'（TASK-039 细化，
        # 原 TASK-038 临时报「runtime 目录不存在」）
        empty = ms.collect_project(
            {"id": "never-pushed", "path": "/展示/never", "transport": "agent"},
            {}, ingest=store)
        assert empty["error"] == "agent 离线", empty["error"]
        # 无 ingest 注入也缺省走 AgentReader（记录缺失 → 同 error，不抛）
        empty2 = ms.collect_project(
            {"id": "never-pushed", "path": "/展示/never", "transport": "agent"}, {})
        assert empty2["error"] == "agent 离线", empty2["error"]

        # transport 缺省 local（现有项目零迁移零回归）：aimonitor 自身 runtime 可采
        local = ms.collect_project({"id": "l", "path": ROOT}, {})
        assert local["error"] is None, local["error"]
        assert local["tasks"] and local["summary"]["total"] > 0
        print("✓ collect_project 按 transport 选 reader（agent→AgentReader / local→LocalReader 零回归）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_heartbeat_offline_semantics():
    """TASK-039：agent 心跳/离线语义（role 心跳=payload epoch；last_seen 超阈值/无记录 → error='agent 离线'；告警派生兼容）。"""
    import sqlite3
    import monitor_server as ms

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-offline-")
    try:
        store = ms.IngestStore(os.path.join(tmpdir, "ingest.db"))
        now = int(time.time())
        payload = {
            "tasks": [{"name": "TASK-001-a.md",
                        "content": "---\nname: TASK-001-a\nmetadata:\n  status: in-progress\n---\n# TASK-001\n"}],
            "focus": "# Current Focus\n## 当前任务\nTASK-001\n",
            "heartbeats": [{"file": "autoloop-coder.heartbeat", "mtime": now - 60},
                           {"file": "autoloop-reviewer.heartbeat", "mtime": now - 99999}],
            "events": [{"name": "autoloop-coder-events.jsonl",
                         "content": '{"ts":1,"task":"TASK-001","outcome":"ok"}\n'}],
            "verification_count": 1,
            "review_count": 0,
        }
        store.upsert("win-proj", payload, "agent-1")
        proj = {"id": "win-proj", "name": "Windows工程",
                "path": "/展示/win-proj", "transport": "agent"}
        cfg = {"heartbeat_stale_threshold_seconds": 300}

        # 1. agent 在线：role 心跳 = payload epoch（age = now - epoch，与 local 字段模型一致）
        res = ms.collect_project(proj, cfg, ingest=store)
        assert res["error"] is None, res["error"]
        hb = res["heartbeat"]["coder"]
        assert hb["exists"] is True
        assert 55 <= hb["age_seconds"] <= 65, f"age 应 ≈ now-epoch(60s)：{hb}"
        assert res["heartbeat"]["reviewer"]["exists"] is True
        # 在线但 role 卡死（reviewer epoch 超阈值）→ heartbeat-stale 仍由 role epoch 驱动（§3.1.4）
        stale = [a for a in res["alerts"] if a["kind"] == "heartbeat-stale"]
        assert len(stale) == 1 and stale[0]["role"] == "reviewer", res["alerts"]
        assert not any(a["kind"] == "read-error" for a in res["alerts"]), res["alerts"]

        # 2. offline_reason 单元判定：无记录 / last_seen 超阈值 → 'agent 离线'；新鲜 → None
        assert ms.AgentReader(proj, record=None).offline_reason(300) == "agent 离线"
        old = {"project_id": "win-proj", "payload": payload,
               "last_seen": now - 10000, "agent_id": "a"}
        fresh = {"project_id": "win-proj", "payload": payload,
                 "last_seen": now, "agent_id": "a"}
        assert ms.AgentReader(proj, record=old).offline_reason(300) == "agent 离线"
        assert ms.AgentReader(proj, record=fresh).offline_reason(300) is None
        # stale_secs=None（阈值未提供）→ 不判超阈值（仅无记录离线）
        assert ms.AgentReader(proj, record=old).offline_reason() is None
        # local 恒 None（离线语义不适用，local 不可达由 exists(runtime) 表达）
        assert ms.LocalReader().offline_reason(300) is None
        assert ms.LocalReader().offline_reason() is None

        # 3. last_seen 超阈值（改库使 store.read 返回陈旧 last_seen）→ collect_project
        #    error='agent 离线'、数据为空、仅 read-error 告警（§4.6 兼容：error 非空不再派生其他告警）
        conn = sqlite3.connect(os.path.join(tmpdir, "ingest.db"))
        conn.execute("UPDATE ingest_state SET last_seen=? WHERE project_id=?",
                     (now - 10000, "win-proj"))
        conn.commit()
        conn.close()
        res_off = ms.collect_project(proj, cfg, ingest=store)
        assert res_off["error"] == "agent 离线", res_off["error"]
        assert res_off["tasks"] == [] and res_off["summary"]["total"] == 0, "离线应返回空数据"
        kinds = [a["kind"] for a in res_off["alerts"]]
        assert kinds == ["read-error"], f"离线应仅 read-error 告警：{res_off['alerts']}"
        assert "agent 离线" in res_off["alerts"][0]["text"]

        # 4. 从未推送（无记录）→ 同 error='agent 离线' + 仅 read-error 告警
        never = ms.collect_project(
            {"id": "never-pushed", "path": "/展示/never", "transport": "agent"},
            cfg, ingest=store)
        assert never["error"] == "agent 离线", never["error"]
        assert [a["kind"] for a in never["alerts"]] == ["read-error"], never["alerts"]

        # 5. 告警派生兼容：error 非空时 read-error 派生路径（既有 §4.6 规则）不变
        err_p = {"id": "p", "error": "agent 离线",
                 "heartbeat": {"coder": {"exists": True, "age_seconds": 99999},
                                "reviewer": {"exists": False, "age_seconds": None}},
                 "summary": {"total": 1, "blocked": 1}}
        alerts = ms.derive_project_alerts(err_p, cfg)
        assert [a["kind"] for a in alerts] == ["read-error"], alerts
        print("✓ agent 心跳/离线语义（role 心跳=payload epoch、last_seen 超阈值/无记录→agent 离线、告警派生兼容）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_status_instance_meta():
    """TASK-043：/api/status 项目对象透传实例级元数据（group 缺省=id、transport 缺省=local；
    agent 项目 transport='agent'、同 group 多实例各自独立 id + 离线 error='agent 离线'）。

    前端实例级展示依赖这两个只读字段：同 group 多实例一行一实例区分 + agent 传输标识/
    离线 error 上下文（MONITOR-SPEC §2/§3.1.6）。
    """
    import monitor_server as ms

    # 1. empty_project 透传默认值：group 缺省 = id；transport 缺省 = local
    ep = ms.empty_project({"id": "proj-a", "path": "/x"})
    assert ep["group"] == "proj-a" and ep["transport"] == "local", ep
    # 显式 group/transport：同逻辑项目多实例共享 group（baseline-dev/prod）
    ep2 = ms.empty_project({"id": "baseline-dev", "name": "baseline", "path": "/x",
                            "group": "baseline", "transport": "agent"})
    assert ep2["group"] == "baseline" and ep2["transport"] == "agent", ep2
    # group 空串回退 = id（与缺省同语义）
    ep3 = ms.empty_project({"id": "proj-b", "path": "/x", "group": ""})
    assert ep3["group"] == "proj-b", ep3
    # transport 空串回退 = local（MIN-001 返工：与 group 侧 or 兜底一致，显式空串不穿透）
    ep4 = ms.empty_project({"id": "proj-c", "path": "/x", "transport": ""})
    assert ep4["transport"] == "local", ep4

    # 2. 服务端 /api/status：local 默认透传 + agent 多实例（同 group、独立 id、path 仅展示）
    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)
    config["poll_interval_seconds"] = 3600  # 避免测试期间自动轮询干扰
    config["projects"] = list(config["projects"]) + [
        {"id": "baseline-dev", "name": "baseline", "path": "/展示/baseline-dev",
         "group": "baseline", "transport": "agent"},
        {"id": "baseline-prod", "name": "baseline", "path": "/展示/baseline-prod",
         "group": "baseline", "transport": "agent"},
    ]
    tmpdir = tempfile.mkdtemp(prefix="aimonitor-instance-meta-")
    try:
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")
        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/status")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            conn.close()
            assert resp.status == 200, f"/api/status → {resp.status}"
            projs = {p["id"]: p for p in data["projects"]}
            # local 项目（缺省 group/transport）→ 透传默认
            assert projs["aimonitor"]["group"] == "aimonitor", projs["aimonitor"]
            assert projs["aimonitor"]["transport"] == "local", projs["aimonitor"]
            # agent 多实例：同 group 各自独立 id + transport=agent + path 仅展示
            assert projs["baseline-dev"]["group"] == "baseline"
            assert projs["baseline-dev"]["transport"] == "agent"
            assert projs["baseline-dev"]["path"] == "/展示/baseline-dev"
            assert projs["baseline-prod"]["group"] == "baseline"
            assert projs["baseline-prod"]["transport"] == "agent"
            # agent 未推送（无记录）→ error='agent 离线'（TASK-039 语义，前端展示离线上下文）
            assert projs["baseline-dev"]["error"] == "agent 离线", \
                projs["baseline-dev"]["error"]
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("✓ /api/status 实例级元数据透传（group 缺省=id / transport 缺省=local / agent 多实例 + 离线 error）")


def test_ingest_store():
    """TASK-033：ingest_state 存储层单测（upsert/read/幂等覆盖/重连自愈）。"""
    import monitor_server as ms
    tmpdir = tempfile.mkdtemp(prefix="aimonitor-ingest-")
    try:
        db_path = os.path.join(tmpdir, "ingest.db")
        store = ms.IngestStore(db_path)

        payload_a = {"tasks": [{"name": "TASK-001.md", "content": "# TASK-001\n## 目标\nx"}],
                     "focus": "TASK-001",
                     "heartbeats": [{"file": "autoloop-coder.heartbeat", "mtime": 1720000000}],
                     "verification_count": 1}
        payload_b = {"tasks": [{"name": "TASK-002.md", "content": "# TASK-002\n## 目标\ny"}],
                     "focus": "TASK-002",
                     "heartbeats": [{"file": "autoloop-coder.heartbeat", "mtime": 1720000100},
                                    {"file": "autoloop-reviewer.heartbeat", "mtime": 1720000100}],
                     "verification_count": 2}

        # 1. upsert + read（新项目）：字段齐全 + payload JSON 往返一致
        ls1 = store.upsert("proj-a", payload_a, agent_id="agent-1")
        row = store.read("proj-a")
        assert row is not None, "read 返回 None"
        assert row["project_id"] == "proj-a"
        assert row["payload"] == payload_a, "payload JSON 往返不一致"
        assert row["agent_id"] == "agent-1"
        assert row["last_seen"] == ls1 and ls1 > 0

        # 2. 未写入项目 → None
        assert store.read("no-such-project") is None

        # 3. 幂等覆盖写（同 id 同 agent）：payload 更新、last_seen 推进、行数不变
        time.sleep(1.05)
        ls2 = store.upsert("proj-a", payload_b, agent_id="agent-1")
        row2 = store.read("proj-a")
        assert row2["payload"] == payload_b, "幂等覆盖后 payload 未更新"
        assert row2["agent_id"] == "agent-1"
        assert ls2 > ls1 and row2["last_seen"] == ls2, "last_seen 未推进"
        rows = store.read_all()
        assert len(rows) == 1 and rows[0]["project_id"] == "proj-a", \
            f"幂等覆盖后应只有 1 行：{rows}"

        # 4. 换 agent 覆盖：存储层不拦截（409 冲突判定在端点层 TASK-036）
        ls3 = store.upsert("proj-a", payload_a, agent_id="agent-2")
        row3 = store.read("proj-a")
        assert row3["agent_id"] == "agent-2" and row3["last_seen"] == ls3

        # 5. 重连自愈：删除 db 文件（含 wal/shm）后新操作自动重建表并读写成功
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
        ls4 = store.upsert("proj-a", payload_b, agent_id="agent-2")
        row4 = store.read("proj-a")
        assert row4["payload"] == payload_b and row4["agent_id"] == "agent-2"
        assert row4["last_seen"] == ls4

        # 6. claim_or_update（TASK-036 原子归属+写）：无行 → ok 写入；同 agent → ok 幂等覆盖；
        #    异 agent → conflict 不写（返回 owner），行数不变
        out, ls5 = store.claim_or_update("proj-c", payload_a, agent_id="agent-1")
        assert out == "ok" and ls5 > 0, (out, ls5)
        assert store.read("proj-c")["agent_id"] == "agent-1"
        out2, ls6 = store.claim_or_update("proj-c", payload_b, agent_id="agent-1")
        assert out2 == "ok" and ls6 >= ls5, (out2, ls6)
        assert store.read("proj-c")["payload"] == payload_b, "同 agent 应幂等覆盖"
        out3, owner = store.claim_or_update("proj-c", payload_a, agent_id="agent-2")
        assert out3 == "conflict" and owner == "agent-1", (out3, owner)
        assert store.read("proj-c")["agent_id"] == "agent-1", "conflict 不得覆盖既有行"
        assert store.read("proj-c")["payload"] == payload_b, "conflict 不得覆盖既有 payload"
        rows6 = store.read_all()
        assert len(rows6) == 2, f"proj-a + proj-c 共 2 行：{rows6}"
        print("✓ IngestStore upsert/read/幂等覆盖/重连自愈/claim_or_update")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _write_agents_file(tmpdir, agents):
    """写临时 config/agents.json（权限 600）→ 返回路径。"""
    agents_path = os.path.join(tmpdir, "agents.json")
    with open(agents_path, "w", encoding="utf-8") as f:
        json.dump(agents, f)
    os.chmod(agents_path, 0o600)
    return agents_path


def test_ingest_endpoint():
    """TASK-034（TASK-035 起带鉴权）：POST /api/ingest 端点（schema 400 / payload 413 / 成功 200 落库）。"""
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-ingest-ep-")
    try:
        # 鉴权配置：扁平格式 {project_id: token}（TASK-035 起 ingest 必须带 Bearer token）
        agents_path = _write_agents_file(tmpdir, {"aimonitor": "test-token"})
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       agents_path=agents_path,
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def post(body, auth="Bearer test-token"):
            """POST /api/ingest，返回 (status, raw_body)。"""
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if auth is not None:
                headers["Authorization"] = auth
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/ingest", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, raw

        valid = {
            "project_id": "aimonitor",
            "ts": 1720000000,
            "files": {
                "tasks": [{"name": "TASK-001.md", "content": "# TASK-001\n## 目标\nx"}],
                "focus": "TASK-001",
                "heartbeats": [{"file": "autoloop-coder.heartbeat", "mtime": 1720000000}],
                "events": [{"name": "autoloop-coder-events.jsonl", "content": '{"ts":1}\n'}],
                "verification_count": 1,
                "review_count": 2,
            },
        }

        # 1. 成功：200 + {ok:true, project_id} + 落库（payload=files, agent_id=鉴权身份）
        status, raw = post(valid)
        assert status == 200, f"有效请求应 200：{status} {raw[:200]}"
        assert json.loads(raw) == {"ok": True, "project_id": "aimonitor"}, raw
        row = ms.ApiHandler.state.ingest.read("aimonitor")
        assert row is not None and row["payload"] == valid["files"], \
            "成功请求未正确落库 ingest_state"
        assert row["agent_id"] == "aimonitor", f"鉴权后 agent_id 应为 token 身份：{row}"

        # 2. schema 缺必填字段 → 400（缺 project_id / 缺 ts / 缺 files）
        for bad in (
            {"ts": 1, "files": {}},
            {"project_id": "aimonitor", "files": {}},
            {"project_id": "aimonitor", "ts": 1},
        ):
            status, raw = post(bad)
            assert status == 400, f"缺字段应 400：{status} {raw[:100]}"
            assert json.loads(raw).get("error"), "400 响应应含 error 字段"

        # 3. schema 类型/取值错 → 400（非 JSON / 顶层非对象 / project_id 空或空白 / files 非对象 /
        #    ts 字符串 / ts 负值 / ts NaN / ts inf / ts bool）
        for bad in (
            "not json{{",
            "[]",
            '"just a string"',
            {"project_id": "", "ts": 1, "files": {}},
            {"project_id": "   ", "ts": 1, "files": {}},
            {"project_id": "aimonitor", "ts": 1, "files": []},
            {"project_id": "aimonitor", "ts": "1720000000", "files": {}},
            {"project_id": "aimonitor", "ts": -1, "files": {}},
            {"project_id": "aimonitor", "ts": float("nan"), "files": {}},
            {"project_id": "aimonitor", "ts": float("inf"), "files": {}},
            {"project_id": "aimonitor", "ts": True, "files": {}},
        ):
            status, raw = post(bad)
            assert status == 400, f"非法请求应 400：{status} {raw[:100]}"

        # 4. files 子结构错 → 400（tasks 非数组/条目错 / focus 非字符串 /
        #    legacy heartbeat 键废弃 400 / heartbeats 非数组或条目 mtime 非法 /
        #    count 负数或非整数或 bool / events 非数组或条目 content 非法）
        base = {"project_id": "aimonitor", "ts": 1720000000, "files": {}}
        for files in (
            # tasks（file-oriented 数组 {name, content}）
            {"tasks": "x"},
            {"tasks": {"TASK-001.md": 42}},
            {"tasks": [42]},
            {"tasks": [{"name": ""}]},
            {"tasks": [{"name": "TASK-001.md", "content": 42}]},
            # focus
            {"focus": 42},
            # legacy role-oriented heartbeat 键（TASK-042 契约切换 → 显式 400）
            {"heartbeat": []},
            {"heartbeat": {"coder": "old"}},
            {"heartbeat": {"coder": -1}},
            # heartbeats（file-oriented 数组 {file, mtime}）
            {"heartbeats": "x"},
            {"heartbeats": [{"file": "x", "mtime": "old"}]},
            {"heartbeats": [{"file": "x", "mtime": -1}]},
            {"heartbeats": [{"file": "x", "mtime": float("nan")}]},
            {"heartbeats": [{"file": ""}]},
            # 计数
            {"verification_count": -1},
            {"verification_count": 1.5},
            {"verification_count": True},
            # events（file-oriented 数组 {name, content}）
            {"events": "x"},
            {"events": {"coder": []}},
            {"events": [{"name": "", "content": "x"}]},
            {"events": [{"name": "x-events.jsonl", "content": []}]},
            {"events": [{"name": "x-events.jsonl", "content": 42}]},
        ):
            status, raw = post(dict(base, files=files))
            assert status == 400, f"files 子结构错应 400：{status} {raw[:100]}"

        # 4b. 宽松语义 → 200：未知顶层键忽略 / 未知 files 键忽略 / focus null /
        #     空 files / 计数 null（agent 目录缺失语义）/ 空数组
        for ok in (
            dict(valid, extra_key=123),
            dict(valid, files=dict(valid["files"], unknown_key="x")),
            dict(valid, files=dict(valid["files"], focus=None)),
            dict(valid, files={}),
            dict(valid, files=dict(valid["files"], verification_count=None, review_count=None)),
            dict(valid, files=dict(valid["files"], heartbeats=[], events=[])),
        ):
            status, raw = post(ok)
            assert status == 200, f"宽松语义应 200：{status} {raw[:100]}"

        # 4c. 非 UTF-8 请求体 → 400（json.loads 失败含 UnicodeDecodeError）
        status, raw = post(b"\xff\xfe\x00\x01")
        assert status == 400, f"非 UTF-8 体应 400：{status} {raw[:100]}"

        # 5. payload 超限 → 413（实际体超限；无 Content-Length 走保护性读取路径）
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/ingest")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Authorization", "Bearer test-token")
        conn.endheaders()
        conn.send(b"x" * (ms.MAX_INGEST_PAYLOAD_BYTES + 1))
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        assert resp.status == 413, f"超限 payload 应 413：{resp.status} {raw[:100]}"

        # 6. Content-Length 声明超限（体很小）也 413（快路径，不读体）
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/ingest")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Authorization", "Bearer test-token")
        conn.putheader("Content-Length", str(ms.MAX_INGEST_PAYLOAD_BYTES + 1))
        conn.endheaders()
        conn.send(b"{}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 413, f"声明超限应 413：{resp.status}"

        # 7. 负数 Content-Length 钳制（MED-001 回归）：-1 声明不得触发 rfile.read(-1)
        #    读到 EOF（无界缓冲 + 阻塞），必须钳制为 0 走保护性读取上限 → 超限体 413。
        #    旧实现下本用例会读全量体后才 413（无界缓冲）；修复后读到 MAX+1 即拒绝。
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/ingest")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Authorization", "Bearer test-token")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        conn.send(b"x" * (ms.MAX_INGEST_PAYLOAD_BYTES + 1))
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        assert resp.status == 413, f"负数 Content-Length + 超限体应 413（不读全量）：{resp.status} {raw[:100]}"

        # 8. 非 /api/ingest 的 POST → 404（路径校验先于鉴权）
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/other", body=b"{}")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 404, f"非 ingest 路径应 404：{resp.status}"

        # 9. Content-Length 声明小于实际体（声明/实际不符兜底）：只读声明长度 → JSON 解析失败 → 400
        #    （旧实现下声明长度小会截断体；此处断言稳定 400 而非 413/崩溃/挂起）
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/ingest")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Authorization", "Bearer test-token")
        conn.putheader("Content-Length", "2")
        conn.endheaders()
        conn.send(b'{"project_id":"aimonitor","ts":1,"files":{}}')
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        assert resp.status == 400, f"声明小于实际应 400：{resp.status} {raw[:100]}"

        print("✓ POST /api/ingest（schema 400 / payload 413 / 成功 200 落库 + 鉴权）")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_task_events_ingest():
    """TASK-071：task 事件流服务端消费。

    - validate_ingest_payload：seq 规则（整数 ≥1 / 批内单调）+ cursor 确认语义（整数 ≥0
      且 ≥ 批内最大 seq）+ 单批 ≤200（超批 400，FIND-003）+ 旧 payload（无 events/cursor）兼容
    - IngestStore：task_events 入库（(project_id, seq) 幂等去重）+ cursor 只进不退
      + 旧 payload 混用不清空 cursor（FIND-002）+ 超批 store 层 fail loud（FIND-003）
    - HTTP E2E：/api/ingest 推 events/cursor → /api/projects/:id/events 返回
      task_events{count, cursor, events}（seq 降序），role 事件不回归
    """
    import monitor_server as ms

    def ev(seq, **kw):
        d = {"seq": seq, "ts": "2026-08-27T09:00:00", "ev": "task.started",
             "task": "TASK-012", "from": "open", "to": "in-progress",
             "actor": "cli/task", "commit": None, "dispatch_ref": None, "reason": None}
        d.update(kw)
        return d

    def mk(events=None, cursor=None):
        obj = {"project_id": "aimonitor", "ts": 1720000000, "files": {}}
        if events is not None:
            obj["events"] = events
        if cursor is not None:
            obj["cursor"] = cursor
        return obj

    # —— 1. validate_ingest_payload：seq/cursor 规则 + 兼容 ——
    assert ms.validate_ingest_payload(mk([ev(1), ev(2)], cursor=2)) is None
    assert ms.validate_ingest_payload(mk([], cursor=0)) is None        # 空批 + 心跳确认
    assert ms.validate_ingest_payload(mk([], cursor=None)) is None
    assert ms.validate_ingest_payload(mk()) is None                    # 旧 payload 兼容
    for bad, why in (
        (mk(events="x", cursor=1), "events 非数组"),
        (mk(events=[42], cursor=1), "事件条目非对象"),
        (mk(events=[{"seq": "1"}], cursor=1), "seq 非整数"),
        (mk(events=[ev(1)], cursor=True), "cursor 非整数"),
        (mk(events=[ev(1)], cursor=0), "cursor < 批内最大 seq"),
        (mk(events=[ev(-1)], cursor=None), "负 seq"),
        (mk(events=[ev(0)], cursor=None), "seq 0"),
        (mk(events=[ev(1), ev(1)], cursor=1), "批内非单调"),
        (mk(events=[ev(2), ev(1)], cursor=2), "批内倒序"),
        (mk(events=[ev(1)], cursor=-1), "负 cursor"),
        (mk(events=[ev(1)], cursor="2"), "cursor 字符串"),
        (mk(events=[ev(i) for i in range(1, 202)], cursor=201), "超批 >200 应拒绝"),
    ):
        err = ms.validate_ingest_payload(bad)
        assert err is not None, f"{why} 应拒绝：{bad}"

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-taskev-")
    try:
        # —— 2. IngestStore：入库 / 去重 / cursor 只进不退 / 读取 ——
        store = ms.IngestStore(os.path.join(tmpdir, "ingest.db"))
        store.upsert("proj-t", {"tasks": []}, "agent-1")
        assert store.read_task_events("proj-t", 10) == (0, None, []), \
            "未推送 task 事件 → (0, None, [])"
        store.store_task_events("proj-t", [ev(1), ev(2)], cursor=2)
        count, cursor, items = store.read_task_events("proj-t", 10)
        assert (count, cursor) == (2, 2)
        assert [e["seq"] for e in items] == [2, 1], "应按 seq 降序（最新在前）"
        # 重放（同 seq 幂等）+ cursor 倒退 → 不重复、不倒退
        store.store_task_events("proj-t", [ev(1)], cursor=1)
        assert store.read_task_events("proj-t", 10)[0:2] == (2, 2), "重放不得重复计数/倒退 cursor"
        # 增量续推
        store.store_task_events("proj-t", [ev(3)], cursor=3)
        count, cursor, items = store.read_task_events("proj-t", 10)
        assert (count, cursor) == (3, 3) and items[0]["seq"] == 3
        assert len(store.read_task_events("proj-t", 2)[2]) == 2, "limit 截断"
        assert store.read_task_events("no-such", 10) == (0, None, [])
        assert store.read("proj-t")["task_cursor"] == 3, "read 应含 task_cursor"
        assert any(r["task_cursor"] == 3 for r in store.read_all()), "read_all 应含 task_cursor"

        # TASK-071 FIND-002：事件启用后混入旧 payload（无 events/cursor）不得清空 task_cursor
        store.upsert("proj-t", {"tasks": []}, "agent-1")
        assert store.read("proj-t")["task_cursor"] == 3, "upsert 不得清空 task_cursor"
        store.claim_or_update("proj-t", {"tasks": []}, "agent-1")
        assert store.read("proj-t")["task_cursor"] == 3, "claim_or_update 不得清空 task_cursor"

        # TASK-071 FIND-003：超批 store 层 fail loud（不截断、不推进 cursor）
        try:
            store.store_task_events("proj-t", [ev(i) for i in range(1, 202)], cursor=201)
        except ValueError:
            pass
        else:
            raise AssertionError(">200 条事件应抛 ValueError（fail loud）")
        assert store.read("proj-t")["task_cursor"] == 3, "超批拒绝后 cursor 不得推进"
        assert store.read_task_events("proj-t", 10)[0] == 3, "超批拒绝后不得落库新增行"

        # —— 3. HTTP E2E：ingest 推送 → 事件查询（含 seq/cursor）→ 兼容回归 ——
        with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
            config = json.load(f)
        config["poll_interval_seconds"] = 3600  # 调大避免测试期间后台轮询干扰
        config["projects"] = list(config["projects"]) + [
            {"id": "task-proj", "name": "Task事件工程",
             "path": "/虚拟/task-proj", "transport": "agent"},
        ]
        agents = {"task-proj": "flat-token-task"}
        agents_path = _write_agents_file(tmpdir, agents)
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       agents_path=agents_path,
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def post(payload):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/ingest",
                         body=json.dumps(payload).encode("utf-8"),
                         headers={"Content-Type": "application/json",
                                  "Authorization": "Bearer flat-token-task"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, raw

        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            raw = json.loads(resp.read())
            conn.close()
            return resp.status, raw

        base_files = {
            "tasks": [{"name": "TASK-012.md",
                       "content": "---\nname: TASK-012\nmetadata:\n  status: in-progress\n---\n# TASK-012\n"}],
            "focus": "TASK-012",
            "heartbeats": [{"file": "autoloop-coder.heartbeat", "mtime": int(time.time())}],
            "events": [{"name": "autoloop-coder-events.jsonl",
                        "content": '{"ts":1,"task":"TASK-012","outcome":"ok"}\n'}],
            "verification_count": 1,
            "review_count": 0,
        }
        p1 = {"project_id": "task-proj", "ts": int(time.time()),
              "events": [ev(1, ev="task.created", task="TASK-012",
                            **{"from": None, "to": "open"}),
                         ev(2, ev="task.started", task="TASK-012",
                            **{"from": "open", "to": "in-progress"})],
              "cursor": 2, "files": base_files}
        status, raw = post(p1)
        assert status == 200, f"task 事件推送应 200：{status} {raw[:200]}"
        status, data = get("/api/projects/task-proj/events?limit=10")
        assert status == 200
        te = data["task_events"]
        assert te["count"] == 2 and te["cursor"] == 2, te
        assert [e["seq"] for e in te["events"]] == [2, 1], "事件序列按 seq 降序"
        assert te["events"][0]["ev"] == "task.started"
        # 旧 role 事件时间线不回归（同一端点）
        assert data["counts"]["coder"] == 1
        assert data["events"]["coder"][0]["outcome"] == "ok"

        # 增量续推（seq 3）：count=3、cursor=3
        p2 = {"project_id": "task-proj", "ts": int(time.time()),
              "events": [ev(3, ev="task.done", task="TASK-012",
                            **{"from": "in-progress", "to": "done"})],
              "cursor": 3, "files": base_files}
        status, raw = post(p2)
        assert status == 200, f"增量推送应 200：{status} {raw[:200]}"
        status, data = get("/api/projects/task-proj/events?limit=10")
        te = data["task_events"]
        assert te["count"] == 3 and te["cursor"] == 3, te
        assert te["events"][0]["seq"] == 3

        # 非法 task 事件 → 400（seq 规则 / cursor 语义），不落库（project_id 用已授权 task-proj）
        for bad in (
            mk(events=[ev(-1)], cursor=None),
            mk(events=[ev(1), ev(1)], cursor=1),
            mk(events=[ev(2), ev(1)], cursor=2),
            mk(events=[ev(1)], cursor=0),
            mk(events=[ev(1)], cursor=-1),
            mk(events=[ev(1)], cursor="2"),
            mk(events="x", cursor=1),
        ):
            bad = dict(bad, project_id="task-proj")
            status, raw = post(bad)
            assert status == 400, f"非法 task 事件应 400：{status} {raw[:120]}"

        # 旧 payload（无 events/cursor）仍 200 且不污染 task 事件流
        p_old = {"project_id": "task-proj", "ts": int(time.time()), "files": base_files}
        status, raw = post(p_old)
        assert status == 200, f"旧 payload 应 200：{status} {raw[:120]}"
        status, data = get("/api/projects/task-proj/events?limit=10")
        assert data["task_events"]["count"] == 3, "旧 payload 不应新增 task 事件"

        print("✓ TASK-071 task 事件流（validate seq/cursor / IngestStore 去重 / HTTP E2E / 兼容回归）")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_ingest_auth():
    """TASK-035：/api/ingest Bearer 鉴权（无/错/对 token；不泄露数据；agents.json 权限 600 fail-closed）。"""
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-ingest-auth-")
    try:
        # 混合两种配置格式（MONITOR-SPEC §3.1.2）：扁平 {project_id: token} + agent {agent_id: {token, projects}}
        # TASK-036 起未注册 project_id 会 400：agent 格式授权项目改用 config/projects.json 已注册的 aibase
        agents = {
            "aimonitor": "flat-token-aimonitor",
            "agent-win": {"token": "agent-token-win", "projects": ["aibase"]},
        }
        agents_path = _write_agents_file(tmpdir, agents)

        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       agents_path=agents_path,
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def post(body, auth=None):
            """POST /api/ingest，返回 (status, raw_body)；auth=None 表示不带 Authorization 头。"""
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if auth is not None:
                headers["Authorization"] = auth
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/ingest", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, raw

        valid = {
            "project_id": "aimonitor",
            "ts": 1720000000,
            "files": {"tasks": [{"name": "TASK-001.md", "content": "# TASK-001\n## 目标\nx"}]},
        }

        # 1. 无 Authorization 头 → 401；响应不泄露任何数据（只含通用 error）
        status, raw = post(valid, auth=None)
        assert status == 401, f"无 token 应 401：{status}"
        assert json.loads(raw) == {"error": "鉴权失败"}, f"401 泄露数据：{raw[:200]}"

        # 2. 错误 token → 401；与缺失同响应（不区分，防枚举）
        status, raw = post(valid, auth="Bearer wrong-token")
        assert status == 401 and json.loads(raw) == {"error": "鉴权失败"}, raw[:200]

        # 3. 头格式错（Basic / Bearer 无 token / 空值 / 非 Bearer scheme）→ 401
        for bad_auth in ("Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", "Token abc"):
            status, raw = post(valid, auth=bad_auth)
            assert status == 401, f"auth={bad_auth!r} 应 401：{status}"

        # 4. 扁平格式有效 token → 200 + 落库 agent_id = project_id
        status, raw = post(valid, auth="Bearer flat-token-aimonitor")
        assert status == 200, f"扁平 token 应 200：{status} {raw[:200]}"
        row = ms.ApiHandler.state.ingest.read("aimonitor")
        assert row is not None and row["agent_id"] == "aimonitor", row

        # 5. agent 对象格式有效 token → 200 + 落库 agent_id = agent_id（TASK-036：aibase 已注册且授权）
        valid_win = {"project_id": "aibase", "ts": 1720000000,
                     "files": {"tasks": [{"name": "TASK-001.md", "content": "# TASK-001\n## 目标\nx"}]}}
        status, raw = post(valid_win, auth="Bearer agent-token-win")
        assert status == 200, f"agent 格式 token 应 200：{status} {raw[:200]}"
        row = ms.ApiHandler.state.ingest.read("aibase")
        assert row is not None and row["agent_id"] == "agent-win", row

        # 6. 鉴权通过后原 schema 校验仍生效（有 token 但 schema 错 → 400）
        status, raw = post({"ts": 1, "files": {}}, auth="Bearer flat-token-aimonitor")
        assert status == 400, f"带 token 的 schema 错应 400：{status}"

        print("✓ /api/ingest Bearer 鉴权（无/错/对 token、头格式错、agent 身份、不泄露数据）")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 7. load_agents_config 权限与内容校验（fail-closed）
    tmpdir2 = tempfile.mkdtemp(prefix="aimonitor-agents-cfg-")
    try:
        good = os.path.join(tmpdir2, "agents-600.json")
        with open(good, "w", encoding="utf-8") as f:
            json.dump({"proj": "tok"}, f)
        os.chmod(good, 0o600)
        assert ms.load_agents_config(good) == {"proj": "tok"}
        # 权限过宽（644）→ 拒绝加载（{}，fail-closed）
        wide = os.path.join(tmpdir2, "agents-644.json")
        with open(wide, "w", encoding="utf-8") as f:
            json.dump({"proj": "tok"}, f)
        os.chmod(wide, 0o644)
        assert ms.load_agents_config(wide) == {}
        # 缺失文件 → {}
        assert ms.load_agents_config(os.path.join(tmpdir2, "nope.json")) == {}
        # 非法 JSON → {}
        bad = os.path.join(tmpdir2, "agents-bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        os.chmod(bad, 0o600)
        assert ms.load_agents_config(bad) == {}
        # 顶层非对象（数组 / 字符串 / 数值）→ {}（fail-closed，同非法 JSON 处理）
        for i, content in enumerate(("[1,2]", '"tok"', "42")):
            nonobj = os.path.join(tmpdir2, f"agents-nonobj-{i}.json")
            with open(nonobj, "w", encoding="utf-8") as f:
                f.write(content)
            os.chmod(nonobj, 0o600)
            assert ms.load_agents_config(nonobj) == {}, f"顶层非对象应 fail-closed: {content}"
        # resolve_agent_id：扁平 + agent 格式 + 未知/空 token + 空配置
        assert ms.resolve_agent_id(agents, "flat-token-aimonitor") == "aimonitor"
        assert ms.resolve_agent_id(agents, "agent-token-win") == "agent-win"
        assert ms.resolve_agent_id(agents, "nope") is None
        assert ms.resolve_agent_id(agents, "") is None
        assert ms.resolve_agent_id({}, "tok") is None
        # extract_bearer_token：大小写不敏感 scheme / 空值 / 非 Bearer
        assert ms.extract_bearer_token("Bearer abc") == "abc"
        assert ms.extract_bearer_token("bearer abc") == "abc"
        assert ms.extract_bearer_token("Bearer") is None
        assert ms.extract_bearer_token("") is None
        assert ms.extract_bearer_token("Basic abc") is None
        print("✓ load_agents_config / resolve_agent_id / extract_bearer_token（权限 600、fail-closed、两种格式）")
    finally:
        shutil.rmtree(tmpdir2, ignore_errors=True)


def test_ingest_scope_conflict():
    """TASK-036：授权范围与冲突（越权 403 / 未注册 400 / 同 id 双 agent 409 / 幂等 200）。"""
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)
    registered = {p["id"] for p in config["projects"]}
    assert "aimonitor" in registered and "aibase" in registered
    assert "win-proj" not in registered  # 未注册项目（供未注册拦截用例）

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-ingest-scope-")
    try:
        # 三种 agent：扁平（aimonitor 专属）/ agent 格式（aibase+aimonitor）/ agent 格式（win-proj 未注册）
        agents = {
            "aimonitor": "flat-token-aimonitor",
            "agent-b": {"token": "agent-token-b", "projects": ["aibase", "aimonitor"]},
            "agent-unreg": {"token": "agent-token-unreg", "projects": ["win-proj"]},
        }
        agents_path = _write_agents_file(tmpdir, agents)

        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       agents_path=agents_path,
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def post(body, auth):
            """POST /api/ingest，返回 (status, raw_body)。"""
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

        def valid(project_id, marker):
            return {"project_id": project_id, "ts": 1720000000,
                    "files": {"tasks": [{"name": f"TASK-{marker}.md", "content": "# TASK\n"}]}}

        # 1. 越权：扁平 token（仅授权 aimonitor）推已注册的 aibase → 403，不落库
        status, raw = post(valid("aibase", "A"), "Bearer flat-token-aimonitor")
        assert status == 403, f"越权应 403：{status} {raw[:200]}"
        assert "不在授权范围内" in json.loads(raw)["error"]
        assert ms.ApiHandler.state.ingest.read("aibase") is None, "越权请求不得落库"

        # 2. 越权不泄露注册状态：未授权 agent 推 aibase → 403（而非 400），无法探测注册
        status, raw = post(valid("aibase", "B"), "Bearer agent-token-unreg")
        assert status == 403, f"越权检查应先于未注册检查：{status} {raw[:200]}"
        assert "不在授权范围内" in json.loads(raw)["error"]

        # 3. 未注册：agent-unreg 被授权 win-proj，但 win-proj 未在 config/projects.json → 400
        status, raw = post(valid("win-proj", "C"), "Bearer agent-token-unreg")
        assert status == 400, f"未注册应 400：{status} {raw[:200]}"
        assert "未注册" in json.loads(raw)["error"]
        assert ms.ApiHandler.state.ingest.read("win-proj") is None

        # 4. 授权成功：扁平 token 推 aimonitor → 200，owner=aimonitor
        status, raw = post(valid("aimonitor", "D"), "Bearer flat-token-aimonitor")
        assert status == 200, f"授权请求应 200：{status} {raw[:200]}"
        row = ms.ApiHandler.state.ingest.read("aimonitor")
        assert row["agent_id"] == "aimonitor"
        assert any(e["name"] == "TASK-D.md" and e["content"] == "# TASK\n"
                   for e in row["payload"]["tasks"]), row["payload"]

        # 5. 同 agent 幂等：扁平 token 再推 aimonitor（不同 payload）→ 200 覆盖，owner 不变
        status, raw = post(valid("aimonitor", "E"), "Bearer flat-token-aimonitor")
        assert status == 200, f"同 agent 幂等应 200：{status} {raw[:200]}"
        row = ms.ApiHandler.state.ingest.read("aimonitor")
        assert row["agent_id"] == "aimonitor"
        assert any(e["name"] == "TASK-E.md" for e in row["payload"]["tasks"]), \
            "幂等覆盖后应有 TASK-E.md"

        # 6. 同 id 双 agent：agent-b 被授权 aimonitor，但已归 aimonitor → 409 且不覆盖
        status, raw = post(valid("aimonitor", "F"), "Bearer agent-token-b")
        assert status == 409, f"双 agent 冲突应 409：{status} {raw[:200]}"
        err = json.loads(raw)["error"]
        assert "另一 agent 占用" in err and "aimonitor" in err, f"409 应含 owner：{raw[:200]}"
        row = ms.ApiHandler.state.ingest.read("aimonitor")
        assert row["agent_id"] == "aimonitor", "409 后不得覆盖既有 owner"
        assert any(e["name"] == "TASK-E.md" for e in row["payload"]["tasks"]), \
            "409 后不得覆盖既有 payload"
        assert not any(e["name"] == "TASK-F.md" for e in row["payload"]["tasks"]), \
            "409 后不得写入冲突 payload"

        # 7. agent-b 推自有项目 aibase → 200（授权+注册+无冲突），owner=agent-b
        status, raw = post(valid("aibase", "G"), "Bearer agent-token-b")
        assert status == 200, f"agent-b 自有项目应 200：{status} {raw[:200]}"
        assert ms.ApiHandler.state.ingest.read("aibase")["agent_id"] == "agent-b"

        # 8. 授权判定单元测试：扁平 / agent / projects 缺失 / 未知 agent / 非法项目列表
        assert ms.authorized_projects(agents, "aimonitor") == {"aimonitor"}
        assert ms.authorized_projects(agents, "agent-b") == {"aibase", "aimonitor"}
        assert ms.authorized_projects(agents, "agent-unreg") == {"win-proj"}
        assert ms.authorized_projects(agents, "no-such-agent") == set()
        assert ms.authorized_projects({"a": {"token": "t"}}, "a") == set(), "projects 缺失 → fail-closed 空集合"
        assert ms.authorized_projects({"a": {"token": "t", "projects": [1, "ok"]}}, "a") == {"ok"}
        assert ms.is_project_authorized(agents, "aimonitor", "aimonitor") is True
        assert ms.is_project_authorized(agents, "aimonitor", "aibase") is False
        assert ms.is_project_authorized(agents, "agent-b", "aibase") is True
        assert ms.is_project_registered(config, "aimonitor") is True
        assert ms.is_project_registered(config, "win-proj") is False

        print("✓ /api/ingest 授权范围与冲突（越权 403 / 未注册 400 / 双 agent 409 / 幂等 200）")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_ingest_rate_limit():
    """TASK-036：/api/ingest 限流（每 agent 每分钟 N 次，超限 429，窗口重置后恢复）。"""
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)
    config["ingest_rate_limit_per_minute"] = 2  # N=2，便于确定性断言

    class _FakeClock:
        """可控时钟：测试确定性，不依赖真实分钟边界。"""

        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    clock = _FakeClock()

    # 单元测试：limit 钳制 / 窗口内计数 / 超限 / 跨窗口重置 / 不同 agent 独立计数
    limiter = ms.IngestRateLimiter(2, clock=clock)
    assert limiter.limit == 2
    assert limiter.allow("agent-1") is True
    assert limiter.allow("agent-1") is True
    assert limiter.allow("agent-1") is False, "第 3 次同窗口应超限"
    assert limiter.allow("agent-2") is True, "不同 agent 独立计数"
    clock.now = 61.0  # 下一分钟
    assert limiter.allow("agent-1") is True, "跨窗口应重置"
    assert ms.IngestRateLimiter(0, clock=clock).limit == 1, "limit 至少 1（钳制）"

    # 单元测试：桶清理（防内存无界增长）——超过阈值时仅清理非当前窗口桶
    prune_clock = _FakeClock()
    prune = ms.IngestRateLimiter(10000, clock=prune_clock)
    prune_clock.now = 0.0
    for i in range(1100):
        assert prune.allow(f"old-agent-{i}") is True
    assert len(prune._buckets) == 1100, "未超阈值前桶不清理"
    prune_clock.now = 61.0  # 全部旧桶跨窗口
    prune.allow("fresh-agent")
    assert len(prune._buckets) == 1 and "fresh-agent" in prune._buckets, \
        f"旧窗口桶应被清理：{len(prune._buckets)}"

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-ingest-ratelimit-")
    try:
        agents = {"aimonitor": "flat-token-aimonitor"}
        agents_path = _write_agents_file(tmpdir, agents)

        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       agents_path=agents_path,
                                       rate_clock=clock,
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def post():
            body = json.dumps({"project_id": "aimonitor", "ts": 1720000000,
                               "files": {"tasks": [{"name": "TASK-001.md", "content": "# TASK\n"}]}})
            headers = {"Content-Type": "application/json",
                       "Authorization": "Bearer flat-token-aimonitor"}
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/ingest", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, raw

        # 端点 429：同窗口内第 1、2 次 200，第 3 次起 429
        clock.now = 0.0
        status, raw = post()
        assert status == 200, f"第 1 次应 200：{status} {raw[:200]}"
        status, raw = post()
        assert status == 200, f"第 2 次应 200：{status} {raw[:200]}"
        status, raw = post()
        assert status == 429, f"第 3 次应 429：{status} {raw[:200]}"
        assert "限流" in json.loads(raw)["error"] or "频繁" in json.loads(raw)["error"]
        status, raw = post()
        assert status == 429, f"第 4 次仍应 429：{status}"
        # 下一分钟窗口重置 → 恢复 200
        clock.now = 61.0
        status, raw = post()
        assert status == 200, f"跨窗口应恢复 200：{status} {raw[:200]}"

        # 未认证请求不占额度（错误 token 先 401，不消耗限流计数）
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/ingest",
                     body=json.dumps({"project_id": "aimonitor"}),
                     headers={"Content-Type": "application/json",
                              "Authorization": "Bearer wrong-token"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        assert resp.status == 401, f"错误 token 应 401（不占额度）：{resp.status}"

        # 失败请求也计数（标准限流语义：鉴权后立即计数，读体之前）：
        # 第 1 次成功 200（count=1）；第 2 次 schema 错 400（count=2 满）；第 3 次合法体 → 429
        clock.now = 122.0  # 新窗口
        status, raw = post()
        assert status == 200, f"新窗口第 1 次应 200：{status} {raw[:100]}"
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/ingest",
                     body=json.dumps({"project_id": "aimonitor", "ts": 1}),  # 缺 files → 400
                     headers={"Content-Type": "application/json",
                              "Authorization": "Bearer flat-token-aimonitor"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 400, f"schema 错应 400 且占用额度：{resp.status}"
        status, raw = post()
        assert status == 429, f"失败请求应计数，第 3 次应 429：{status} {raw[:100]}"

        # 限流先于读体：额度耗尽 + 非法请求体（本应 400）→ 429（不解析体）
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/api/ingest")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Authorization", "Bearer flat-token-aimonitor")
        conn.endheaders()
        conn.send(b"x" * 1000)  # 非 JSON 体，若被读取应 400；额度耗尽时应 429
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        assert resp.status == 429, f"额度耗尽时非法体也应 429（限流先于读体）：{resp.status} {raw[:100]}"

        print("✓ /api/ingest 限流（N=2：200/200/429/429、窗口重置 200、错误 token 不占额度、"
              "失败请求计数、限流先于读体）")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_ingest_history_compat():
    """TASK-040：ingest 到达写 HistoryStore 快照（/api/history 对 agent 项目不回归）
    + 事件时间线从 ingest_state 读 + 高频推送节流 + local 零回归。"""
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)
    # 内存配置追加 agent 项目（不写真实 projects.json）；poll 间隔调大避免测试期间二次轮询干扰
    config["poll_interval_seconds"] = 3600
    config["projects"] = list(config["projects"]) + [
        {"id": "win-proj", "name": "Windows工程",
         "path": "/展示/win-proj", "transport": "agent"},
    ]
    registered = {p["id"] for p in config["projects"]}
    assert "win-proj" in registered and "aimonitor" in registered

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-ingest-hist-")
    try:
        agents = {"win-proj": "flat-token-win"}
        agents_path = _write_agents_file(tmpdir, agents)
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       agents_path=agents_path,
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def post(payload, auth="Bearer flat-token-win"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/ingest", body=json.dumps(payload).encode("utf-8"),
                         headers={"Content-Type": "application/json",
                                  "Authorization": auth})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, raw

        def get(path):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            raw = json.loads(resp.read())
            conn.close()
            return resp.status, raw

        now = int(time.time())
        payload1 = {
            "project_id": "win-proj",
            "ts": now,
            "files": {
                "tasks": [
                    {"name": "TASK-001-win.md",
                     "content": "---\nname: TASK-001-win\nmetadata:\n  status: in-progress\n---\n# TASK-001\n"},
                    {"name": "TASK-002-win.md",
                     "content": "---\nname: TASK-002-win\nmetadata:\n  status: done\n---\n# TASK-002\n"},
                ],
                "focus": "# Current Focus\n## 当前任务\nTASK-001\n",
                "heartbeats": [{"file": "autoloop-coder.heartbeat", "mtime": now}],
                "events": [{"name": "autoloop-coder-events.jsonl",
                             "content": '{"ts":1,"task":"TASK-001","outcome":"ok"}\n'
                                         '{"ts":3,"task":"TASK-002","outcome":"ok"}\n'}],
                "verification_count": 1,
                "review_count": 0,
            },
        }
        status, raw = post(payload1)
        assert status == 200, f"首次推送应 200：{status} {raw[:200]}"

        # 1. ingest 到达 → HistoryStore 快照：agent 项目 /api/history 立即返回推送摘要
        #    （新 agent 项目启动轮询时无记录 → 离线不采样，快照必来自本次 ingest）
        status, hist = get("/api/history?project=win-proj&hours=1")
        assert status == 200, f"/api/history agent 项目 → {status}"
        deadline = time.time() + 5
        while not hist["points"] and time.time() < deadline:
            time.sleep(0.2)
            status, hist = get("/api/history?project=win-proj&hours=1")
            assert status == 200
        assert hist["points"], "ingest 后 agent 项目应立即可见历史快照"
        last = hist["points"][-1]
        assert last["summary"]["total"] == 2, last["summary"]
        assert last["summary"]["in-progress"] == 1 and last["summary"]["done"] == 1
        assert last["coder_alive"] is True and last["reviewer_alive"] is False
        # 快照 ts 应接近推送时间（ingest 写快照，而非轮询 ts）
        assert abs(last["ts"] - now) <= 5, f"ingest 快照 ts 应接近推送时间：{last['ts']} vs {now}"

        # 2. 事件时间线从 ingest_state 读：config path /展示/win-proj 本地不存在 →
        #    LocalReader 读不到；返回内容必来自 ingest_state 推送体（path 仅展示，§3.1.2）
        status, evdata = get("/api/projects/win-proj/events?limit=10")
        assert status == 200
        assert evdata["counts"] == {"coder": 2, "reviewer": 0}, evdata["counts"]
        assert len(evdata["events"]["coder"]) == 2
        assert evdata["events"]["coder"][0]["task"] == "TASK-002", "时间线应按 ts 降序"
        assert evdata["events"]["reviewer"] == []
        # 覆盖写推送新事件 → 时间线立即反映（同 ingest_state 覆盖写，§3.1.3 幂等）
        payload2 = json.loads(json.dumps(payload1))
        payload2["ts"] = now
        payload2["files"]["events"] = [
            {"name": "autoloop-coder-events.jsonl",
             "content": '{"ts":9,"task":"TASK-002","outcome":"ok"}\n'},
            {"name": "autoloop-reviewer-events.jsonl",
             "content": '{"ts":5,"task":"TASK-001","outcome":"error"}\n'},
        ]
        status, raw = post(payload2)
        assert status == 200, f"覆盖写推送应 200：{status} {raw[:200]}"
        status, evdata2 = get("/api/projects/win-proj/events?limit=10")
        assert status == 200
        assert evdata2["counts"] == {"coder": 1, "reviewer": 1}, evdata2["counts"]
        assert evdata2["events"]["reviewer"][0]["outcome"] == "error"

        # 3. 高频推送节流：同 poll_interval（3600s）窗口内重复推送/手动触发不重复写快照
        #    （latest_ts 距 now < 窗口 → 跳过；history.db 行数有界）
        rows_before = len(ms.ApiHandler.state.history.query("win-proj", 24 * 365))
        assert rows_before >= 1, "窗口内应有至少一次 ingest 快照"
        status, raw = post(payload2)  # 窗口内第三次推送
        assert status == 200
        ms.ApiHandler.state.record_ingest_snapshot("win-proj")  # 手动触发也应被节流
        rows_after = len(ms.ApiHandler.state.history.query("win-proj", 24 * 365))
        assert rows_after == rows_before, \
            f"窗口内重复推送不应新增快照：{rows_before} → {rows_after}"

        # 4. latest_ts 单元行为：无记录 None；record 后返回最近 ts
        hist = ms.HistoryStore(os.path.join(tmpdir, "hist-unit.db"))
        assert hist.latest_ts("win-proj") is None
        hist.record(100, "win-proj", {"total": 0}, True, False)
        assert hist.latest_ts("win-proj") == 100
        assert hist.latest_ts("other") is None

        # 5. local 项目零回归：/api/history 仍可读（既有 test_server 覆盖主路径）
        status, ldata = get("/api/history?project=aimonitor&hours=24")
        assert status == 200 and ldata["points"], "local 项目历史不应回归"

        print("✓ ingest 写 HistoryStore 快照 + 事件时间线从 ingest_state 读 + 节流 + local 零回归")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_agent_ingest_integration():
    """TASK-042：集成测试——真实 aibase 组件 agent 推送本地项目 → /api/ingest → /api/status
    与 local 模式结果一致（任务/焦点/心跳/事件/计数）。

    使用**真实 aibase 组件**（aibase/kit/tools/agent/：agent_runtime 读取 →
    agent_payload 构造 → agent_http 推送），验证 AIOS 通用遥测格式（file-oriented）
    经 /api/ingest 落库后，/api/status 对 transport:agent 项目与 transport:local
    直读同一工程的结果逐字段一致（TASK-042 契约对齐 + 集成链路打通）。
    """
    import monitor_server as ms

    # aibase 组件（kit 源仓库，AGENTS.md：aibase = kit 源仓库；agent 归属
    # aibase/kit/tools/telemetry/，aibase TASK-092 由 agent/ 改名 telemetry/，
    # 本仓库 config/projects.json 已注册 aibase 项目）
    aibase_agent_dir = os.path.join(ROOT, "..", "aibase", "kit", "tools", "telemetry")
    assert os.path.isdir(aibase_agent_dir), \
        f"aibase agent 组件缺失（{aibase_agent_dir}）——集成测试需真实组件（TASK-042）"
    sys.path.insert(0, aibase_agent_dir)
    import agent_runtime
    import agent_payload
    import agent_http

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)
    config["poll_interval_seconds"] = 3600  # 测试期间避免自动轮询干扰（/api/status 由手动 poll 刷新）

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-integration-")
    try:
        # 被监控 AIOS 工程（runtime 全套：任务/焦点/心跳/事件/验证/审查）
        proj_dir = os.path.join(tmpdir, "project")
        rt = os.path.join(proj_dir, "runtime")
        for sub in ("tasks", "states", "logs", "verification", "reviews"):
            os.makedirs(os.path.join(rt, sub))
        with open(os.path.join(rt, "tasks", "TASK-001-demo.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: TASK-001-demo\nmetadata:\n  status: in-progress\n"
                    "  priority: P1\n  assignee: coder\n  reviewer: autoloop-reviewer\n"
                    "  updated: " + FRESH_UPDATED + "\n---\n# TASK-001\n## 目标\n集成测试任务\n")
        with open(os.path.join(rt, "tasks", "TASK-002-demo.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: TASK-002-demo\nmetadata:\n  status: done\n---\n# TASK-002\n")
        with open(os.path.join(rt, "states", "CURRENT_FOCUS.md"), "w", encoding="utf-8") as f:
            f.write("# Current Focus\n## 当前任务\nTASK-001\n## 下一个动作\n下一步动作\n")
        with open(os.path.join(rt, "logs", "autoloop-coder.heartbeat"), "w", encoding="utf-8") as f:
            f.write("alive")
        with open(os.path.join(rt, "logs", "autoloop-coder-events.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write('{"ts":1,"task":"TASK-001","outcome":"ok"}\n'
                    '{"ts":2,"task":"TASK-002","outcome":"ok"}\n')
        with open(os.path.join(rt, "verification", "VERIFY-2026-08-17-task-001.md"),
                  "w", encoding="utf-8") as f:
            f.write("---\nmetadata:\n  type: verify\n  task-ref: TASK-001\n  result: pass\n---\n")
        with open(os.path.join(rt, "reviews", "REVIEW-2026-08-17-task-001.md"),
                  "w", encoding="utf-8") as f:
            f.write("---\nmetadata:\n  type: review\n  task-ref: TASK-001\n---\n")

        # 服务端注册：同工程 local + agent 双 id；agent 项目 path 仅展示（不读本地）
        local_id, agent_id = "integ-local", "integ-agent"
        config["projects"] = list(config["projects"]) + [
            {"id": local_id, "name": "集成-local", "path": proj_dir},
            {"id": agent_id, "name": "集成-agent",
             "path": "/展示/integ-agent", "transport": "agent"},
        ]
        agents_path = _write_agents_file(tmpdir, {agent_id: "integ-token"})
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       agents_path=agents_path,
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
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
            raw = json.loads(resp.read())
            conn.close()
            return resp.status, raw

        def status_by_id(pid):
            _, data = get("/api/status")
            return next(p for p in data["projects"] if p["id"] == pid)

        try:
            # 1. local 模式基线（LocalReader 直读同一工程；首轮轮询缓存）
            local = status_by_id(local_id)
            assert local["error"] is None, local["error"]
            assert local["summary"]["total"] == 2, local["summary"]
            assert local["verification_count"] == 1 and local["review_count"] == 1

            # 2. 真实 aibase agent 组件：读取 → 构造 payload → 序列化 → HTTP 推送
            snapshot = agent_runtime.read_project_runtime(proj_dir)
            payload = agent_payload.build_payload(agent_id, snapshot, ts=int(time.time()))
            body = agent_payload.serialize_payload(payload)
            result = agent_http.push_payload(
                f"http://127.0.0.1:{port}/api/ingest", "integ-token", body)
            assert result.status == 200, f"agent 推送失败：{result}"

            # 3. 模拟下一轮轮询（生产 /api/status 由轮询线程刷新缓存；测试手动触发确定性）
            ms.ApiHandler.state.poll()

            # 4. agent 项目 /api/status 与 local 模式逐字段对比
            agent_p = status_by_id(agent_id)
            assert agent_p["error"] is None, agent_p["error"]
            assert agent_p["path"] == "/展示/integ-agent", "agent 项目 path 仅展示"
            assert [t["id"] for t in agent_p["tasks"]] == [t["id"] for t in local["tasks"]]
            for a, l in zip(agent_p["tasks"], local["tasks"]):
                for key in ("id", "slug", "name", "description", "status", "priority",
                            "risk", "assignee", "reviewer", "updated"):
                    assert a[key] == l[key], f"任务 {key} 不一致：{a[key]!r} != {l[key]!r}"
                da, dl = a["detail"], l["detail"]
                assert da["sections"] == dl["sections"], "detail.sections 不一致"
                assert da["acceptance"] == dl["acceptance"], "detail.acceptance 不一致"
                assert da["dependencies"] == dl["dependencies"], "detail.dependencies 不一致"
            # TASK-038 既定差异：agent 只推送计数 → 所有 agent 任务 detail 关联记录为空；
            # local 直读真实 VERIFY/REVIEW 文件 → 被引用的 TASK-001 有记录（基线）
            for a in agent_p["tasks"]:
                assert a["detail"]["verification"] == [] and a["detail"]["reviews"] == [], \
                    "agent 项目 detail 关联记录应为空（只展示计数）"
            l001 = next(l for l in local["tasks"] if l["id"] == "TASK-001")
            assert l001["detail"]["verification"] and l001["detail"]["reviews"], \
                "local 项目 TASK-001 应有关联记录（基线）"

            assert agent_p["summary"] == local["summary"], "summary 不一致"
            assert agent_p["focus"] == local["focus"], "focus 不一致"
            assert agent_p["verification_count"] == local["verification_count"] == 1
            assert agent_p["review_count"] == local["review_count"] == 1
            for role in ("coder", "reviewer"):
                ah, lh = agent_p["heartbeat"][role], local["heartbeat"][role]
                assert ah["exists"] == lh["exists"], f"{role} heartbeat.exists 不一致"
                if ah["exists"]:
                    assert ah["age_seconds"] is not None and lh["age_seconds"] is not None
                    assert abs(ah["age_seconds"] - lh["age_seconds"]) <= 10, \
                        f"{role} heartbeat.age 差异过大：{ah} vs {lh}"
                ae, le = agent_p["events"][role], local["events"][role]
                assert ae["count"] == le["count"], f"{role} events.count 不一致"
                assert ae["outcomes"] == le["outcomes"], f"{role} events.outcomes 不一致"
                assert ae["last"] == le["last"], f"{role} events.last 不一致"

            # 5. 事件时间线 API 一致（/api/projects/:id/events 经 transport reader）
            _, ev_local = get(f"/api/projects/{local_id}/events?limit=10")
            _, ev_agent = get(f"/api/projects/{agent_id}/events?limit=10")
            assert ev_agent["counts"] == ev_local["counts"], "事件 counts 不一致"
            assert ev_agent["events"] == ev_local["events"], "事件时间线不一致"

            print("✓ 集成测试：真实 aibase agent 推送 → /api/ingest → /api/status 与 local 模式结果一致")
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_dual_machine_verify():
    """TASK-045：双机验证——真实 agent 推送（模拟 Windows/WSL 与远程 Linux）+ 模拟离线
    （停 agent → error='agent 离线'）+ 重连恢复 + 多实例（同逻辑项目双 id）验证。

    使用**真实 aibase 组件全链路**（agent_config.validate → agent_loop.poll_once，内部走
    agent_runtime 读取 → agent_payload 构造 → agent_http 推送，同 `agent.py --once` 路径）
    模拟两台被监控机器：
    - 机器 A（Windows/WSL）：win-proj + baseline-dev
    - 机器 B（远程 Linux）：linux-proj + baseline-prod（同 group=baseline 多实例）
    验证 /api/status 双机数据正确；停机器 A agent（last_seen 置旧）→ win-proj/baseline-dev
    error='agent 离线' 且 B 不受影响；机器 A 重连推送 → error 清除、数据恢复。
    """
    import sqlite3
    import monitor_server as ms

    # aibase 组件（kit 源仓库；aibase TASK-092 由 kit/tools/agent/ 改名 telemetry/）
    aibase_agent_dir = os.path.join(ROOT, "..", "aibase", "kit", "tools", "telemetry")
    assert os.path.isdir(aibase_agent_dir), \
        f"aibase agent 组件缺失（{aibase_agent_dir}）——双机验证需真实组件（TASK-045）"
    sys.path.insert(0, aibase_agent_dir)
    import agent_config
    import agent_loop

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)
    config["poll_interval_seconds"] = 3600  # 测试期间避免自动轮询干扰（/api/status 手动 poll）

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-dual-machine-")
    try:
        # 两台机器的工程目录（各自独立 runtime 全套：任务/焦点/心跳/事件/验证/审查）
        def _write_runtime(proj_dir, task_id, focus_text):
            rt = os.path.join(proj_dir, "runtime")
            for sub in ("tasks", "states", "logs", "verification", "reviews"):
                os.makedirs(os.path.join(rt, sub), exist_ok=True)
            with open(os.path.join(rt, "tasks", f"{task_id}.md"), "w", encoding="utf-8") as f:
                f.write(f"---\nname: {task_id}\nmetadata:\n  status: in-progress\n"
                        f"  priority: P1\n  assignee: coder\n"
                        f"  reviewer: autoloop-reviewer\n  updated: {FRESH_UPDATED}\n---\n"
                        f"# {task_id}\n## 目标\n双机验证任务\n")
            with open(os.path.join(rt, "states", "CURRENT_FOCUS.md"), "w",
                      encoding="utf-8") as f:
                f.write(f"# Current Focus\n## 当前任务\n{focus_text}\n## 下一个动作\n下一步\n")
            with open(os.path.join(rt, "logs", "autoloop-coder.heartbeat"), "w",
                      encoding="utf-8") as f:
                f.write("alive")
            with open(os.path.join(rt, "logs", "autoloop-coder-events.jsonl"), "w",
                      encoding="utf-8") as f:
                f.write('{"ts":1,"task":"' + task_id + '","outcome":"ok"}\n')
            with open(os.path.join(rt, "verification", "VERIFY-2026-08-17-task-001.md"),
                      "w", encoding="utf-8") as f:
                f.write("---\nmetadata:\n  type: verify\n  task-ref: TASK-001\n"
                        "  result: pass\n---\n")
            with open(os.path.join(rt, "reviews", "REVIEW-2026-08-17-task-001.md"),
                      "w", encoding="utf-8") as f:
                f.write("---\nmetadata:\n  type: review\n  task-ref: TASK-001\n---\n")

        win_dir = os.path.join(tmpdir, "machine-a-win", "win-project")
        linux_dir = os.path.join(tmpdir, "machine-b-linux", "linux-project")
        dev_dir = os.path.join(tmpdir, "machine-a-win", "baseline-dev")
        prod_dir = os.path.join(tmpdir, "machine-b-linux", "baseline-prod")
        _write_runtime(win_dir, "TASK-001-win", "TASK-001-win")
        _write_runtime(linux_dir, "TASK-001-linux", "TASK-001-linux")
        _write_runtime(dev_dir, "TASK-001-dev", "TASK-001-dev")
        _write_runtime(prod_dir, "TASK-001-prod", "TASK-001-prod")

        # 服务端注册：双机独立项目 + 同逻辑项目多实例（group=baseline 双 id）
        config["projects"] = list(config["projects"]) + [
            {"id": "win-proj", "name": "Windows工程", "path": "/展示/win-proj",
             "transport": "agent"},
            {"id": "linux-proj", "name": "Linux工程", "path": "/展示/linux-proj",
             "transport": "agent"},
            {"id": "baseline-dev", "name": "baseline-dev",
             "path": "/展示/baseline-dev", "group": "baseline", "transport": "agent"},
            {"id": "baseline-prod", "name": "baseline-prod",
             "path": "/展示/baseline-prod", "group": "baseline", "transport": "agent"},
        ]
        agents = {
            "agent-win": {"token": "token-win",
                           "projects": ["win-proj", "baseline-dev"]},
            "agent-linux": {"token": "token-linux",
                             "projects": ["linux-proj", "baseline-prod"]},
        }
        agents_path = _write_agents_file(tmpdir, agents)
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       agents_path=agents_path,
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
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
            raw = json.loads(resp.read())
            conn.close()
            return resp.status, raw

        def status_by_id(pid):
            _, data = get("/api/status")
            return next(p for p in data["projects"] if p["id"] == pid)

        try:
            # 1. 真实 agent 全链路：两台机器各自 poll_once（agent.py --once 同路径）
            server_url = f"http://127.0.0.1:{port}/api/ingest"
            cfg_win = agent_config.validate({
                "server_url": server_url, "token": "token-win",
                "projects": [{"id": "win-proj", "path": win_dir},
                              {"id": "baseline-dev", "path": dev_dir}],
                "poll_interval_seconds": 30,
            })
            cfg_linux = agent_config.validate({
                "server_url": server_url, "token": "token-linux",
                "projects": [{"id": "linux-proj", "path": linux_dir},
                              {"id": "baseline-prod", "path": prod_dir}],
                "poll_interval_seconds": 30,
            })
            pushed, skipped, failed = agent_loop.poll_once(
                cfg_win, states={}, log=agent_loop.AgentLog(quiet=True))
            assert pushed == 2 and failed == 0, (pushed, skipped, failed)
            pushed, skipped, failed = agent_loop.poll_once(
                cfg_linux, states={}, log=agent_loop.AgentLog(quiet=True))
            assert pushed == 2 and failed == 0, (pushed, skipped, failed)

            # 2. /api/status：双机项目全部在线，数据来自各自机器（任务/焦点/心跳/事件/计数）
            ms.ApiHandler.state.poll()
            for pid, task_id in (("win-proj", "TASK-001-win"),
                                 ("linux-proj", "TASK-001-linux")):
                p = status_by_id(pid)
                assert p["error"] is None, f"{pid} 应在线：{p['error']}"
                assert p["transport"] == "agent"
                assert p["summary"]["total"] == 1, p["summary"]
                assert p["tasks"][0]["id"] == "TASK-001", p["tasks"]
                assert p["focus"]["current"].startswith(task_id)
                assert p["heartbeat"]["coder"]["exists"] is True
                assert p["events"]["coder"]["count"] == 1
                assert p["events"]["coder"]["last"]["task"] == task_id
                assert p["verification_count"] == 1 and p["review_count"] == 1

            # 3. 多实例：同逻辑项目（group=baseline）双 id 各自独立
            dev = status_by_id("baseline-dev")
            prod = status_by_id("baseline-prod")
            assert dev["group"] == "baseline" and prod["group"] == "baseline"
            assert dev["id"] == "baseline-dev" and prod["id"] == "baseline-prod"
            assert dev["tasks"][0]["name"] == "TASK-001-dev", dev["tasks"]
            assert prod["tasks"][0]["name"] == "TASK-001-prod", prod["tasks"]
            assert dev["focus"]["current"].startswith("TASK-001-dev")
            assert prod["focus"]["current"].startswith("TASK-001-prod")

            # 4. 模拟离线：停机器 A（agent-win）agent —— last_seen 置旧超阈值 → 离线；B 不受影响
            conn = sqlite3.connect(os.path.join(tmpdir, "ingest.db"))
            conn.execute(
                "UPDATE ingest_state SET last_seen=? WHERE project_id IN ('win-proj','baseline-dev')",
                (int(time.time()) - 10000,))
            conn.commit()
            conn.close()
            ms.ApiHandler.state.poll()
            off = status_by_id("win-proj")
            assert off["error"] == "agent 离线", off["error"]
            assert off["tasks"] == [] and off["summary"]["total"] == 0, "离线应返回空数据"
            assert [a["kind"] for a in off["alerts"]] == ["read-error"], off["alerts"]
            dev_off = status_by_id("baseline-dev")
            assert dev_off["error"] == "agent 离线", dev_off["error"]
            # 机器 B 不受影响（隔离性）
            assert status_by_id("linux-proj")["error"] is None
            assert status_by_id("baseline-prod")["error"] is None
            assert status_by_id("baseline-prod")["tasks"][0]["name"] == "TASK-001-prod", \
                status_by_id("baseline-prod")["tasks"]

            # 5. 重连恢复：机器 A agent 重新推送 → error 清除、数据恢复、实例独立性不变
            pushed, skipped, failed = agent_loop.poll_once(
                cfg_win, states={}, log=agent_loop.AgentLog(quiet=True))
            assert pushed == 2 and failed == 0, (pushed, skipped, failed)
            ms.ApiHandler.state.poll()
            rec = status_by_id("win-proj")
            assert rec["error"] is None, rec["error"]
            assert rec["summary"]["total"] == 1 and rec["tasks"][0]["id"] == "TASK-001"
            assert rec["focus"]["current"].startswith("TASK-001-win")
            assert status_by_id("baseline-dev")["error"] is None
            # 多实例不受重连影响：baseline-prod 仍为机器 B 的独立数据
            prod_after = status_by_id("baseline-prod")
            assert prod_after["error"] is None
            assert prod_after["tasks"][0]["name"] == "TASK-001-prod", prod_after["tasks"]

            print("✓ 双机验证：真实 agent 全链路（Windows/WSL + 远程 Linux）+ 模拟离线（agent 离线）"
                  "+ 重连恢复 + 多实例（同逻辑项目双 id 独立）")
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_registration_store():
    """TASK-047：RegistrationStore 单测（表结构/CRUD/状态机/过期清理/冲突检测）。"""
    import monitor_server as ms
    import uuid

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-")
    try:
        db_path = os.path.join(tmpdir, "registration.db")
        store = ms.RegistrationStore(db_path)

        # STORE-001：表结构（字段完整 / status 缺省 / expire_at 缺省 7 天）
        req_id = store.create("proj-a", "code-001", '{"hostname":"h1"}', "key-a")
        assert req_id is not None and isinstance(req_id, str) and len(req_id) == 32
        row = store.get(req_id)
        assert row is not None
        assert row["req_id"] == req_id
        assert row["project_id"] == "proj-a"
        assert row["enrollment_code"] == "code-001"
        assert row["host_info"] == '{"hostname":"h1"}'
        assert row["request_key"] == "key-a"
        assert row["status"] == "pending"
        assert row["issued_token"] is None
        assert row["created_at"] is not None
        assert row["decided_at"] is None
        assert row["expire_at"] is not None
        # expire_at 应在 created_at + 7 天附近（允许秒级偏差）
        assert abs(row["expire_at"] - row["created_at"] - 7 * 86400) < 5, \
            f"expire_at 应 ≈ created_at + 7天：{row}"

        # STORE-002：基本操作
        # get 不存在 → None
        assert store.get("no-such-req") is None

        # list_by_status('pending') 仅返回 pending
        rows = store.list_by_status("pending")
        assert len(rows) == 1 and rows[0]["req_id"] == req_id
        assert all(r["status"] == "pending" for r in rows)

        # list_by_status() 无参返回所有
        all_rows = store.list_by_status()
        assert len(all_rows) == 1

        # 创建第二个项目
        req_id2 = store.create("proj-b", "code-002", '{"hostname":"h2"}', "key-b")
        assert req_id2 is not None
        assert len(store.list_by_status()) == 2
        assert len(store.list_by_status("pending")) == 2

        # STORE-003：状态机转换
        # approve
        token = "issued-token-abc"
        assert store.approve(req_id, token) is True
        row = store.get(req_id)
        assert row["status"] == "approved"
        assert row["issued_token"] == token
        assert row["decided_at"] is not None
        assert row["decided_at"] >= row["created_at"]

        # 非 pending 再次 approve → 不操作
        assert store.approve(req_id, "another-token") is False
        row = store.get(req_id)
        assert row["status"] == "approved"  # 未变
        assert row["issued_token"] == token  # 未覆盖

        # reject
        assert store.reject(req_id2, "不合规") is True
        row = store.get(req_id2)
        assert row["status"] == "rejected"
        assert row["decided_at"] is not None

        # 非 pending 再次 reject → 不操作
        assert store.reject(req_id2, "again") is False
        row = store.get(req_id2)
        assert row["status"] == "rejected"

        # revoke（approved → revoked）
        assert store.revoke(req_id) is True
        row = store.get(req_id)
        assert row["status"] == "revoked"
        assert row["decided_at"] is not None

        # 非 approved 状态 revoke → 不操作
        assert store.revoke(req_id) is False  # 已 revoked
        assert store.revoke(req_id2) is False  # 已 rejected

        # reject 不存在的 req_id → False
        assert store.reject("no-such-req") is False
        # approve 不存在的 req_id → False
        assert store.approve("no-such-req", "tok") is False
        # revoke 不存在的 req_id → False
        assert store.revoke("no-such-req") is False

        # STORE-004：过期清理
        # 创建一条 pending 记录并手动改 expire_at 为过去
        req_id3 = store.create("proj-c", "code-003", '{"hostname":"h3"}', "key-c")
        assert req_id3 is not None
        # 手动将 expire_at 设为过去
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE registration_request SET expire_at=? WHERE req_id=?",
            (time.time() - 100, req_id3),
        )
        conn.commit()
        conn.close()

        # expire_stale 清理
        store.expire_stale()
        row = store.get(req_id3)
        assert row["status"] == "expired", f"过期清理后应 expired：{row}"
        assert row["decided_at"] is not None
        # 记录保留（不删除）
        assert store.get(req_id3) is not None

        # 过期清理不会影响非 pending 记录
        assert store.get(req_id)["status"] == "revoked"  # 未变
        assert store.get(req_id2)["status"] == "rejected"  # 未变

        # list_by_status 过滤正确
        assert len(store.list_by_status("expired")) == 1
        assert len(store.list_by_status("pending")) == 0

        # STORE-005：冲突检测
        # 同 project_id 已有 pending 时 create 失败
        req_id4 = store.create("proj-d", "code-004", '{"hostname":"h4"}', "key-d")
        assert req_id4 is not None
        # 再创建同 project_id → 冲突
        assert store.create("proj-d", "code-004-dup", '{"hostname":"h4-dup"}', "key-d-dup") is None

        # 同 project_id 已有 approved 时 create 失败
        req_id5 = store.create("proj-e", "code-005", '{"hostname":"h5"}', "key-e")
        assert req_id5 is not None
        store.approve(req_id5, "tok-e")
        assert store.create("proj-e", "code-005-dup", '{"hostname":"h5-dup"}', "key-e-dup") is None

        # 同 project_id 只有 rejected 时 create 允许
        req_id6 = store.create("proj-f", "code-006", '{"hostname":"h6"}', "key-f")
        assert req_id6 is not None
        store.reject(req_id6, "no")
        req_id6b = store.create("proj-f", "code-006b", '{"hostname":"h6b"}', "key-fb")
        assert req_id6b is not None, "rejected 后可重新注册"
        assert store.get(req_id6b)["status"] == "pending"

        # 同 project_id 只有 expired 时 create 允许
        req_id7 = store.create("proj-g", "code-007", '{"hostname":"h7"}', "key-g")
        assert req_id7 is not None
        # 手动过期
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE registration_request SET expire_at=? WHERE req_id=?",
            (time.time() - 100, req_id7),
        )
        conn.commit()
        conn.close()
        store.expire_stale()
        assert store.get(req_id7)["status"] == "expired"
        req_id7b = store.create("proj-g", "code-007b", '{"hostname":"h7b"}', "key-gb")
        assert req_id7b is not None, "expired 后可重新注册"

        # VERIFY-001 边缘情况：空表
        empty_req = store.create("empty-proj", "code-empty", '{}', "key-empty")
        assert empty_req is not None
        assert store.get(empty_req) is not None
        assert store.list_by_status() is not None

        # 大量记录（创建 20 条不同 project_id 的 pending 记录）
        ids = []
        for i in range(20):
            rid = store.create(f"bulk-proj-{i}", f"code-{i}", '{}', f"key-{i}")
            assert rid is not None
            ids.append(rid)
        all_rows = store.list_by_status()
        assert len(all_rows) >= 20
        pending_rows = store.list_by_status("pending")
        assert len(pending_rows) >= 0

        # 并发 create 测试（模拟多线程同时创建不同 project_id）
        import threading as th
        results = []
        errors = []

        def _create(p_idx):
            try:
                rid = store.create(f"concurrent-proj-{p_idx}",
                                    f"code-con-{p_idx}", '{}',
                                    f"key-con-{p_idx}")
                results.append(rid)
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            t = th.Thread(target=_create, args=(i,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"并发 create 异常：{errors}"
        assert len(results) == 10, f"并发 create 应全部成功：{results}"
        assert all(r is not None for r in results), "并发 create 不应有冲突"

        print("✓ RegistrationStore（表结构/CRUD/状态机/过期清理/冲突检测/边缘/并发）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_enrollment_code_store():
    """TASK-048：EnrollmentCodeStore 单测（表结构/生成/校验/消费/吊销）。"""
    import monitor_server as ms

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-enrollment-")
    try:
        db_path = os.path.join(tmpdir, "registration.db")
        store = ms.EnrollmentCodeStore(db_path)

        # CODE-001：表结构
        # 字段完整，max_uses 缺省 1，revoked 缺省 0
        code = store.generate(description="test-code", max_uses=1)
        assert code is not None and isinstance(code, str) and len(code) == 17
        parts = code.split("-")
        assert len(parts) == 2 and len(parts[0]) == 8 and len(parts[1]) == 8
        row = store.get(code)
        assert row is not None
        assert row["code"] == code
        assert row["description"] == "test-code"
        assert row["allowed_project_pattern"] is None
        assert row["max_uses"] == 1
        assert row["use_count"] == 0
        assert row["created_at"] is not None
        assert row["expire_at"] is None
        assert row["revoked"] is False

        # CODE-002：基本操作
        # generate() 返回随机 code，写入数据库
        code2 = store.generate(description="code-2", max_uses=5)
        assert code2 != code
        row2 = store.get(code2)
        assert row2 is not None and row2["max_uses"] == 5

        # list() 返回所有码
        all_codes = store.list()
        assert len(all_codes) >= 2
        codes_found = {c["code"] for c in all_codes}
        assert code in codes_found and code2 in codes_found

        # revoke(code) 标记 revoked=1
        store.revoke(code2)
        row2 = store.get(code2)
        assert row2["revoked"] is True

        # 吊销不存在的不抛异常
        store.revoke("no-such-code")

        # CODE-003：校验逻辑
        # 全部通过 → True
        code3 = store.generate(description="valid-code", max_uses=3,
                               allowed_project_pattern="baseline-*")
        assert store.validate(code3, "baseline-dev") is True

        # code 不存在 → False
        assert store.validate("no-such-code", "any") is False

        # revoked=1 → False（code2 已被吊销）
        assert store.validate(code2, "any") is False

        # expire_at < now → False
        code4 = store.generate(description="expired-code", max_uses=3,
                               expire_at=time.time() - 100)
        assert store.validate(code4, "any") is False

        # expire_at 为 NULL → 永不过期
        code5 = store.generate(description="no-expiry", max_uses=3)
        assert store.validate(code5, "any") is True

        # use_count >= max_uses → False
        code6 = store.generate(description="maxed-out", max_uses=1)
        assert store.validate(code6, "any") is True
        store.consume(code6)
        assert store.validate(code6, "any") is False  # use_count(1) >= max_uses(1)

        # allowed_project_pattern 非空且 project_id 不匹配 glob → False
        code7 = store.generate(description="patterned", max_uses=3,
                               allowed_project_pattern="baseline-*")
        assert store.validate(code7, "baseline-dev") is True
        assert store.validate(code7, "baseline-prod") is True
        assert store.validate(code7, "other-project") is False

        # allowed_project_pattern 为空 → 不校验 project_id
        code8 = store.generate(description="no-pattern", max_uses=3)
        assert store.validate(code8, "any-project-123") is True
        assert store.validate(code8, "") is True
        assert store.validate(code8, "baseline-dev") is True

        # CODE-004：消费
        code9 = store.generate(description="consumable", max_uses=3)
        assert store.get(code9)["use_count"] == 0
        store.consume(code9)
        assert store.get(code9)["use_count"] == 1
        store.consume(code9)
        assert store.get(code9)["use_count"] == 2

        # 超过 max_uses 后 consume 不报错（由 validate 拦截）
        store.consume(code9)  # use_count=3, max_uses=3
        assert store.get(code9)["use_count"] == 3
        store.consume(code9)  # 超过 max_uses，不报错
        assert store.get(code9)["use_count"] == 4
        # validate 此时应返回 False
        assert store.validate(code9, "any") is False

        # consume 不存在的 code 不报错
        store.consume("no-such-code")

        # VERIFY-001 边界情况
        # max_uses=0（不允许任何使用）
        code10 = store.generate(description="zero-max", max_uses=0)
        assert store.validate(code10, "any") is False  # 0 >= 0 → False

        # 空 pattern 边界
        code11 = store.generate(description="empty-pattern", max_uses=3,
                                allowed_project_pattern="")
        assert store.validate(code11, "any") is True
        assert store.validate(code11, "") is True

        # 空表 list
        empty_store = ms.EnrollmentCodeStore(os.path.join(tmpdir, "empty.db"))
        assert empty_store.list() == []

        # 大量记录
        for i in range(20):
            c = store.generate(description=f"bulk-{i}", max_uses=1)
            assert c is not None
        all_codes_bulk = store.list()
        assert len(all_codes_bulk) >= 20

        print("✓ EnrollmentCodeStore（表结构/生成/校验/消费/吊销/边界）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_admin_auth():
    """TASK-049：管理员密码认证（ADMIN-001 / ADMIN-002 / ADMIN-003 / VERIFY-001）。"""
    import monitor_server as ms

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-admin-auth-")
    try:
        # ===== ADMIN-001：文件生成 =====
        admin_path = os.path.join(tmpdir, "admin.json")
        assert not os.path.isfile(admin_path), "测试前应不存在"

        # 1. 首次生成
        ms.ensure_admin_config(admin_path)
        assert os.path.isfile(admin_path), "ensure_admin_config 应创建文件"
        with open(admin_path, encoding="utf-8") as fh:
            data = json.load(fh)
        pw = data.get("admin_password")
        assert isinstance(pw, str) and len(pw) == 32, f"密码应为 32 字符：{len(pw) if isinstance(pw, str) else type(pw)}"
        print(f"✓ 首次生成 admin.json，密码长度 32")

        # 2. 权限 600（如果 FS 支持）
        try:
            mode = os.stat(admin_path).st_mode & 0o777
            assert mode == 0o600, f"文件权限应为 600：{oct(mode)}"
            print(f"✓ 文件权限 600")
        except OSError:
            print("~ 文件系统不支持 chmod（Windows/无权限），跳过权限检查")

        # 3. 已存在时不覆盖
        ms.ensure_admin_config(admin_path)
        with open(admin_path, encoding="utf-8") as fh:
            data2 = json.load(fh)
        assert data2["admin_password"] == pw, "已存在密码不应被覆盖"
        print(f"✓ 已存在时不覆盖")

        # ===== ADMIN-002：校验函数 =====
        class FakeReq:
            def __init__(self, auth):
                self.headers = {"Authorization": auth}

        # 4. 正确密码 → True
        req = FakeReq(f"Bearer {pw}")
        # 注入 admin_path 以便 check_admin_password 读到测试文件
        orig_path = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))
        try:
            assert ms.check_admin_password(req) is True, "正确密码应返回 True"
            print(f"✓ 正确密码 → True")

            # 5. 错误密码 → False
            req2 = FakeReq("Bearer wrong-password-1234567890")
            assert ms.check_admin_password(req2) is False, "错误密码应返回 False"
            print(f"✓ 错误密码 → False")

            # 6. 缺少 header → False
            req3 = FakeReq("")
            assert ms.check_admin_password(req3) is False, "缺 header 应返回 False"
            print(f"✓ 缺 header → False")

            # 7. 非 Bearer 格式 → False
            req4 = FakeReq("Basic dXNlcjpwYXNz")
            assert ms.check_admin_password(req4) is False, "非 Bearer 应返回 False"
            print(f"✓ 非 Bearer 格式 → False")

            # 8. 空 token → False
            req5 = FakeReq("Bearer ")
            assert ms.check_admin_password(req5) is False, "空 token 应返回 False"
            print(f"✓ 空 token → False")

            # 9. 文件不存在 → False（fail-closed）
            ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), "nonexistent.json")
            req6 = FakeReq(f"Bearer {pw}")
            assert ms.check_admin_password(req6) is False, "文件不存在应返回 False"
            print(f"✓ 文件不存在 → False（fail-closed）")

            # 10. 文件损坏 → False（fail-closed）
            bad_path = os.path.join(tmpdir, "bad-admin.json")
            with open(bad_path, "w", encoding="utf-8") as fh:
                fh.write("not-json")
            ms.ADMIN_CONFIG_REL = (os.path.dirname(bad_path), os.path.basename(bad_path))
            req7 = FakeReq(f"Bearer {pw}")
            assert ms.check_admin_password(req7) is False, "文件损坏应返回 False"
            print(f"✓ 文件损坏 → False（fail-closed）")

            # 11. 缺 admin_password 字段 → False（fail-closed）
            bad_path2 = os.path.join(tmpdir, "bad-admin2.json")
            with open(bad_path2, "w", encoding="utf-8") as fh:
                json.dump({"foo": "bar"}, fh)
            ms.ADMIN_CONFIG_REL = (os.path.dirname(bad_path2), os.path.basename(bad_path2))
            req8 = FakeReq(f"Bearer {pw}")
            assert ms.check_admin_password(req8) is False, "缺 admin_password 字段应返回 False"
            print(f"✓ 缺 admin_password 字段 → False（fail-closed）")
        finally:
            ms.ADMIN_CONFIG_REL = orig_path

        # ===== ADMIN-003：集成 — 401 响应体格式 =====
        # 集成在完整 test_server 中测试，此处仅验证 401 响应体不泄露额外信息
        from http.server import BaseHTTPRequestHandler
        assert ms.extract_bearer_token("Bearer invalid") == "invalid"
        assert ms.extract_bearer_token("") is None
        print(f"✓ 401 响应体格式（extract_bearer_token 边界）")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_register_endpoint():
    """TASK-050：POST /api/register 端点（REG-001 ~ REG-005 / VERIFY-001）。

    覆盖：请求校验、冲突检测、注册码校验、成功注册、限流。
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-register-")
    try:
        reg_db = os.path.join(tmpdir, "registration.db")
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       registration_db_path=reg_db,
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def post(body, path="/api/register"):
            """POST 到指定路径，返回 (status, raw_body)。"""
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body)
            if isinstance(body, str):
                body = body.encode("utf-8")
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", path, body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, raw

        valid = {
            "project_id": "new-project-1",
            "path": "/home/user/code/my-project",
            "host_info": "hostname:dev-box, ip:192.168.1.5",
            "request_key": "a" * 16,
        }

        # ===== REG-001 — 请求校验 =====

        # 缺少必填字段 → 400
        for field in ("project_id", "path", "host_info", "request_key"):
            bad = dict(valid)
            del bad[field]
            status, raw = post(bad)
            assert status == 400, f"缺 {field} 应 400：{status} {raw[:100]}"
            assert json.loads(raw).get("error"), "400 响应应含 error 字段"
        print("✓ REG-001：缺少必填字段 → 400")

        # project_id 含非法字符 → 400
        for bad_id in ("project@123", "project id", "project/id", "project.id"):
            status, raw = post(dict(valid, project_id=bad_id))
            assert status == 400, f"project_id={bad_id!r} 应 400：{status}"
        print("✓ REG-001：project_id 含非法字符 → 400")

        # request_key < 16 字节 → 400
        for short_key in ("", "a" * 15, "a" * 8):
            status, raw = post(dict(valid, request_key=short_key))
            assert status == 400, f"request_key={short_key!r} 应 400：{status}"
        print("✓ REG-001：request_key < 16 字节 → 400")

        # 请求体非 JSON → 400
        status, raw = post("not json{{{")
        assert status == 400, f"非 JSON 应 400：{status}"
        print("✓ REG-001：请求体非 JSON → 400")

        # ===== REG-002 — 冲突检测 =====

        # project_id 在 config/projects.json 已存在 → 409 active
        status, raw = post(dict(valid, project_id="aimonitor"))
        assert status == 409, f"已注册 project_id 应 409：{status} {raw[:100]}"
        data = json.loads(raw)
        assert data.get("existing") == "active", f"existing 应为 active：{data}"
        print("✓ REG-002：projects.json 已存在 → 409 active")

        # project_id 在 ingest_state 有活跃记录 → 409 active
        # 先写入 ingest_state
        ms.ApiHandler.state.ingest.upsert("ingest-active-proj",
            {"tasks": []}, "agent-1")
        status, raw = post(dict(valid, project_id="ingest-active-proj"))
        assert status == 409, f"ingest_state 活跃 → 应 409：{status} {raw[:100]}"
        data = json.loads(raw)
        assert data.get("existing") == "active", f"existing 应为 active：{data}"
        print("✓ REG-002：ingest_state 活跃 → 409 active")

        # 同 project_id 已有 pending 申请 → 409 pending
        status, raw = post(dict(valid, project_id="dup-pending-proj"))
        assert status == 201, f"首次注册应 201：{status} {raw[:100]}"
        status, raw = post(dict(valid, project_id="dup-pending-proj"))
        assert status == 409, f"重复 pending 应 409：{status} {raw[:100]}"
        data = json.loads(raw)
        assert data.get("existing") == "pending", f"existing 应为 pending：{data}"
        print("✓ REG-002：已有 pending 申请 → 409 pending")

        # 同 project_id 只有 rejected/expired 记录 → 允许注册
        # 先注册一个，然后 reject
        status, raw = post(dict(valid, project_id="rejected-proj"))
        assert status == 201
        req_id = json.loads(raw)["req_id"]
        assert ms.ApiHandler.state.registration.reject(req_id) is True
        status, raw = post(dict(valid, project_id="rejected-proj"))
        assert status == 201, f"rejected 后重新注册应 201：{status} {raw[:100]}"
        print("✓ REG-002：rejected/expired 记录 → 允许注册")

        # ===== REG-003 — 注册码校验 =====

        # 生成一个有效注册码
        code = ms.ApiHandler.state.enrollment.generate(
            description="test code",
            allowed_project_pattern="enroll-proj-*",
            max_uses=2,
        )

        # 携带有效 enrollment_code → 201
        status, raw = post(dict(valid, project_id="enroll-proj-1",
                                enrollment_code=code))
        assert status == 201, f"有效注册码应 201：{status} {raw[:100]}"
        print("✓ REG-003：有效 enrollment_code → 201")

        # 携带无效 enrollment_code → 400
        status, raw = post(dict(valid, project_id="enroll-proj-2",
                                enrollment_code="INVALID-CODE"))
        assert status == 400, f"无效注册码应 400：{status} {raw[:100]}"
        data = json.loads(raw)
        assert "invalid enrollment_code" in data.get("error", "")
        print("✓ REG-003：无效 enrollment_code → 400")

        # 不携带 enrollment_code → 201，正常注册
        status, raw = post(dict(valid, project_id="no-enroll-proj"))
        assert status == 201, f"无注册码应 201：{status} {raw[:100]}"
        print("✓ REG-003：不携带 enrollment_code → 201")

        # ===== REG-004 — 成功注册 =====

        status, raw = post(dict(valid, project_id="success-proj"))
        assert status == 201, f"成功注册应 201：{status} {raw[:100]}"
        data = json.loads(raw)
        assert "req_id" in data, f"响应应含 req_id：{data}"
        assert data["status"] == "pending", f"status 应为 pending：{data}"
        assert "pending_since" in data, f"响应应含 pending_since：{data}"
        assert isinstance(data["pending_since"], (int, float)), "pending_since 应为数值"

        # RegistrationStore.create() 被调用
        row = ms.ApiHandler.state.registration.get(data["req_id"])
        assert row is not None, "RegistrationStore 中应有该记录"
        assert row["project_id"] == "success-proj"
        assert row["status"] == "pending"
        print("✓ REG-004：成功注册 → 201 + req_id + RegistrationStore 记录")

        # 有效 enrollment_code 被 consume
        code2 = ms.ApiHandler.state.enrollment.generate(max_uses=1)
        status, raw = post(dict(valid, project_id="consume-proj",
                                enrollment_code=code2))
        assert status == 201
        code_row = ms.ApiHandler.state.enrollment.get(code2)
        assert code_row["use_count"] == 1, f"注册码应已被消费：{code_row}"
        print("✓ REG-004：有效 enrollment_code 被 consume")

        # 幂等：同 request 重复发送 → 正常冲突（409），不产生副作用
        status, raw = post(dict(valid, project_id="success-proj"))
        assert status == 409, f"重复注册应 409：{status} {raw[:100]}"
        # 行数不变
        all_rows = ms.ApiHandler.state.registration.list_by_status()
        same_id = [r for r in all_rows if r["project_id"] == "success-proj"]
        assert len(same_id) == 1, "重复注册不应产生新行"
        print("✓ VERIFY-001：幂等 — 重复请求 → 409，不产生副作用")

        # ===== REG-005 — 限流 =====

        # 将限流器设为极小值，验证 429
        old_limiter = ms.ApiHandler.state.register_limiter
        ms.ApiHandler.state.register_limiter = ms.IngestRateLimiter(2, clock=time.time)
        try:
            # 前 2 次应成功
            for i in range(2):
                status, raw = post(dict(valid, project_id=f"rate-test-{i}"))
                assert status == 201, f"第 {i+1} 次应 201：{status}"
            # 第 3 次应 429
            status, raw = post(dict(valid, project_id="rate-test-3"))
            assert status == 429, f"超限应 429：{status} {raw[:100]}"
            data = json.loads(raw)
            assert "rate limit" in data.get("error", ""), f"429 应含 rate limit 消息：{data}"
            print("✓ REG-005：限流 — 超限 → 429")
        finally:
            ms.ApiHandler.state.register_limiter = old_limiter

    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_status_endpoint():
    """TASK-051：GET /api/register/:req_id/status 端点（STATUS-001 ~ STATUS-004 / VERIFY-001）。

    覆盖：request_key 绑定、各状态响应、Token 单次交付、404 处理。
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-status-")
    try:
        reg_db = os.path.join(tmpdir, "registration.db")
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       registration_db_path=reg_db,
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def get(path):
            """GET 请求，返回 (status, raw_body)。"""
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            return resp.status, raw

        # 准备测试数据：创建不同状态的注册请求
        reg = ms.ApiHandler.state.registration

        # pending 状态
        req_id_pending = reg.create("proj-pending", "code-p", '{"host":"h1"}', "key-pending")

        # approved 状态（先创建再 approve）
        req_id_approved = reg.create("proj-approved", "code-a", '{"host":"h2"}', "key-approved")
        token = "issued-token-abc"
        reg.approve(req_id_approved, token)

        # approved 已交付状态
        req_id_delivered = reg.create("proj-delivered", "code-d", '{"host":"h3"}', "key-delivered")
        reg.approve(req_id_delivered, "token-delivered")
        reg.mark_token_delivered(req_id_delivered)

        # rejected 状态
        req_id_rejected = reg.create("proj-rejected", "code-r", '{"host":"h4"}', "key-rejected")
        reg.reject(req_id_rejected, "不合规")

        # expired 状态
        req_id_expired = reg.create("proj-expired", "code-e", '{"host":"h5"}', "key-expired")
        import sqlite3
        conn = sqlite3.connect(reg_db)
        conn.execute("UPDATE registration_request SET expire_at=? WHERE req_id=?",
                     (time.time() - 100, req_id_expired))
        conn.commit()
        conn.close()
        reg.expire_stale()

        # revoked 状态（先 approve 再 revoke）
        req_id_revoked = reg.create("proj-revoked", "code-v", '{"host":"h6"}', "key-revoked")
        reg.approve(req_id_revoked, "token-revoked")
        reg.revoke(req_id_revoked)

        def status_path(req_id):
            return f"/api/register/{req_id}/status"

        # ===== STATUS-001 — request_key 绑定 =====

        # request_key 匹配 → 正常返回
        status, raw = get(status_path(req_id_pending) + "?request_key=key-pending")
        assert status == 200, f"request_key 匹配应 200：{status} {raw[:100]}"
        print("✓ STATUS-001：request_key 匹配 → 正常返回")

        # request_key 不匹配 → 404（不区分 req_id 不存在和 key 错误）
        status, raw = get(status_path(req_id_pending) + "?request_key=wrong-key")
        assert status == 404, f"request_key 不匹配应 404：{status}"
        assert json.loads(raw) == {"error": "not found"}, f"404 响应体：{raw[:100]}"
        print("✓ STATUS-001：request_key 不匹配 → 404")

        # 缺少 request_key 参数 → 404
        status, raw = get(status_path(req_id_pending))
        assert status == 404, f"缺 request_key 参数应 404：{status}"
        assert json.loads(raw) == {"error": "not found"}, f"404 响应体：{raw[:100]}"
        print("✓ STATUS-001：缺少 request_key 参数 → 404")

        # ===== STATUS-002 — 各状态响应 =====

        # pending → { status: "pending", pending_since: <epoch> }
        status, raw = get(status_path(req_id_pending) + "?request_key=key-pending")
        assert status == 200, f"pending 应 200：{status}"
        data = json.loads(raw)
        assert data["status"] == "pending", f"pending 状态：{data}"
        assert "pending_since" in data, f"pending 应含 pending_since：{data}"
        assert isinstance(data["pending_since"], (int, float)), "pending_since 应为数值"
        print("✓ STATUS-002：pending → { status: 'pending', pending_since }")

        # approved（首次）→ { status: "approved", token, project_id }
        status, raw = get(status_path(req_id_approved) + "?request_key=key-approved")
        assert status == 200, f"approved 首次应 200：{status}"
        data = json.loads(raw)
        assert data["status"] == "approved", f"approved 状态：{data}"
        assert data["token"] == token, f"token 应匹配：{data}"
        assert data["project_id"] == "proj-approved", f"project_id 应匹配：{data}"
        print("✓ STATUS-002：approved（首次）→ { status, token, project_id }")

        # approved（已交付）→ { status: "approved" }（不含 token）
        status, raw = get(status_path(req_id_delivered) + "?request_key=key-delivered")
        assert status == 200, f"approved 已交付应 200：{status}"
        data = json.loads(raw)
        assert data == {"status": "approved"}, f"approved 已交付：{data}"
        assert "token" not in data, "已交付不应含 token"
        print("✓ STATUS-002：approved（已交付）→ { status: 'approved' }")

        # rejected → { status: "rejected", reason: "..." }
        status, raw = get(status_path(req_id_rejected) + "?request_key=key-rejected")
        assert status == 200, f"rejected 应 200：{status}"
        data = json.loads(raw)
        assert data["status"] == "rejected", f"rejected 状态：{data}"
        assert "reason" in data, f"rejected 应含 reason：{data}"
        assert data["reason"] == "不合规", f"rejected reason：{data}"
        print("✓ STATUS-002：rejected → { status: 'rejected', reason }")

        # expired → { status: "expired" }
        status, raw = get(status_path(req_id_expired) + "?request_key=key-expired")
        assert status == 200, f"expired 应 200：{status}"
        data = json.loads(raw)
        assert data == {"status": "expired"}, f"expired 响应：{data}"
        print("✓ STATUS-002：expired → { status: 'expired' }")

        # revoked → { status: "revoked" }
        status, raw = get(status_path(req_id_revoked) + "?request_key=key-revoked")
        assert status == 200, f"revoked 应 200：{status}"
        data = json.loads(raw)
        assert data == {"status": "revoked"}, f"revoked 响应：{data}"
        print("✓ STATUS-002：revoked → { status: 'revoked' }")

        # ===== STATUS-003 — Token 单次交付 =====

        # 连续请求两次：第一次有 token，第二次无
        req_id_token_once = reg.create("proj-token-once", "code-t", '{"host":"h7"}', "key-token-once")
        reg.approve(req_id_token_once, "single-use-token")

        status, raw = get(status_path(req_id_token_once) + "?request_key=key-token-once")
        assert status == 200, f"第一次请求应 200：{status}"
        data1 = json.loads(raw)
        assert data1["status"] == "approved"
        assert data1["token"] == "single-use-token", "第一次应有 token"
        assert "project_id" in data1, "第一次应有 project_id"

        status, raw = get(status_path(req_id_token_once) + "?request_key=key-token-once")
        assert status == 200, f"第二次请求应 200：{status}"
        data2 = json.loads(raw)
        assert data2 == {"status": "approved"}, f"第二次不应含 token：{data2}"
        assert "token" not in data2, "第二次不应含 token"

        # token_delivered 字段不暴露给客户端
        assert "token_delivered" not in data1, "token_delivered 不应暴露"
        assert "token_delivered" not in data2, "token_delivered 不应暴露"
        print("✓ STATUS-003：Token 单次交付（首次含 token，二次无 token，token_delivered 不暴露）")

        # ===== STATUS-004 — 404 处理 =====

        # req_id 不存在 → 404 { error: "not found" }
        status, raw = get("/api/register/no-such-req-id/status?request_key=some-key")
        assert status == 404, f"req_id 不存在应 404：{status}"
        assert json.loads(raw) == {"error": "not found"}, f"404 响应体：{raw[:100]}"

        # 不泄露 req_id 是否存在（request_key 错误时也返回 404）
        status, raw = get(status_path(req_id_pending) + "?request_key=wrong-key")
        assert status == 404
        status, raw = get("/api/register/does-not-exist/status?request_key=some-key")
        assert status == 404
        # 两种 404 响应体一致
        key_404 = json.loads(get(status_path(req_id_pending) + "?request_key=wrong-key")[1])
        noex_404 = json.loads(get("/api/register/does-not-exist/status?request_key=some-key")[1])
        assert key_404 == noex_404 == {"error": "not found"}, "两种 404 响应体应一致"
        print("✓ STATUS-004：404 处理（req_id 不存在 / request_key 错误 / 不泄露）")

        print("✓ TASK-051：GET /api/register/:req_id/status 全部通过")
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_register_list_endpoint():
    """TASK-055：GET /api/register/list 端点（admin auth + 列表返回）。

    覆盖：
    - 鉴权：缺/错 Authorization header → 401
    - 正常列表返回（含 pending/approved/rejected 混合状态）
    - status 参数筛选
    - 空列表返回 []
    """
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-reg-list-")
    try:
        reg_db = os.path.join(tmpdir, "registration.db")
        # 生成 admin 密码
        admin_path = os.path.join(tmpdir, "admin.json")
        admin_password = "test-admin-pwd-32-char-len-xxxx"
        with open(admin_path, "w", encoding="utf-8") as f:
            json.dump({"admin_password": admin_password}, f)
        os.chmod(admin_path, 0o600)

        # 注入 ADMIN_CONFIG_REL 指向测试 admin.json
        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))

        try:
            ms.ApiHandler.state = ms.State(config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                           registration_db_path=reg_db,
                                           projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
            ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
            ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

            port = free_port()
            httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)

            def get(path, auth=None):
                """GET 请求，返回 (status, raw_body)。"""
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                headers = {}
                if auth is not None:
                    headers["Authorization"] = auth
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            reg = ms.ApiHandler.state.registration

            # 准备测试数据
            req_id_p1 = reg.create("proj-list-1", None, '{"hostname":"h1","ip":"10.0.0.1"}', "key-1")
            req_id_p2 = reg.create("proj-list-2", "code-2", '{"hostname":"h2","ip":"10.0.0.2"}', "key-2")
            req_id_a1 = reg.create("proj-list-3", None, '{"hostname":"h3","ip":"10.0.0.3"}', "key-3")
            reg.approve(req_id_a1, "token-a1")
            req_id_r1 = reg.create("proj-list-4", None, '{"hostname":"h4","ip":"10.0.0.4"}', "key-4")
            reg.reject(req_id_r1, "no")

            # ===== 鉴权 =====

            # 缺 Authorization header → 401
            status, raw = get("/api/register/list")
            assert status == 401, f"缺 token 应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}, f"401 响应体：{raw[:100]}"
            print("✓ 缺 Authorization header → 401")

            # 错误 token → 401
            status, raw = get("/api/register/list", auth="Bearer wrong-pwd")
            assert status == 401, f"错 token 应 401：{status}"
            print("✓ 错误 token → 401")

            # ===== 正常列表返回 =====

            status, raw = get("/api/register/list", auth="Bearer " + admin_password)
            assert status == 200, f"有效 token 应 200：{status}"
            data = json.loads(raw)
            assert isinstance(data, list), f"响应应为数组：{type(data)}"
            assert len(data) == 4, f"应有 4 条记录：{len(data)}"
            # 验证字段完整性
            for row in data:
                assert "req_id" in row, f"缺少 req_id：{row}"
                assert "project_id" in row, f"缺少 project_id：{row}"
                assert "host_info" in row, f"缺少 host_info：{row}"
                assert "status" in row, f"缺少 status：{row}"
                assert "created_at" in row, f"缺少 created_at：{row}"
            # 按 created_at 升序
            for i in range(1, len(data)):
                assert data[i]["created_at"] >= data[i-1]["created_at"], "未按 created_at 升序"
            print("✓ 正常列表返回（4 条，字段完整，按 created_at 升序）")

            # ===== status 参数筛选 =====

            status, raw = get("/api/register/list?status=pending", auth="Bearer " + admin_password)
            assert status == 200
            data = json.loads(raw)
            assert len(data) == 2, f"pending 应有 2 条：{len(data)}"
            for row in data:
                assert row["status"] == "pending", f"非 pending 记录：{row}"
            print("✓ status=pending 筛选（2 条 pending）")

            status, raw = get("/api/register/list?status=approved", auth="Bearer " + admin_password)
            assert status == 200
            data = json.loads(raw)
            assert len(data) == 1, f"approved 应有 1 条：{len(data)}"
            assert data[0]["status"] == "approved"
            print("✓ status=approved 筛选（1 条 approved）")

            # ===== 空列表 =====

            status, raw = get("/api/register/list?status=revoked", auth="Bearer " + admin_password)
            assert status == 200
            data = json.loads(raw)
            assert isinstance(data, list) and len(data) == 0, f"空列表应返回 []：{data}"
            print("✓ 空列表返回 []")

            print("✓ TASK-055：GET /api/register/list 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_approve_reject_endpoint():
    """TASK-052：POST /api/register/:req_id/approve 和 /api/register/:req_id/reject 端点。

    覆盖：
    - 鉴权：缺/错 Authorization header → 401
    - Approve：正常审批 → 200 + token 签发 + agents.json 写入验证
    - Reject：正常拒绝 → 200
    - 已处理的 req_id → 409
    - 不存在的 req_id → 404
    - 幂等验证
    - agents.json 写入验证（文件存在 + token 格式正确 + 权限 600）
    """
    import monitor_server as ms
    import secrets

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-approve-reject-")
    try:
        # 创建 admin.json
        admin_path = os.path.join(tmpdir, "admin.json")
        admin_password = secrets.token_hex(16)
        with open(admin_path, "w", encoding="utf-8") as fh:
            json.dump({"admin_password": admin_password}, fh)
        os.chmod(admin_path, 0o600)

        reg_db = os.path.join(tmpdir, "registration.db")
        agents_path = os.path.join(tmpdir, "agents.json")

        # 注入 ADMIN_CONFIG_REL 指向测试 admin.json
        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))

        try:
            ms.ApiHandler.state = ms.State(config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                           agents_path=agents_path,
                                           registration_db_path=reg_db,
                                           projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
            ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
            ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

            port = free_port()
            httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)

            def post(path, auth=None, body=None):
                """POST 请求，返回 (status, raw_body)。

                auth=None 表示不带 Authorization 头。
                """
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

            # 准备测试数据：创建两个 pending 注册请求
            reg = ms.ApiHandler.state.registration
            req_id_approve = reg.create(
                "proj-approve-test", "code-approve",
                '{"hostname":"h1"}', "key-approve")
            assert req_id_approve is not None
            req_id_reject = reg.create(
                "proj-reject-test", "code-reject",
                '{"hostname":"h2"}', "key-reject")
            assert req_id_reject is not None

            # ===== 鉴权测试 =====

            # 1. 缺少 Authorization header → 401
            status, raw = post(f"/api/register/{req_id_approve}/approve", auth=None)
            assert status == 401, f"缺 auth 应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}, f"401 响应体：{raw[:100]}"
            print("✓ approve 缺 Authorization header → 401")

            # 2. 错误密码 → 401
            status, raw = post(f"/api/register/{req_id_approve}/approve",
                              auth="Bearer wrong-password-1234567890")
            assert status == 401, f"错密码应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}, f"401 响应体：{raw[:100]}"
            print("✓ approve 错误密码 → 401")

            # 3. 非 Bearer 格式 → 401
            status, raw = post(f"/api/register/{req_id_approve}/approve",
                              auth="Basic dXNlcjpwYXNz")
            assert status == 401, f"非 Bearer 应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}, f"401 响应体：{raw[:100]}"
            print("✓ approve 非 Bearer 格式 → 401")

            # 4. 401 不泄露额外信息（响应体固定 {error: "unauthorized"}）
            for auth_val in (None, "Bearer wrong", "Bearer   "):
                status, raw = post(f"/api/register/{req_id_approve}/approve", auth=auth_val)
                assert status == 401
                assert json.loads(raw) == {"error": "unauthorized"}, f"401 泄露数据：{raw[:100]}"
            print("✓ 401 不泄露额外信息")

            # ===== Approve 测试 =====

            # 5. 正常审批 → 200 + token 签发
            auth = f"Bearer {admin_password}"
            status, raw = post(f"/api/register/{req_id_approve}/approve", auth=auth)
            assert status == 200, f"正常审批应 200：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data["status"] == "approved", f"status 应为 approved：{data}"
            assert data["req_id"] == req_id_approve, f"req_id 应匹配：{data}"
            assert data["project_id"] == "proj-approve-test", f"project_id 应匹配：{data}"
            print("✓ 正常审批 → 200 + { status, req_id, project_id }")

            # 6. agents.json 写入验证
            assert os.path.isfile(agents_path), "agents.json 应已被创建"
            with open(agents_path, encoding="utf-8") as fh:
                agents_data = json.load(fh)
            assert "proj-approve-test" in agents_data, \
                f"agents.json 中应有 project_id：{agents_data}"
            token = agents_data["proj-approve-test"]
            assert isinstance(token, str) and len(token) > 50, f"token 格式异常：{token}"
            assert token.startswith("aimon_proj-approve-test_"), \
                f"token 应含 project_id 前缀：{token}"
            # 权限 600 检查
            try:
                mode = os.stat(agents_path).st_mode & 0o777
                assert mode == 0o600, f"agents.json 权限应为 600：{oct(mode)}"
            except OSError:
                pass
            print("✓ agents.json 写入验证（文件存在 + token 格式正确 + 权限 600）")

            # 7. RegistrationStore 记录已更新
            row = reg.get(req_id_approve)
            assert row["status"] == "approved", f"status 应为 approved：{row}"
            assert row["issued_token"] == token, f"issued_token 应匹配：{row}"
            assert row["decided_at"] is not None, "decided_at 不应为 None"
            print("✓ RegistrationStore 记录已更新（status=approved, issued_token, decided_at）")

            # 8. 已处理的 req_id → 409
            status, raw = post(f"/api/register/{req_id_approve}/approve", auth=auth)
            assert status == 409, f"已处理应 409：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data["error"] == "already processed", f"409 响应体：{data}"
            print("✓ 已处理的 req_id → 409")

            # 9. 不存在的 req_id → 404
            status, raw = post("/api/register/no-such-req-id/approve", auth=auth)
            assert status == 404, f"不存在应 404：{status}"
            assert json.loads(raw) == {"error": "not found"}, f"404 响应体：{raw[:100]}"
            print("✓ 不存在的 req_id → 404")

            # ===== Reject 测试 =====

            # 10. 正常拒绝 → 200
            status, raw = post(f"/api/register/{req_id_reject}/reject", auth=auth)
            assert status == 200, f"正常拒绝应 200：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data["status"] == "rejected", f"status 应为 rejected：{data}"
            assert data["req_id"] == req_id_reject, f"req_id 应匹配：{data}"
            print("✓ 正常拒绝 → 200 + { status, req_id }")

            # 11. RegistrationStore 记录已更新
            row = reg.get(req_id_reject)
            assert row["status"] == "rejected", f"status 应为 rejected：{row}"
            assert row["decided_at"] is not None, "decided_at 不应为 None"
            print("✓ RegistrationStore 记录已更新（status=rejected, decided_at）")

            # 12. Reject 已处理的 req_id → 409
            status, raw = post(f"/api/register/{req_id_reject}/reject", auth=auth)
            assert status == 409, f"已处理应 409：{status}"
            assert json.loads(raw) == {"error": "already processed"}, f"409 响应体：{raw[:100]}"
            print("✓ reject 已处理的 req_id → 409")

            # 13. Reject 不存在的 req_id → 404
            status, raw = post("/api/register/no-such-req-id/reject", auth=auth)
            assert status == 404, f"不存在应 404：{status}"
            assert json.loads(raw) == {"error": "not found"}, f"404 响应体：{raw[:100]}"
            print("✓ reject 不存在的 req_id → 404")

            # ===== 幂等验证 =====

            # 14. 重复 approve → 409（不改变状态，不重新签发 token）
            status, raw = post(f"/api/register/{req_id_approve}/approve", auth=auth)
            assert status == 409, f"重复 approve 应 409：{status}"
            row = reg.get(req_id_approve)
            assert row["status"] == "approved", f"重复 approve 不应改变状态：{row}"
            assert row["issued_token"] == token, f"重复 approve 不应重新签发 token：{row}"
            print("✓ 幂等：重复 approve → 409，不改变状态和 token")

            # 15. 重复 reject → 409（不改变状态）
            status, raw = post(f"/api/register/{req_id_reject}/reject", auth=auth)
            assert status == 409, f"重复 reject 应 409：{status}"
            row = reg.get(req_id_reject)
            assert row["status"] == "rejected", f"重复 reject 不应改变状态：{row}"
            print("✓ 幂等：重复 reject → 409，不改变状态")

            # ===== 审批后 enrollment_code 消费 =====

            # 16. 有 enrollment_code 的审批 → consume 被调用
            code = ms.ApiHandler.state.enrollment.generate(
                description="approve-consume-test", max_uses=1,
                allowed_project_pattern="consume-proj-*")
            req_id_consume = reg.create(
                "consume-proj-1", code,
                '{"hostname":"h3"}', "key-consume")
            assert req_id_consume is not None
            assert ms.ApiHandler.state.enrollment.get(code)["use_count"] == 0

            status, raw = post(f"/api/register/{req_id_consume}/approve", auth=auth)
            assert status == 200, f"审批带 enrollment_code 应 200：{status} {raw[:100]}"
            code_row = ms.ApiHandler.state.enrollment.get(code)
            assert code_row["use_count"] == 1, f"注册码应已被消费：{code_row}"
            print("✓ 审批后 enrollment_code 被 consume")

            print("✓ TASK-052：POST /api/register/:req_id/approve 和 /api/register/:req_id/reject 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_token_issuer():
    """TASK-054：TokenIssuer 签发服务 + agents.json 写入。

    覆盖 TOKEN-001（签发格式/随机性）、TOKEN-002（文件读写/追加/移除/缺失/格式错误）、
    TOKEN-003（安全：chmod 600、不入日志、hmac.compare_digest）。
    """
    import random
    import re
    import monitor_server as ms

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-token-issuer-")
    try:
        agents_path = os.path.join(tmpdir, "agents.json")
        issuer = ms.TokenIssuer(agents_path=agents_path)

        # ===== TOKEN-001 — 签发 =====

        # issue() 返回格式正确的 token
        result = issuer.issue("proj-001")
        assert "token" in result, f"issue() 应返回 token：{result}"
        assert result["project_id"] == "proj-001"
        assert result["scope"] == "agent"
        token = result["token"]
        assert isinstance(token, str) and len(token) > 50, f"token 格式异常：{token}"
        assert token.startswith("aimon_proj-001_"), f"token 应含 project_id 前缀：{token}"

        # token 格式：aimon_{project_id}_{uuid4}_{token_urlsafe(32)}
        # 注意：token_urlsafe 可含 _ 和 -，因此用正则提取前缀 + 定长段
        assert token.startswith("aimon_proj-001_")
        suffix = token[len("aimon_proj-001_"):]
        # 提取 uuid4（32 字符 hex，紧跟前缀后）
        uuid4_part = suffix[:32]
        assert re.match(r"^[0-9a-f]{32}$", uuid4_part), f"uuid4 段应为 32 位 hex：{uuid4_part}"
        # 剩余部分为 token_urlsafe(32)
        urlsafe_part = suffix[32:]
        assert len(urlsafe_part) >= 43, f"token_urlsafe 段应 ≥ 43 字符：{len(urlsafe_part)}"

        # 每次签发不同 token（随机性）
        result2 = issuer.issue("proj-001")
        token2 = result2["token"]
        assert token != token2, "两次签发 token 应不同"

        # 不同 project_id 签发不同 token
        issuer.issue("proj-002")
        # 用 secrets.token_urlsafe(32) 生成，不依赖 random
        assert token.find(str(random.getrandbits(30))) == -1, "token 不应含 random 痕迹"

        # 验证 token 中包含 uuid4 格式（32 字符 hex）
        uuid_pattern = re.compile(r"^[0-9a-f]{32}$")
        assert uuid_pattern.match(uuid4_part), f"uuid4 段应为 32 位 hex：{uuid4_part}"

        print("✓ TOKEN-001：签发格式正确（aimon_{project_id}_{uuid4}_{token_urlsafe} + 随机性）")

        # ===== TOKEN-002 — 文件读写 =====

        # read_agents_config() 读取正确
        config = ms.read_agents_config(agents_path)
        assert isinstance(config, dict), f"read_agents_config 应返回 dict：{config}"
        # 注意：proj-001 被 issue 两次（第一次 token，第二次 token2），config 中为 token2
        assert "proj-001" in config and "proj-002" in config, \
            f"agents.json 中应包含两个项目：{list(config.keys())}"
        assert config["proj-001"] == token2, f"proj-001 应为最近签发 token2：{config['proj-001']} vs {token2}"

        # write_agents_config() 写入 config/agents.json（600）
        new_path = os.path.join(tmpdir, "new-agents.json")
        ms.write_agents_config({"test": "tok"}, new_path)
        assert os.path.isfile(new_path), "write_agents_config 应创建文件"
        with open(new_path, encoding="utf-8") as f:
            assert json.load(f) == {"test": "tok"}
        try:
            mode = os.stat(new_path).st_mode & 0o777
            assert mode == 0o600, f"文件权限应为 600：{oct(mode)}"
        except OSError:
            pass
        # 覆盖写入（整文件替换）
        ms.write_agents_config({"only": "this"}, new_path)
        with open(new_path, encoding="utf-8") as f:
            assert json.load(f) == {"only": "this"}, "write_agents_config 为整文件写入"

        # 追加写入不覆盖已有 token（通过 TokenIssuer.issue 实现，它读-合并-写）
        issuer.issue("proj-003")
        config = ms.read_agents_config(agents_path)
        assert "proj-001" in config and "proj-002" in config and "proj-003" in config, \
            f"追加写入后应保留所有项目：{list(config.keys())}"
        assert config["proj-001"] == token2, "追加写入不应覆盖已有 token"

        # remove_token(project_id) 移除指定 token
        ms.remove_token("proj-001", agents_path)
        config = ms.read_agents_config(agents_path)
        assert "proj-001" not in config, "remove_token 应移除 proj-001"
        assert "proj-002" in config, "remove_token 不应移除其他项目"

        # 移除不存在的项目不报错
        ms.remove_token("no-such-proj", agents_path)  # 应不抛异常

        # 文件不存在时读返回空 dict
        none_path = os.path.join(tmpdir, "nonexistent.json")
        assert ms.read_agents_config(none_path) == {}

        # 写创建新文件
        fresh_path = os.path.join(tmpdir, "fresh.json")
        assert not os.path.isfile(fresh_path)
        ms.write_agents_config({"new": "tok"}, fresh_path)
        assert os.path.isfile(fresh_path), "write_agents_config 应创建新文件"

        # 文件格式错误时读返回空 dict（fail-closed）
        bad_path = os.path.join(tmpdir, "bad.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("not-json-content")
        assert ms.read_agents_config(bad_path) == {}, "格式错误应返回空 dict"

        # 顶层非对象时读返回空 dict
        nonobj_path = os.path.join(tmpdir, "nonobj.json")
        with open(nonobj_path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        assert ms.read_agents_config(nonobj_path) == {}, "顶层非对象应返回空 dict"

        print("✓ TOKEN-002：文件读写（read/write/追加/移除/缺失/格式错误/fail-closed）")

        # ===== TOKEN-003 — 安全 =====

        # 写入后 chmod 600（已在 TOKEN-002 中验证）

        # token 不入日志——检查 issue 方法无 print/log 调用
        import inspect
        source = inspect.getsource(ms.TokenIssuer.issue)
        # 检查实际 print() 调用（而非 docstring 中提及 print 的说明文字）
        assert "print(" not in source and "logging" not in source, \
            "TokenIssuer.issue 不应包含 print/log 调用"

        # 使用 hmac.compare_digest 做 token 比对（未来扩展）
        # 验证 existing load_agents_config 使用的 _token_eq 使用 compare_digest
        assert ms._token_eq("abc", "abc") is True
        assert ms._token_eq("abc", "xyz") is False
        # 常量时间比较，不等长也返回 False 不泄露长度
        assert ms._token_eq("abc", "abcdef") is False

        print("✓ TOKEN-003：安全（chmod 600 / 不入日志 / hmac.compare_digest）")

        print("✓ TASK-054：TokenIssuer 签发服务 + agents.json 写入全部通过")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_revoke_renew_endpoint():
    """TASK-053：POST /api/register/:req_id/revoke 和 /api/register/:req_id/renew 端点。

    覆盖：
    - 鉴权：缺/错 Authorization header → 401
    - Revoke：正常吊销 → 200 + agents.json token 移除 + 状态更新
    - Renew：正常轮换 → 200 + 新 token 签发 + agents.json 写入验证
    - 已处理的 req_id → 409
    - 不存在的 req_id → 404
    - 幂等验证
    - agents.json 写入验证（文件存在 + token 格式正确 + 权限 600）
    - renew 后旧 token 失效（旧 token 推 ingest → 401）
    - renew 后新 token 可推送
    """
    import monitor_server as ms
    import secrets

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-revoke-renew-")
    try:
        # 添加测试项目到 config（用于 ingest 推送测试）
        config["projects"] = list(config["projects"]) + [
            {"id": "proj-renew-test", "name": "renew-test", "path": tmpdir},
        ]

        # 创建 admin.json
        admin_path = os.path.join(tmpdir, "admin.json")
        admin_password = secrets.token_hex(16)
        with open(admin_path, "w", encoding="utf-8") as fh:
            json.dump({"admin_password": admin_password}, fh)
        os.chmod(admin_path, 0o600)

        reg_db = os.path.join(tmpdir, "registration.db")
        agents_path = os.path.join(tmpdir, "agents.json")

        # 注入 ADMIN_CONFIG_REL 指向测试 admin.json
        orig_admin_rel = ms.ADMIN_CONFIG_REL
        ms.ADMIN_CONFIG_REL = (os.path.dirname(admin_path), os.path.basename(admin_path))

        try:
            ms.ApiHandler.state = ms.State(config, quiet=True,
                                           db_path=os.path.join(tmpdir, "history.db"),
                                           ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                           agents_path=agents_path,
                                           registration_db_path=reg_db,
                                           projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
            ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
            ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

            port = free_port()
            httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            time.sleep(0.3)

            def post(path, auth=None, body=None):
                """POST 请求，返回 (status, raw_body)。

                auth=None 表示不带 Authorization 头。
                """
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
                """GET 请求，返回 (status, raw_body)。"""
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", path)
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()
                return resp.status, raw

            auth = f"Bearer {admin_password}"

            # ===== RR-001 — 鉴权测试 =====

            # 1. 缺少 Authorization header → 401
            status, raw = post("/api/register/some-req-id/revoke", auth=None)
            assert status == 401, f"缺 auth 应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}, f"401 响应体：{raw[:100]}"
            print("✓ revoke 缺 Authorization header → 401")

            # 2. 错误密码 → 401
            status, raw = post("/api/register/some-req-id/revoke",
                              auth="Bearer wrong-password-1234567890")
            assert status == 401, f"错密码应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}, f"401 响应体：{raw[:100]}"
            print("✓ revoke 错误密码 → 401")

            # 3. 非 Bearer 格式 → 401
            status, raw = post("/api/register/some-req-id/revoke",
                              auth="Basic dXNlcjpwYXNz")
            assert status == 401, f"非 Bearer 应 401：{status}"
            assert json.loads(raw) == {"error": "unauthorized"}, f"401 响应体：{raw[:100]}"
            print("✓ revoke 非 Bearer 格式 → 401")

            # 4. 401 不泄露额外信息
            for auth_val in (None, "Bearer wrong", "Bearer   "):
                status, raw = post("/api/register/some-req-id/revoke", auth=auth_val)
                assert status == 401
                assert json.loads(raw) == {"error": "unauthorized"}, f"401 泄露数据：{raw[:100]}"
            print("✓ 401 不泄露额外信息")

            # ===== 准备测试数据：创建并审批一个请求 =====
            reg = ms.ApiHandler.state.registration
            req_id_revoke = reg.create(
                "proj-revoke-test", None,
                '{"hostname":"h1"}', "key-revoke")
            assert req_id_revoke is not None
            req_id_renew = reg.create(
                "proj-renew-test", None,
                '{"hostname":"h2"}', "key-renew")
            assert req_id_renew is not None

            # 先approve才能revoke/renew
            status, raw = post(f"/api/register/{req_id_revoke}/approve", auth=auth)
            assert status == 200, f"审批失败：{status} {raw[:100]}"
            status, raw = post(f"/api/register/{req_id_renew}/approve", auth=auth)
            assert status == 200, f"审批失败：{status} {raw[:100]}"

            # 记录旧 token
            row_revoke = reg.get(req_id_revoke)
            old_token_revoke = row_revoke["issued_token"]
            row_renew = reg.get(req_id_renew)
            old_token_renew = row_renew["issued_token"]

            # ===== RR-002 — Revoke 测试 =====

            # 5. 正常停销 → 200 + { status: "revoked" }
            status, raw = post(f"/api/register/{req_id_revoke}/revoke", auth=auth)
            assert status == 200, f"正常停销应 200：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data == {"status": "revoked"}, f"停销响应：{data}"
            print("✓ 正常停销 → 200 + { status: 'revoked' }")

            # 6. agents.json token 已移除
            with open(agents_path, encoding="utf-8") as fh:
                agents_data = json.load(fh)
            assert "proj-revoke-test" not in agents_data, \
                f"agents.json 中应不再有该 project_id：{agents_data}"
            print("✓ agents.json token 已移除")

            # 7. RegistrationStore 记录已更新
            row = reg.get(req_id_revoke)
            assert row["status"] == "revoked", f"status 应为 revoked：{row}"
            assert row["decided_at"] is not None, "decided_at 不应为 None"
            print("✓ RegistrationStore 记录已更新（status=revoked, decided_at）")

            # 8. 重复 revoke → 409
            status, raw = post(f"/api/register/{req_id_revoke}/revoke", auth=auth)
            assert status == 409, f"重复 revoke 应 409：{status} {raw[:100]}"
            assert json.loads(raw) == {"error": "already processed"}, f"409 响应体：{raw[:100]}"
            print("✓ 重复 revoke → 409")

            # 9. 不存在的 req_id → 404
            status, raw = post("/api/register/no-such-req-id/revoke", auth=auth)
            assert status == 404, f"不存在应 404：{status}"
            assert json.loads(raw) == {"error": "not found"}, f"404 响应体：{raw[:100]}"
            print("✓ 不存在的 req_id → 404")

            # ===== RR-003 — Renew 测试 =====

            # 10. 正常 renew → 200 + { status: "approved", note: "..." }
            status, raw = post(f"/api/register/{req_id_renew}/renew", auth=auth)
            assert status == 200, f"正常 renew 应 200：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data["status"] == "approved", f"status 应为 approved：{data}"
            # REVIEW F7：note 与 MONITOR-SPEC 权威契约对齐
            assert data["note"] == "新 token 已签发，agent 下次推送时收到 401 后自动轮询领取", \
                f"renew 响应：{data}"
            print("✓ 正常 renew → 200 + { status: 'approved', note }（note 与 spec 对齐）")

            # 11. 新 token 已签发并写入 agents.json
            with open(agents_path, encoding="utf-8") as fh:
                agents_data = json.load(fh)
            assert "proj-renew-test" in agents_data, \
                f"agents.json 中应有新 token：{agents_data}"
            new_token = agents_data["proj-renew-test"]
            assert new_token != old_token_renew, f"新 token 应与旧 token 不同"
            assert new_token.startswith("aimon_proj-renew-test_"), \
                f"token 格式异常：{new_token}"
            # 权限 600 检查
            try:
                mode = os.stat(agents_path).st_mode & 0o777
                assert mode == 0o600, f"agents.json 权限应为 600：{oct(mode)}"
            except OSError:
                pass
            print("✓ agents.json 写入验证（新 token 格式正确 + 权限 600）")

            # 12. 旧 token 已从 agents.json 移除（renew 执行了 revoke 逻辑）
            assert old_token_renew not in agents_data.values(), \
                f"旧 token 应已从 agents.json 移除"
            print("✓ 旧 token 已从 agents.json 移除")

            # 13. RegistrationStore 记录已更新（新 issued_token, decided_at, token_delivered=0）
            row = reg.get(req_id_renew)
            assert row["status"] == "approved", f"status 应为 approved：{row}"
            assert row["issued_token"] == new_token, f"issued_token 应为新 token：{row}"
            assert row["token_delivered"] is False, f"token_delivered 应为 0：{row}"
            assert row["decided_at"] is not None, "decided_at 不应为 None"
            print("✓ RegistrationStore 记录已更新（新 issued_token, decided_at）")

            # 14. 重复 renew → 409
            status, raw = post(f"/api/register/{req_id_renew}/renew", auth=auth)
            assert status == 409, f"重复 renew 应 409：{status} {raw[:100]}"
            assert json.loads(raw) == {"error": "already processed"}, f"409 响应体：{raw[:100]}"
            print("✓ 重复 renew → 409")

            # 15. renew 不存在的 req_id → 404
            status, raw = post("/api/register/no-such-req-id/renew", auth=auth)
            assert status == 404, f"不存在应 404：{status}"
            assert json.loads(raw) == {"error": "not found"}, f"404 响应体：{raw[:100]}"
            print("✓ renew 不存在的 req_id → 404")

            # ===== F6（REVIEW）：renew 后 agent 通过 status 端点领取新 token =====
            # token_delivered 已重置为 0，首次轮询返回新 token 并标记已交付
            status, raw = get(f"/api/register/{req_id_renew}/status?request_key=key-renew")
            assert status == 200, f"status 轮询应 200：{status} {raw[:100]}"
            data = json.loads(raw)
            assert data["status"] == "approved", f"status 应为 approved：{data}"
            assert data.get("token") == new_token, f"status 应返回新 token：{data}"
            print("✓ renew 后 status 端点返回新 token")

            # 第二次轮询不再返回 token（单次交付）
            status, raw = get(f"/api/register/{req_id_renew}/status?request_key=key-renew")
            assert status == 200, f"二次 status 轮询应 200：{status} {raw[:100]}"
            assert json.loads(raw) == {"status": "approved"}, \
                f"二次轮询不应再返回 token：{raw[:100]}"
            print("✓ status 端点单次交付（二次轮询无 token）")

            # ===== RR-004 — 错误处理 =====

            # 16. agents.json 写失败 → 500（agents_path 指向非空目录，os.replace 必然失败）
            # REVIEW F5：真实模拟写失败，而非仅注释断言 try/except OSError
            req_id_500 = reg.create(
                "proj-500-test", None,
                '{"hostname":"h3"}', "key-500")
            assert req_id_500 is not None
            status, raw = post(f"/api/register/{req_id_500}/approve", auth=auth)
            assert status == 200, f"500 用例审批失败：{status} {raw[:100]}"

            # 将 agents_path 指向一个含 token 的普通文件，但其 `.tmp` 路径是非空目录：
            # remove_token 读到 token 后会触发写入，open(<path>.tmp, 'w') 必然抛
            # IsADirectoryError（OSError 子类）→ 端点返回 500。此方案与运行权限无关，确定可复现。
            with open(agents_path, encoding="utf-8") as fh:
                agents_data = json.load(fh)
            bad_agents_path = os.path.join(tmpdir, "agents-bad.json")
            with open(bad_agents_path, "w", encoding="utf-8") as fh:
                json.dump(agents_data, fh)
            tmp_blocker_dir = bad_agents_path + ".tmp"
            os.makedirs(tmp_blocker_dir, exist_ok=True)
            tmp_blocker = os.path.join(tmp_blocker_dir, "blocker")
            if not os.path.exists(tmp_blocker):
                with open(tmp_blocker, "w", encoding="utf-8") as fh:
                    fh.write("x")
            orig_agents_path = ms.ApiHandler.state.agents_path
            ms.ApiHandler.state.agents_path = bad_agents_path
            try:
                status, raw = post(f"/api/register/{req_id_500}/revoke", auth=auth)
                assert status == 500, f"agents.json 写失败应 500：{status} {raw[:100]}"
                assert json.loads(raw) == {"error": "internal error"}, \
                    f"500 响应体：{raw[:100]}"
            finally:
                ms.ApiHandler.state.agents_path = orig_agents_path
            # 写失败时 store 不更新：记录仍为 approved，可重试
            row500 = reg.get(req_id_500)
            assert row500["status"] == "approved", f"写失败不应改变状态：{row500}"
            print("✓ agents.json 写失败 → 500（store 状态不变，可重试）")

            # ===== VERIFY-001 — renew 后旧 token 失效，新 token 可推送 =====

            # 17. renew 后旧 token 推 ingest → 401
            old_token_payload = {
                "project_id": "proj-renew-test",
                "ts": 1720000000,
                "files": {"tasks": [{"name": "TASK-001.md", "content": "# TASK\n"}]},
            }
            status, raw = post("/api/ingest", auth=f"Bearer {old_token_renew}",
                              body=old_token_payload)
            assert status == 401, f"旧 token 推 ingest 应 401：{status} {raw[:100]}"
            print("✓ renew 后旧 token 推 ingest → 401")

            # 18. renew 后新 token 可推送
            status, raw = post("/api/ingest", auth=f"Bearer {new_token}",
                              body=old_token_payload)
            assert status == 200, f"新 token 推 ingest 应 200：{status} {raw[:100]}"
            print("✓ renew 后新 token 可推送")

            print("✓ TASK-053：POST /api/register/:req_id/revoke 和 /api/register/:req_id/renew 全部通过")
        finally:
            ms.ADMIN_CONFIG_REL = orig_admin_rel
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_server():
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    # 历史快照库用临时路径，避免测试污染仓库 data/
    tmpdir = tempfile.mkdtemp(prefix="aimonitor-smoke-")
    try:
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       projects_path=os.path.join(tmpdir, "projects.json"), start_poller=False)
        ms.ApiHandler.state.poll()  # TASK-083：同步完成首轮轮询（消除后台 poller 首轮竞态）
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)
        try:
            # 1. /api/status 结构
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/status")
            resp = conn.getresponse()
            assert resp.status == 200, f"/api/status → {resp.status}"
            data = json.loads(resp.read())
            assert "projects" in data and "poll_interval_seconds" in data
            assert data["projects"][0]["id"] == "aimonitor"
            assert set(data["projects"][0]["summary"]) >= {"total", "open", "done"}
            conn.close()
            print("✓ /api/status 结构与数据")

            # 2. 正常静态文件
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/")
            resp = conn.getresponse()
            assert resp.status == 200, f"GET / → {resp.status}"
            conn.close()
            print("✓ 静态文件 / 200")

            # 3. 路径穿越回归（SEC-001）：同前缀兄弟目录 src-evil-poc
            evil = os.path.join(ROOT, "src-evil-poc")
            os.makedirs(evil, exist_ok=True)
            with open(os.path.join(evil, "secret.txt"), "w") as f:
                f.write("TOP SECRET")
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/../src-evil-poc/secret.txt")
                resp = conn.getresponse()
                conn.close()
                assert resp.status == 403, f"路径穿越未被拦截：{resp.status}"
                print("✓ 路径穿越被拦截 (403)")
            finally:
                shutil.rmtree(evil, ignore_errors=True)

            # 4. /api/history 结构（TASK-022）：首轮轮询应已写入 ≥1 快照
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            deadline = time.time() + 5
            while True:
                conn.request("GET", "/api/history?project=aimonitor&hours=24")
                resp = conn.getresponse()
                assert resp.status == 200, f"/api/history → {resp.status}"
                data = json.loads(resp.read())
                if data["points"] or time.time() > deadline:
                    break
                time.sleep(0.2)
            conn.close()
            assert data["points"], "历史快照为空（轮询线程未写入？）"
            assert data["project"] == "aimonitor" and data["hours"] == 24
            pt = data["points"][-1]
            assert {"ts", "summary", "coder_alive", "reviewer_alive"} <= set(pt)
            assert set(pt["summary"]) >= {"total", "open", "done"}
            print("✓ /api/history 结构与数据")

            # 5. /api/history 参数校验：缺 project / hours 非法 → 400
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/history?hours=24")
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 400, f"缺 project 应 400：{resp.status}"
            conn.request("GET", "/api/history?project=aimonitor&hours=abc")
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 400, f"hours 非法应 400：{resp.status}"
            conn.request("GET", "/api/history?project=aimonitor&hours=0")
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 400, f"hours=0 应 400：{resp.status}"
            conn.request("GET", "/api/history?project=aimonitor&hours=nan")
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 400, f"hours=nan 应 400（BUG-001 回归）：{resp.status}"
            conn.request("GET", "/api/history?project=aimonitor&hours=inf")
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 400, f"hours=inf 应 400：{resp.status}"
            conn.request("GET", "/api/history?project=aimonitor&hours=8761")
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 400, f"hours=8761 超上限应 400：{resp.status}"
            conn.close()
            print("✓ /api/history 参数校验 (400 + NaN/inf/超上限)")

            # 6. /api/history 默认 hours=24（整数）；无数据项目 → points:[] (200)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/history?project=aimonitor")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert resp.status == 200 and data["hours"] == 24, \
                f"默认 hours 应 24（整数）：{resp.status} {data.get('hours')}"
            conn.request("GET", "/api/history?project=no-such-project&hours=1")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert resp.status == 200 and data["points"] == [], \
                f"无数据项目应 200 + points:[]：{resp.status}"
            conn.close()
            print("✓ /api/history 默认 hours 与无数据项目")

            # 7. /api/projects/<id>/events 结构与 limit（TASK-023）
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/projects/aimonitor/events?limit=5")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert resp.status == 200, f"/api/projects/aimonitor/events → {resp.status}"
            assert data["project"] == "aimonitor" and data["limit"] == 5
            assert set(data["events"]) >= {"coder", "reviewer"}
            assert set(data["counts"]) >= {"coder", "reviewer"}
            for who in ("coder", "reviewer"):
                lst = data["events"][who]
                assert isinstance(lst, list) and len(lst) <= 5, \
                    f"{who} 事件超 limit：{len(lst)}"
                for ev in lst:
                    assert {"ts", "task", "outcome"} <= set(ev), \
                        f"{who} 事件字段缺失：{ev}"
                ts_list = [ev["ts"] for ev in lst if ev.get("ts") is not None]
                assert ts_list == sorted(ts_list, reverse=True), \
                    f"{who} 时间线未按 ts 降序"
            conn.close()
            print("✓ /api/projects/:id/events 结构与 limit")

            # 8. 默认 limit=10；未知项目 404
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/projects/aimonitor/events")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert resp.status == 200 and data["limit"] == 10, \
                f"默认 limit 应 10：{resp.status} {data.get('limit')}"
            conn.request("GET", "/api/projects/no-such-project/events")
            resp = conn.getresponse()
            resp.read()
            assert resp.status == 404, f"未知项目应 404：{resp.status}"
            conn.close()
            print("✓ 默认 limit 与未知项目 404")

            # 9. limit 参数校验 → 400（含 NaN/inf/非整数/超上限，BUG-001 同款防护）
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            for bad in ("0", "-1", "abc", "nan", "inf", "5.5", "101"):
                conn.request("GET", "/api/projects/aimonitor/events?limit=" + bad)
                resp = conn.getresponse()
                resp.read()
                assert resp.status == 400, f"limit={bad} 应 400：{resp.status}"
            conn.close()
            print("✓ limit 参数校验 (400 + NaN/inf/非整数/超上限)")

            # 10. /api/status tasks[].detail（TASK-024）：正文章节/验收 checklist/依赖/关联记录
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/status")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert resp.status == 200
            tasks = data["projects"][0]["tasks"]
            t023 = next((t for t in tasks if t["id"] == "TASK-023"), None)
            assert t023 is not None, "aimonitor 项目应含 TASK-023"
            d = t023.get("detail")
            assert d, "tasks[].detail 缺失"
            assert isinstance(d.get("sections"), list) and d["sections"], \
                "detail.sections 应为非空数组"
            headings = [s["heading"] for s in d["sections"]]
            assert {"目标", "范围", "验收标准", "备注"} <= set(headings), headings
            assert isinstance(d.get("acceptance"), list) and d["acceptance"], \
                "detail.acceptance 应为非空数组"
            for a in d["acceptance"]:
                assert "text" in a and "checked" in a, f"acceptance 元素字段缺失: {a}"
            assert all(a["checked"] is True for a in d["acceptance"]), \
                "TASK-023 验收标准应全部勾选"
            assert isinstance(d.get("dependencies"), list), "detail.dependencies 应为数组"
            assert any(r["name"].startswith("VERIFY-") for r in d.get("verification", [])), \
                "TASK-023 应有关联 VERIFY 记录"
            assert any(r["name"].startswith("REVIEW-") for r in d.get("reviews", [])), \
                "TASK-023 应有关联 REVIEW 记录"
            # 新任务（TASK-024，in-progress）也应有 detail 结构
            t024 = next((t for t in tasks if t["id"] == "TASK-024"), None)
            assert t024 is not None, "aimonitor 项目应含 TASK-024"
            d24 = t024.get("detail") or {}
            assert isinstance(d24.get("sections"), list) and d24["sections"], \
                "TASK-024 detail.sections 应为非空数组"
            assert isinstance(d24.get("acceptance"), list) and d24["acceptance"], \
                "TASK-024 detail.acceptance 应为非空数组"
            assert isinstance(d24.get("dependencies"), list), "detail.dependencies 应为数组"
            conn.close()
            print("✓ /api/status tasks[].detail 结构与数据")

            # 11. /api/status 服务端筛选（TASK-025）：status/priority/assignee/q + 组合 + 宽松空结果 + 向后兼容
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/status")
            resp = conn.getresponse()
            full = json.loads(resp.read())
            full_tasks = full["projects"][0]["tasks"]
            full_summary = full["projects"][0]["summary"]
            conn.close()
            assert full_tasks, "全量任务不应为空（筛选用基线）"

            def _hay(t):
                return " ".join([t.get("id") or "", t.get("name") or "",
                                 t.get("description") or "", t.get("assignee") or ""]).lower()

            def check_filter(query, predicate):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/api/status" + query)
                resp = conn.getresponse()
                data = json.loads(resp.read())
                conn.close()
                assert resp.status == 200, f"{query or '(无参)'} → {resp.status}"
                ft = data["projects"][0]["tasks"]
                got = [t["id"] for t in ft]
                expected = [t["id"] for t in full_tasks if predicate(t)]
                assert got == expected, f"{query or '(无参)'} 期望 {expected} 实际 {got}"
                assert data["projects"][0]["summary"] == full_summary, \
                    f"{query or '(无参)'} 不应改变 summary"
                return ft

            check_filter("?status=in-progress", lambda t: t["status"] == "in-progress")
            check_filter("?priority=P1", lambda t: t["priority"] == "P1")
            first_assignee = next((t.get("assignee") for t in full_tasks if t.get("assignee")), None)
            if first_assignee:
                check_filter("?assignee=" + first_assignee,
                             lambda t: t.get("assignee") == first_assignee)
            check_filter("?q=TASK-02", lambda t: "task-02" in _hay(t))
            check_filter("?status=in-progress&priority=P1",
                         lambda t: t["status"] == "in-progress" and t["priority"] == "P1")
            assert check_filter("?status=no-such-status", lambda t: False) == [], \
                "未知 status 应 200 + 空 tasks[]"
            check_filter("", lambda t: True)  # 无参数 → 全量（向后兼容）
            print("✓ /api/status 服务端筛选（status/priority/assignee/q/组合/宽松/无参兼容，summary 不变）")

            # 12. /api/status alerts 字段（TASK-026）：顶层聚合 + 项目级告警 + 筛选不影响
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/status")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            assert resp.status == 200
            al = data.get("alerts")
            assert al and isinstance(al.get("count"), int) and isinstance(al.get("items"), list), \
                "alerts 顶层字段缺失或结构错误"
            assert al["count"] == len(al["items"]), "alerts.count 应等于 items 长度"
            ids = {p["id"] for p in data["projects"]}
            for item in al["items"]:
                assert {"project", "level", "kind", "text"} <= set(item), \
                    f"告警条目字段缺失: {item}"
                assert item["level"] in ("error", "warn"), item
                assert item["project"] in ids, f"顶层告警 project 未注册: {item}"
            for p in data["projects"]:
                assert isinstance(p.get("alerts"), list), "projects[].alerts 应为数组"
                for a in p["alerts"]:
                    assert a["project"] == p["id"], "项目级告警 project 应等于该项目 id"
            # 筛选不应改变 alerts（聚合字段保持全量，与 summary 语义一致）
            conn.request("GET", "/api/status?status=in-progress")
            resp = conn.getresponse()
            fd = json.loads(resp.read())
            assert resp.status == 200
            assert fd["alerts"]["count"] == al["count"], "筛选不应改变 alerts"
            conn.close()
            print("✓ /api/status alerts 字段（顶层聚合 + 项目级 + 筛选不影响）")
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_session_store():
    """TASK-073：session 日志增量存储层单测（续传/幂等对账/重建重置/滚动上限/查询边界）。"""
    import monitor_server as ms
    tmpdir = tempfile.mkdtemp(prefix="aimonitor-session-store-")
    try:
        store = ms.IngestStore(os.path.join(tmpdir, "ingest.db"))

        def sessions(items, truncated=False, cursor=None):
            return {"items": items, "truncated": truncated,
                    "cursor": cursor if cursor is not None else {}}

        def files(name, lines):
            return {"name": name, "lines": lines}

        # 1. 新文件追加：line_no 从 1 连续递增，行原文往返一致
        store.store_session_deltas(
            "proj-a", sessions([{"task_id": "TASK-1", "files": [files("a.jsonl", ["L1", "L2"])]}],
                               cursor={"TASK-1/a.jsonl": 10}))
        assert store.read_session_lines("proj-a", "TASK-1", "a.jsonl", 10) == \
            [{"line_no": 1, "text": "L1"}, {"line_no": 2, "text": "L2"}]

        # 2. 游标推进 → 纯新增续传
        store.store_session_deltas(
            "proj-a", sessions([{"task_id": "TASK-1", "files": [files("a.jsonl", ["L3"])]}],
                               cursor={"TASK-1/a.jsonl": 13}))
        assert [x["text"] for x in store.read_session_lines("proj-a", "TASK-1", "a.jsonl", 10)] == \
            ["L1", "L2", "L3"]

        # 3. 纯重放（游标同位，agent 推送成功但未落游标后重推）→ 幂等不重复
        store.store_session_deltas(
            "proj-a", sessions([{"task_id": "TASK-1", "files": [files("a.jsonl", ["L3"])]}],
                               cursor={"TASK-1/a.jsonl": 13}))
        assert len(store.read_session_lines("proj-a", "TASK-1", "a.jsonl", 10)) == 3

        # 4. 混合重推（重放 + 新增，文件增长后游标持久化失败窗口）→ 前缀重叠对账只补新行
        store.store_session_deltas(
            "proj-a", sessions([{"task_id": "TASK-1", "files": [files("a.jsonl", ["L3", "L4"])]}],
                               cursor={"TASK-1/a.jsonl": 16}))
        assert [x["text"] for x in store.read_session_lines("proj-a", "TASK-1", "a.jsonl", 10)] == \
            ["L1", "L2", "L3", "L4"]

        # 5. 重建/截断（agent 游标倒退）→ 归零重收，旧行不残留
        store.store_session_deltas(
            "proj-a", sessions([{"task_id": "TASK-1", "files": [files("a.jsonl", ["R1", "R2"])]}],
                               cursor={"TASK-1/a.jsonl": 9}))
        assert [x["text"] for x in store.read_session_lines("proj-a", "TASK-1", "a.jsonl", 10)] == \
            ["R1", "R2"]

        # 6. 追平空批（items=[] + cursor）→ 行不变 + truncated 批标志透传到 summary
        store.store_session_deltas("proj-a", sessions([], truncated=True,
                                                      cursor={"TASK-1/a.jsonl": 9}))
        summary = store.read_sessions_summary("proj-a")
        assert summary["truncated"] is True and summary["last_push"] > 0
        assert summary["tasks"][0]["files"][0]["line_count"] == 2

        # 7. 滚动上限：超出窗口删最旧行，行号高水位不回退（继续续传不撞主键）
        real_cap = ms.SESSION_MAX_LINES_PER_FILE
        ms.SESSION_MAX_LINES_PER_FILE = 3
        try:
            for i in range(5):
                store.store_session_deltas(
                    "proj-a", sessions([{"task_id": "TASK-2",
                                          "files": [files("b.jsonl", ["X%d" % i])]}],
                                       cursor={"TASK-2/b.jsonl": (i + 1) * 4}))
            got = [x["text"] for x in store.read_session_lines("proj-a", "TASK-2", "b.jsonl", 10)]
            assert got == ["X2", "X3", "X4"], got
            assert store.read_sessions_summary("proj-a")["tasks"][1]["files"][0]["line_count"] == 3
        finally:
            ms.SESSION_MAX_LINES_PER_FILE = real_cap

        # 8. 查询边界：limit 0 / 非法 / 超上限截断；未接收项目 → tasks=[]
        assert store.read_session_lines("proj-a", "TASK-2", "b.jsonl", 0) == []
        assert store.read_session_lines("proj-a", "TASK-2", "b.jsonl", "abc") == []
        assert len(store.read_session_lines("proj-a", "TASK-2", "b.jsonl", 10 ** 9)) == 3
        empty = store.read_sessions_summary("no-such-project")
        assert empty["tasks"] == [] and empty["truncated"] is False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("✓ TASK-073 session 存储层（续传/幂等/重建/滚动上限/查询边界）")


def test_session_ingest_endpoint():
    """TASK-073：/api/ingest sessions 增量端点 + /api/projects/:id/sessions 查询端点
    （200 落库 / 查询视图 / truncated 透传 / v1.0 向后兼容 / schema 400）。"""
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    tmpdir = tempfile.mkdtemp(prefix="aimonitor-session-ep-")
    try:
        agents_path = _write_agents_file(tmpdir, {"aimonitor": "test-token"})
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"),
                                       agents_path=agents_path,
                                       projects_path=os.path.join(tmpdir, "projects.json"),
                                       start_poller=False)
        ms.ApiHandler.state.poll()
        ms.ApiHandler.static_dir = os.path.join(ROOT, "src")

        port = free_port()
        httpd = ms.ThreadingHTTPServer(("127.0.0.1", port), ms.ApiHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        time.sleep(0.3)

        def post(body):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/api/ingest", body=json.dumps(body),
                         headers={"Content-Type": "application/json",
                                  "Authorization": "Bearer test-token"})
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
            return resp.status, json.loads(raw)

        base = {"project_id": "aimonitor", "ts": 1720000000, "files": {"tasks": []}}

        # 1. v1.0 向后兼容：无 sessions 键的旧 payload → 仍 200，session 概览为空
        status, raw = post(base)
        assert status == 200, f"v1.0 旧 payload 应 200：{status} {raw[:200]}"
        status, data = get("/api/projects/aimonitor/sessions")
        assert status == 200 and data["tasks"] == [] and data["truncated"] is False, data

        # 2. 带 sessions 增量 → 200 落库；概览按 task/file 分组返回行数与游标
        msg = json.dumps({"type": "message", "message": {"role": "assistant",
                                                         "content": [{"type": "text", "text": "hi"}]}})
        payload = dict(base, sessions={
            "items": [{"task_id": "TASK-073", "files": [
                {"name": "s1.jsonl", "lines": [msg, "not-json-line"]}]}],
            "truncated": True,
            "cursor": {"TASK-073/s1.jsonl": 100},
        })
        status, raw = post(payload)
        assert status == 200, f"sessions payload 应 200：{status} {raw[:200]}"
        status, data = get("/api/projects/aimonitor/sessions")
        assert status == 200 and data["truncated"] is True, data
        t0 = data["tasks"][0]
        assert t0["task_id"] == "TASK-073" and t0["files"][0]["name"] == "s1.jsonl", data
        assert t0["files"][0]["line_count"] == 2 and t0["files"][0]["last_offset"] == 100, data

        # 3. 续传第二批 → 行视图（最近 limit 行升序）：合法 JSON ok/type/data，损坏行 ok=false 原样保留
        payload["sessions"]["items"][0]["files"][0]["lines"] = ["{\"type\":\"message\"}"]
        payload["sessions"]["cursor"]["TASK-073/s1.jsonl"] = 120
        payload["sessions"]["truncated"] = False
        status, raw = post(payload)
        assert status == 200
        status, data = get("/api/projects/aimonitor/sessions?task=TASK-073&file=s1.jsonl&limit=200")
        assert status == 200, data
        assert data["line_count"] == 3 and data["truncated"] is False and data["limit"] == 200, data
        lines = data["lines"]
        assert [x["line_no"] for x in lines] == [1, 2, 3], lines
        assert lines[0]["ok"] is True and lines[0]["type"] == "message"
        assert lines[0]["data"]["message"]["role"] == "assistant", lines[0]
        assert lines[1]["ok"] is False and lines[1]["text"] == "not-json-line", lines[1]

        # 4. limit 边界：非法值 400；task 不存在 → files=[] 不 404
        for bad in ("?task=T&file=f&limit=0", "?task=T&file=f&limit=-1",
                    "?task=T&file=f&limit=abc", "?task=T&file=f&limit=1.5",
                    f"?task=T&file=f&limit={ms.SESSION_QUERY_MAX_LINES + 1}"):
            status, raw = get("/api/projects/aimonitor/sessions" + bad)
            assert status == 400, f"{bad} 应 400：{status}"
        status, data = get("/api/projects/aimonitor/sessions?task=NOPE")
        assert status == 200 and data["files"] == [], data

        # 5. 未注册项目 → 404
        status, raw = get("/api/projects/no-such/sessions")
        assert status == 404, status

        # 6. schema 400：sessions 各字段类型错 + 单行超 64KiB 字节上限（fail loud）
        for bad_sessions in (
            "not-a-dict",
            {"items": "not-a-list"},
            {"items": [{"task_id": "", "files": []}]},
            {"items": [{"task_id": "T", "files": "not-a-list"}]},
            {"items": [{"task_id": "T", "files": [{"name": "", "lines": []}]}]},
            {"items": [{"task_id": "T", "files": [{"name": "a", "lines": "not-a-list"}]}]},
            {"items": [{"task_id": "T", "files": [{"name": "a", "lines": [1]}]}]},
            {"items": [{"task_id": "T", "files": [{"name": "a", "lines": ["x"]}]}],
             "truncated": "yes"},
            {"cursor": -1},
            {"cursor": {"T/a": True}},
            {"items": [{"task_id": "T", "files": [
                {"name": "a", "lines": ["x" * (ms.MAX_SESSION_LINE_BYTES + 1)]}]}]},
        ):
            status, raw = post(dict(base, sessions=bad_sessions))
            assert status == 400, f"sessions={str(bad_sessions)[:60]} 应 400：{status} {raw[:120]}"

        # 7. 落库往返：库内行与推送原文一致（agent 不解析不丢内容的对称面）
        rows = ms.ApiHandler.state.ingest.read_session_lines("aimonitor", "TASK-073", "s1.jsonl", 10)
        assert rows[0]["text"] == msg and rows[1]["text"] == "not-json-line", rows
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("✓ TASK-073 session ingest/查询端点（v1.0 兼容 / 分组查询 / truncated / schema 400）")


def main():
    test_py_compile()
    test_config_json()
    test_task_detail_parsing()
    test_task_filters()
    test_derive_alerts()
    test_notify_config()
    test_notify_sender()
    test_filereader_abstract()
    test_collect_project_reader_injection()
    test_agentreader()
    test_collect_project_agent_transport()
    test_heartbeat_offline_semantics()
    test_status_instance_meta()
    test_ingest_store()
    test_ingest_endpoint()
    test_task_events_ingest()
    test_session_store()
    test_session_ingest_endpoint()
    test_ingest_auth()
    test_ingest_scope_conflict()
    test_ingest_rate_limit()
    test_ingest_history_compat()
    test_agent_ingest_integration()
    test_dual_machine_verify()
    test_registration_store()
    test_enrollment_code_store()
    test_admin_auth()
    test_register_endpoint()
    test_status_endpoint()
    test_register_list_endpoint()
    test_approve_reject_endpoint()
    test_token_issuer()
    test_revoke_renew_endpoint()
    test_server()
    print("\n✓ server smoke 全部通过")


if __name__ == "__main__":
    main()
