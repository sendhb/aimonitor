#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_server.py — aimonitor 后端采集服务（零第三方依赖，Python 3.12 标准库）

按 docs/MONITOR-SPEC.md 实现：
- 后台线程按 poll_interval_seconds 轮询 config/projects.json 中所有项目
- 只读各项目 runtime/tasks|states|logs|verification|reviews
- 采集层经 FileReader 抽象读 runtime 文件（TASK-037）：local（服务端直读）/ agent（TASK-038）
- 聚合为 MONITOR-SPEC §4 JSON，内存缓存
- ThreadingHTTPServer：GET /api/status → 聚合 JSON；GET / → 静态前端
- POST /api/ingest → Bearer token 鉴权（TASK-035）→ 限流（TASK-036）→ 授权范围/未注册/双 agent 冲突检查
  （TASK-036）后落库 ingest_state（TASK-034/035/036），并写历史快照（TASK-040：趋势即时反映推送）；
  请求体为 AIOS 通用遥测格式（file-oriented，TASK-042 与 aibase 组件 agent 零转换对接）

用法:
  python3 server/monitor_server.py [--port 3113] [--dev] [--quiet]
"""
import abc
import argparse
import hmac
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "projects.json")
STATUSES = ("open", "in-progress", "in-review", "blocked", "done", "cancelled")

# 历史快照存储（TASK-022）：aimonitor 自身 data/，不入被监控项目
HISTORY_DB_REL = ("data", "history.db")
DEFAULT_HISTORY_RETENTION_DAYS = 90
MAX_HISTORY_HOURS = 24 * 365  # hours 参数上限（365 天）

# 事件时间线 API（TASK-023）：limit 参数默认值与上限
DEFAULT_EVENT_LIMIT = 10
MAX_EVENT_LIMIT = 100

EVENTS_RE = re.compile(r"^/api/projects/([^/]+)/events$")

# 告警派生（TASK-026，见 MONITOR-SPEC §4.6）：默认阈值与参与 task-stale 判定的非终态状态
DEFAULT_BLOCKED_RATIO_THRESHOLD = 0.2
DEFAULT_STALE_TASK_DAYS = 14
ALERT_STALE_STATUSES = ("open", "in-progress", "in-review", "blocked")

# agent 推送存储（TASK-033/034）：默认库位置与 HistoryStore 同目录 data/ingest.db
INGEST_DB_REL = ("data", "ingest.db")
# ingest API（TASK-034，见 MONITOR-SPEC §3.1.3）：payload 上限，超限 413
MAX_INGEST_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5MB（tasks+events 原文体量留足余量）
# ingest 鉴权（TASK-035，见 MONITOR-SPEC §3.1.2）：config/agents.json（权限 600，gitignored）
AGENTS_CONFIG_REL = ("config", "agents.json")
# ingest 限流（TASK-036，见 MONITOR-SPEC §3.1.3）：每 agent 每分钟 N 次，超限 429；
# N 由 config/projects.json 顶层 ingest_rate_limit_per_minute 配置（缺省此默认值）
DEFAULT_INGEST_RATE_LIMIT_PER_MINUTE = 60


class FileReader(abc.ABC):
    """文件/目录读取抽象（TASK-037）：采集层唯一 runtime 文件访问入口。

    使 local（服务端直读）与 agent（远端 ingest_state，TASK-038）两种 transport
    共用同一套解析逻辑（MONITOR-SPEC §3.1.1「agent 只搬运不解析」）。

    四个方法均为**缺失容忍**语义（不抛异常；缺失/不可读 → None/[]）：
    - exists(path) -> bool：路径是否存在（文件或目录）
    - read(path) -> str | None：文件原文（缺失/不可读 → None）
    - mtime(path) -> float | None：文件 mtime（epoch 秒；缺失 → None）
    - listdir(path) -> list[str]：目录条目名（缺失/不可读/非目录 → []，不排序，与 os.listdir 一致）
    - offline_reason(stale_secs) -> str | None：源整体离线原因（TASK-039，MONITOR-SPEC
      §3.1.4；默认 None = 在线，AgentReader 按 last_seen 覆写）
    """

    @abc.abstractmethod
    def exists(self, path):
        raise NotImplementedError

    @abc.abstractmethod
    def read(self, path):
        raise NotImplementedError

    @abc.abstractmethod
    def mtime(self, path):
        raise NotImplementedError

    @abc.abstractmethod
    def listdir(self, path):
        raise NotImplementedError

    def offline_reason(self, stale_secs=None):
        """源整体离线原因（TASK-039，MONITOR-SPEC §3.1.4）：None = 在线。

        默认实现恒返回 None——local 源「在线/不可达」已由 exists(runtime) 表达
        （runtime 缺失 → collect_project 报「runtime 目录不存在」）；
        AgentReader 覆写为按 last_seen 判定 agent 整体离线 → 'agent 离线'。
        stale_secs 为心跳卡死阈值（heartbeat_stale_threshold_seconds）。
        """
        return None


class LocalReader(FileReader):
    """transport: local 的现有实现（TASK-037）：直接读本地文件系统（os 模块）。

    语义与 TASK-037 之前各 helper 的容错行为一致，保证现有 7 项目零回归：
    - exists = os.path.exists（文件或目录）
    - read = open(path, encoding="utf-8").read()（OSError/UnicodeDecodeError → None）
    - mtime = os.path.getmtime（OSError → None）
    - listdir = os.listdir（OSError/NotADirectoryError → []）
    """

    def exists(self, path):
        return os.path.exists(path)

    def read(self, path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except (OSError, UnicodeDecodeError):
            return None

    def mtime(self, path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def listdir(self, path):
        try:
            return os.listdir(path)
        except OSError:
            return []


class AgentReader(FileReader):
    """transport: agent 的 FileReader 实现（TASK-038，MONITOR-SPEC §3.1.1/§3.1.3）。

    把 ingest_state 中 §3.1.3 请求体 files 结构（**AIOS 通用遥测格式，TASK-042**：
    file-oriented 文件条目数组，与 aibase 组件 agent 输出零转换对接）映射为虚拟 runtime
    文件树，使现有 collect_tasks/read_focus/read_heartbeat/read_events/count_files
    解析逻辑零改动复用（agent 只搬运不解析，§3.1.1）：
    - tasks:   [{"name": "TASK-001-x.md", "content": 原文}, ...] → runtime/tasks/ 下的 .md 文件
    - focus:   CURRENT_FOCUS.md 原文 → runtime/states/CURRENT_FOCUS.md
    - heartbeats: [{"file": "autoloop-<role>.heartbeat", "mtime": epoch}, ...] →
                  mtime（§3.1.4 角色心跳 = payload epoch，服务端按文件名识别 role）
    - events:  [{"name": "autoloop-<role>-events.jsonl", "content": 原文}, ...] →
               runtime/logs/autoloop-<role>-events.jsonl
    - verification_count / review_count（null 容忍，TASK-042）→
      runtime/verification|reviews 的合成计数条目
      （agent 不推送单条 VERIFY/REVIEW 原文，服务端只展示计数；detail 关联记录为空）

    record 为 IngestStore.read() 行（{project_id, payload, last_seen, agent_id}），
    ingest 参数供运行时现查；两者皆无或记录缺失 → 整树视为不存在，且
    offline_reason() 返回 'agent 离线'（TASK-039：collect_project 判离线早退，
    error='agent 离线'，与 last_seen 超阈值同语义）。
    缺失容忍语义与 LocalReader 对齐（不存在/不可读 → None/[]，不抛异常）。
    """

    def __init__(self, proj, ingest=None, record=None):
        self.project_id = proj.get("id", "")
        self.root = proj.get("path", "")
        if record is None and ingest is not None:
            record = ingest.read(self.project_id)
        self.record = record
        self.payload = (record or {}).get("payload") or {}
        self.last_seen = (record or {}).get("last_seen")

    def _rel(self, path):
        """绝对路径 → 相对 self.root 的虚拟路径；不在 root 下（含跨盘符）→ None。"""
        try:
            rel = os.path.relpath(path, self.root)
        except ValueError:
            return None
        if rel == os.pardir or rel.startswith(".." + os.sep):
            return None
        return rel

    def _tasks(self):
        """files.tasks（数组 {name, content}）→ {文件名: 原文}（TASK-042 file-oriented）。

        content 缺失/None（agent 侧文件不可读）→ 空串（任务仍展示，字段缺省）。
        """
        t = self.payload.get("tasks")
        if not isinstance(t, list):
            return {}
        out = {}
        for entry in t:
            if not (isinstance(entry, dict) and isinstance(entry.get("name"), str)
                    and entry["name"]):
                continue
            content = entry.get("content")
            out[entry["name"]] = content if isinstance(content, str) else ""
        return out

    def _heartbeat(self, role):
        """files.heartbeats（数组 {file, mtime}）→ 该 role 心跳文件 mtime（§3.1.4）。

        按文件名 `autoloop-<role>.heartbeat` 匹配（TASK-042 file-oriented）；
        缺失/未推送 → None（与 LocalReader.mtime 缺失语义一致）。
        """
        hbs = self.payload.get("heartbeats")
        if not isinstance(hbs, list):
            return None
        target = f"autoloop-{role}.heartbeat"
        for entry in hbs:
            if isinstance(entry, dict) and entry.get("file") == target:
                return entry.get("mtime")
        return None

    def _events(self, role):
        """files.events（数组 {name, content}）→ 该 role 事件文件原文。

        按文件名 `autoloop-<role>-events.jsonl` 匹配（TASK-042 file-oriented）；
        缺失/未推送 → None（与 LocalReader.read 缺失语义一致）。
        """
        evs = self.payload.get("events")
        if not isinstance(evs, list):
            return None
        target = f"autoloop-{role}-events.jsonl"
        for entry in evs:
            if isinstance(entry, dict) and entry.get("name") == target:
                return entry.get("content")
        return None

    def _exists_rel(self, rel):
        """虚拟路径存在性判定；record 缺失（无推送）→ 全不存在。"""
        if self.record is None:
            return False
        if rel == "runtime":
            return True
        if rel == "runtime/tasks":
            return isinstance(self.payload.get("tasks"), list)
        if rel == "runtime/states":
            return self.payload.get("focus") is not None
        if rel == "runtime/logs":
            return any(v is not None for v in (
                self._heartbeat("coder"), self._heartbeat("reviewer"),
                self._events("coder"), self._events("reviewer")))
        if rel == "runtime/verification":
            return (self.payload.get("verification_count") or 0) > 0
        if rel == "runtime/reviews":
            return (self.payload.get("review_count") or 0) > 0
        if rel == "runtime/states/CURRENT_FOCUS.md":
            return self.payload.get("focus") is not None
        m = re.match(r"^runtime/tasks/(.+)$", rel)
        if m:
            return m.group(1) in self._tasks()
        m = re.match(r"^runtime/logs/(autoloop-(coder|reviewer)\.heartbeat)$", rel)
        if m:
            return self._heartbeat(m.group(2)) is not None
        m = re.match(r"^runtime/logs/(autoloop-(coder|reviewer)-events\.jsonl)$", rel)
        if m:
            return self._events(m.group(2)) is not None
        return False

    def exists(self, path):
        rel = self._rel(path)
        return rel is not None and self._exists_rel(rel)

    def read(self, path):
        rel = self._rel(path)
        if rel is None:
            return None
        m = re.match(r"^runtime/tasks/(.+)$", rel)
        if m:
            return self._tasks().get(m.group(1))
        if rel == "runtime/states/CURRENT_FOCUS.md":
            return self.payload.get("focus")
        m = re.match(r"^runtime/logs/(autoloop-(coder|reviewer)-events\.jsonl)$", rel)
        if m:
            return self._events(m.group(2))
        # VERIFY/REVIEW 原文未推送（只推送计数）→ 无内容
        return None

    def mtime(self, path):
        rel = self._rel(path)
        if rel is None:
            return None
        m = re.match(r"^runtime/logs/(autoloop-(coder|reviewer)\.heartbeat)$", rel)
        if m:
            return self._heartbeat(m.group(2))
        return None

    def listdir(self, path):
        rel = self._rel(path)
        if rel is None or not self._exists_rel(rel):
            return []
        if rel == "runtime/tasks":
            return sorted(self._tasks().keys())
        if rel == "runtime/states":
            return ["CURRENT_FOCUS.md"] if self.payload.get("focus") is not None else []
        if rel == "runtime/logs":
            names = []
            for role in ("coder", "reviewer"):
                if self._heartbeat(role) is not None:
                    names.append(f"autoloop-{role}.heartbeat")
                if self._events(role) is not None:
                    names.append(f"autoloop-{role}-events.jsonl")
            return names
        if rel == "runtime/verification":
            n = self.payload.get("verification_count") or 0
            return [f"VERIFY-{i}.md" for i in range(n)]
        if rel == "runtime/reviews":
            n = self.payload.get("review_count") or 0
            return [f"REVIEW-{i}.md" for i in range(n)]
        if rel == "runtime":
            return [d for d in ("tasks", "states", "logs", "verification", "reviews")
                    if self._exists_rel("runtime/" + d)]
        return []

    def offline_reason(self, stale_secs=None):
        """agent 整体离线判定（TASK-039，MONITOR-SPEC §3.1.4）：'agent 离线' / None = 在线。

        与角色心跳（payload epoch，TASK-038）解耦：
        - record 缺失（从未推送）→ 'agent 离线'
        - stale_secs 非 None 且 now - last_seen > stale_secs → 'agent 离线'
        - 其余（有记录且 last_seen 新鲜）→ None
        离线 → collect_project 报 error='agent 离线'（数据为空 + 历史留缺口），
        展示层以 error 区分「agent 失联」与 role 卡死（heartbeat-stale 告警仍由 role epoch 驱动）。
        """
        if self.record is None:
            return "agent 离线"
        if stale_secs is not None and self.last_seen is not None:
            if time.time() - self.last_seen > stale_secs:
                return "agent 离线"
        return None


def read_frontmatter(text):
    """解析 frontmatter → 扁平 dict（键如 'name'、'metadata.status'）。"""
    fm = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return fm
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return fm
    section = None
    for line in lines[1:end]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", s)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "metadata":
            section = "metadata"
            continue
        if val == "":
            continue
        fm[f"{section}.{key}" if section else key] = val
    return fm


def read_task_sections(text):
    """按 '## ' 标题切分 TASK 正文（frontmatter 之后）→ [(heading, body)]。

    TASK-024：正文通用解析，不硬编码标题列表；frontmatter 用首行 '---' 定界剔除。
    无 frontmatter 或定界缺失时退化处理为直接解析正文。
    """
    body = text
    if body.lstrip().startswith("---"):
        parts = body.split("---", 2)
        if len(parts) >= 3:
            body = parts[2]
    sections = []
    for m in re.finditer(r"^## (.+?)$(.*?)(?=^## |\Z)", body, re.M | re.S):
        heading = m.group(1).strip()
        sec_body = m.group(2).strip()
        if heading:
            sections.append((heading, sec_body))
    return sections


def parse_acceptance(body):
    """解析 '## 验收标准' 章节内 '- [ ]' / '- [x]' 清单 → [{text, checked}]。"""
    items = []
    for line in body.splitlines():
        m = re.match(r"^\s*[-*]\s+\[([ xX])\]\s+(.+)$", line)
        if m:
            items.append({"text": m.group(2).strip(),
                          "checked": m.group(1).lower() == "x"})
    return items


def parse_dependencies(fm):
    """解析 frontmatter metadata.depends-on（YAML 列表文本）→ [id]。

    兼容 '[]' / '[TASK-001, TASK-002]' / 'TASK-001' / 空值，并容忍引号包裹（单/双引号）。
    """
    raw = (fm.get("metadata.depends-on") or "").strip()
    if not raw or raw == "[]":
        return []
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        raw = raw[1:-1]
    raw = raw.strip("[]").strip()
    if not raw:
        return []
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def index_task_records(rt, reader=None):
    """读全部 VERIFY/REVIEW frontmatter → {task_id: {verification: [], reviews: []}}。

    TASK-024：每项目单遍建索引，collect_tasks 按 task-ref 直接取，避免逐任务重复扫盘。
    只保留 frontmatter 摘要（name/result/date/verifier|reviewer/commit），不读全文。
    TASK-037：文件访问经注入 reader（缺省 LocalReader），供 agent transport 复用。
    """
    reader = reader or LocalReader()
    index = {}
    for sub, key in (("verification", "verification"), ("reviews", "reviews")):
        d = os.path.join(rt, sub)
        if not reader.exists(d):
            continue
        names = sorted(reader.listdir(d))
        for fn in names:
            if not fn.endswith(".md"):
                continue
            rec = reader.read(os.path.join(d, fn))
            if rec is None:
                continue
            fm = read_frontmatter(rec)
            tid = fm.get("metadata.task-ref")
            if not tid:
                continue
            entry = index.setdefault(tid, {"verification": [], "reviews": []})
            item = {
                "name": fn[:-3],
                "result": fm.get("metadata.result", ""),
                "date": fm.get("metadata.date", ""),
                "commit": fm.get("metadata.commit", ""),
            }
            if key == "verification":
                item["verifier"] = fm.get("metadata.verifier", "")
            else:
                item["reviewer"] = fm.get("metadata.reviewer", "")
            entry[key].append(item)
    return index


def read_task_detail(task_id, text, fm, records):
    """组装任务 detail：正文章节 + 验收 checklist + 依赖 + 关联 VERIFY/REVIEW 摘要。"""
    sections, acceptance = [], []
    for heading, body in read_task_sections(text):
        if heading == "验收标准":
            acceptance = parse_acceptance(body)
        sections.append({"heading": heading, "body": body})
    records = records or {}
    return {
        "sections": sections,
        "acceptance": acceptance,
        "dependencies": parse_dependencies(fm),
        "verification": records.get("verification", []),
        "reviews": records.get("reviews", []),
    }


def collect_tasks(rt, records=None, reader=None):
    """读 runtime/tasks/TASK-*.md → (tasks, summary)。

    TASK-024：每个任务附加 detail（正文/验收/依赖/关联记录）；records 由
    index_task_records 单遍构建（缺省 None → 关联记录为空，仅依赖+章节解析）。
    TASK-037：文件访问经注入 reader（缺省 LocalReader），供 agent transport 复用。
    """
    reader = reader or LocalReader()
    tasks_dir = os.path.join(rt, "tasks")
    tasks = []
    records = records or {}
    if reader.exists(tasks_dir):
        for fn in sorted(reader.listdir(tasks_dir)):
            if not re.match(r"^TASK-\d{3}-", fn) or not fn.endswith(".md"):
                continue
            text = reader.read(os.path.join(tasks_dir, fn))
            if text is None:
                continue
            fm = read_frontmatter(text)
            mid = re.match(r"^(TASK-\d{3})", fn)
            task_id = mid.group(1) if mid else fn[:-3]
            tasks.append({
                "id": task_id,
                "slug": fn[:-3],
                "name": fm.get("name", fn[:-3]),
                "description": fm.get("description", ""),
                "status": fm.get("metadata.status", "unknown"),
                "priority": fm.get("metadata.priority", "?"),
                "risk": fm.get("metadata.risk", "?"),
                "assignee": fm.get("metadata.assignee", ""),
                "reviewer": fm.get("metadata.reviewer", ""),
                "updated": fm.get("metadata.updated", ""),
                "detail": read_task_detail(task_id, text, fm, records.get(task_id)),
            })
    summary = {"total": len(tasks)}
    for st in STATUSES:
        summary[st] = sum(1 for t in tasks if t["status"] == st)
    return tasks, summary


def read_heartbeat(rt, name, reader=None):
    """读 runtime/logs/autoloop-<name>.heartbeat → {exists, age_seconds}。

    TASK-037：mtime 经注入 reader（缺省 LocalReader）；reader.mtime 返回 None = 缺失。
    """
    reader = reader or LocalReader()
    fp = os.path.join(rt, "logs", f"autoloop-{name}.heartbeat")
    mtime = reader.mtime(fp)
    if mtime is None:
        return {"exists": False, "age_seconds": None}
    return {"exists": True, "age_seconds": round(time.time() - mtime, 1)}


def read_events(rt, name, reader=None):
    """读 runtime/logs/autoloop-<name>-events.jsonl → {count, last, outcomes}。

    TASK-037：文件原文经注入 reader（缺省 LocalReader）；read 返回 None = 缺失。
    """
    reader = reader or LocalReader()
    fp = os.path.join(rt, "logs", f"autoloop-{name}-events.jsonl")
    count, last, outcomes = 0, None, {}
    content = reader.read(fp)
    if content is not None:
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            count += 1
            o = ev.get("outcome", "?")
            outcomes[o] = outcomes.get(o, 0) + 1
            last = ev
    return {"count": count, "last": last, "outcomes": outcomes}


def read_events_timeline(rt, name, limit, reader=None):
    """读 runtime/logs/autoloop-<name>-events.jsonl → (count, 最近 limit 条 [{ts,task,outcome}])。

    TASK-023：时间线 API 按需读取，单遍解析；count = 有效事件总数（与 read_events 口径一致）；
    items 为最近 limit 条且按 ts 降序（最近在前）。文件缺失/解析失败 → (0, [])。
    TASK-037：文件原文经注入 reader（缺省 LocalReader）；read 返回 None = 缺失。
    """
    reader = reader or LocalReader()
    fp = os.path.join(rt, "logs", f"autoloop-{name}-events.jsonl")
    items, count = [], 0
    content = reader.read(fp)
    if content is not None:
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            count += 1
            items.append(ev)
    recent = [
        {"ts": ev.get("ts"), "task": ev.get("task"), "outcome": ev.get("outcome")}
        for ev in items[-limit:]
    ]
    return count, recent[::-1]


def count_files(rt, subdir, prefix, reader=None):
    """统计 runtime/<subdir>/ 下前缀匹配且 .md 结尾的文件数（VERIFY/REVIEW 计数）。

    TASK-037：目录/条目经注入 reader（缺省 LocalReader）；缺失/不可读 → 0。
    """
    reader = reader or LocalReader()
    d = os.path.join(rt, subdir)
    if not reader.exists(d):
        return 0
    return sum(1 for fn in reader.listdir(d) if fn.startswith(prefix) and fn.endswith(".md"))


def read_focus(rt, reader=None):
    """读 runtime/states/CURRENT_FOCUS.md，按 ## 标题提取 current/next 两段。

    TASK-037：文件原文经注入 reader（缺省 LocalReader）；read 返回 None = 缺失 → 空两段。
    """
    reader = reader or LocalReader()
    fp = os.path.join(rt, "states", "CURRENT_FOCUS.md")
    text = reader.read(fp)
    if text is None:
        return {"current": "", "next": ""}
    current, nxt = "", ""
    for m in re.finditer(r"^## (.+?)$(.*?)(?=^## |\Z)", text, re.M | re.S):
        title, body = m.group(1).strip(), m.group(2).strip()
        if "当前" in title or "focus" in title.lower():
            current = body
        elif "下一个" in title or "next" in title.lower():
            nxt = body
    return {"current": current, "next": nxt}


def parse_date_ymd(s):
    """解析 'YYYY-MM-DD' → date；缺失/非法 → None（task-stale 判定跳过）。"""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def task_is_stale(updated, days):
    """非终态任务长期未更新判定（TASK-026）：updated 距今超过 days 天。"""
    d = parse_date_ymd(updated)
    if d is None:
        return False
    return (datetime.now().date() - d).days > days


def derive_project_alerts(p, config=None):
    """派生单项目告警列表（TASK-026，见 MONITOR-SPEC §4.6）。

    规则：项目读取失败 → 仅 read-error；心跳文件存在但 age 超阈值 →
    heartbeat-stale(error)；blocked/total 超 alert_blocked_ratio_threshold →
    blocked-ratio(warn)；非终态任务 updated 超 alert_stale_task_days 天 →
    task-stale(warn)。只读派生，不写被监控项目；心跳缺失不算告警（视为"无进程"）。
    """
    cfg = config or {}
    alerts = []
    if p.get("error"):
        alerts.append({
            "project": p.get("id", ""),
            "level": "error",
            "kind": "read-error",
            "text": "读取失败: " + p["error"],
        })
        return alerts
    stale_secs = cfg.get("heartbeat_stale_threshold_seconds", 300)
    for role in ("coder", "reviewer"):
        hb = (p.get("heartbeat") or {}).get(role) or {}
        if hb.get("exists") and hb.get("age_seconds") is not None and hb["age_seconds"] > stale_secs:
            alerts.append({
                "project": p.get("id", ""),
                "level": "error",
                "kind": "heartbeat-stale",
                "role": role,
                "text": "Coder 心跳卡死" if role == "coder" else "Reviewer 心跳卡死",
            })
    s = p.get("summary") or {}
    total = s.get("total") or 0
    blocked = s.get("blocked") or 0
    ratio = cfg.get("alert_blocked_ratio_threshold", DEFAULT_BLOCKED_RATIO_THRESHOLD)
    if total and blocked and blocked / total > ratio:
        alerts.append({
            "project": p.get("id", ""),
            "level": "warn",
            "kind": "blocked-ratio",
            "blocked": blocked,
            "total": total,
            "threshold": ratio,
            "text": "blocked 占比 {:.0%} 超阈值 {:.0%}".format(blocked / total, ratio),
        })
    stale_days = cfg.get("alert_stale_task_days", DEFAULT_STALE_TASK_DAYS)
    stale_ids = [
        t.get("id") for t in (p.get("tasks") or [])
        if t.get("status") in ALERT_STALE_STATUSES and task_is_stale(t.get("updated"), stale_days)
    ]
    if stale_ids:
        alerts.append({
            "project": p.get("id", ""),
            "level": "warn",
            "kind": "task-stale",
            "count": len(stale_ids),
            "days": stale_days,
            "tasks": stale_ids,
            "text": f"{len(stale_ids)} 个任务超过 {stale_days} 天未更新",
        })
    return alerts


def empty_project(proj):
    return {
        "id": proj["id"],
        "name": proj.get("name", proj["id"]),
        "path": proj["path"],
        # TASK-043：实例级展示元数据透传——group 缺省 = id（§2 多实例分组展示，同逻辑项目
        # 多机器共享 group，供前端区分 baseline-dev/prod 等实例）、transport 缺省 = local
        # （§2；agent 项目前端标记传输方式并展示 agent 离线 error 上下文）。只读透传，
        # 不改变既有字段语义与输出结构（向后兼容）。
        "group": proj.get("group") or proj["id"],
        "transport": proj.get("transport") or "local",
        "error": None,
        "last_read_at": None,
        "summary": {"total": 0, **{st: 0 for st in STATUSES}},
        "tasks": [],
        "focus": {"current": "", "next": ""},
        "heartbeat": {"coder": {"exists": False, "age_seconds": None},
                      "reviewer": {"exists": False, "age_seconds": None}},
        "events": {"coder": {"count": 0, "last": None, "outcomes": {}},
                   "reviewer": {"count": 0, "last": None, "outcomes": {}}},
        "alerts": [],
        "verification_count": 0,
        "review_count": 0,
    }


def heartbeat_alive(info, stale):
    """心跳存活判定（TASK-022）：文件存在且年龄 ≤ 卡死阈值。"""
    if not info.get("exists"):
        return False
    age = info.get("age_seconds")
    return age is None or age <= stale


def reader_for_project(proj, ingest=None):
    """按 transport 选择 FileReader（TASK-038，MONITOR-SPEC §2）。

    local（缺省）→ LocalReader（服务端直读本地文件系统，现状）；
    agent → AgentReader（读 ingest_state；config path 仅作展示，§3.1.2）。
    """
    if proj.get("transport") == "agent":
        return AgentReader(proj, ingest=ingest)
    return LocalReader()


def collect_project(proj, config=None, reader=None, ingest=None):
    """采集单个项目；任何异常记录到 error，不中断其他项目。

    TASK-026：采集后按 MONITOR-SPEC §4.6 派生项目告警（config 提供阈值）。
    TASK-037：文件访问经注入 reader（缺省 LocalReader）——transport: local 直读本地。
    TASK-038：reader 缺省时按 transport 选择——transport: agent 用 AgentReader
    （读 ingest_state，path 仅展示不读本地；ingest 由 State 注入），解析逻辑零改动。
    TASK-039：先判源整体离线（offline_reason）——agent 无记录 / last_seen 超阈值 →
    error='agent 离线' 早退（数据为空 + 仅 read-error 告警 + 历史不采样留缺口，
    §3.1.4 语义）；local 恒 None，走既有 exists(runtime) 错误路径。
    """
    reader = reader or reader_for_project(proj, ingest)
    result = empty_project(proj)
    rt = os.path.join(proj["path"], "runtime")
    cfg = config or {}
    reason = reader.offline_reason(
        cfg.get("heartbeat_stale_threshold_seconds", 300))
    if reason:
        # agent 整体离线（§3.1.4）：error 非空 → 仅 read-error 告警（§4.6 兼容），
        # 数据为空；_record_history 因 error 不采样 → 趋势缺口（现有缺口语义）
        result["error"] = reason
        result["alerts"] = derive_project_alerts(result, config)
        return result
    if not reader.exists(rt):
        result["error"] = "runtime 目录不存在（项目不可达或非 AIOS 项目）"
        result["alerts"] = derive_project_alerts(result, config)
        return result
    try:
        records = index_task_records(rt, reader)
        result["tasks"], result["summary"] = collect_tasks(rt, records, reader)
        result["focus"] = read_focus(rt, reader)
        result["heartbeat"]["coder"] = read_heartbeat(rt, "coder", reader)
        result["heartbeat"]["reviewer"] = read_heartbeat(rt, "reviewer", reader)
        result["events"]["coder"] = read_events(rt, "coder", reader)
        result["events"]["reviewer"] = read_events(rt, "reviewer", reader)
        result["verification_count"] = count_files(rt, "verification", "VERIFY", reader)
        result["review_count"] = count_files(rt, "reviews", "REVIEW", reader)
        result["last_read_at"] = time.time()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    result["alerts"] = derive_project_alerts(result, config)
    return result


class HistoryStore:
    """历史快照存储（TASK-022）：stdlib sqlite3，记录每轮轮询快照。

    表 snapshots(ts, project, summary_json, coder_alive, reviewer_alive)：
    - 每轮每项目一行；ts+project 复合主键（同轮重跑 INSERT OR REPLACE 覆盖）
    - summary_json 为 §4 summary 的 JSON（total + 各状态计数）
    - 单写者（轮询线程） + HTTP 读线程；每次操作独立连接，WAL 保证读写并发
    """

    def __init__(self, db_path, retention_days=DEFAULT_HISTORY_RETENTION_DAYS):
        self.db_path = db_path
        self.retention_seconds = int(retention_days) * 86400
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "CREATE TABLE IF NOT EXISTS snapshots ("
                        " ts INTEGER NOT NULL,"
                        " project TEXT NOT NULL,"
                        " summary_json TEXT NOT NULL,"
                        " coder_alive INTEGER NOT NULL,"
                        " reviewer_alive INTEGER NOT NULL,"
                        " PRIMARY KEY (ts, project))"
                    )
            finally:
                conn.close()

    def record(self, ts, project, summary, coder_alive, reviewer_alive):
        """写入一行快照（同轮同项目重复调用时覆盖）。"""
        payload = json.dumps(summary, ensure_ascii=False, sort_keys=True)
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?)",
                        (int(ts), project, payload,
                         int(bool(coder_alive)), int(bool(reviewer_alive))),
                    )
            finally:
                conn.close()

    def prune(self):
        """惰性清理超过保留期的行（每轮轮询后调用）。"""
        cutoff = int(time.time()) - self.retention_seconds
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
            finally:
                conn.close()

    def latest_ts(self, project):
        """返回该项目最近一次快照的 ts（epoch 秒）；无记录 → None（TASK-040 节流用）。"""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT MAX(ts) FROM snapshots WHERE project=?",
                    (project,),
                ).fetchone()
            finally:
                conn.close()
        return row[0] if row else None

    def query(self, project, hours):
        """返回 [now - hours*3600, now] 窗口内该项目的升序时间序列。"""
        cutoff = int(time.time()) - int(hours * 3600)
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT ts, summary_json, coder_alive, reviewer_alive"
                    " FROM snapshots WHERE project=? AND ts>=? ORDER BY ts",
                    (project, cutoff),
                ).fetchall()
            finally:
                conn.close()
        return [
            {"ts": ts, "summary": json.loads(sj),
             "coder_alive": bool(ca), "reviewer_alive": bool(ra)}
            for ts, sj, ca, ra in rows
        ]


class IngestStore:
    """agent 推送存储层（TASK-033，见 MONITOR-SPEC §3.1.3/§3.1.4）：stdlib sqlite3。

    表 ingest_state(project_id PK, payload_json, last_seen, agent_id)：
    - project_id 主键：同 id 重复推送 INSERT OR REPLACE 覆盖写（幂等，§3.1.3）
    - payload_json 为 §3.1.3 请求体 files 的原始 JSON（解析留待采集层 TASK-038）
    - last_seen 为最近成功推送时间（epoch 秒），agent 整体离线判定依据（§3.1.4）
    - agent_id 记录最近推送者，TASK-036 同 id 双 agent 冲突判定（409）依据

    连接模式复用 HistoryStore（TASK-022）：每次操作独立连接 + WAL 保证读写并发；
    _connect 内幂等建表（CREATE TABLE IF NOT EXISTS）→ 连接丢失或 db 文件重建后
    自动重连自愈。默认库位置与 HistoryStore 同目录 data/ingest.db（由接入方注入）。
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._connect().close()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ingest_state ("
                " project_id TEXT PRIMARY KEY,"
                " payload_json TEXT NOT NULL,"
                " last_seen INTEGER NOT NULL,"
                " agent_id TEXT NOT NULL)"
            )
        except Exception:
            conn.close()
            raise
        return conn

    def upsert(self, project_id, payload, agent_id):
        """覆盖写一行 ingest_state（幂等）；返回写入的 last_seen（epoch 秒）。

        payload 为 §3.1.3 请求体的 files 对象（dict）；序列化 JSON 存 payload_json。
        """
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        last_seen = int(time.time())
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO ingest_state"
                        " (project_id, payload_json, last_seen, agent_id)"
                        " VALUES (?,?,?,?)",
                        (project_id, payload_json, last_seen, agent_id),
                    )
            finally:
                conn.close()
        return last_seen

    def claim_or_update(self, project_id, payload, agent_id):
        """同 id 单 agent 语义（TASK-036，MONITOR-SPEC §3.1.6）：原子「查归属 + 写」。

        返回 ("ok", last_seen) 或 ("conflict", owner_agent_id)：
        - 无既有行 → 写入（owner = 当前 agent）
        - 既有行 owner == agent_id → 幂等覆盖写（同 agent 重复推送，§3.1.3 幂等行）
        - 既有行 owner != agent_id → 不写入，返回 owner（后到者拒绝 → 409，防互相覆盖污染）
        查询与写入在同一连接同一事务 + self.lock 内完成（单进程 ThreadingHTTPServer 内线程安全），
        事务失败自动回滚；两 agent 并发抢同一 id 只有一个成功。
        """
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        last_seen = int(time.time())
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    row = conn.execute(
                        "SELECT agent_id FROM ingest_state WHERE project_id=?",
                        (project_id,),
                    ).fetchone()
                    if row is not None and row[0] != agent_id:
                        return ("conflict", row[0])
                    conn.execute(
                        "INSERT OR REPLACE INTO ingest_state"
                        " (project_id, payload_json, last_seen, agent_id)"
                        " VALUES (?,?,?,?)",
                        (project_id, payload_json, last_seen, agent_id),
                    )
            finally:
                conn.close()
        return ("ok", last_seen)

    def read(self, project_id):
        """读取一行 → {project_id, payload, last_seen, agent_id}；无记录 → None。"""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT project_id, payload_json, last_seen, agent_id"
                    " FROM ingest_state WHERE project_id=?",
                    (project_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return {"project_id": row[0], "payload": json.loads(row[1]),
                "last_seen": row[2], "agent_id": row[3]}

    def read_all(self):
        """枚举全部 ingest_state 行（供采集层按 transport 选 agent 项目，TASK-038）。"""
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT project_id, payload_json, last_seen, agent_id"
                    " FROM ingest_state ORDER BY project_id",
                ).fetchall()
            finally:
                conn.close()
        return [{"project_id": r[0], "payload": json.loads(r[1]),
                 "last_seen": r[2], "agent_id": r[3]} for r in rows]


class IngestRateLimiter:
    """ingest 限流器（TASK-036，MONITOR-SPEC §3.1.3）：每 agent 每分钟 N 次，超限 429。

    固定窗口计数（分钟粒度）：同一窗口内计数 ≥ limit → 拒绝；跨窗口自动重置。
    线程安全（ThreadingHTTPServer 多线程并发请求）；clock 可注入便于测试确定性。
    """

    def __init__(self, limit_per_minute, clock=time.time):
        self.limit = max(1, int(limit_per_minute))
        self.clock = clock
        self.lock = threading.Lock()
        self._buckets = {}

    def allow(self, agent_id):
        """记录一次请求；未超限 → True；超限 → False（不再累加，计数封顶）。"""
        minute = int(self.clock() // 60)
        with self.lock:
            m, c = self._buckets.get(agent_id, (minute, 0))
            if m != minute:
                m, c = minute, 0
            if c >= self.limit:
                self._prune_locked(minute)
                return False
            self._buckets[agent_id] = (m, c + 1)
            self._prune_locked(minute)
            return True

    def _prune_locked(self, minute):
        """防内存无界增长：桶数量超阈值时清理非当前窗口条目（须持锁调用）。"""
        if len(self._buckets) > 1024:
            stale = [k for k, (m, _) in self._buckets.items() if m != minute]
            for k in stale:
                del self._buckets[k]


class State:
    """聚合缓存 + 后台轮询线程（daemon）。"""

    def __init__(self, config, quiet=False, db_path=None, ingest_db_path=None, agents_path=None,
                 rate_clock=None):
        self.config = config
        self.quiet = quiet
        self.lock = threading.Lock()
        self.payload = self._build_payload([])
        # 历史快照存储（TASK-022）：默认 <ROOT>/data/history.db，测试可注入临时路径
        db_path = db_path or os.path.join(ROOT, *HISTORY_DB_REL)
        self.history = HistoryStore(
            db_path,
            retention_days=config.get("history_retention_days", DEFAULT_HISTORY_RETENTION_DAYS),
        )
        # agent 推送存储（TASK-033/034）：默认 <ROOT>/data/ingest.db，测试可注入临时路径
        ingest_db_path = ingest_db_path or os.path.join(ROOT, *INGEST_DB_REL)
        self.ingest = IngestStore(ingest_db_path)
        # agent token 配置（TASK-035）：config/agents.json（权限 600，gitignored），测试可注入临时路径
        agents_path = agents_path or os.path.join(ROOT, *AGENTS_CONFIG_REL)
        self.agents = load_agents_config(agents_path)
        if not self.agents:
            self._log(f"⚠ {os.path.relpath(agents_path, ROOT)} 缺失或不可用 → "
                      "/api/ingest 全部 401（fail-closed）")
        # ingest 限流（TASK-036）：每 agent 每分钟 N 次（config.projects.json 顶层可配置）；
        # rate_clock 仅供测试注入确定性时钟，生产用 time.time
        self.rate_limiter = IngestRateLimiter(
            config.get("ingest_rate_limit_per_minute", DEFAULT_INGEST_RATE_LIMIT_PER_MINUTE),
            clock=rate_clock or time.time,
        )
        self._start_poller()

    def _log(self, msg):
        if not self.quiet:
            print(f"[{datetime.now().strftime('%F %T')}] {msg}", flush=True)

    def _build_payload(self, projects_data):
        items = []
        for p in projects_data:
            items.extend(p.get("alerts") or [])
        return {
            "generated_at": time.time(),
            "poll_interval_seconds": self.config.get("poll_interval_seconds", 30),
            "heartbeat_stale_threshold_seconds": self.config.get("heartbeat_stale_threshold_seconds", 300),
            "alerts": {"count": len(items), "items": items},
            "projects": projects_data,
        }

    def poll(self):
        t0 = time.time()
        data = [collect_project(p, self.config, ingest=self.ingest)
                for p in self.config.get("projects", [])]
        with self.lock:
            self.payload = self._build_payload(data)
        self._record_history(data)
        self._log(f"轮询完成：{len(data)} 个项目，耗时 {time.time() - t0:.2f}s")

    def _record_history(self, data):
        """每轮为每个读取成功的项目写历史快照（TASK-022）。

        项目读取失败（error 非空）时不采样——时间序列留缺口，避免把"读不到"
        伪装成"全部归零"误导趋势图。
        """
        ts = int(time.time())
        stale = self.config.get("heartbeat_stale_threshold_seconds", 300)
        for p in data:
            if p.get("error"):
                continue
            hb = p.get("heartbeat", {})
            self.history.record(
                ts, p["id"], p.get("summary", {}),
                coder_alive=heartbeat_alive(hb.get("coder", {}), stale),
                reviewer_alive=heartbeat_alive(hb.get("reviewer", {}), stale),
            )
        self.history.prune()

    def record_ingest_snapshot(self, project_id):
        """TASK-040：ingest 成功落库后立即写一行 HistoryStore 快照（趋势即时反映推送）。

        - 复用 collect_project（与轮询采样同口径：summary/存活标记一致），error 非空不采样
          （与 _record_history 语义一致；刚推送的 payload 已过 schema 校验，理论上不触发）
        - 节流：距该项目最近快照 < poll_interval_seconds（缺省 30）→ 跳过——高频推送不撑爆
          history.db（行数上界 ≈ 轮询密度）；轮询采样路径不变，仍兜底补点
        - 项目未注册 → 直接返回（ingest 端点已校验注册，此处防御）
        """
        proj = next((p for p in self.config.get("projects", [])
                     if p.get("id") == project_id), None)
        if proj is None:
            return
        now = int(time.time())
        min_interval = self.config.get("poll_interval_seconds", 30)
        last = self.history.latest_ts(project_id)
        if last is not None and now - last < min_interval:
            return
        data = collect_project(proj, self.config, ingest=self.ingest)
        if data.get("error"):
            return
        stale = self.config.get("heartbeat_stale_threshold_seconds", 300)
        self.history.record(
            now, project_id, data.get("summary", {}),
            coder_alive=heartbeat_alive(data.get("heartbeat", {}).get("coder", {}), stale),
            reviewer_alive=heartbeat_alive(data.get("heartbeat", {}).get("reviewer", {}), stale),
        )

    def _start_poller(self):
        interval = self.config.get("poll_interval_seconds", 30)

        def loop():
            while True:
                try:
                    self.poll()
                except Exception as e:
                    self._log(f"轮询异常：{e}")
                time.sleep(interval)

        threading.Thread(target=loop, daemon=True, name="poller").start()
        self._log(f"后台轮询已启动（间隔 {interval}s）")


def validate_ingest_payload(obj):
    """校验 POST /api/ingest 请求体 schema（TASK-034，MONITOR-SPEC §3.1.3）。

    返回错误消息字符串；合法返回 None。
    - 必填：project_id（非空字符串）、ts（有限数值 ≥0，排除 bool/NaN/inf）、files（对象）
    - files 可选字段（TASK-042 起为 **AIOS 通用遥测格式** file-oriented 文件条目数组，
      与 aibase 组件 agent 输出零转换对接；类型错才 400）：
      tasks（数组，条目 {name: 非空字符串, content?: 字符串|null}）、
      focus（字符串|null）、
      heartbeats（数组，条目 {file: 非空字符串, mtime: 有限数值 ≥0}）、
      events（数组，条目 {name: 非空字符串, content?: 字符串|null}）、
      verification_count / review_count（非负整数|null，null = 目录缺失）
    - 宽松语义：忽略未知键；空数组 / 计数 null 不报错（与代码库宽松风格一致）
    """
    if not isinstance(obj, dict):
        return "请求体必须为 JSON 对象"
    project_id = obj.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        return "project_id 必须为非空字符串"
    ts = obj.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return "ts 必须为数值（epoch 秒）"
    if not math.isfinite(ts) or ts < 0:
        return "ts 必须为有限数值且 ≥ 0"
    files = obj.get("files")
    if not isinstance(files, dict):
        return "files 必须为对象"

    # TASK-042 契约切换：legacy role-oriented 格式的 files.heartbeat（dict）不再接受。
    # 宽松语义只适用于未知键（向前兼容）；heartbeat 是已知的旧契约键，静默忽略会造成
    # 「200 但心跳数据丢失」——显式 400 引导改为 file-oriented 的 files.heartbeats 数组。
    if "heartbeat" in files:
        return "files.heartbeat 已废弃（TASK-042 起使用 files.heartbeats 数组）"

    tasks = files.get("tasks")
    if tasks is not None:
        if not isinstance(tasks, list):
            return "files.tasks 必须为数组（AIOS 遥测格式：{name, content}）"
        for i, entry in enumerate(tasks):
            if not isinstance(entry, dict):
                return f"files.tasks[{i}] 必须为对象"
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                return f"files.tasks[{i}].name 必须为非空字符串"
            content = entry.get("content")
            if content is not None and not isinstance(content, str):
                return f"files.tasks.{name}.content 必须为字符串或 null"

    focus = files.get("focus")
    if focus is not None and not isinstance(focus, str):
        return "files.focus 必须为字符串或 null"

    heartbeats = files.get("heartbeats")
    if heartbeats is not None:
        if not isinstance(heartbeats, list):
            return "files.heartbeats 必须为数组（AIOS 遥测格式：{file, mtime}）"
        for i, entry in enumerate(heartbeats):
            if not isinstance(entry, dict):
                return f"files.heartbeats[{i}] 必须为对象"
            fname = entry.get("file")
            if not isinstance(fname, str) or not fname.strip():
                return f"files.heartbeats[{i}].file 必须为非空字符串"
            mt = entry.get("mtime")
            if isinstance(mt, bool) or not isinstance(mt, (int, float)):
                return f"files.heartbeats.{fname}.mtime 必须为数值"
            if not math.isfinite(mt) or mt < 0:
                return f"files.heartbeats.{fname}.mtime 必须为有限数值且 ≥ 0"

    events = files.get("events")
    if events is not None:
        if not isinstance(events, list):
            return "files.events 必须为数组（AIOS 遥测格式：{name, content}）"
        for i, entry in enumerate(events):
            if not isinstance(entry, dict):
                return f"files.events[{i}] 必须为对象"
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                return f"files.events[{i}].name 必须为非空字符串"
            content = entry.get("content")
            if content is not None and not isinstance(content, str):
                return f"files.events.{name}.content 必须为字符串或 null"

    for key in ("verification_count", "review_count"):
        if key in files:
            val = files[key]
            if val is not None:
                if isinstance(val, bool) or not isinstance(val, int) or val < 0:
                    return f"files.{key} 必须为非负整数或 null"
    return None


def load_agents_config(path=None):
    """加载 config/agents.json（TASK-035，MONITOR-SPEC §3.1.2）→ dict。

    fail-closed 语义：任何不可用情形返回 {}（= 无有效 token → 全部 ingest 401），
    绝不带病加载部分密钥：
    - 文件缺失 → {}（未部署 agent 时正常，服务端只服务 local 项目）
    - 权限过宽（非 600，group/other 可读）→ 拒绝加载 + 告警
    - JSON 非法 / 顶层非对象 → {} + 告警

    返回结构即文件 JSON 原样（扁平 {project_id: token} 或 {agent_id: {token, projects}}），
    由 resolve_agent_id 统一解析两种格式。
    """
    path = path or os.path.join(ROOT, *AGENTS_CONFIG_REL)
    if not os.path.isfile(path):
        return {}
    try:
        if os.stat(path).st_mode & 0o077:
            print(f"⚠ {path} 权限不是 600（group/other 可读），拒绝加载 token（fail-closed）",
                  file=sys.stderr)
            return {}
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"⚠ 读取 {path} 失败（{e}），拒绝加载 token（fail-closed）", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"⚠ {path} 顶层必须是 JSON 对象，拒绝加载 token（fail-closed）", file=sys.stderr)
        return {}
    return data


def extract_bearer_token(authorization):
    """解析 Authorization 头 → Bearer token；缺失/格式错/空 token → None。"""
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _token_eq(a, b):
    """常量时间字符串比较（token 为密钥，避免 == 的时序侧信道）。"""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def resolve_agent_id(agents, token):
    """校验 Bearer token → 返回 agent 身份（str）；无效 → None（TASK-035，§3.1.2）。

    支持两种配置格式：
    - 扁平 {"<project_id>": "<token>"}：token 属于该 project 的专属 agent，
      身份取 project_id（每项目一 token，天然单 agent）
    - agent {"<agent_id>": {"token": "...", "projects": [...]}}：身份取 agent_id
    token 用常量时间比较，避免时序侧信道。
    只做"token 是否有效"（鉴权）；token 对某 project_id 的授权范围由 TASK-036
    （is_project_authorized）判定。
    """
    if not token:
        return None
    for key, entry in agents.items():
        if isinstance(entry, str) and _token_eq(entry, token):
            return key
        if (isinstance(entry, dict) and isinstance(entry.get("token"), str)
                and _token_eq(entry["token"], token)):
            return key
    return None


def authorized_projects(agents, agent_id):
    """token 身份对应的授权项目集合（TASK-036，MONITOR-SPEC §3.1.2/§3.1.3）。

    - 扁平格式 {"<project_id>": "<token>"}：身份即 project_id（每项目一专属 token，
      天然单 agent），授权集合 = {该 id}
    - agent 格式 {"<agent_id>": {"token", "projects"}}：授权集合 = projects 列表
    projects 缺失 / 非列表 / 含非字符串元素 → 空集合（fail-closed，该 agent 无可推送项目）。
    """
    entry = agents.get(agent_id)
    if isinstance(entry, str):
        return {agent_id}
    if isinstance(entry, dict):
        projects = entry.get("projects")
        if isinstance(projects, list):
            return {p for p in projects if isinstance(p, str) and p}
    return set()


def is_project_authorized(agents, agent_id, project_id):
    """project_id 是否在 token 授权项目集合内（TASK-036，§3.1.3 授权范围行）。"""
    return project_id in authorized_projects(agents, agent_id)


def is_project_registered(config, project_id):
    """project_id 是否已注册在 config/projects.json（TASK-036，§3.1.3 未注册拦截）。"""
    return any(p.get("id") == project_id for p in config.get("projects", []))


def apply_task_filters(tasks, filters):
    """按 status/priority/assignee 精确匹配 + q 大小写不敏感子串搜索过滤任务（TASK-025）。

    多参数 AND；q 匹配 id/name/description/assignee 拼接文本；无筛选条件原样返回。
    宽松语义：未知筛选值 → 空列表（调用方返回 200），不做枚举校验——
    status/priority/assignee 均为数据驱动字段，项目可能使用非标准值。
    """
    status = filters.get("status")
    priority = filters.get("priority")
    assignee = filters.get("assignee")
    q = (filters.get("q") or "").strip().lower()
    if not (status or priority or assignee or q):
        return tasks
    out = []
    for t in tasks:
        if status and t.get("status") != status:
            continue
        if priority and (t.get("priority") or "") != priority:
            continue
        if assignee and (t.get("assignee") or "") != assignee:
            continue
        if q:
            hay = " ".join([
                t.get("id") or "",
                t.get("name") or "",
                t.get("description") or "",
                t.get("assignee") or "",
            ]).lower()
            if q not in hay:
                continue
        out.append(t)
    return out


class ApiHandler(BaseHTTPRequestHandler):
    state = None
    static_dir = None

    def do_GET(self):
        path, _, query = self.path.partition("?")
        m = EVENTS_RE.match(path)
        if m:
            self._events(m.group(1), query)
        elif path == "/api/status":
            self._json(self._status_payload(query))
        elif path == "/api/history":
            self._history(query)
        else:
            self._static()

    def do_POST(self):
        """POST /api/ingest（TASK-034/035/036，MONITOR-SPEC §3.1.3）：鉴权 → 限流 → 解析 → 授权 → 落库。

        顺序：先鉴权（401）再限流（429）再读体——未认证请求不消耗解析资源，也不泄露任何数据；
        授权范围（403）/ 未注册（400）/ 同 id 双 agent（409）由 _ingest 内检查。
        错误：401（无/错 token）/ 429（限流超限）/ 400（schema 错 / project_id 未注册）/
        403（project_id 不在授权范围）/ 409（同 id 已被另一 agent 占用）/ 413（payload 超限）/
        404（非 ingest 路径）/ 500（落库失败）。
        """
        if self.path.split("?", 1)[0] != "/api/ingest":
            self._json_error(404, "未找到端点")
            return
        token = extract_bearer_token(self.headers.get("Authorization", ""))
        agent_id = resolve_agent_id(ApiHandler.state.agents, token)
        if agent_id is None:
            # 401 不泄露数据：不区分"缺失"与"错误"token，也不返回任何项目/schema 信息
            self._json_error(401, "鉴权失败")
            return
        # 限流（TASK-036，§3.1.3）：每 agent 每分钟 N 次（可配置），超限 429；
        # 鉴权通过后立即计数（读体之前，保护解析资源）；未认证请求不占额度
        if not ApiHandler.state.rate_limiter.allow(agent_id):
            self._json_error(429, "请求过于频繁，请稍后重试")
            return
        self._ingest(agent_id)

    def _read_body(self):
        """读取请求体；返回 (data, None) 或 (None, 413 错误消息)。

        先按 Content-Length 声明拒绝超限（快路径），再按上限保护性读取
        （无 Content-Length / chunked / 声明与实际不符兜底）。
        """
        try:
            declared = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            declared = 0
        # REVIEW MED-001：负数 Content-Length（如 -1）钳制为 0——否则 int("-1")=-1
        # 为真值分支走 rfile.read(-1) 读到 EOF，绕过 MAX+1 保护性读取上限（无界内存缓冲）
        declared = max(0, declared)
        if declared > MAX_INGEST_PAYLOAD_BYTES:
            return None, f"payload 超限（上限 {MAX_INGEST_PAYLOAD_BYTES} 字节）"
        if declared:
            data = self.rfile.read(declared)
        else:
            data = self.rfile.read(MAX_INGEST_PAYLOAD_BYTES + 1)
        if len(data) > MAX_INGEST_PAYLOAD_BYTES:
            return None, f"payload 超限（上限 {MAX_INGEST_PAYLOAD_BYTES} 字节）"
        return data, None

    def _ingest(self, agent_id):
        data, err = self._read_body()
        if err:
            self._json_error(413, err)
            return
        try:
            obj = json.loads(data)
        except (UnicodeDecodeError, ValueError):
            self._json_error(400, "请求体不是合法 JSON")
            return
        if not isinstance(obj, dict):
            self._json_error(400, "请求体必须为 JSON 对象")
            return
        # 授权范围（TASK-036，§3.1.3）：先做最小 project_id 提取，越权检查先于完整 schema
        # 校验与未注册检查——未授权 agent 只能得到统一 403，无法探测注册状态（防跨项目污染）
        project_id = obj.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            self._json_error(400, "project_id 必须为非空字符串")
            return
        if not is_project_authorized(ApiHandler.state.agents, agent_id, project_id):
            self._json_error(403, "project_id 不在授权范围内")
            return
        # 未注册 project_id 拦截（TASK-036，§3.1.3）：仅对已授权该项目的 agent 暴露 400
        if not is_project_registered(ApiHandler.state.config, project_id):
            self._json_error(400, "project_id 未注册")
            return
        err = validate_ingest_payload(obj)
        if err:
            self._json_error(400, err)
            return
        try:
            # TASK-036：原子查归属+写（同 agent 幂等覆盖；异 agent → 409 拒绝，防互相覆盖）
            outcome, detail = ApiHandler.state.ingest.claim_or_update(
                project_id, obj["files"], agent_id)
        except Exception as e:
            # 存储异常不返回 200（幂等由存储层保证；此处仅隔离端点失败）
            # REVIEW MIN-001：500 不透传异常内部细节（db 路径/sqlite 错误），只记服务端日志
            print(f"[{datetime.now().strftime('%F %T')}] 落库失败 project_id={project_id}: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            self._json_error(500, "落库失败")
            return
        if outcome == "conflict":
            # 同 id 双 agent 冲突（§3.1.6）：记录 owner（agent id 非密钥），后到者拒绝、不覆盖
            self._json_error(409, f"项目已被另一 agent 占用（当前归属: {detail}）")
            return
        # TASK-040：ingest 到达 → 立即写 HistoryStore 快照（趋势即时反映推送；/api/history 对
        # agent 项目不回归）。best-effort：失败不影响 ingest 200——推送已落库 ingest_state，
        # 轮询仍会补采样（§3.1.4 恢复推送自动恢复）；仅记服务端日志。
        try:
            ApiHandler.state.record_ingest_snapshot(project_id)
        except Exception as e:
            print(f"[{datetime.now().strftime('%F %T')}] ingest 历史快照失败 "
                  f"project_id={project_id}: {type(e).__name__}: {e}", file=sys.stderr)
        self._json({"ok": True, "project_id": project_id})

    def _status_payload(self, query=""):
        """GET /api/status（TASK-025 支持筛选）：无参数直接返回轮询缓存；
        有参数时对 projects[].tasks 派生筛选视图（summary 等聚合字段保持全量）。"""
        params = parse_qs(query)
        filters = {}
        for key in ("status", "priority", "assignee", "q"):
            v = (params.get(key) or [None])[0]
            if v:
                filters[key] = v
        with ApiHandler.state.lock:
            payload = ApiHandler.state.payload
        if not filters:
            return payload
        projects = []
        for p in payload.get("projects", []):
            p2 = dict(p)
            if isinstance(p2.get("tasks"), list):
                p2["tasks"] = apply_task_filters(p2["tasks"], filters)
            projects.append(p2)
        return {**payload, "projects": projects}

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, status, message):
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _history(self, query):
        """GET /api/history?project=<id>&hours=<n>（TASK-022，见 MONITOR-SPEC §4.2）。"""
        params = parse_qs(query)
        project = (params.get("project") or [""])[0]
        if not project:
            self._json_error(400, "缺少 project 参数")
            return
        try:
            hours = float((params.get("hours") or ["24"])[0])
        except ValueError:
            self._json_error(400, "hours 必须为数字")
            return
        # REVIEW BUG-001：NaN 能通过纯范围检查（nan<=0 与 nan>8760 均为 False），
        # 后续 query() 中 int(nan*3600) 抛 ValueError 泄漏 traceback；非有限数一律 400。
        if not math.isfinite(hours) or hours <= 0 or hours > MAX_HISTORY_HOURS:
            self._json_error(400, f"hours 超出范围 (0, {MAX_HISTORY_HOURS}]")
            return
        # REVIEW NOTE-002：整数输入返回 int（与 MONITOR-SPEC §4.2 示例一致），浮点输入保留原样
        if hours.is_integer():
            hours = int(hours)
        points = ApiHandler.state.history.query(project, hours)
        self._json({
            "project": project,
            "hours": hours,
            "generated_at": time.time(),
            "points": points,
        })

    def _events(self, project_id, query):
        """GET /api/projects/<id>/events?limit=<n>（TASK-023，见 MONITOR-SPEC §4.3）。

        项目 id 未注册 → 404；limit 默认 10、(0,100] 正整数，非法 → 400。
        响应：{project, limit, generated_at, counts, events}，每 role 最近 limit 条 ts 降序。
        """
        proj = next((p for p in ApiHandler.state.config.get("projects", [])
                     if p.get("id") == project_id), None)
        if proj is None:
            self._json_error(404, f"项目不存在: {project_id}")
            return
        params = parse_qs(query)
        limit = DEFAULT_EVENT_LIMIT
        raw = (params.get("limit") or [None])[0]
        if raw is not None:
            try:
                limit = float(raw)
            except ValueError:
                self._json_error(400, "limit 必须为数字")
                return
            # REVIEW BUG-001 同款防护：非有限数（NaN/inf）能通过纯范围检查，必须显式拒绝
            if not math.isfinite(limit) or limit <= 0 or limit > MAX_EVENT_LIMIT:
                self._json_error(400, f"limit 超出范围 (0, {MAX_EVENT_LIMIT}]")
                return
            if not limit.is_integer():
                self._json_error(400, "limit 必须为整数")
                return
            limit = int(limit)
        rt = os.path.join(proj["path"], "runtime")
        # TASK-038：按 transport 选 reader——agent 项目事件从 ingest_state 读（path 仅展示）
        reader = reader_for_project(proj, ApiHandler.state.ingest)
        events, counts = {}, {}
        for who in ("coder", "reviewer"):
            counts[who], events[who] = read_events_timeline(rt, who, limit, reader)
        self._json({
            "project": project_id,
            "limit": limit,
            "generated_at": time.time(),
            "counts": counts,
            "events": events,
        })

    def _static(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            path = "/index.html"
        base = os.path.abspath(ApiHandler.static_dir)
        full = os.path.abspath(os.path.join(base, path.lstrip("/")))
        # 防目录穿越（CWE-22 修复，reviewer SEC-001）：
        # 用 commonpath 做边界安全比较，替换 naive startswith 前缀检查——
        # 后者会被 src-evil-poc/ 这类同前缀兄弟目录绕过（prefix boundary bug）。
        if os.path.commonpath([full, base]) != base:
            self.send_error(403)
            return
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            self.send_error(404)
            return
        ctype = ("text/html" if full.endswith(".html") else
                 "text/css" if full.endswith(".css") else
                 "application/javascript" if full.endswith(".js") else
                 "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass  # 默认静默请求日志；错误仍由 BaseHTTPRequestHandler 走 stderr


def main():
    ap = argparse.ArgumentParser(description="aimonitor 监控后端")
    ap.add_argument("--port", type=int, default=3113)
    ap.add_argument("--dev", action="store_true", help="服务 src/（开发）而非 dist/")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print(f"✗ 读取 {CONFIG_PATH} 失败: {e}", file=sys.stderr)
        sys.exit(1)

    ApiHandler.state = State(config, quiet=args.quiet)
    ApiHandler.static_dir = os.path.join(ROOT, "src" if args.dev else "dist")
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), ApiHandler)
    print(f"aimonitor 监听 http://0.0.0.0:{args.port}  (static: {ApiHandler.static_dir})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


if __name__ == "__main__":
    main()
