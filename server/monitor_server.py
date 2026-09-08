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
  可选顶层 sessions 增量（TASK-073，契约 v1.1 = aibase TASK-104 agent 端）：按游标续传落库 +
  GET /api/projects/:id/sessions 查询端点（task/file 分组 + 消息级行读取，前端轮询查看页）

用法:
  python3 server/monitor_server.py [--port 3113] [--dev] [--quiet]
"""
import abc
import argparse
import fnmatch
import hmac
import json
import math
import os
import secrets
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from urllib.request import Request, urlopen
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config", "projects.json")
# projects.json 读写锁（TASK-069）：审批并发写保护——register_project_in_config 的
# 读-改-写（load → append → os.replace）需要互斥，否则两个并发审批可能互相覆盖丢条目
PROJECTS_CONFIG_LOCK = threading.Lock()
STATUSES = ("open", "in-progress", "in-review", "blocked", "done", "cancelled")

# 历史快照存储（TASK-022）：aimonitor 自身 data/，不入被监控项目
HISTORY_DB_REL = ("data", "history.db")
DEFAULT_HISTORY_RETENTION_DAYS = 90
MAX_HISTORY_HOURS = 24 * 365  # hours 参数上限（365 天）

# 事件时间线 API（TASK-023）：limit 参数默认值与上限
DEFAULT_EVENT_LIMIT = 10
MAX_EVENT_LIMIT = 100

EVENTS_RE = re.compile(r"^/api/projects/([^/]+)/events$")
SESSIONS_RE = re.compile(r"^/api/projects/([^/]+)/sessions$")
STATUS_RE = re.compile(r"^/api/register/([^/]+)/status$")
APPROVE_RE = re.compile(r"^/api/register/([^/]+)/approve$")
REJECT_RE = re.compile(r"^/api/register/([^/]+)/reject$")
REVOKE_RE = re.compile(r"^/api/register/([^/]+)/revoke$")
RENEW_RE = re.compile(r"^/api/register/([^/]+)/renew$")

# 注册码管理端点（TASK-057）
CODES_GENERATE_RE = re.compile(r"^/api/register/codes/generate$")
CODES_REVOKE_RE = re.compile(r"^/api/register/codes/([^/]+)/revoke$")
# 下行指令队列（TASK-035，AGENT-DOWNLINK-CONTRACT）
DOWNLINK_RESULT_RE = re.compile(r"^/api/downlink/commands/(\d+)/result$")
DOWNLINK_STATUS_RE = re.compile(r"^/api/downlink/commands/(\d+)$")

# 下行指令端点（TASK-071，AGENT-DOWNLINK-CONTRACT v1.0 §一）
DOWNLINK_COMMANDS_RE = re.compile(r"^/api/downlink/commands$")
DOWNLINK_COMMAND_ID_RE = re.compile(r"^/api/downlink/commands/([^/]+)$")
DOWNLINK_PICKUP_RE = re.compile(r"^/api/downlink/pickup$")
DOWNLINK_RESULT_RE = re.compile(r"^/api/downlink/commands/([^/]+)/result$")
# 命令白名单（契约 §二：server 第一道闸；agent 侧独立枚举，不共享代码路径）
DOWNLINK_COMMAND_NAMES = ("task_start", "autoloop_coder", "autoloop_reviewer")
DOWNLINK_PICKUP_TIMEOUT_SECS = 90     # 契约 §三：> 2×poll_interval(10s)
DOWNLINK_MAX_REDELIVERIES = 2         # pickup 超时重投 ≤2 次 → failed(human)
DOWNLINK_DEFAULT_TIMEOUT_SECS = 1800  # 契约 §二：执行超时缺省
DOWNLINK_MAX_TIMEOUT_SECS = 86400     # 保守上限 1 天，防滥用
DOWNLINK_RESULT_MAX_LINES = 200       # 契约 §四：tail ≤200 行
DOWNLINK_TERMINAL_STATUSES = ("done", "failed", "skipped")
# §四 脱敏（server 侧独立实现，与 agent_downlink.SECRET_LINE_RE 各自维护）
DOWNLINK_SECRET_LINE_RE = re.compile(r"authorization|bearer|token", re.IGNORECASE)

# 告警派生（TASK-026，见 MONITOR-SPEC §4.6）：默认阈值与参与 task-stale 判定的非终态状态
DEFAULT_BLOCKED_RATIO_THRESHOLD = 0.2
DEFAULT_STALE_TASK_DAYS = 14
ALERT_STALE_STATUSES = ("open", "in-progress", "in-review", "blocked")

# agent 推送存储（TASK-033/034）：默认库位置与 HistoryStore 同目录 data/ingest.db
INGEST_DB_REL = ("data", "ingest.db")
DOWNLINK_DB_REL = ("data", "downlink.db")
# 下行指令（TASK-035，AGENT-DOWNLINK-CONTRACT §二/§三）：白名单/超时/重投上限/tail 截断
ALLOWED_DOWNLINK_COMMANDS = frozenset({"task_start", "autoloop_coder", "autoloop_reviewer"})
DOWNLINK_PICKUP_TIMEOUT_DEFAULT = 90   # > 2×poll_interval(30s)：未拾取即 stale
DOWNLINK_MAX_REQUEUE = 2               # 重投 ≤2 次后 failed(human)
DOWNLINK_TAIL_MAX_LINES = 200
DOWNLINK_TERMINAL_STATUSES = ("done", "failed", "skipped")
# 注册审批存储（TASK-047）：默认库位置 data/registration.db
REGISTRATION_DB_REL = ("data", "registration.db")
# 下行指令存储（TASK-071，AGENT-DOWNLINK-CONTRACT v1.0）：默认库位置 data/downlink.db
DOWNLINK_DB_REL = ("data", "downlink.db")
# ingest API（TASK-034，见 MONITOR-SPEC §3.1.3）：payload 上限，超限 413
MAX_INGEST_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5MB（tasks+events 原文体量留足余量）
# task 事件流（TASK-071，aimonitor 服务端消费）：agent 推送的 task-events.jsonl 增量
# （TASK-066 payload 顶层 events/cursor）。单批上限与 aibase
# kit/tools/agent/agent_payload.py 的 MAX_TASK_EVENTS=200 对齐；cursor 为已确认推进点。
MAX_TASK_EVENTS_INGEST = 200
TASK_EVENTS_TABLE = "task_events"

# session 日志增量（TASK-073，aibase TASK-104 契约 v1.1）：
# - 单行字节上限与 agent 端 MAX_SESSION_LINE_BYTES 对齐：agent 发送前已截断，
#   超限 = 契约违反，400 fail loud（FIND-003 同款：不静默截断再推进游标）
# - 单文件行数滚动上限：长会话 MB 级 jsonl 逐轮续传，落库不封顶会无界增长；
#   超限删最旧行（滚动窗口，查看页语义 = “最近 N 行”，丢最旧不影响实时性）
# - 查询端点单次行数上限（查看页默认 200，够轮询一屏 + 后端响应体积可控）
MAX_SESSION_LINE_BYTES = 64 * 1024
SESSION_MAX_LINES_PER_FILE = 5000
SESSION_QUERY_MAX_LINES = 1000
SESSION_FILES_TABLE = "session_files"
SESSION_LINES_TABLE = "session_lines"
SESSION_STATE_TABLE = "session_state"
# ingest 鉴权（TASK-035，见 MONITOR-SPEC §3.1.2）：config/agents.json（权限 600，gitignored）
AGENTS_CONFIG_REL = ("config", "agents.json")
# admin 密码（TASK-049，见 MONITOR-SPEC §3.2.5）：config/admin.json（权限 600，gitignored）
ADMIN_CONFIG_REL = ("config", "admin.json")
# 告警通知渠道（TASK-072，见 MONITOR-SPEC §4.7）：config/notify.json（权限 600，gitignored）
# 或环境变量 AIMONITOR_NOTIFY_WEBHOOK_URL / AIMONITOR_NOTIFY_WEBHOOK_TOKEN。
# 机密不进 commit（security-policy）：文件缺失/未配置 → 通知禁用（fail-open，不影响轮询）。
NOTIFY_CONFIG_REL = ("config", "notify.json")
NOTIFY_ENV_URL = "AIMONITOR_NOTIFY_WEBHOOK_URL"
NOTIFY_ENV_TOKEN = "AIMONITOR_NOTIFY_WEBHOOK_TOKEN"
DEFAULT_NOTIFY_TIMEOUT_SECONDS = 5
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


def load_notify_config(path=None):
    """加载告警通知渠道配置（TASK-072，见 MONITOR-SPEC §4.7）→ dict。

    优先级：环境变量（AIMONITOR_NOTIFY_WEBHOOK_URL/_TOKEN）> config/notify.json
    （权限 600，gitignored）。未配置 / 文件缺失 / JSON 非法 / URL 非 http(s) →
    返回 {}（通知禁用）。fail-open：通知是增强能力，配置错误绝不拖垮轮询。

    返回结构：{"webhook": {"url": "...", "token": "..."}}（token 可缺省）。
    """
    cfg = {}
    path = path or os.path.join(ROOT, *NOTIFY_CONFIG_REL)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg = data
    except OSError:
        pass  # 文件缺失 = 未配置（fail-open）
    except json.JSONDecodeError:
        pass  # 非法 JSON = 按未配置处理（fail-open）
    webhook = cfg.get("webhook") if isinstance(cfg.get("webhook"), dict) else {}
    env_url = (os.environ.get(NOTIFY_ENV_URL) or "").strip()
    env_token = (os.environ.get(NOTIFY_ENV_TOKEN) or "").strip()
    url = env_url or (webhook.get("url") or "").strip()
    token = env_token or (webhook.get("token") or "").strip()
    if webhook.get("enabled") is False and not env_url:
        return {}
    if not url:
        return {}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {}
    return {"webhook": {"url": url, "token": token}}


class NotificationSender:
    """告警通知投递（TASK-072，见 MONITOR-SPEC §4.7）：webhook POST（零第三方依赖）。

    - urllib.request POST JSON；token 走 Authorization: Bearer（与 ingest 同风格）；
      无 token 的 webhook 同样支持
    - 超时防卡死；任何失败返回 (False, 原因) 不抛异常——通知失败绝不拖垮轮询循环
    - 幂等/防抖由调用方（State._notify_alerts）按告警指纹控制
    """

    def __init__(self, webhook_url, token=None, timeout=DEFAULT_NOTIFY_TIMEOUT_SECONDS):
        self.webhook_url = webhook_url
        self.token = token
        self.timeout = timeout

    @property
    def enabled(self):
        return bool(self.webhook_url)

    def send(self, items, generated_at=None):
        """投递一批告警条目 → (ok, detail)。未配置/空 items → 不投递。"""
        if not self.enabled:
            return False, "通知未配置"
        if not items:
            return False, "无告警条目"
        payload = {
            "event": "alerts.changed",
            "ts": generated_at or time.time(),
            "count": len(items),
            "items": items,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = Request(self.webhook_url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                return True, f"HTTP {code}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


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

    表 ingest_state(project_id PK, payload_json, last_seen, agent_id, task_cursor)：
    - project_id 主键：同 id 重复推送 INSERT OR REPLACE 覆盖写（幂等，§3.1.3）
    - payload_json 为 §3.1.3 请求体 files 的原始 JSON（解析留待采集层 TASK-038）
    - last_seen 为最近成功推送时间（epoch 秒），agent 整体离线判定依据（§3.1.4）
    - agent_id 记录最近推送者，TASK-036 同 id 双 agent 冲突判定（409）依据
    - task_cursor（TASK-071，可空）为 task 事件流已确认覆盖的最大 seq；
      仅推进不倒退（重放/乱序到达取 max），供查询接口作 cursor 确认展示。

    表 task_events(project_id, seq, event_json)（TASK-071）：
    - (project_id, seq) 复合主键：同批重放/游标写失败后重推 → INSERT OR IGNORE
      幂等去重（§3.1.3 幂等语义，agent README「服务端按 (project_id, seq) 去重」）
    - event_json 为单条 task 事件原文（含 seq/ts/ev/task/from/to/actor/...）

    连接模式复用 HistoryStore（TASK-022）：每次操作独立连接 + WAL 保证读写并发；
    _connect 内幂等建表（CREATE TABLE IF NOT EXISTS + 既有库 ALTER 迁移）→ 连接丢失
    或 db 文件重建后自动重连自愈。默认库位置与 HistoryStore 同目录 data/ingest.db
    （由接入方注入）。
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
                " agent_id TEXT NOT NULL,"
                " task_cursor INTEGER)"
            )
            # 既有库迁移（TASK-071）：老库没有 task_cursor 列 → ALTER 补列
            cols = {r[1] for r in conn.execute("PRAGMA table_info(ingest_state)").fetchall()}
            if "task_cursor" not in cols:
                conn.execute("ALTER TABLE ingest_state ADD COLUMN task_cursor INTEGER")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS " + TASK_EVENTS_TABLE + " ("
                " project_id TEXT NOT NULL,"
                " seq INTEGER NOT NULL,"
                " event_json TEXT NOT NULL,"
                " PRIMARY KEY (project_id, seq))"
            )
            # TASK-073：session 日志增量三表（aibase TASK-104 契约 v1.1）
            # - session_files：每文件接收状态（行数 + agent 字节游标 + 最后更新）。
            #   last_offset 为 agent 端已确认送达的字节偏移（游标语义：只反映实际装入行，
            #   outbox 不虚报）——幂等判定/重建重置/追平确认均以此为基准。
            # - session_lines：逐行原文（line_no 严格递增 = 到达序；解析留待查询层，
            #   损坏行由查询层宽松标注——与契约「agent 不解析不丢内容」对称）
            # - session_state：项目级批标志（truncated = 最近一批有积压）+ 最近推送时间
            conn.execute(
                "CREATE TABLE IF NOT EXISTS " + SESSION_FILES_TABLE + " ("
                " project_id TEXT NOT NULL,"
                " task_id TEXT NOT NULL,"
                " file_name TEXT NOT NULL,"
                " line_count INTEGER NOT NULL DEFAULT 0,"
                " last_offset INTEGER,"
                " updated_at INTEGER NOT NULL,"
                " PRIMARY KEY (project_id, task_id, file_name))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS " + SESSION_LINES_TABLE + " ("
                " project_id TEXT NOT NULL,"
                " task_id TEXT NOT NULL,"
                " file_name TEXT NOT NULL,"
                " line_no INTEGER NOT NULL,"
                " line_text TEXT NOT NULL,"
                " PRIMARY KEY (project_id, task_id, file_name, line_no))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS " + SESSION_STATE_TABLE + " ("
                " project_id TEXT PRIMARY KEY,"
                " truncated INTEGER NOT NULL DEFAULT 0,"
                " last_push INTEGER NOT NULL)"
            )
        except Exception:
            conn.close()
            raise
        return conn

    def upsert(self, project_id, payload, agent_id):
        """覆盖写一行 ingest_state（幂等）；返回写入的 last_seen（epoch 秒）。

        payload 为 §3.1.3 请求体的 files 对象（dict）；序列化 JSON 存 payload_json。
        用 `INSERT ... ON CONFLICT DO UPDATE` 而非 `INSERT OR REPLACE`（TASK-071 FIND-002）：
        DO UPDATE 列清单**不含 task_cursor** → 事件流启用后混入旧 payload（无 events/cursor）
        推送不会把已确认的 task_cursor 清空（「只推进不倒退」不变量，见 store_task_events）。
        """
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        last_seen = int(time.time())
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO ingest_state"
                        " (project_id, payload_json, last_seen, agent_id)"
                        " VALUES (?,?,?,?)"
                        " ON CONFLICT(project_id) DO UPDATE SET"
                        " payload_json=excluded.payload_json,"
                        " last_seen=excluded.last_seen,"
                        " agent_id=excluded.agent_id",
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
        用 `INSERT ... ON CONFLICT DO UPDATE` 而非 `INSERT OR REPLACE`（TASK-071 FIND-002）：
        DO UPDATE 列清单**不含 task_cursor** → 同 agent 旧 payload 重推不会把已确认的
        task_cursor 清空（「只推进不倒退」不变量，见 store_task_events）。
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
                        "INSERT INTO ingest_state"
                        " (project_id, payload_json, last_seen, agent_id)"
                        " VALUES (?,?,?,?)"
                        " ON CONFLICT(project_id) DO UPDATE SET"
                        " payload_json=excluded.payload_json,"
                        " last_seen=excluded.last_seen,"
                        " agent_id=excluded.agent_id",
                        (project_id, payload_json, last_seen, agent_id),
                    )
            finally:
                conn.close()
        return ("ok", last_seen)

    def store_task_events(self, project_id, events, cursor=None):
        """入库 task 事件增量（TASK-071）+ 推进 cursor（仅前进，幂等去重）。

        events 为 payload 顶层 events（list[dict]，validate_ingest_payload 已校验
        seq 为正整数且批内单调）；cursor 为 payload 顶层 cursor（int|None）。
        - 事件按 (project_id, seq) INSERT OR IGNORE：重放/重推不报错、不覆盖已有行。
        - cursor 只取 max(既有, 新值)：乱序到达/旧批重放不会把已确认推进点倒退。
        - 与 claim_or_update 同一 self.lock 串行化。
        - **超限即抛 ValueError（TASK-071 FIND-003 fail loud）**：超过 MAX_TASK_EVENTS_INGEST
          条不允许静默截断再推进 cursor（截断 = 事件永久丢失 + cursor 虚高）；HTTP 层在
          validate_ingest_payload 已 400 拦截，此处为 store 层兜底（直接调用者也 fail loud）。
        """
        if not events:
            events = []
        if len(events) > MAX_TASK_EVENTS_INGEST:
            raise ValueError(
                f"task 事件批超限（{len(events)} > {MAX_TASK_EVENTS_INGEST}）："
                "不截断、不推进 cursor，请分批推送")
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    if events:
                        conn.executemany(
                            "INSERT OR IGNORE INTO " + TASK_EVENTS_TABLE +
                            " (project_id, seq, event_json) VALUES (?,?,?)",
                            [(project_id, ev["seq"],
                              json.dumps(ev, ensure_ascii=False, sort_keys=True))
                             for ev in events],
                        )
                    if cursor is not None:
                        row = conn.execute(
                            "SELECT task_cursor FROM ingest_state WHERE project_id=?",
                            (project_id,),
                        ).fetchone()
                        old = row[0] if row is not None else None
                        if old is None or cursor > old:
                            conn.execute(
                                "UPDATE ingest_state SET task_cursor=?"
                                " WHERE project_id=?",
                                (cursor, project_id),
                            )
            finally:
                conn.close()

    def append_server_event(self, project_id, event):
        """服务端生成事件追加（TASK-071 downlink.stale / downlink.result）。

        沿用 task_events 表与 (project_id, seq) 主键：seq = MAX(seq)+1（服务端单调），
        与 agent 推送事件同流读出（read_task_events 按 seq 降序）——契约 §四
        「追加事件到该项目事件流（沿用现有事件机制）」。仅 agent 传输项目调用
        （下行指令只指向 agent 条目）。
        """
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(seq), 0) FROM " + TASK_EVENTS_TABLE
                        + " WHERE project_id=?", (project_id,)).fetchone()
                    seq = (row[0] or 0) + 1
                    conn.execute(
                        "INSERT INTO " + TASK_EVENTS_TABLE
                        + " (project_id, seq, event_json) VALUES (?,?,?)",
                        (project_id, seq,
                         json.dumps(event, ensure_ascii=False, sort_keys=True)))
            finally:
                conn.close()

    def read_task_events(self, project_id, limit=10):
        """读取 task 事件流 → (count, cursor, items)。

        count = 该项目 task_events 总行数；cursor = 已确认覆盖的最大 seq（未推送 → None）；
        items = 最近 limit 条按 seq 降序（最新在前），每条为事件 dict（含 seq）。
        limit ≤ 0 → 不返回事件（count/cursor 仍可用）；local transport 项目 → (0, None, [])。
        """
        limit = max(0, int(limit))
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT task_cursor FROM ingest_state WHERE project_id=?",
                    (project_id,),
                ).fetchone()
                cursor = row[0] if row is not None else None
                count = conn.execute(
                    "SELECT COUNT(*) FROM " + TASK_EVENTS_TABLE +
                    " WHERE project_id=?", (project_id,),
                ).fetchone()[0]
                if limit:
                    rows = conn.execute(
                        "SELECT event_json FROM " + TASK_EVENTS_TABLE +
                        " WHERE project_id=? ORDER BY seq DESC LIMIT ?",
                        (project_id, limit),
                    ).fetchall()
                else:
                    rows = []
            finally:
                conn.close()
        items = [json.loads(r[0]) for r in rows]
        return count, cursor, items

    def read(self, project_id):
        """读取一行 → {project_id, payload, last_seen, agent_id, task_cursor}；无记录 → None。"""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT project_id, payload_json, last_seen, agent_id, task_cursor"
                    " FROM ingest_state WHERE project_id=?",
                    (project_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return {"project_id": row[0], "payload": json.loads(row[1]),
                "last_seen": row[2], "agent_id": row[3], "task_cursor": row[4]}

    def read_all(self):
        """枚举全部 ingest_state 行（供采集层按 transport 选 agent 项目，TASK-038）。"""
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT project_id, payload_json, last_seen, agent_id, task_cursor"
                    " FROM ingest_state ORDER BY project_id",
                ).fetchall()
            finally:
                conn.close()
        return [{"project_id": r[0], "payload": json.loads(r[1]),
                 "last_seen": r[2], "agent_id": r[3], "task_cursor": r[4]} for r in rows]

    # ---------------- session 日志增量（TASK-073，aibase TASK-104 契约 v1.1） ----------------

    def store_session_deltas(self, project_id, sessions):
        """入库 session 日志增量（TASK-073）：逐文件续传 + 幂和对账 + 重建重置 + 滚动上限。

        sessions 为 payload 顶层 sessions（dict，validate_ingest_payload 已校验）：
        {items: [{task_id, files: [{name, lines: [原始行文本...]}]}], truncated: bool,
         cursor: {"<TASK-ID>/<文件名>": 字节偏移}}

        逐文件判定（agent 端游标语义：cursor 只反映实际装入行，推送成功后才持久化）：
        - 无接收记录 → 新文件，从 line_no=1 追加；
        - incoming < stored → agent 端文件重建/截断（游标归零重读）→ 删旧行重收
          （宁重收不静默丢，契约「截断重建归零重读」对称）；
        - incoming == stored → 纯重放（服务端已收、agent 未落游标）→ 幂等跳过；
        - incoming > stored → 混合重推（重放+新增）或纯新增：以行内容前缀重叠对账，
          找最大 k 使 stored 尾部 k 行 == lines 前 k 行，只补 lines[k:]。
          覆盖「推送成功 → agent 崩溃未落游标 → 整批重推且文件已增长」窗口。

        整批单事务：任何文件失败全部回滚（cursor 推进与行写入原子，不落「游标虚高
        而行缺失」）；超 SESSION_MAX_LINES_PER_FILE 删最旧行（滚动窗口）。
        session_state 每批刷新（truncated 批标志 + last_push），空 items（追平确认）也更新。
        """
        now = int(time.time())
        items = sessions.get("items") or []
        cursor_map = sessions.get("cursor") or {}
        truncated = 1 if sessions.get("truncated") else 0
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    for entry in items:
                        task_id = entry.get("task_id", "")
                        for fentry in entry.get("files") or []:
                            fname = fentry.get("name", "")
                            lines = fentry.get("lines") or []
                            incoming = cursor_map.get(f"{task_id}/{fname}")
                            row = conn.execute(
                                "SELECT line_count, last_offset FROM "
                                + SESSION_FILES_TABLE
                                + " WHERE project_id=? AND task_id=? AND file_name=?",
                                (project_id, task_id, fname)).fetchone()
                            if row is None:
                                skip, reset, stored_count = False, False, 0
                            else:
                                stored_count, stored_off = row
                                if (incoming is not None and stored_off is not None
                                        and incoming < stored_off):
                                    # 重建/截断：agent 游标倒退 → 归零重收
                                    conn.execute(
                                        "DELETE FROM " + SESSION_LINES_TABLE
                                        + " WHERE project_id=? AND task_id=? AND file_name=?",
                                        (project_id, task_id, fname))
                                    skip, reset, stored_count = False, True, 0
                                elif (incoming is not None and stored_off is not None
                                        and incoming == stored_off):
                                    # 纯重放：游标同位 → 幂等跳过（只刷新批标志/时间）
                                    skip, reset = True, False
                                else:
                                    skip, reset = False, False
                                if not skip and not reset and lines:
                                    # 前缀重叠对账（混合重推）：找最大重叠 k，只补新行
                                    tail = conn.execute(
                                        "SELECT line_text FROM " + SESSION_LINES_TABLE
                                        + " WHERE project_id=? AND task_id=? AND file_name=?"
                                        " ORDER BY line_no DESC LIMIT ?",
                                        (project_id, task_id, fname,
                                         min(len(lines), stored_count))).fetchall()
                                    tail = [r[0] for r in reversed(tail)]
                                    k = 0
                                    for cand in range(min(len(tail), len(lines)), 0, -1):
                                        if tail[-cand:] == lines[:cand]:
                                            k = cand
                                            break
                                    lines = lines[k:]
                            if skip:
                                continue
                            # 插入基准 = MAX(line_no)（行号高水位只增不回退：滚动窗口删旧行
                            # 后 line_count 归位，若用 count 作基准会撞 UNIQUE 主键）
                            base = conn.execute(
                                "SELECT COALESCE(MAX(line_no), 0) FROM "
                                + SESSION_LINES_TABLE
                                + " WHERE project_id=? AND task_id=? AND file_name=?",
                                (project_id, task_id, fname)).fetchone()[0]
                            for i, text in enumerate(lines):
                                conn.execute(
                                    "INSERT INTO " + SESSION_LINES_TABLE
                                    + " (project_id, task_id, file_name, line_no, line_text)"
                                    " VALUES (?,?,?,?,?)",
                                    (project_id, task_id, fname, base + i + 1, text))
                            # 滚动上限：行号高水位超出窗口则删最旧行（阈值基于 line_no，
                            # 而非现存行数——行号只增不回退，用 count 会漏删）
                            high = base + len(lines)
                            if high > SESSION_MAX_LINES_PER_FILE:
                                conn.execute(
                                    "DELETE FROM " + SESSION_LINES_TABLE
                                    + " WHERE project_id=? AND task_id=? AND file_name=?"
                                    " AND line_no <= ?",
                                    (project_id, task_id, fname,
                                     high - SESSION_MAX_LINES_PER_FILE))
                            new_count = conn.execute(
                                "SELECT COUNT(*) FROM " + SESSION_LINES_TABLE
                                + " WHERE project_id=? AND task_id=? AND file_name=?",
                                (project_id, task_id, fname)).fetchone()[0]
                            conn.execute(
                                "INSERT INTO " + SESSION_FILES_TABLE
                                + " (project_id, task_id, file_name, line_count,"
                                " last_offset, updated_at) VALUES (?,?,?,?,?,?)"
                                " ON CONFLICT(project_id, task_id, file_name) DO UPDATE SET"
                                " line_count=excluded.line_count,"
                                " last_offset=excluded.last_offset,"
                                " updated_at=excluded.updated_at",
                                (project_id, task_id, fname, new_count,
                                 incoming, now))
                    conn.execute(
                        "INSERT INTO " + SESSION_STATE_TABLE
                        + " (project_id, truncated, last_push) VALUES (?,?,?)"
                        " ON CONFLICT(project_id) DO UPDATE SET"
                        " truncated=excluded.truncated, last_push=excluded.last_push",
                        (project_id, truncated, now))
            finally:
                conn.close()

    def read_sessions_summary(self, project_id):
        """读取项目 session 接收概览（TASK-073 查询端点）：task/file 分组 + 批标志。

        返回 {project_id, truncated, last_push, tasks: [{task_id, files: [{name,
        line_count, last_offset, updated_at}]}]}；无任何接收记录 → tasks=[]。
        """
        with self.lock:
            conn = self._connect()
            try:
                files = conn.execute(
                    "SELECT task_id, file_name, line_count, last_offset, updated_at"
                    " FROM " + SESSION_FILES_TABLE
                    + " WHERE project_id=? ORDER BY task_id, file_name",
                    (project_id,)).fetchall()
                st = conn.execute(
                    "SELECT truncated, last_push FROM " + SESSION_STATE_TABLE
                    + " WHERE project_id=?", (project_id,)).fetchone()
            finally:
                conn.close()
        tasks = {}
        for tid, fname, cnt, off, upd in files:
            tasks.setdefault(tid, []).append(
                {"name": fname, "line_count": cnt, "last_offset": off,
                 "updated_at": upd})
        return {
            "project_id": project_id,
            "truncated": bool(st[0]) if st is not None else False,
            "last_push": st[1] if st is not None else None,
            "tasks": [{"task_id": tid, "files": fs} for tid, fs in sorted(tasks.items())],
        }

    def read_session_lines(self, project_id, task_id, file_name, limit=200):
        """读取单文件最近 limit 行（升序返回，行号连续）：[{line_no, text}...]。

        limit ≤ 0 或非法 → 空列表；超 SESSION_QUERY_MAX_LINES 截到上限（响应体积可控）。
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0  # 非法 fail-closed：端点层已 400 拦截，store 层防御不回落默认
        limit = max(0, min(limit, SESSION_QUERY_MAX_LINES))
        if not limit:
            return []
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT line_no, line_text FROM " + SESSION_LINES_TABLE
                    + " WHERE project_id=? AND task_id=? AND file_name=?"
                    " ORDER BY line_no DESC LIMIT ?",
                    (project_id, task_id, file_name, limit)).fetchall()
            finally:
                conn.close()
        return [{"line_no": r[0], "text": r[1]} for r in reversed(rows)]


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


class RegistrationStore:
    """注册审批存储层（TASK-047，MONITOR-SPEC §3.2.3）：stdlib sqlite3。

    表 registration_request（data/registration.db）记录注册请求及其状态机转换：
    - pending \u2192 approved|rejected|expired
    - approved \u2192 revoked
    - rejected/expired \u2192 noop（可重新注册）

    连接模式复用 HistoryStore/IngestStore：每次操作独立连接 + WAL 保证读写并发；
    _connect 内幂等建表（CREATE TABLE IF NOT EXISTS）→ 连接丢失或 db 文件重建后
    自动重连自愈。默认库位置 data/registration.db（由接入方注入）。
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
                "CREATE TABLE IF NOT EXISTS registration_request ("
                " req_id TEXT PRIMARY KEY,"
                " project_id TEXT NOT NULL,"
                " path TEXT,"
                " enrollment_code TEXT,"
                " host_info TEXT NOT NULL,"
                " request_key TEXT NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'pending',"
                " issued_token TEXT,"
                " token_delivered INTEGER DEFAULT 0,"
                " reject_reason TEXT,"
                " created_at REAL NOT NULL,"
                " decided_at REAL,"
                " expire_at REAL NOT NULL)"
            )
            # 迁移：既有表可能缺少 token_delivered / reject_reason / renew_count / path 列
            for col in ("token_delivered", "reject_reason", "renew_count", "path"):
                try:
                    conn.execute(
                        "ALTER TABLE registration_request ADD COLUMN {}".format(col))
                except sqlite3.OperationalError:
                    pass  # 列已存在
        except Exception:
            conn.close()
            raise
        return conn

    def _now(self):
        return time.time()

    def _expire_at(self):
        return time.time() + 7 * 86400

    def create(self, project_id, enrollment_code, host_info, request_key, path=None):
        """创建注册请求；成功返回 req_id，冲突（同 project_id 已有 pending/approved）返回 None。

        path（TASK-069）：申请 payload 的被监控项目路径，审批通过后用于自动登记
        projects.json；旧记录/旧调用缺省 None（展示与登记时按 project_id 兜底）。
        每次创建后触发过期清理（expire_stale）。
        """
        req_id = uuid.uuid4().hex
        now = self._now()
        expire = self._expire_at()
        with self.lock:
            conn = self._connect()
            try:
                # 冲突检查：同 project_id 有活跃记录（pending 或 approved）
                existing = conn.execute(
                    "SELECT status FROM registration_request"
                    " WHERE project_id=? AND status IN ('pending', 'approved')",
                    (project_id,),
                ).fetchone()
                if existing is not None:
                    return None
                with conn:
                    conn.execute(
                        "INSERT INTO registration_request"
                        " (req_id, project_id, path, enrollment_code, host_info, request_key,"
                        "  status, created_at, expire_at)"
                        " VALUES (?,?,?,?,?,?, 'pending',?,?)",
                        (req_id, project_id, path, enrollment_code, host_info,
                         request_key, now, expire),
                    )
            finally:
                conn.close()
        # 每次创建后清理过期 pending
        self.expire_stale()
        return req_id

    def get(self, req_id):
        """读取一行；无记录返回 None。"""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT req_id, project_id, path, enrollment_code, host_info, request_key,"
                    "       status, issued_token, token_delivered, reject_reason,"
                    "       created_at, decided_at, expire_at, renew_count"
                    " FROM registration_request WHERE req_id=?",
                    (req_id,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return {
            "req_id": row[0], "project_id": row[1], "path": row[2],
            "enrollment_code": row[3], "host_info": row[4],
            "request_key": row[5], "status": row[6],
            "issued_token": row[7], "token_delivered": bool(row[8]),
            "reject_reason": row[9], "created_at": row[10],
            "decided_at": row[11], "expire_at": row[12],
            "renew_count": row[13] or 0,
        }

    def list_by_status(self, status=None):
        """按 status 筛选；无参返回全部（按 created_at 升序）。"""
        with self.lock:
            conn = self._connect()
            try:
                if status:
                    rows = conn.execute(
                        "SELECT req_id, project_id, path, enrollment_code, host_info, request_key,"
                        "       status, issued_token, token_delivered, reject_reason,"
                        "       created_at, decided_at, expire_at, renew_count"
                        " FROM registration_request WHERE status=? ORDER BY created_at",
                        (status,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT req_id, project_id, path, enrollment_code, host_info, request_key,"
                        "       status, issued_token, token_delivered, reject_reason,"
                        "       created_at, decided_at, expire_at, renew_count"
                        " FROM registration_request ORDER BY created_at",
                    ).fetchall()
            finally:
                conn.close()
        return [{
            "req_id": r[0], "project_id": r[1], "path": r[2],
            "enrollment_code": r[3], "host_info": r[4],
            "request_key": r[5], "status": r[6],
            "issued_token": r[7], "token_delivered": bool(r[8]),
            "reject_reason": r[9], "created_at": r[10],
            "decided_at": r[11], "expire_at": r[12],
            "renew_count": r[13] or 0,
        } for r in rows]

    def approve(self, req_id, token):
        """审批通过：pending → approved。非 pending 状态 → 不操作返回 False。"""
        now = self._now()
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT status FROM registration_request WHERE req_id=?",
                    (req_id,),
                ).fetchone()
                if row is None or row[0] != "pending":
                    return False
                with conn:
                    conn.execute(
                        "UPDATE registration_request"
                        " SET status='approved', issued_token=?, decided_at=?"
                        " WHERE req_id=?",
                        (token, now, req_id),
                    )
                return True
            finally:
                conn.close()

    def reject(self, req_id, reason=None):
        """拒绝：pending → rejected。非 pending 状态 → 不操作返回 False。

        reason 参数接受拒绝原因字符串，持久化于 reject_reason 列。
        """
        now = self._now()
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT status FROM registration_request WHERE req_id=?",
                    (req_id,),
                ).fetchone()
                if row is None or row[0] != "pending":
                    return False
                with conn:
                    conn.execute(
                        "UPDATE registration_request"
                        " SET status='rejected', decided_at=?, reject_reason=?"
                        " WHERE req_id=?",
                        (now, reason, req_id),
                    )
                return True
            finally:
                conn.close()

    def revoke(self, req_id):
        """吊销：approved → revoked。非 approved 状态 → 不操作返回 False。"""
        now = self._now()
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT status FROM registration_request WHERE req_id=?",
                    (req_id,),
                ).fetchone()
                if row is None or row[0] != "approved":
                    return False
                with conn:
                    conn.execute(
                        "UPDATE registration_request"
                        " SET status='revoked', decided_at=?"
                        " WHERE req_id=?",
                        (now, req_id),
                    )
                return True
            finally:
                conn.close()

    def renew(self, req_id, token):
        """轮换：approved → 保持 approved，更新 issued_token/decided_at/token_delivered/renew_count。

        状态守卫（REVIEW F1）：仅当 status='approved' 且 renew_count=0 时更新（返回 True），
        否则返回 False。守卫在 store 锁内执行，避免并发 revoke/并发双 renew 破坏状态机：
        并发 revoke 先提交后，renew 不会在 revoked 记录上写入新 token；
        并发双 renew 只有一个成功（rowcount=1），另一个返回 False。
        """
        now = self._now()
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE registration_request"
                        " SET issued_token=?, decided_at=?, token_delivered=0,"
                        "     renew_count=COALESCE(renew_count,0)+1"
                        " WHERE req_id=? AND status='approved'"
                        "   AND COALESCE(renew_count,0)=0",
                        (token, now, req_id),
                    )
                return cur.rowcount == 1
            finally:
                conn.close()

    def mark_token_delivered(self, req_id):
        """标记 token_delivered=1（TASK-051：单次交付后不再返回 token）。

        req_id 不存在时无操作（不抛异常）。
        """
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "UPDATE registration_request"
                        " SET token_delivered=1"
                        " WHERE req_id=?",
                        (req_id,),
                    )
            finally:
                conn.close()

    def expire_stale(self):
        """将 expire_at < now 的 pending 记录置为 expired（保留记录，不删除）。"""
        now = self._now()
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "UPDATE registration_request"
                        " SET status='expired', decided_at=?"
                        " WHERE status='pending' AND expire_at<?",
                        (now, now),
                    )
            finally:
                conn.close()


class EnrollmentCodeStore:
    """注册码存储层（TASK-048，MONITOR-SPEC §3.2.4）：stdlib sqlite3。

    表 enrollment_code（data/registration.db，与 RegistrationStore 同一 DB）
    支持注册码的生成、校验、消费、吊销。

    连接模式复用 RegistrationStore/IngestStore：每次操作独立连接 + WAL 保证读写并发；
    _connect 内幂等建表（CREATE TABLE IF NOT EXISTS）→ 连接丢失或 db 文件重建后
    自动重连自愈。默认库位置 data/registration.db（由接入方注入）。
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
                "CREATE TABLE IF NOT EXISTS enrollment_code ("
                " code TEXT PRIMARY KEY,"
                " description TEXT,"
                " allowed_project_pattern TEXT,"
                " max_uses INTEGER DEFAULT 1,"
                " use_count INTEGER DEFAULT 0,"
                " created_at REAL NOT NULL,"
                " expire_at REAL,"
                " revoked INTEGER DEFAULT 0)"
            )
        except Exception:
            conn.close()
            raise
        return conn

    def _now(self):
        return time.time()

    def generate(self, description=None, allowed_project_pattern=None,
                 max_uses=1, expire_at=None):
        """生成随机注册码并写入数据库；返回 code 字符串。

        code 由 secrets.token_urlsafe(16) 生成，格式化为 XXXXXXXX-XXXXXXXX
        （16 字节随机，可读性强）。
        """
        raw = secrets.token_urlsafe(16)  # 22 chars base64 url-safe
        # 置换 - 和 _ 为字母数字，确保输出格式为 XXXXXXXX-XXXXXXXX（可读性强）
        clean = raw.replace("-", "A").replace("_", "B")[:16]
        code = clean[:8] + "-" + clean[8:16]
        now = self._now()
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO enrollment_code"
                        " (code, description, allowed_project_pattern, max_uses,"
                        "  use_count, created_at, expire_at, revoked)"
                        " VALUES (?,?,?,?,0,?,?,0)",
                        (code, description, allowed_project_pattern, max_uses,
                         now, expire_at),
                    )
            finally:
                conn.close()
        return code

    def list(self):
        """返回所有注册码（按 created_at 升序）。"""
        with self.lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT code, description, allowed_project_pattern, max_uses,"
                    "       use_count, created_at, expire_at, revoked"
                    " FROM enrollment_code ORDER BY created_at",
                ).fetchall()
            finally:
                conn.close()
        return [{
            "code": r[0], "description": r[1],
            "allowed_project_pattern": r[2], "max_uses": r[3],
            "use_count": r[4], "created_at": r[5],
            "expire_at": r[6], "revoked": bool(r[7]),
        } for r in rows]

    def get(self, code):
        """读取一行；无记录返回 None。"""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT code, description, allowed_project_pattern, max_uses,"
                    "       use_count, created_at, expire_at, revoked"
                    " FROM enrollment_code WHERE code=?",
                    (code,),
                ).fetchone()
            finally:
                conn.close()
        if row is None:
            return None
        return {
            "code": row[0], "description": row[1],
            "allowed_project_pattern": row[2], "max_uses": row[3],
            "use_count": row[4], "created_at": row[5],
            "expire_at": row[6], "revoked": bool(row[7]),
        }

    def validate(self, code, project_id):
        """校验注册码是否可用于指定 project_id。

        全部通过 → True；任一条件不满足 → False：
        - code 存在
        - 未吊销（revoked=0）
        - 未过期（expire_at IS NULL 或 expire_at > now）
        - 使用次数未超限（use_count < max_uses）
        - project_id 匹配 allowed_project_pattern（非空时 glob 匹配；为空则不校验）
        """
        row = self.get(code)
        if row is None:
            return False
        if row["revoked"]:
            return False
        if row["expire_at"] is not None and row["expire_at"] < self._now():
            return False
        if row["use_count"] >= row["max_uses"]:
            return False
        pat = row["allowed_project_pattern"]
        if pat:
            if not fnmatch.fnmatch(project_id, pat):
                return False
        return True

    def consume(self, code):
        """消费一次注册码：use_count +1。

        超过 max_uses 后 consume 不报错（由 validate 拦截消费前校验）。
        code 不存在时无操作（不抛异常）。
        """
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "UPDATE enrollment_code SET use_count=use_count+1"
                        " WHERE code=?",
                        (code,),
                    )
            finally:
                conn.close()

    def revoke(self, code):
        """吊销注册码（标记 revoked=1）。code 不存在时无操作（不抛异常）。"""
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "UPDATE enrollment_code SET revoked=1 WHERE code=?",
                        (code,),
                    )
            finally:
                conn.close()


def read_agents_config(agents_path=None):
    """读取 config/agents.json → dict（TASK-054，TASK-052 依赖）。

    文件不存在/格式错误/顶层非对象 → {}（fail-closed，与 load_agents_config 不同：
    后者额外检查权限 600，用于 TASK-035 鉴权；本函数只做简单读取，供 TokenIssuer 内部使用）。
    """
    path = agents_path or os.path.join(ROOT, *AGENTS_CONFIG_REL)
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_agents_config(config, agents_path=None):
    """写入 config/agents.json（600）；原子写入：先写临时文件再 rename。

    config 为 {project_id: token} 结构的 dict。
    """
    path = agents_path or os.path.join(ROOT, *AGENTS_CONFIG_REL)
    dirname = os.path.dirname(path)
    os.makedirs(dirname, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass  # Windows 或无权限 FS 不阻塞
    os.replace(tmp, path)


def remove_token(project_id, agents_path=None):
    """从 config/agents.json 中移除指定 project_id 的 token。

    项目不存在时不操作（不抛异常）。
    """
    config = read_agents_config(agents_path)
    if project_id in config:
        del config[project_id]
        write_agents_config(config, agents_path)


class TokenIssuer:
    """Token 签发服务（TASK-054，TASK-052 依赖）。

    生成 bearer token、写入 config/agents.json（600）。
    Token 格式：aimon_{project_id}_{uuid4}_{token_urlsafe(32)}
    实现细节（与规格示例字面略有差异，功能合规）：
      - uuid4 部分为 uuid4().hex，即 32 位十六进制、无连字符；
      - 后缀为 secrets.token_urlsafe(32)，base64url 字符集（含 -/_，非纯 hex）。
    写入格式：{ project_id: token }（兼容现有 load_agents_config/resolve_agent_id）。
    安全：secrets.token_urlsafe 生成、写入后 chmod 600、不入日志；
    原子写入：先写临时文件再 rename，避免写入中断导致文件损坏。
    """

    def __init__(self, agents_path=None):
        self.agents_path = agents_path or os.path.join(ROOT, *AGENTS_CONFIG_REL)

    def issue(self, project_id):
        """签发 token → { token, project_id, scope }。

        scope 固定为 "agent"（当前只支持 agent 角色）。
        token 不入日志（调用方保证不 print/log）。
        """
        token = (f"aimon_{project_id}_"
                 f"{uuid.uuid4().hex}_"
                 f"{secrets.token_urlsafe(32)}")
        config = read_agents_config(self.agents_path)
        config[project_id] = token
        write_agents_config(config, self.agents_path)
        return {"token": token, "project_id": project_id, "scope": "agent"}

    def remove(self, project_id):
        """移除指定 project_id 的 token。"""
        remove_token(project_id, self.agents_path)


_DOWNLINK_SECRET_RE = re.compile(r"(?i)authorization|bearer|token")


def scrub_downlink_tail(text, max_lines=DOWNLINK_TAIL_MAX_LINES):
    """回报 tail 脱敏 + 截断（TASK-035，AGENT-DOWNLINK-CONTRACT §四）。

    剔除含凭据字样（authorization/bearer/token）的行——敏感数据不入下行通道/日志
    （security-policy Rule of Two：② 不叠加）；行数钳制 ≤ max_lines。
    """
    if not isinstance(text, str) or not text:
        return ""
    lines = [ln for ln in text.splitlines() if not _DOWNLINK_SECRET_RE.search(ln)]
    return "\n".join(lines[:max_lines])


def validate_downlink_body(body):
    """入队 schema 校验（TASK-035，契约 §二）；合法返回规范化 dict，非法返回错误消息 str。"""
    if not isinstance(body, dict):
        return "请求体必须是 JSON 对象"
    project_id = body.get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return "project_id 必须是非空字符串"
    dedup_key = body.get("dedup_key")
    if not isinstance(dedup_key, str) or not dedup_key:
        return "dedup_key 必须是非空字符串"
    command = body.get("command")
    if not isinstance(command, dict):
        return "command 必须是对象"
    name = command.get("name")
    if name not in ALLOWED_DOWNLINK_COMMANDS:
        return "command.name 不在白名单"
    args = command.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return "command.args 必须是字符串数组"
    timeout_secs = body.get("timeout_secs", 1800)
    if not isinstance(timeout_secs, int) or not (1 <= timeout_secs <= 86400):
        return "timeout_secs 必须是 1..86400 的整数"
    return {"project_id": project_id, "dedup_key": dedup_key,
            "command": {"name": name, "args": args}, "timeout_secs": timeout_secs}


class DownlinkStore:
    """下行指令队列存储层（TASK-035，AGENT-DOWNLINK-CONTRACT §二/§三）：stdlib sqlite3。

    表 downlink_commands(command_id PK AUTOINCREMENT, dedup_key, project_id,
    command_json, timeout_secs, status, created_by, created_at, picked_at,
    finished_at, attempt, result_json)：
    - command_id 即契约 seq（AUTOINCREMENT 单调递增，乱序/重放检测依据）
    - 部分唯一索引 idx_dl_dedup：dedup_key 在 queued/running 态唯一（幂等入队 409 依据）；
      终态后同 key 可再次入队（重试场景）
    - 指令状态机（契约 §三）：queued →(pickup)→ running →(result)→ done/failed/skipped；
      pickup 超时 → 重投（attempt+1，≤ max_requeue）→ failed(human)
    - 连接模式复用 HistoryStore/IngestStore：每次操作独立连接 + WAL + 幂等建表自愈。
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS downlink_commands(
                command_id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT NOT NULL,
                project_id TEXT NOT NULL,
                command_json TEXT NOT NULL,
                timeout_secs INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_by TEXT,
                created_at REAL,
                picked_at REAL,
                finished_at REAL,
                attempt INTEGER NOT NULL DEFAULT 0,
                result_json TEXT)""")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_dl_dedup ON downlink_commands(dedup_key) "
            "WHERE status IN ('queued','running')")
        return conn

    @staticmethod
    def _row_to_dict(row):
        if row is None:
            return None
        d = dict(row)
        d["command"] = json.loads(d.pop("command_json"))
        if d.get("result_json"):
            d["result"] = json.loads(d.pop("result_json"))
        else:
            d.pop("result_json", None)
        d["seq"] = d["command_id"]
        return d

    def enqueue(self, project_id, dedup_key, command, timeout_secs, created_by, now):
        """入队；返回 (row, reused)。reused=True 表示 dedup_key 已有未终态指令（409 语义）。"""
        with self.lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT * FROM downlink_commands WHERE dedup_key=? AND status IN ('queued','running')",
                    (dedup_key,)).fetchone()
                if existing is not None:
                    return self._row_to_dict(existing), True
                cur = conn.execute(
                    "INSERT INTO downlink_commands(dedup_key, project_id, command_json, timeout_secs,"
                    " status, created_by, created_at) VALUES(?,?,?,?,?,?,?)",
                    (dedup_key, project_id, json.dumps(command, ensure_ascii=False),
                     timeout_secs, "queued", created_by, now))
                conn.commit()
                row = conn.execute("SELECT * FROM downlink_commands WHERE command_id=?",
                                   (cur.lastrowid,)).fetchone()
                return self._row_to_dict(row), False
            finally:
                conn.close()

    def pickup(self, allowed_projects, now,
               pickup_timeout=DOWNLINK_PICKUP_TIMEOUT_DEFAULT,
               max_requeue=DOWNLINK_MAX_REQUEUE):
        """拾取（契约 §三/§五）：先回收超时未拾取指令（重投/判死），再领取 allowed 内最旧 queued。

        - 超时 queued：attempt ≥ max_requeue → failed(pickup-timeout, human)；否则重投
          （created_at 刷新 + attempt+1，pickup 超时窗口重新计时）
        - 领取即置 running（picked_at=now），pickup 超时窗口自此终止——执行期只有
          timeout_secs 生效（R2-001：「忙而非死」不误判 stale）
        - allowed_projects 为空（fail-closed）→ 不下发任何指令
        """
        with self.lock:
            conn = self._connect()
            try:
                stale = conn.execute(
                    "SELECT * FROM downlink_commands WHERE status='queued' AND created_at IS NOT NULL").fetchall()
                for row in stale:
                    if now - row["created_at"] <= pickup_timeout:
                        continue
                    if row["attempt"] >= max_requeue:
                        conn.execute(
                            "UPDATE downlink_commands SET status='failed', finished_at=?, result_json=?"
                            " WHERE command_id=?",
                            (now, json.dumps({"reason": "pickup-timeout",
                                              "attempts": row["attempt"] + 1}), row["command_id"]))
                    else:
                        conn.execute(
                            "UPDATE downlink_commands SET created_at=?, attempt=attempt+1"
                            " WHERE command_id=?", (now, row["command_id"]))
                conn.commit()
                if not allowed_projects:
                    return None
                marks = ",".join("?" for _ in allowed_projects)
                row = conn.execute(
                    f"SELECT * FROM downlink_commands WHERE status='queued' AND project_id IN ({marks})"
                    " ORDER BY command_id LIMIT 1", tuple(allowed_projects)).fetchone()
                if row is None:
                    return None
                conn.execute(
                    "UPDATE downlink_commands SET status='running', picked_at=? WHERE command_id=?",
                    (now, row["command_id"]))
                conn.commit()
                return self._row_to_dict(conn.execute(
                    "SELECT * FROM downlink_commands WHERE command_id=?",
                    (row["command_id"],)).fetchone())
            finally:
                conn.close()

    def result(self, command_id, status, exit_code, stdout_tail, stderr_tail, now):
        """回报终态；返回 (row, already_terminal)。already_terminal=True → 409 幂等忽略（契约 §五）。"""
        with self.lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM downlink_commands WHERE command_id=?",
                                   (command_id,)).fetchone()
                if row is None:
                    return None, False
                if row["status"] in DOWNLINK_TERMINAL_STATUSES:
                    return self._row_to_dict(row), True
                result = {"status": status, "exit_code": exit_code,
                          "stdout_tail": stdout_tail, "stderr_tail": stderr_tail,
                          "finished_at": now}
                conn.execute(
                    "UPDATE downlink_commands SET status=?, finished_at=?, result_json=?"
                    " WHERE command_id=?",
                    (status, now, json.dumps(result, ensure_ascii=False), command_id))
                conn.commit()
                return self._row_to_dict(conn.execute(
                    "SELECT * FROM downlink_commands WHERE command_id=?",
                    (command_id,)).fetchone()), False
            finally:
                conn.close()

    def get(self, command_id):
        with self.lock:
            conn = self._connect()
            try:
                return self._row_to_dict(conn.execute(
                    "SELECT * FROM downlink_commands WHERE command_id=?",
                    (command_id,)).fetchone())
            finally:
                conn.close()


class State:
    """聚合缓存 + 后台轮询线程（daemon）。"""

    def __init__(self, config, quiet=False, db_path=None, ingest_db_path=None, agents_path=None,
                 rate_clock=None, registration_db_path=None, projects_path=None, notify=None,
                 downlink_db_path=None, start_poller=True):
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
        # 下行指令队列（TASK-035，AGENT-DOWNLINK-CONTRACT）：默认 <ROOT>/data/downlink.db，
        # 测试可注入临时路径
        downlink_db_path = downlink_db_path or os.path.join(ROOT, *DOWNLINK_DB_REL)
        self.downlink = DownlinkStore(downlink_db_path)
        # agent token 配置（TASK-035）：config/agents.json（权限 600，gitignored），测试可注入临时路径
        agents_path = agents_path or os.path.join(ROOT, *AGENTS_CONFIG_REL)
        self.agents_path = agents_path
        self.agents = load_agents_config(agents_path)
        if not self.agents:
            # 跨盘符（Windows C:/D:）os.path.relpath 抛 ValueError（SMELL-002，TASK-035 返工）：
            # 与 _rel 同语义兜底——降级为绝对路径日志，绝不因告警路径崩启动
            try:
                agents_rel = os.path.relpath(agents_path, ROOT)
            except ValueError:
                agents_rel = agents_path
            self._log(f"⚠ {agents_rel} 缺失或不可用 → "
                      "/api/ingest 全部 401（fail-closed）")
        # ingest 限流（TASK-036）：每 agent 每分钟 N 次（config.projects.json 顶层可配置）；
        # rate_clock 仅供测试注入确定性时钟，生产用 time.time
        self.rate_limiter = IngestRateLimiter(
            config.get("ingest_rate_limit_per_minute", DEFAULT_INGEST_RATE_LIMIT_PER_MINUTE),
            clock=rate_clock or time.time,
        )
        # 注册存储（TASK-047）：默认 <ROOT>/data/registration.db，测试可注入临时路径
        registration_db_path = registration_db_path or os.path.join(ROOT, *REGISTRATION_DB_REL)
        self.registration = RegistrationStore(registration_db_path)
        # 项目注册表路径（TASK-069）：审批通过自动登记 projects.json 的目标文件；
        # 缺省 config/projects.json，测试可注入临时路径避免污染真实配置
        self.projects_path = projects_path or CONFIG_PATH
        # 注册码存储（TASK-048）：与 RegistrationStore 同一 DB
        self.enrollment = EnrollmentCodeStore(registration_db_path)
        # 下行指令存储（TASK-071，AGENT-DOWNLINK-CONTRACT v1.0）：
        # 默认 <ROOT>/data/downlink.db，测试可注入临时路径；事件经 self.ingest 落 task_events
        downlink_db_path = downlink_db_path or os.path.join(ROOT, *DOWNLINK_DB_REL)
        self.downlink = DownlinkStore(downlink_db_path, ingest_store=self.ingest,
                                      clock=rate_clock or time.time)
        # 注册端点限流（TASK-050）：全局限流，config.projects.json 顶层可配置
        self.register_limiter = IngestRateLimiter(
            config.get("register_rate_limit_per_minute", 60),
            clock=rate_clock or time.time,
        )
        # 告警通知渠道（TASK-072）：缺省按 config/notify.json / 环境变量构建；测试可注入
        webhook_cfg = load_notify_config().get("webhook") or {}
        self.notifier = (notify if notify is not None else
                         NotificationSender(webhook_url=webhook_cfg.get("url") or "",
                                            token=webhook_cfg.get("token") or ""))
        # 最近一次已投递告警指纹（TASK-072 防抖：告警集合变化才通知，相同告警不重复轰炸）
        self._last_alert_fp = None
        # TASK-083：测试可禁用后台 poller（改同步 poll() 消除首轮轮询竞态 + 避免孤儿线程）；
        # 缺省 True → 生产行为零变化（启动后首轮轮询仍立即执行）
        if start_poller:
            self._start_poller()
        else:
            self._log("后台轮询已禁用（start_poller=False）")

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
        self._notify_alerts(data)
        self._log(f"轮询完成：{len(data)} 个项目，耗时 {time.time() - t0:.2f}s")

    def _notify_alerts(self, data):
        """TASK-072：轮询后比较告警指纹，集合变化时投递 webhook 通知（防抖）。

        - 未配置/禁用 → 直接返回（通知是增强能力，不影响轮询）
        - 指纹 = 全部告警条目稳定字段（project/kind/role/text）排序元组——
          相同告警不重复轰炸；告警清空时重置指纹，下次再出现会再次通知
        - 投递成功才更新指纹；失败保留旧指纹 → 下一轮自动重试（最终一致）
        - 通知失败只记日志，绝不抛异常
        """
        notifier = self.notifier
        if notifier is None or not notifier.enabled:
            return
        items = []
        for p in data:
            items.extend(p.get("alerts") or [])
        if not items:
            self._last_alert_fp = None
            return
        fp = tuple(sorted((a.get("project"), a.get("kind"), a.get("role"), a.get("text"))
                          for a in items))
        if fp == self._last_alert_fp:
            return
        ok, detail = notifier.send(items, generated_at=time.time())
        if ok:
            self._last_alert_fp = fp
            self._log(f"告警通知已投递：{len(items)} 条（{detail}）")
        else:
            self._log(f"告警通知投递失败：{detail}（下轮重试）")

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


def parse_session_line(line_no, text):
    """解析一行 session jsonl（TASK-073 查看端点）：宽松语义，损坏行原样标注不丢弃。

    契约「agent 不解析不丢内容」的对称面：服务端也不因解析失败丢行——
    合法 JSON → {line_no, ok: True, type, data}；非法/非 dict → ok: False + 原文。
    """
    try:
        data = json.loads(text)
    except ValueError:
        return {"line_no": line_no, "ok": False, "type": "raw", "text": text}
    if isinstance(data, dict):
        return {"line_no": line_no, "ok": True,
                "type": str(data.get("type") or "unknown"), "data": data}
    return {"line_no": line_no, "ok": True, "type": "raw", "data": data}


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

    # TASK-071：task 事件流（payload 顶层 events/cursor，TASK-066 agent 增量推送）。
    # 宽松语义：缺省/空 events = 未启用或本轮无新事件（向后兼容，旧 payload 无此键仍 200）。
    # seq 规则沿用 TASK-065 `task validate` 既有规则：整数、≥1（拒绝负 seq/0）、批内单调递增
    # （seq 必须大于上一条）；cursor 为已确认覆盖最大 seq：整数、≥0、不得小于批量最大 seq；
    # 单批 ≤ MAX_TASK_EVENTS_INGEST（超限 400，FIND-003 fail loud，不静默截断）。
    cursor = obj.get("cursor")
    if cursor is not None:
        if isinstance(cursor, bool) or not isinstance(cursor, int):
            return "cursor 必须为整数或 null"
        if cursor < 0:
            return "cursor 必须 ≥ 0（拒绝负 cursor）"
    events = obj.get("events")
    if events is not None:
        if not isinstance(events, list):
            return "events 必须为数组（task 事件增量）"
        # TASK-071 FIND-003：超批 fail loud（400）——agent README 契约「≤ 200 条/批」；
        # 静默截断会让 seq 201..N 永久丢失而 cursor 仍宣称覆盖（不静默丢弃事件不变量）。
        if len(events) > MAX_TASK_EVENTS_INGEST:
            return (f"events 超出单批上限（{MAX_TASK_EVENTS_INGEST} 条），请分批推送"
                    f"（收到 {len(events)} 条）")
        last_seq = None
        for i, entry in enumerate(events):
            if not isinstance(entry, dict):
                return f"events[{i}] 必须为对象"
            seq = entry.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int):
                return f"events[{i}].seq 必须为整数"
            if seq < 1:
                return f"events[{i}].seq 必须 ≥ 1（拒绝负 seq/0）"
            if last_seq is not None and seq <= last_seq:
                return f"events[{i}].seq 非单调（{seq} ≤ 上一条 {last_seq}）"
            last_seq = seq
        if events and cursor is not None and cursor < last_seq:
            return f"cursor（{cursor}）小于批量最大 seq（{last_seq}），违反确认语义"

    # TASK-073：session 日志增量（payload 顶层 sessions，aibase TASK-104 契约 v1.1）。
    # 宽松语义：缺省 sessions = agent 未启用 session 流（v1.0 旧 payload 向后兼容，仍 200）。
    # - items[]: 按 task 分组 [{task_id, files: [{name, lines: [原始 jsonl 行文本...]}]}]
    # - truncated: bool（本轮有未送达积压，下轮续推）
    # - cursor: {"<TASK-ID>/<文件名>": 字节偏移 ≥ 0}（本轮确认覆盖；空批追平时也携带）
    # 单行 > MAX_SESSION_LINE_BYTES → 400 fail loud（agent 端已截断，超限 = 契约违反；
    # 不静默收下——与 events 超批同款不变量：不落一条虚高游标认领的行）。
    sessions = obj.get("sessions")
    if sessions is not None:
        if not isinstance(sessions, dict):
            return "sessions 必须为对象"
        items = sessions.get("items")
        if items is not None:
            if not isinstance(items, list):
                return "sessions.items 必须为数组"
            for i, entry in enumerate(items):
                if not isinstance(entry, dict):
                    return f"sessions.items[{i}] 必须为对象"
                task_id = entry.get("task_id")
                if not isinstance(task_id, str) or not task_id.strip():
                    return f"sessions.items[{i}].task_id 必须为非空字符串"
                files = entry.get("files")
                if not isinstance(files, list):
                    return f"sessions.items[{task_id}].files 必须为数组"
                for j, fentry in enumerate(files):
                    if not isinstance(fentry, dict):
                        return f"sessions.items[{task_id}].files[{j}] 必须为对象"
                    fname = fentry.get("name")
                    if not isinstance(fname, str) or not fname.strip():
                        return f"sessions.items[{task_id}].files[{j}].name 必须为非空字符串"
                    lines = fentry.get("lines")
                    if not isinstance(lines, list):
                        return f"sessions.items[{task_id}].files[{fname}].lines 必须为数组"
                    for k, line in enumerate(lines):
                        if not isinstance(line, str):
                            return f"sessions.items[{task_id}].files[{fname}].lines[{k}] 必须为字符串"
                        if len(line.encode("utf-8")) > MAX_SESSION_LINE_BYTES:
                            return (f"sessions.items[{task_id}].files[{fname}]"
                                    f".lines[{k}] 单行超出字节上限"
                                    f"（{MAX_SESSION_LINE_BYTES}），请 agent 端截断后再推")
        truncated = sessions.get("truncated")
        if truncated is not None and not isinstance(truncated, bool):
            return "sessions.truncated 必须为布尔值"
        session_cursor = sessions.get("cursor")
        if session_cursor is not None:
            if not isinstance(session_cursor, dict):
                return "sessions.cursor 必须为对象（{\"<TASK-ID>/<文件名>\": 字节偏移}）"
            for ckey, coff in session_cursor.items():
                if not isinstance(ckey, str) or not ckey.strip():
                    return "sessions.cursor 键必须为非空字符串（<TASK-ID>/<文件名>）"
                if isinstance(coff, bool) or not isinstance(coff, int) or coff < 0:
                    return f"sessions.cursor[{ckey}] 必须为非负整数（字节偏移）"
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
        # POSIX 600 检查（fail-closed）：NTFS 无 POSIX 权限位（st_mode 恒 0o666/0o444，
        # chmod 仅能切只读），0o077 检查在 Windows 上永真 → agents.json 永不可用；
        # Windows 访问控制由 NTFS ACL 接管，跳过本检查（TASK-035：下行队列需在本机
        # Windows 跑测试/集成验证）；POSIX 平台维持原检查不变
        if os.name != "nt" and os.stat(path).st_mode & 0o077:
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


def ensure_admin_config(path=None):
    """首次启动时若 config/admin.json 不存在，自动生成并写入（TASK-049，§3.2.5）。

    生成 32 字符随机密码（secrets.token_hex(16)），文件权限 600，stdout 打印一次。
    已存在时不覆盖（幂等）。
    """
    path = path or os.path.join(ROOT, *ADMIN_CONFIG_REL)
    if os.path.isfile(path):
        return
    password = secrets.token_hex(16)  # 32 字符十六进制
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"admin_password": password}, fh, ensure_ascii=False)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 或无权限 FS 不阻塞
    print(f"Admin password: {password}", flush=True)


def load_admin_config(path=None):
    """加载 config/admin.json → 密码字符串；不可用 → None（fail-closed）。

    fail-closed 语义：
    - 文件缺失 → None
    - 权限过宽（非 600）→ None + 告警
    - JSON 非法 / 顶层非对象 / 缺 admin_password 字段 → None + 告警
    """
    path = path or os.path.join(ROOT, *ADMIN_CONFIG_REL)
    if not os.path.isfile(path):
        return None
    try:
        if os.stat(path).st_mode & 0o077:
            print(f"\u26a0 {path} 权限不是 600（group/other 可读），拒绝加载 admin 密码（fail-closed）",
                  file=sys.stderr)
            return None
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"\u26a0 读取 {path} 失败（{e}），拒绝加载 admin 密码（fail-closed）", file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"\u26a0 {path} 顶层必须是 JSON 对象，拒绝加载 admin 密码（fail-closed）", file=sys.stderr)
        return None
    pw = data.get("admin_password")
    if not isinstance(pw, str) or not pw:
        print(f"\u26a0 {path} 缺少 admin_password 字段或非字符串，拒绝加载 admin 密码（fail-closed）",
              file=sys.stderr)
        return None
    return pw


def load_projects_config(path=None):
    """读取 config/projects.json → dict（TASK-069）。

    fail-open 语义（区别于 agents/admin 的 fail-closed）：本函数服务于"审批通过自动
    登记"，文件缺失/损坏时返回最小默认结构，不阻断审批；文件在服务启动时已由 main()
    校验，此处异常多为运维瞬时状态。
    默认结构：{"poll_interval_seconds": 30, "projects": []}
    """
    path = path or CONFIG_PATH
    default = {"poll_interval_seconds": 30, "projects": []}
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        print(f"\u26a0 读取 {path} 失败（{e}），按默认结构处理", file=sys.stderr)
        return default
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        print(f"\u26a0 {path} 结构异常（顶层非对象或 projects 非列表），按默认结构处理",
              file=sys.stderr)
        return default
    return data


def register_project_in_config(path, project_id, name=None, path_value=None, transport="agent"):
    """审批通过后把 project_id 自动登记进 config/projects.json（TASK-069）。

    幂等：project_id 已存在 → 不重复追加，返回 None。
    成功新登记 → 返回新条目 dict（调用方用于同步内存 config）。
    原子写：先写 <path>.tmp 再 os.replace（进程崩溃不产生半截文件）。
    并发：模块级 PROJECTS_CONFIG_LOCK 互斥读-改-写，防并发审批丢失条目。
    """
    with PROJECTS_CONFIG_LOCK:
        data = load_projects_config(path)
        projects = data.setdefault("projects", [])
        for p in projects:
            if isinstance(p, dict) and p.get("id") == project_id:
                return None
        entry = {
            "id": project_id,
            "name": name or project_id,
            "path": path_value or project_id,
            "transport": transport,
        }
        projects.append(entry)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
        return entry


def check_admin_password(req):
    """校验请求的 Authorization: Bearer 是否匹配 config/admin.json 密码（TASK-049）。

    参数 req 为 BaseHTTPRequestHandler 实例（含 .headers 和 .command）。
    返回 True/False。fail-closed：配置文件缺失/损坏/权限错误 → 全部返回 False。
    """
    token = extract_bearer_token(req.headers.get("Authorization", ""))
    if token is None:
        return False
    pw = load_admin_config()
    if pw is None:
        return False
    return hmac.compare_digest(token.encode("utf-8"), pw.encode("utf-8"))


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


# ---------------- 下行指令存储（TASK-071，AGENT-DOWNLINK-CONTRACT v1.0 §二/§三/§五） ----------------

def _downlink_iso(epoch):
    """epoch → ISO8601 UTC（契约示例格式 2026-08-30T12:00:00Z）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _downlink_sanitize_tail(text):
    """回报 tail 清洗（契约 §四）：剔除含 Authorization/Bearer/token 字样的行 + ≤200 行。

    agent 侧已脱敏一次，server 侧独立重做（纵深防御，两套实现互不依赖）；
    仅按行处理，超限截断不报错（回报尾部本就是截断语义）。
    """
    lines = [ln for ln in str(text).splitlines()
             if not DOWNLINK_SECRET_LINE_RE.search(ln)]
    return "\n".join(lines[:DOWNLINK_RESULT_MAX_LINES])


class DownlinkStore:
    """下行指令队列存储层：stdlib sqlite3，data/downlink.db（不入被监控项目）。

    - 表 command 一行一指令；meta.next_id 计数器 → command_id（dl-NNNNNN）与 seq
      同源单调，重启不回退（契约 §二）；
    - dedup 幂等（契约 §三 R2-001 ③①）：dedup_key 部分唯一索引仅约束在途
      （queued/running）——终态后同 key 可再入队（新一轮派发）；
    - 状态机：queued ──pickup──▶ running ──result──▶ done|failed|skipped；
      pickup 超时（90s）重投 ≤2 次 → failed(human)；执行超时 → failed（转人工）；
      回收惰性：pickup 超时仅在**对应 token 白名单项目的 pickup 调用内**扫描
      （busy 窗口暂停语义：执行中的 agent 不调 pickup，窗口自然不推进——契约 §三
      R2-001 与 agent_downlink「执行期间跳过拾取」配套）；执行超时为全局兜底，
      在 pickup/status 调用内均扫描；
    - 服务端事件（downlink.stale / downlink.result）经 IngestStore.append_server_event
      落入该项目 task_events 流（契约 §四「沿用现有事件机制」）。
    """

    _COLS = ("command_id, seq, dedup_key, project_id, name, args_json, timeout_secs,"
             " created_by, created_at, status, deliveries, redeliveries,"
             " picked_at, picked_by, result_json, finished_at")

    def __init__(self, db_path, ingest_store=None, clock=None):
        self.db_path = db_path
        self.ingest_store = ingest_store
        self.lock = threading.Lock()
        self._clock = clock or time.time
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._connect().close()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS command ("
            " command_id TEXT PRIMARY KEY,"
            " seq INTEGER NOT NULL,"
            " dedup_key TEXT NOT NULL,"
            " project_id TEXT NOT NULL,"
            " name TEXT NOT NULL,"
            " args_json TEXT NOT NULL,"
            " timeout_secs INTEGER NOT NULL,"
            " created_by TEXT NOT NULL,"
            " created_at TEXT NOT NULL,"
            " status TEXT NOT NULL"
            "   CHECK (status IN ('queued','running','done','failed','skipped')),"
            " deliveries INTEGER NOT NULL DEFAULT 0,"
            " redeliveries INTEGER NOT NULL DEFAULT 0,"
            " picked_at TEXT,"
            " picked_by TEXT,"
            " picked_epoch REAL,"
            " pickup_deadline REAL,"
            " result_json TEXT,"
            " finished_at TEXT)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_command_project_status"
            " ON command(project_id, status)")
        # dedup 闸（契约 §三 ③①）：部分唯一索引仅约束在途状态
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_command_dedup_active"
            " ON command(dedup_key) WHERE status IN ('queued','running')")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        return conn

    def _next_id(self, conn):
        """command_id 单调计数器（meta 持久化，重启不回退）。"""
        row = conn.execute("SELECT value FROM meta WHERE key='next_id'").fetchone()
        n = int(row[0]) if row else 1
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('next_id', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(n + 1),))
        return n

    @staticmethod
    def _to_dict(row):
        (cid, seq, dedup, proj, name, args_json, timeout, by, at, status,
         deliveries, redeliveries, picked_at, picked_by, result_json,
         finished_at) = row
        d = {"command_id": cid, "seq": seq, "dedup_key": dedup,
             "project_id": proj,
             "command": {"name": name, "args": json.loads(args_json)},
             "timeout_secs": timeout, "created_by": by, "created_at": at,
             "status": status, "deliveries": deliveries,
             "redeliveries": redeliveries, "picked_at": picked_at,
             "picked_by": picked_by, "finished_at": finished_at}
        if result_json:
            d["result"] = json.loads(result_json)
        return d

    def enqueue(self, project_id, dedup_key, name, args, timeout_secs, created_by):
        """入队 → (指令 dict, reused)；409 语义（同 key 在途 → 复用）在此归一。"""
        now = self._clock()
        now_iso = _downlink_iso(now)
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    row = conn.execute(
                        "SELECT command_id FROM command"
                        " WHERE dedup_key=? AND status IN ('queued','running')",
                        (dedup_key,)).fetchone()
                    if row:
                        full = conn.execute(
                            "SELECT " + self._COLS + " FROM command"
                            " WHERE command_id=?", (row[0],)).fetchone()
                        return self._to_dict(full), True
                    n = self._next_id(conn)
                    cid = "dl-%06d" % n
                    conn.execute(
                        "INSERT INTO command (command_id, seq, dedup_key, project_id,"
                        " name, args_json, timeout_secs, created_by, created_at, status,"
                        " deliveries, redeliveries, picked_at, picked_by, picked_epoch,"
                        " pickup_deadline, result_json, finished_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,'0','0',NULL,NULL,NULL,?,NULL,NULL)",
                        (cid, n, dedup_key, project_id, name,
                         json.dumps(list(args or []), ensure_ascii=False),
                         int(timeout_secs), created_by, now_iso, "queued",
                         now + DOWNLINK_PICKUP_TIMEOUT_SECS))
                    full = conn.execute(
                        "SELECT " + self._COLS + " FROM command WHERE command_id=?",
                        (cid,)).fetchone()
                    return self._to_dict(full), False
            finally:
                conn.close()

    def _sweep_running_locked(self, conn, now, now_iso):
        """执行超时兜底（全局安全）：running 且 picked_epoch+timeout_secs < now
        → failed（转人工，无 exit_code → dispatcher 侧 rc=1）+ downlink.stale 事件。"""
        rows = conn.execute(
            "SELECT command_id, project_id, picked_epoch, timeout_secs FROM command"
            " WHERE status='running' AND picked_epoch IS NOT NULL"
            " AND picked_epoch + timeout_secs < ?", (now,)).fetchall()
        events = []
        for cid, proj, picked, _t in rows:
            result = {"status": "failed", "exit_code": None,
                      "reason": "exec-timeout", "finished_at": now_iso}
            conn.execute(
                "UPDATE command SET status='failed', result_json=?, finished_at=?"
                " WHERE command_id=?",
                (json.dumps(result, ensure_ascii=False, sort_keys=True),
                 now_iso, cid))
            events.append({"ts": now_iso, "ev": "downlink.stale",
                           "command_id": cid, "project_id": proj,
                           "reason": "exec-timeout"})
        return events

    def _sweep_pickup_timeout_locked(self, conn, allowed_projects, now, now_iso):
        """pickup 超时（仅本 token 白名单项目，busy 暂停语义）：queued 且过 90s 窗口
        → 重投（redeliveries+1、seq+1、窗口重置）≤2 次 → failed(human)+事件。"""
        ph = ",".join("?" for _ in allowed_projects)
        rows = conn.execute(
            "SELECT command_id, project_id, redeliveries FROM command"
            " WHERE status='queued' AND pickup_deadline < ?"
            f" AND project_id IN ({ph})", (now,) + tuple(allowed_projects)).fetchall()
        events = []
        for cid, proj, redeliveries in rows:
            if redeliveries < DOWNLINK_MAX_REDELIVERIES:
                conn.execute(
                    "UPDATE command SET redeliveries=redeliveries+1, seq=seq+1,"
                    " pickup_deadline=? WHERE command_id=?",
                    (now + DOWNLINK_PICKUP_TIMEOUT_SECS, cid))
            else:
                result = {"status": "failed", "exit_code": None,
                          "reason": "pickup-timeout", "finished_at": now_iso}
                conn.execute(
                    "UPDATE command SET status='failed', result_json=?, finished_at=?"
                    " WHERE command_id=?",
                    (json.dumps(result, ensure_ascii=False, sort_keys=True),
                     now_iso, cid))
                events.append({"ts": now_iso, "ev": "downlink.stale",
                               "command_id": cid, "project_id": proj,
                               "reason": "pickup-timeout"})
        return events

    def pickup(self, agent_id, allowed_projects):
        """拾取（契约 §三）：惰性回收 → 白名单内最旧 queued → running。无 → None。"""
        now = self._clock()
        now_iso = _downlink_iso(now)
        events = []
        cmd = None
        row = None
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    events = self._sweep_running_locked(conn, now, now_iso)
                    if allowed_projects:
                        events += self._sweep_pickup_timeout_locked(
                            conn, allowed_projects, now, now_iso)
                        ph = ",".join("?" for _ in allowed_projects)
                        row = conn.execute(
                            "SELECT command_id FROM command WHERE status='queued'"
                            f" AND project_id IN ({ph})"
                            " ORDER BY created_at ASC LIMIT 1",
                            tuple(allowed_projects)).fetchone()
                    if row is not None:
                        conn.execute(
                            "UPDATE command SET status='running', picked_at=?,"
                            " picked_by=?, picked_epoch=?, deliveries=deliveries+1"
                            " WHERE command_id=?",
                            (now_iso, agent_id, now, row[0]))
                        full = conn.execute(
                            "SELECT " + self._COLS + " FROM command"
                            " WHERE command_id=?", (row[0],)).fetchone()
                        cmd = self._to_dict(full)
            finally:
                conn.close()
        for ev in events:
            if self.ingest_store is not None:
                self.ingest_store.append_server_event(ev["project_id"], ev)
        return cmd

    def get(self, command_id):
        """状态轮询（契约 §一）：附带执行超时兜底扫描；未知 → None。"""
        now = self._clock()
        now_iso = _downlink_iso(now)
        events = []
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    events = self._sweep_running_locked(conn, now, now_iso)
                    row = conn.execute(
                        "SELECT " + self._COLS + " FROM command WHERE command_id=?",
                        (command_id,)).fetchone()
                    cmd = self._to_dict(row) if row else None
            finally:
                conn.close()
        for ev in events:
            if self.ingest_store is not None:
                self.ingest_store.append_server_event(ev["project_id"], ev)
        return cmd

    def report_result(self, command_id, report):
        """回报（契约 §四/§五）→ 'ok' | 'already-terminal' | None(未知)。

        终态落库 + downlink.result 事件（提交后追加，事件失败不影响终态权威）；
        已终态（含双 sweep 转的 failed）→ 409 幂等忽略语义。
        """
        now = self._clock()
        status = report.get("status")
        result = {"status": status, "exit_code": report.get("exit_code"),
                  "stdout_tail": _downlink_sanitize_tail(report.get("stdout_tail") or ""),
                  "stderr_tail": _downlink_sanitize_tail(report.get("stderr_tail") or ""),
                  "finished_at": report.get("finished_at") or _downlink_iso(now)}
        with self.lock:
            conn = self._connect()
            try:
                with conn:
                    row = conn.execute(
                        "SELECT status, project_id FROM command WHERE command_id=?",
                        (command_id,)).fetchone()
                    if row is None:
                        return None
                    proj, old_status = row[1], row[0]
                    if old_status in DOWNLINK_TERMINAL_STATUSES:
                        return "already-terminal"
                    conn.execute(
                        "UPDATE command SET status=?, result_json=?, finished_at=?"
                        " WHERE command_id=?",
                        (status, json.dumps(result, ensure_ascii=False,
                                            sort_keys=True),
                         result["finished_at"], command_id))
            finally:
                conn.close()
        if self.ingest_store is not None:
            self.ingest_store.append_server_event(proj, {
                "ts": result["finished_at"], "ev": "downlink.result",
                "command_id": command_id, "project_id": proj,
                "status": status, "exit_code": result["exit_code"]})
        return "ok"


class ApiHandler(BaseHTTPRequestHandler):
    state = None
    static_dir = None

    def do_GET(self):
        path, _, query = self.path.partition("?")
        m = DOWNLINK_PICKUP_RE.match(path)
        if m:
            agent_id = self._downlink_gate()
            if agent_id:
                self._downlink_pickup(agent_id)
            return
        m = DOWNLINK_COMMAND_ID_RE.match(path)
        if m:
            agent_id = self._downlink_gate()
            if agent_id:
                self._downlink_status(m.group(1))
            return
        m = EVENTS_RE.match(path)
        if m:
            self._events(m.group(1), query)
            return
        m = SESSIONS_RE.match(path)
        if m:
            self._sessions(m.group(1), query)
            return
        m = STATUS_RE.match(path)
        if m:
            self._register_status(m.group(1), query)
            return
        # 下行指令队列（TASK-035）：agent 拾取 / dispatcher 状态轮询
        if path == "/api/downlink/pickup":
            self._downlink_pickup()
            return
        m = DOWNLINK_STATUS_RE.match(path)
        if m:
            self._downlink_status(int(m.group(1)))
            return
        if path == "/api/register/codes":
            self._enrollment_codes_list(query)
            return
        if path == "/api/register/list":
            self._register_list(query)
            return
        if path == "/api/status":
            self._json(self._status_payload(query))
        elif path == "/api/history":
            self._history(query)
        else:
            self._static()

    def do_POST(self):
        """POST /api/ingest（TASK-034/035/036，MONITOR-SPEC §3.1.3）或 POST /api/register（TASK-050，§3.2.6）。

        /api/ingest：鉴权 → 限流 → 解析 → 授权 → 落库。
        顺序：先鉴权（401）再限流（429）再读体——未认证请求不消耗解析资源，也不泄露任何数据；
        授权范围（403）/ 未注册（400）/ 同 id 双 agent（409）由 _ingest 内检查。
        错误：401（无/错 token）/ 429（限流超限）/ 400（schema 错 / project_id 未注册）/
        403（project_id 不在授权范围）/ 409（同 id 已被另一 agent 占用）/ 413（payload 超限）/
        404（非 ingest 路径）/ 500（落库失败）。

        /api/register：公开端点，全局限流 → 校验 → 冲突检测 → 写入。
        错误：400（schema 错）/ 409（project_id 已存在）/ 429（限流超限）。
        """
        path = self.path.split("?", 1)[0]
        m = APPROVE_RE.match(path)
        if m:
            self._approve(m.group(1))
            return
        m = REJECT_RE.match(path)
        if m:
            self._reject(m.group(1))
            return
        m = REVOKE_RE.match(path)
        if m:
            self._revoke(m.group(1))
            return
        m = RENEW_RE.match(path)
        if m:
            self._renew(m.group(1))
            return
        m = CODES_GENERATE_RE.match(path)
        if m:
            self._enrollment_codes_generate()
            return
        m = CODES_REVOKE_RE.match(path)
        if m:
            self._enrollment_code_revoke(m.group(1))
            return
        # 下行指令队列（TASK-035）：dispatcher 入队 / agent 回报
        if path == "/api/downlink/commands":
            self._downlink_enqueue()
            return
        m = DOWNLINK_RESULT_RE.match(path)
        if m:
            self._downlink_result(int(m.group(1)))
            return
        if path == "/api/register":
            self._register()
            return
        m = DOWNLINK_RESULT_RE.match(path)
        if m:
            agent_id = self._downlink_gate()
            if agent_id:
                self._downlink_result(m.group(1), agent_id)
            return
        m = DOWNLINK_COMMANDS_RE.match(path)
        if m:
            agent_id = self._downlink_gate()
            if agent_id:
                self._downlink_enqueue(agent_id)
            return
        if path != "/api/ingest":
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

    # ---------------- 下行指令端点（TASK-071，AGENT-DOWNLINK-CONTRACT v1.0） ----------------

    def _downlink_gate(self):
        """下行端点共用前置（契约 §一）：Bearer 鉴权（401）→ 每 agent 限流（429）。

        与 /api/ingest 同序：先鉴权再限流再读体；返回 agent 身份（str）或 None
        （错误响应已写，调用方直接 return）。
        """
        token = extract_bearer_token(self.headers.get("Authorization", ""))
        agent_id = resolve_agent_id(ApiHandler.state.agents, token)
        if agent_id is None:
            # 不区分缺失/错误 token，不泄露任何队列信息（同 ingest 401 语义）
            self._json_error(401, "鉴权失败")
            return None
        if not ApiHandler.state.rate_limiter.allow(agent_id):
            self._json_error(429, "请求过于频繁，请稍后重试")
            return None
        return agent_id

    def _downlink_enqueue(self, agent_id):
        """POST /api/downlink/commands（契约 §一/§二/§五）：写入侧双闸第一道。

        闸序：schema 400 → command.name 白名单 400 → 注册表 + agent 传输条目 400
        → dedup 在途 409（复用既有 command_id）→ 200 入队。
        """
        data, err = self._read_body()
        if err:
            self._json_error(413, err)
            return
        try:
            body = json.loads((data or b"").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json_error(400, "body 必须是合法 JSON")
            return
        if not isinstance(body, dict):
            self._json_error(400, "body 必须是 JSON 对象")
            return
        project_id = body.get("project_id")
        dedup_key = body.get("dedup_key")
        command = body.get("command")
        if not (isinstance(project_id, str) and project_id):
            self._json_error(400, "project_id 必须是非空字符串")
            return
        if not (isinstance(dedup_key, str) and dedup_key):
            self._json_error(400, "dedup_key 必须是非空字符串")
            return
        if not isinstance(command, dict):
            self._json_error(400, "command 必须是对象")
            return
        name = command.get("name")
        args = command.get("args")
        if name not in DOWNLINK_COMMAND_NAMES:
            self._json_error(400, "command.name 不在白名单: "
                             + ", ".join(DOWNLINK_COMMAND_NAMES))
            return
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            self._json_error(400, "command.args 必须是字符串数组")
            return
        timeout_secs = body.get("timeout_secs", DOWNLINK_DEFAULT_TIMEOUT_SECS)
        if (isinstance(timeout_secs, bool) or not isinstance(timeout_secs, int)
                or timeout_secs <= 0 or timeout_secs > DOWNLINK_MAX_TIMEOUT_SECS):
            self._json_error(400, "timeout_secs 必须是 1.."
                             f"{DOWNLINK_MAX_TIMEOUT_SECS} 的整数")
            return
        proj = next((p for p in ApiHandler.state.config.get("projects", [])
                     if p.get("id") == project_id), None)
        if proj is None or proj.get("transport", "local") != "agent":
            # 写入侧闸（契约 §一/§五）：未注册 / 非 agent 传输条目 → 400 不入队
            self._json_error(400, "project 未注册或非 agent 传输条目")
            return
        cmd, reused = ApiHandler.state.downlink.enqueue(
            project_id, dedup_key, name, args, timeout_secs, created_by=agent_id)
        if reused:
            # 409 也带 command_id（dispatcher enqueue 防双派依赖它，契约 §五）
            self._json_error(409, "dedup_key 在途，复用既有指令",
                             extra={"command_id": cmd["command_id"]})
            return
        self._json({"command_id": cmd["command_id"], "seq": cmd["seq"]})

    def _downlink_pickup(self, agent_id):
        """GET /api/downlink/pickup（契约 §三）：拾取侧第二道闸——
        只下发该 token 白名单内项目的指令；无 → {"command": null}（等价队列为空）。"""
        allowed = authorized_projects(ApiHandler.state.agents, agent_id)
        cmd = ApiHandler.state.downlink.pickup(agent_id, allowed)
        self._json({"command": cmd})

    def _downlink_status(self, command_id):
        """GET /api/downlink/commands/{id}（契约 §一）：dispatcher 轮询读回。"""
        cmd = ApiHandler.state.downlink.get(command_id)
        if cmd is None:
            self._json_error(404, "指令不存在")
            return
        self._json({"command": cmd})

    def _downlink_result(self, command_id, agent_id):
        """POST /api/downlink/commands/{id}/result（契约 §四/§五）。

        闸序：404 未知 → 已终态 409（幂等忽略）→ 非 picker 403（纵深防御）
        → report schema 400 → 终态落库 + downlink.result 事件 → 200。
        """
        cmd = ApiHandler.state.downlink.get(command_id)
        if cmd is None:
            self._json_error(404, "指令不存在")
            return
        if cmd["status"] in DOWNLINK_TERMINAL_STATUSES:
            self._json_error(409, "already-terminal")
            return
        if cmd.get("picked_by") != agent_id:
            self._json_error(403, "非拾取者不可回报")
            return
        data, err = self._read_body()
        if err:
            self._json_error(413, err)
            return
        try:
            report = json.loads((data or b"").decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json_error(400, "body 必须是合法 JSON")
            return
        if not isinstance(report, dict) or report.get("status") not in DOWNLINK_TERMINAL_STATUSES:
            self._json_error(400, "status 必须是 done|failed|skipped")
            return
        exit_code = report.get("exit_code")
        if exit_code is not None and (isinstance(exit_code, bool)
                                      or not isinstance(exit_code, int)):
            self._json_error(400, "exit_code 必须是整数或 null")
            return
        for k in ("stdout_tail", "stderr_tail"):
            if report.get(k) is not None and not isinstance(report[k], str):
                self._json_error(400, f"{k} 必须是字符串或 null")
                return
        res = ApiHandler.state.downlink.report_result(command_id, report)
        if res == "already-terminal":
            self._json_error(409, "already-terminal")
            return
        if res is None:
            self._json_error(404, "指令不存在")
            return
        self._json({"ok": True})

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

    # ------------------------------------------------------------------
    # 下行指令队列 handlers（TASK-035，AGENT-DOWNLINK-CONTRACT §一~§五）
    # ------------------------------------------------------------------
    def _downlink_auth(self):
        """Bearer 鉴权 + 限流；失败时已写响应并返回 None（顺序同 do_POST：先 401 再 429）。"""
        token = extract_bearer_token(self.headers.get("Authorization", ""))
        agent_id = resolve_agent_id(ApiHandler.state.agents, token)
        if agent_id is None:
            self._json_error(401, "鉴权失败")
            return None
        if not ApiHandler.state.rate_limiter.allow(agent_id):
            self._json_error(429, "请求过于频繁，请稍后重试")
            return None
        return agent_id

    def _downlink_enqueue(self):
        """POST /api/downlink/commands：dispatcher 入队（契约 §二/§五）。

        鉴权 → 限流 → schema（400）→ 注册表闸门（400：未登记 / transport=local）→
        入队；dedup_key 未终态重复 → 409（extra 带既有 command_id/seq，幂等入队依据）。
        """
        agent_id = self._downlink_auth()
        if agent_id is None:
            return
        data, err = self._read_body()
        if err:
            self._json_error(413, err)
            return
        try:
            body = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json_error(400, "请求体不是合法 JSON")
            return
        spec = validate_downlink_body(body)
        if isinstance(spec, str):
            self._json_error(400, spec)
            return
        registry = load_projects_config(ApiHandler.state.projects_path)
        entry = next((p for p in registry.get("projects", [])
                      if isinstance(p, dict) and p.get("id") == spec["project_id"]), None)
        if entry is None:
            self._json_error(400, "project_id 未登记")
            return
        if entry.get("transport") == "local":
            self._json_error(400, "transport=local 条目不支持下行指令")
            return
        row, reused = ApiHandler.state.downlink.enqueue(
            spec["project_id"], spec["dedup_key"], spec["command"],
            spec["timeout_secs"], agent_id, time.time())
        if reused:
            self._json_error(409, "dedup_key 已有未终态指令",
                             {"command_id": row["command_id"], "seq": row["seq"]})
            return
        self._json({"command_id": row["command_id"], "seq": row["seq"], "status": row["status"]})

    def _downlink_pickup(self):
        """GET /api/downlink/pickup：agent 拾取（契约 §三）。

        鉴权 → per-token 项目白名单（fail-closed：空集合不下发）→ 回收超时 → 领取。
        无可领指令 → command=null（等价队列空，不泄露他项目信息）。
        """
        agent_id = self._downlink_auth()
        if agent_id is None:
            return
        allowed = authorized_projects(ApiHandler.state.agents, agent_id)
        cmd = ApiHandler.state.downlink.pickup(allowed, time.time())
        self._json({"command": cmd})

    def _downlink_result(self, command_id):
        """POST /api/downlink/commands/{id}/result：agent 回报（契约 §四/§五）。

        鉴权 → schema（400）→ 指令存在（404）→ 项目授权（403）→ 脱敏截断 → 落终态；
        已终态 → 409 幂等忽略（extra 带既有状态）。
        """
        agent_id = self._downlink_auth()
        if agent_id is None:
            return
        data, err = self._read_body()
        if err:
            self._json_error(413, err)
            return
        try:
            body = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json_error(400, "请求体不是合法 JSON")
            return
        status = body.get("status")
        if status not in DOWNLINK_TERMINAL_STATUSES:
            self._json_error(400, "status 必须是 done/failed/skipped")
            return
        try:
            exit_code = int(body.get("exit_code", -1))
        except (TypeError, ValueError):
            self._json_error(400, "exit_code 必须是整数")
            return
        row = ApiHandler.state.downlink.get(command_id)
        if row is None:
            self._json_error(404, "指令不存在")
            return
        if not is_project_authorized(ApiHandler.state.agents, agent_id, row["project_id"]):
            self._json_error(403, "project_id 不在授权范围")
            return
        stored, already = ApiHandler.state.downlink.result(
            command_id, status, exit_code,
            scrub_downlink_tail(body.get("stdout_tail", "")),
            scrub_downlink_tail(body.get("stderr_tail", "")), time.time())
        if already:
            self._json_error(409, "指令已终态（幂等忽略）", {"command_id": command_id, "status": stored["status"]})
            return
        self._json({"command_id": command_id, "status": stored["status"]})

    def _downlink_status(self, command_id):
        """GET /api/downlink/commands/{id}：dispatcher 轮询状态（契约 §一/§四）。"""
        agent_id = self._downlink_auth()
        if agent_id is None:
            return
        row = ApiHandler.state.downlink.get(command_id)
        if row is None:
            self._json_error(404, "指令不存在")
            return
        self._json({"command": row})

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
        # TASK-071：task 事件流入库（解析/校验已在 validate_ingest_payload 完成）。
        # 事件落库失败返回 500（不 200）——事件是审计级真相，静默丢弃会破坏「推了有人收」
        # 的闭环；files 快照与 task 事件分开存储，互不影响既有 ingest_state 语义。
        events = obj.get("events")
        if events is not None:
            try:
                ApiHandler.state.ingest.store_task_events(
                    project_id, events, obj.get("cursor"))
            except Exception as e:
                print(f"[{datetime.now().strftime('%F %T')}] task 事件落库失败 "
                      f"project_id={project_id}: {type(e).__name__}: {e}", file=sys.stderr)
                self._json_error(500, "task 事件落库失败")
                return
        # TASK-073：session 日志增量落库（aibase TASK-104 契约 v1.1）。与 task 事件同款：
        # 落库失败 500（不 200）——agent 端游标推进以「服务端确认」为前提，静默丢批 =
        # 数据永久丢失；files 快照 / task 事件 / session 增量三者分表，互不影响既有语义。
        sessions = obj.get("sessions")
        if sessions is not None:
            try:
                ApiHandler.state.ingest.store_session_deltas(project_id, sessions)
            except Exception as e:
                print(f"[{datetime.now().strftime('%F %T')}] session 日志落库失败 "
                      f"project_id={project_id}: {type(e).__name__}: {e}", file=sys.stderr)
                self._json_error(500, "session 日志落库失败")
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

    def _json_error(self, status, message, extra=None):
        """发送 JSON 错误响应；extra 为额外键值对（如 existing 字段）。"""
        obj = {"error": message}
        if extra:
            obj.update(extra)
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _register(self):
        """POST /api/register（TASK-050，MONITOR-SPEC §3.2.6）：全局限流 → 校验 → 冲突检测 → 写入。

        公开端点，无 token 鉴权。
        错误：400（schema 错）/ 409（project_id 已存在）/ 429（限流超限）。
        """
        # 1. 全局限流（60 次/分钟，可配置）
        if not ApiHandler.state.register_limiter.allow("__global__"):
            self._json_error(429, "rate limit")
            return

        # 2. 读取请求体
        data, err = self._read_body()
        if err:
            self._json_error(413, err)
            return

        # 3. 解析 JSON
        try:
            obj = json.loads(data)
        except (UnicodeDecodeError, ValueError):
            self._json_error(400, "请求体不是合法 JSON")
            return
        if not isinstance(obj, dict):
            self._json_error(400, "请求体必须为 JSON 对象")
            return

        # 4. Schema 校验
        project_id = obj.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            self._json_error(400, "project_id 必须为非空字符串")
            return
        if not re.match(r"^[a-zA-Z0-9-]+$", project_id):
            self._json_error(400, "project_id 只能包含字母、数字和连字符")
            return

        path = obj.get("path")
        if not isinstance(path, str) or not path.strip():
            self._json_error(400, "path 必须为非空字符串")
            return

        host_info = obj.get("host_info")
        if not isinstance(host_info, str) or not host_info.strip():
            self._json_error(400, "host_info 必须为非空字符串")
            return

        request_key = obj.get("request_key")
        if not isinstance(request_key, str) or not request_key.strip():
            self._json_error(400, "request_key 必须为非空字符串")
            return
        if len(request_key.encode("utf-8")) < 16:
            self._json_error(400, "request_key 长度不能少于 16 字节")
            return

        enrollment_code = obj.get("enrollment_code")
        if enrollment_code is not None and not isinstance(enrollment_code, str):
            self._json_error(400, "enrollment_code 必须为字符串")
            return

        # 5. 冲突检测
        # project_id 在 config/projects.json 已存在 → 409 active
        if is_project_registered(ApiHandler.state.config, project_id):
            self._json_error(409, "project_id 已存在", extra={"existing": "active"})
            return

        # project_id 在 ingest_state 有活跃记录 → 409 active
        ingest_row = ApiHandler.state.ingest.read(project_id)
        if ingest_row is not None:
            self._json_error(409, "project_id 已存在", extra={"existing": "active"})
            return

        # 同 project_id 已有 pending 申请 → 409 pending
        pending = ApiHandler.state.registration.list_by_status("pending")
        for p in pending:
            if p["project_id"] == project_id:
                self._json_error(409, "project_id 已存在", extra={"existing": "pending"})
                return

        # 同 project_id 只有 rejected/expired 记录 → 允许注册（不冲突）

        # 6. 注册码校验
        code_valid = False
        if enrollment_code:
            if ApiHandler.state.enrollment.validate(enrollment_code, project_id):
                code_valid = True
            else:
                self._json_error(400, "invalid enrollment_code")
                return

        # 7. 写入 RegistrationStore（path 一并存储，TASK-069）
        req_id = ApiHandler.state.registration.create(
            project_id, enrollment_code, host_info, request_key, path=path)
        if req_id is None:
            # 竞争条件：两次请求之间插入了 pending
            self._json_error(409, "project_id 已存在", extra={"existing": "pending"})
            return

        # 8. 消费注册码
        if code_valid and enrollment_code:
            ApiHandler.state.enrollment.consume(enrollment_code)

        # 9. 返回 201
        row = ApiHandler.state.registration.get(req_id)
        body = json.dumps({
            "req_id": req_id,
            "status": "pending",
            "pending_since": row["created_at"],
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(201)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _register_list(self, query):
        """GET /api/register/list?status=pending（TASK-055）：返回注册申请列表。

        Admin-authenticated 端点（check_admin_password）。
        按 status 筛选（缺省返回全部），按 created_at 升序。
        错误：401/auth。
        """
        # 1. 鉴权
        if not check_admin_password(self):
            self._json_error(401, "unauthorized")
            return

        # 2. 解析 status 参数
        params = parse_qs(query)
        status = (params.get("status") or [None])[0]

        # 3. 查询注册列表
        rows = ApiHandler.state.registration.list_by_status(status)

        # 4. 返回 JSON 数组
        self._json(rows)

    def _register_status(self, req_id, query):
        """GET /api/register/:req_id/status（TASK-051，MONITOR-SPEC §3.2.7）：agent 轮询审批结果。

        公开端点，无 token 鉴权。
        - request_key 不匹配 → 404（不区分'req_id 不存在'和'key 错误'）
        - 缺少 request_key 参数 → 404
        - 各状态返回不同响应体（验收标准 STATUS-002）
        - Token 单次交付：首次返回 approved 时含 token，标记 token_delivered=1，后续不再返回
        - 不涉及限流（agent 轮询频率低，30s 一次）
        """
        params = parse_qs(query)
        request_key = (params.get("request_key") or [None])[0]
        if not request_key:
            self._json_error(404, "not found")
            return

        row = ApiHandler.state.registration.get(req_id)
        if row is None:
            self._json_error(404, "not found")
            return

        # request_key 绑定校验：不匹配 → 404（不泄露 req_id 是否存在）
        if not hmac.compare_digest(row["request_key"].encode("utf-8"),
                                     request_key.encode("utf-8")):
            self._json_error(404, "not found")
            return

        status = row["status"]

        if status == "pending":
            self._json({"status": "pending", "pending_since": row["created_at"]})
        elif status == "approved":
            if row["token_delivered"]:
                # 已交付 → 不再返回 token
                self._json({"status": "approved"})
            else:
                # 首次交付 → 标记 delivered，返回 token + project_id
                ApiHandler.state.registration.mark_token_delivered(req_id)
                self._json({
                    "status": "approved",
                    "token": row["issued_token"],
                    "project_id": row["project_id"],
                })
        elif status == "rejected":
            self._json({"status": "rejected", "reason": row.get("reject_reason") or ""})
        elif status == "expired":
            self._json({"status": "expired"})
        elif status == "revoked":
            self._json({"status": "revoked"})
        else:
            self._json_error(404, "not found")

    def _approve(self, req_id):
        """POST /api/register/:req_id/approve（TASK-052，MONITOR-SPEC §3.2.8）。

        Admin-authenticated 端点（check_admin_password）。
        错误：401/auth、404/req_id 不存在、409/已处理、500/agents.json 写入失败。
        """
        # 1. 鉴权
        if not check_admin_password(self):
            self._json_error(401, "unauthorized")
            return

        # 2. 读取注册记录
        row = ApiHandler.state.registration.get(req_id)
        if row is None:
            self._json_error(404, "not found")
            return

        # 3. 幂等/状态检查
        if row["status"] != "pending":
            self._json_error(409, "already processed")
            return

        # 4. 签发 token
        try:
            issuer = TokenIssuer(agents_path=ApiHandler.state.agents_path)
            result = issuer.issue(row["project_id"])
        except OSError as e:
            print(f"[{datetime.now().strftime('%F %T')}] agents.json 写入失败: {e}",
                  file=sys.stderr)
            self._json_error(500, "internal error")
            return

        # 5. 更新 RegistrationStore（pending → approved）
        token = result["token"]
        if not ApiHandler.state.registration.approve(req_id, token):
            # 竞争条件：理论上不会发生（步骤 3 已检查 status），但防御。
            # 步骤 4 已把 token 写入 agents.json，此处回滚避免 orphan token
            # （可通过 /api/ingest 鉴权而注册请求未 approved）。
            try:
                issuer.remove(row["project_id"])
            except OSError as e:
                print(f"[{datetime.now().strftime('%F %T')}] agents.json 回滚失败: {e}",
                      file=sys.stderr)
            self._json_error(409, "already processed")
            return

        # 6. 消费注册码（如存在）；失败不阻断审批，但记录告警
        if row.get("enrollment_code"):
            try:
                ApiHandler.state.enrollment.consume(row["enrollment_code"])
            except Exception as e:
                print(f"[{datetime.now().strftime('%F %T')}] "
                      f"enrollment_code 消费失败: {e}", file=sys.stderr)

        # 7. 刷新 agents 缓存
        ApiHandler.state.agents = load_agents_config(ApiHandler.state.agents_path)

        # 7.5 自动登记 project_id 到 config/projects.json（TASK-069）：审批通过即把
        # 申请登记为 agent 项目（transport=agent, path=申请 path），修复审批后 agent
        # 推送 400 "project_id 未注册"。幂等（已存在不重复追加）。
        # 失败不阻断审批（best-effort，与 enrollment_code 消费失败语义一致），仅告警——
        # token 已签发且 agents.json 已写，回滚会引入不一致。
        try:
            entry = register_project_in_config(
                ApiHandler.state.projects_path,
                row["project_id"],
                name=row["project_id"],
                path_value=row.get("path"),
                transport="agent",
            )
            if entry is not None:
                projects = ApiHandler.state.config.setdefault("projects", [])
                if not any(p.get("id") == row["project_id"] for p in projects):
                    projects.append(entry)
        except Exception as e:
            print(f"[{datetime.now().strftime('%F %T')}] "
                  f"projects.json 自动登记失败: {e}", file=sys.stderr)

        # 8. 返回 200
        self._json({
            "status": "approved",
            "req_id": req_id,
            "project_id": row["project_id"],
        })

    def _reject(self, req_id):
        """POST /api/register/:req_id/reject（TASK-052，MONITOR-SPEC §3.2.8）。

        Admin-authenticated 端点（check_admin_password）。
        请求体 JSON（可选）：{ "reason": "拒绝原因" } —— 持久化于 reject_reason 列
        （TASK-056 F2 修复：MONITOR-SPEC §3.2.8 已声明请求体，此前被丢弃）。
        错误：401/auth、404/req_id 不存在、409/已处理。
        """
        # 1. 鉴权
        if not check_admin_password(self):
            self._json_error(401, "unauthorized")
            return

        # 2. 读取注册记录
        row = ApiHandler.state.registration.get(req_id)
        if row is None:
            self._json_error(404, "not found")
            return

        # 3. 幂等/状态检查
        if row["status"] != "pending":
            self._json_error(409, "already processed")
            return

        # 4. 读取可选拒绝原因（MONITOR-SPEC §3.2.8：{ "reason": "拒绝原因" }）
        reason = None
        body = self._read_body_json()
        if body is not None:
            r = body.get("reason")
            if isinstance(r, str) and r.strip():
                reason = r.strip()

        # 5. 更新 RegistrationStore（pending → rejected）
        # 未提供原因时使用 None（区别于空串）
        if not ApiHandler.state.registration.reject(req_id, reason=reason):
            self._json_error(409, "already processed")
            return

        # 6. 返回 200
        self._json({
            "status": "rejected",
            "req_id": req_id,
        })

    def _revoke(self, req_id):
        """POST /api/register/:req_id/revoke（TASK-053，MONITOR-SPEC §3.2.8）。

        Admin-authenticated 端点（check_admin_password）。
        错误：401/auth、404/req_id 不存在、409/已处理（非 approved 状态）。
        """
        # 1. 鉴权
        if not check_admin_password(self):
            self._json_error(401, "unauthorized")
            return

        # 2. 读取注册记录
        row = ApiHandler.state.registration.get(req_id)
        if row is None:
            self._json_error(404, "not found")
            return

        # 3. 状态检查：必须是 approved 状态
        if row["status"] != "approved":
            self._json_error(409, "already processed")
            return

        # 4. 从 agents.json 中移除 token
        try:
            issuer = TokenIssuer(agents_path=ApiHandler.state.agents_path)
            issuer.remove(row["project_id"])
        except OSError as e:
            print(f"[{datetime.now().strftime('%F %T')}] agents.json 写入失败: {e}",
                  file=sys.stderr)
            self._json_error(500, "internal error")
            return

        # 5. 更新 RegistrationStore（approved → revoked）
        if not ApiHandler.state.registration.revoke(req_id):
            # 竞争条件：理论上不会发生（步骤 3 已检查 status），但防御
            self._json_error(409, "already processed")
            return

        # 6. 刷新 agents 缓存
        ApiHandler.state.agents = load_agents_config(ApiHandler.state.agents_path)

        # 7. 返回 200
        self._json({
            "status": "revoked",
        })

    def _renew(self, req_id):
        """POST /api/register/:req_id/renew（TASK-053，MONITOR-SPEC §3.2.6）。

        Admin-authenticated 端点（check_admin_password）。
        执行 revoke 逻辑（移除旧 token）+ 签发新 token。
        错误：401/auth、404/req_id 不存在、409/已处理（非 approved 状态/已 renew）。
        部分失败窗口（REVIEW F2，可重试自愈）：步骤 5 remove 成功后步骤 6 issue 失败 → 500，
        renew_count 未递增、旧 token 已删，agent 重试 renew 会重签；
        步骤 7 store 守卫失败 → 409（并发 revoke 或并发 renew），按需回滚本请求刚签发的 token。
        """
        # 1. 鉴权
        if not check_admin_password(self):
            self._json_error(401, "unauthorized")
            return

        # 2. 读取注册记录
        row = ApiHandler.state.registration.get(req_id)
        if row is None:
            self._json_error(404, "not found")
            return

        # 3. 状态检查：必须是 approved 状态
        if row["status"] != "approved":
            self._json_error(409, "already processed")
            return

        # 4. 重复 renew 检测
        if row.get("renew_count", 0) > 0:
            self._json_error(409, "already processed")
            return

        # 5. 从 agents.json 中移除旧 token
        try:
            issuer = TokenIssuer(agents_path=ApiHandler.state.agents_path)
            issuer.remove(row["project_id"])
        except OSError as e:
            print(f"[{datetime.now().strftime('%F %T')}] agents.json 写入失败: {e}",
                  file=sys.stderr)
            self._json_error(500, "internal error")
            return

        # 6. 签发新 token
        try:
            result = issuer.issue(row["project_id"])
        except OSError as e:
            print(f"[{datetime.now().strftime('%F %T')}] agents.json 写入失败: {e}",
                  file=sys.stderr)
            self._json_error(500, "internal error")
            return

        # 7. 更新 RegistrationStore：保持 status='approved'，更新 issued_token/decided_at/
        #    token_delivered=0（供 agent 通过 status 端点领取新 token），renew_count+1。
        #    守卫在 store 锁内（status='approved' AND renew_count=0），并发安全
        #    （REVIEW F1：并发 revoke/双 renew 不得在非 approved 记录上写新 token）。
        if not ApiHandler.state.registration.renew(req_id, result["token"]):
            # 并发竞争：守卫失败（并发 revoke 已提交，或并发 renew 已提交）。
            # 仅当 agents.json 中仍是本请求刚签发的 token 时回滚——并发 revoke 场景下
            # 避免 revoked 记录残留 live token（REVIEW F1）；并发双 renew 场景下保留
            # 胜出方 token，不误删。
            try:
                current = read_agents_config(ApiHandler.state.agents_path)
                if current.get(row["project_id"]) == result["token"]:
                    issuer.remove(row["project_id"])
            except OSError as e:
                print(f"[{datetime.now().strftime('%F %T')}] agents.json 回滚失败: {e}",
                      file=sys.stderr)
            self._json_error(409, "already processed")
            return

        # 8. 刷新 agents 缓存
        ApiHandler.state.agents = load_agents_config(ApiHandler.state.agents_path)

        # 9. 返回 200
        self._json({
            "status": "approved",
            "note": "新 token 已签发，agent 下次推送时收到 401 后自动轮询领取",
        })

    # ===== TASK-057: 注册码管理端点 =====

    def _enrollment_codes_list(self, query):
        """GET /api/register/codes（TASK-057）：返回所有注册码列表。

        Admin-authenticated 端点（check_admin_password）。
        按 created_at 升序，返回 EnrollmentCodeStore.list() 全部字段。
        错误：401/auth。
        """
        if not check_admin_password(self):
            self._json_error(401, "unauthorized")
            return
        codes = ApiHandler.state.enrollment.list()
        self._json(codes)

    def _enrollment_codes_generate(self):
        """POST /api/register/codes/generate（TASK-057）：生成注册码。

        Admin-authenticated 端点（check_admin_password）。
        请求体 JSON：
          - description（必填，字符串）
          - allowed_project（可选，glob 字符串）
          - max_uses（可选，整数，默认 1）
          - expire_at（可选，timestamp 浮点数，过期时间）
        返回：{code, description, allowed_project_pattern, max_uses, expire_at, created_at}
        错误：401/auth、400/参数校验。
        """
        if not check_admin_password(self):
            self._json_error(401, "unauthorized")
            return

        body = self._read_body_json()
        if body is None:
            self._json_error(400, "请求体必须为 JSON")
            return

        description = body.get("description")
        if not description or not isinstance(description, str) or not description.strip():
            self._json_error(400, "description 为必填字符串")
            return

        allowed_project = body.get("allowed_project")
        if allowed_project is not None and not isinstance(allowed_project, str):
            self._json_error(400, "allowed_project 必须为字符串")
            return

        max_uses = body.get("max_uses", 1)
        # F5（REVIEW）：Python bool 是 int 子类，isinstance(True, int) 为 True；
        # 先排除布尔，避免 {"max_uses": true} 被当作 1 接受
        if isinstance(max_uses, bool) or not isinstance(max_uses, int) or max_uses < 1:
            self._json_error(400, "max_uses 必须为 >=1 的整数")
            return

        expire_at = body.get("expire_at")
        # F5（REVIEW）：同样排除布尔（{"expire_at": true} 不应通过）
        if expire_at is not None and (isinstance(expire_at, bool) or not isinstance(expire_at, (int, float))):
            self._json_error(400, "expire_at 必须为数字（timestamp）")
            return

        code = ApiHandler.state.enrollment.generate(
            description=description.strip(),
            allowed_project_pattern=allowed_project.strip() if allowed_project else None,
            max_uses=max_uses,
            expire_at=expire_at,
        )

        # F6（REVIEW）：created_at 取 store 记录值，与库内时间一致
        # （generate() 内部写入的 now 与响应侧 time.time() 有毫秒级差异）
        created_at = None
        row = ApiHandler.state.enrollment.get(code)
        if row is not None:
            created_at = row.get("created_at")
        if created_at is None:
            created_at = time.time()

        self._json({
            "code": code,
            "description": description.strip(),
            "allowed_project_pattern": allowed_project.strip() if allowed_project else None,
            "max_uses": max_uses,
            "expire_at": expire_at,
            "created_at": created_at,
        })

    def _enrollment_code_revoke(self, code):
        """POST /api/register/codes/:code/revoke（TASK-057）：吊销注册码。

        Admin-authenticated 端点（check_admin_password）。
        错误：401/auth。
        幂等：重复吊销不报错（EnrollmentCodeStore.revoke 对不存在 code 无操作）。
        """
        if not check_admin_password(self):
            self._json_error(401, "unauthorized")
            return

        ApiHandler.state.enrollment.revoke(code)
        self._json({"status": "revoked", "code": code})

    def _read_body_json(self):
        """读取请求体并解析为 JSON；失败返回 None。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            length = 0
        if length <= 0:
            return None
        try:
            raw = self.rfile.read(length)
            return json.loads(raw)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

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
        响应：{project, limit, generated_at, counts, events, task_events}，
        - counts/events：每 role 最近 limit 条 ts 降序（既有契约不变）；
        - task_events（TASK-071）：{count, cursor, events}——task 事件流总条数、
          已确认覆盖最大 seq、最近 limit 条按 seq 降序（含 seq/ts/ev/task/...）。
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
        # TASK-071：task 事件流从 ingest_state 的 task_events 表读（与 role 事件同端点展示）
        try:
            tcount, tcursor, tevents = ApiHandler.state.ingest.read_task_events(
                project_id, limit)
        except Exception as e:
            print(f"[{datetime.now().strftime('%F %T')}] task 事件读取失败 "
                  f"project_id={project_id}: {type(e).__name__}: {e}", file=sys.stderr)
            self._json_error(500, "task 事件读取失败")
            return
        self._json({
            "project": project_id,
            "limit": limit,
            "generated_at": time.time(),
            "counts": counts,
            "events": events,
            "task_events": {
                "count": tcount,
                "cursor": tcursor,
                "events": tevents,
            },
        })

    def _sessions(self, project_id, query):
        """GET /api/projects/<id>/sessions（TASK-073，前端轮询查看页数据源）。

        项目未注册 → 404。两种视图（单端点，参数分派）：
        - 无 task → 概览：{project, generated_at, truncated, last_push, tasks:
          [{task_id, files: [{name, line_count, last_offset, updated_at}]}]}；
          可选 task 过滤（file 缺省时仅返回该 task 的文件列表）。
        - task+file → 行视图：{project, task, file, line_count, last_offset,
          truncated, limit, lines: [{line_no, ok, type, data|text}]}——最近 limit 行
          升序，逐行宽松 JSON 解析（损坏行 ok=false 原样标注，不丢弃）。
        limit 默认 200、(0, SESSION_QUERY_MAX_LINES] 正整数，非法 → 400。
        前端轮询（默认 5s，消息级粒度 + agent 30s 推送周期下满足 ≤10s 级感知；
        SSE 增益有限，选型决策见任务卡备注）。
        """
        proj = next((p for p in ApiHandler.state.config.get("projects", [])
                     if p.get("id") == project_id), None)
        if proj is None:
            self._json_error(404, f"项目不存在: {project_id}")
            return
        params = parse_qs(query)
        task = (params.get("task") or [None])[0]
        fname = (params.get("file") or [None])[0]
        limit = 200
        raw = (params.get("limit") or [None])[0]
        if raw is not None:
            try:
                limit = float(raw)
            except ValueError:
                self._json_error(400, "limit 必须为数字")
                return
            if not math.isfinite(limit) or limit <= 0 or limit > SESSION_QUERY_MAX_LINES:
                self._json_error(400, f"limit 超出范围 (0, {SESSION_QUERY_MAX_LINES}]")
                return
            if not limit.is_integer():
                self._json_error(400, "limit 必须为整数")
                return
            limit = int(limit)
        try:
            summary = ApiHandler.state.ingest.read_sessions_summary(project_id)
        except Exception as e:
            print(f"[{datetime.now().strftime('%F %T')}] session 概览读取失败 "
                  f"project_id={project_id}: {type(e).__name__}: {e}", file=sys.stderr)
            self._json_error(500, "session 概览读取失败")
            return
        if task is None:
            self._json({"project": project_id, "generated_at": time.time(),
                        "truncated": summary["truncated"],
                        "last_push": summary["last_push"],
                        "tasks": summary["tasks"]})
            return
        tasks = {t["task_id"]: t for t in summary["tasks"]}
        if task not in tasks:
            self._json({"project": project_id, "task": task, "generated_at": time.time(),
                        "truncated": summary["truncated"], "files": []})
            return
        files = tasks[task]["files"]
        if fname is None:
            self._json({"project": project_id, "task": task,
                        "generated_at": time.time(),
                        "truncated": summary["truncated"], "files": files})
            return
        meta = next((f for f in files if f["name"] == fname), None)
        try:
            raw_lines = ApiHandler.state.ingest.read_session_lines(
                project_id, task, fname, limit)
        except Exception as e:
            print(f"[{datetime.now().strftime('%F %T')}] session 行读取失败 "
                  f"project_id={project_id}: {type(e).__name__}: {e}", file=sys.stderr)
            self._json_error(500, "session 行读取失败")
            return
        self._json({
            "project": project_id,
            "task": task,
            "file": fname,
            "line_count": meta["line_count"] if meta is not None else 0,
            "last_offset": meta["last_offset"] if meta is not None else None,
            "updated_at": meta["updated_at"] if meta is not None else None,
            "truncated": summary["truncated"],
            "limit": limit,
            "generated_at": time.time(),
            "lines": [parse_session_line(r["line_no"], r["text"]) for r in raw_lines],
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

    ensure_admin_config()
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
