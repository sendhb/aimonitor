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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))


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
            {"id": "TASK-003", "status": "in-progress", "updated": "2026-08-15"},
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
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"))
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
                                       agents_path=agents_path)
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
                                       agents_path=agents_path)
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
                                       agents_path=agents_path)
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
                                       rate_clock=clock)
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
                                       agents_path=agents_path)
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

    # aibase 组件（kit 源仓库，AGENTS.md：aibase = kit 源仓库；agent 归属 §3.1.5
    # aibase/kit/tools/agent/，本仓库 config/projects.json 已注册 aibase 项目）
    aibase_agent_dir = os.path.join(ROOT, "..", "aibase", "kit", "tools", "agent")
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
                    "  updated: 2026-08-17\n---\n# TASK-001\n## 目标\n集成测试任务\n")
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
                                       agents_path=agents_path)
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

    # aibase 组件（kit 源仓库，AGENTS.md：aibase = kit 源仓库；agent 归属 §3.1.5）
    aibase_agent_dir = os.path.join(ROOT, "..", "aibase", "kit", "tools", "agent")
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
                        f"  reviewer: autoloop-reviewer\n  updated: 2026-08-17\n---\n"
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
                                       agents_path=agents_path)
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


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_server():
    import monitor_server as ms

    with open(os.path.join(ROOT, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)

    # 历史快照库用临时路径，避免测试污染仓库 data/
    tmpdir = tempfile.mkdtemp(prefix="aimonitor-smoke-")
    try:
        ms.ApiHandler.state = ms.State(config, quiet=True,
                                       db_path=os.path.join(tmpdir, "history.db"),
                                       ingest_db_path=os.path.join(tmpdir, "ingest.db"))
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


def main():
    test_py_compile()
    test_config_json()
    test_task_detail_parsing()
    test_task_filters()
    test_derive_alerts()
    test_filereader_abstract()
    test_collect_project_reader_injection()
    test_agentreader()
    test_collect_project_agent_transport()
    test_heartbeat_offline_semantics()
    test_status_instance_meta()
    test_ingest_store()
    test_ingest_endpoint()
    test_ingest_auth()
    test_ingest_scope_conflict()
    test_ingest_rate_limit()
    test_ingest_history_compat()
    test_agent_ingest_integration()
    test_dual_machine_verify()
    test_server()
    print("\n✓ server smoke 全部通过")


if __name__ == "__main__":
    main()
