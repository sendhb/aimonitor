"""state.py — 调度状态机（kit/tools/dispatcher/ 状态层）。

TASK-074：Phase 3 调度状态机（分配指纹 / 超时回收 / 重试上限 / 并发上限 / 状态重建）。
TASK-075：治理挂钩——分配前治理判定（P0 无 approval-ref → 不分配；task
frontmatter rework-count ≥ 3 → 拒绝转人工），跨项目下治理闸门仍生效。

设计（无状态 + 事件流，Ralph 不变）：
- 状态真相源 = 各项目 `runtime/tasks/`（只读快照）；调度器本地只保存
  增量认知——`state_dir/dispatcher-state.json`（当前分配）+ 不可变事件流
  `state_dir/dispatcher-events.jsonl`。
- 分配指纹：project_id + task_id + worker + started_at；同一 key 已有活跃
  分配（allocated/running）时拒绝重复分配（防同任务双跑）。
- 超时回收：活跃分配超过 timeout 秒未完成 → 标记 stale（事件流记录）；
  stale 不再计入活跃并发，可被重新分配。
- 重试上限：同一任务连续失败达到 max_retries（默认 3）→ 标记 human
  （不再自动重试；状态/事件流可见）。
- 并发上限：活跃分配数 < max_workers 才允许新启动（CLI 层按剩余额度选候选）。
- 状态重建：状态文件丢失/损坏 → `rebuild_from_projects()` 用项目
  runtime/tasks/ 重建——in-progress 任务恢复为 running 分配（worker=
  recovered，起点取 heartbeat mtime 或当前时间），kill 后重启恢复认知。

零外部依赖（仅 stdlib + 复用 ../agent/agent_runtime.py 只读层）。
写文件原子化（tmp + os.replace），与 agent_runtime.write_push_cursor 一致。
"""
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass

# 复用 agent_runtime 的只读层（同目录层级：kit/tools/dispatcher/ → ../agent/）
AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "agent"
)
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)
import agent_runtime  # noqa: E402

import governance  # noqa: E402
from registry import is_agent  # noqa: E402

STATE_FILE = "dispatcher-state.json"
EVENTS_FILE = "dispatcher-events.jsonl"
HEARTBEAT_FILE = "dispatcher.heartbeat"
CURSOR_FILE = ".push-cursor"

DEFAULT_MAX_RETRIES = 3
ACTIVE_STATUSES = ("allocated", "running")
ALL_STATUSES = ("allocated", "running", "done", "failed", "stale", "human", "cancelled")

# 与 kit/cli/task 的 TASK_RE / probe / policy 保持一致：只认标准任务文件
TASK_FILE_RE = re.compile(r"^TASK-(\d{3})-[a-z0-9-]+\.md$")
_META_START_RE = re.compile(r"^metadata:?\s*$")
_META_FIELD_RE = re.compile(r"^([a-z0-9-]+):\s*(.*)$")


class StateError(Exception):
    """状态文件读取/解析失败；CLI 层捕获后告警并按空状态处理。"""


@dataclass
class Allocation:
    """单条调度分配（project/task/worker/started_at/status + 重试计数）。"""

    project_id: str
    task_id: str
    worker: str
    started_at: float
    status: str
    retry_count: int = 0
    updated_at: float = 0.0
    comment: str = ""

    @property
    def key(self):
        return f"{self.project_id}|{self.task_id}"

    def to_dict(self):
        return asdict(self)


def _metadata_field(content, key):
    """从 TASK frontmatter 的 metadata 块提取字段；缺失/损坏返回 None。"""
    if not content:
        return None
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    in_metadata = False
    for line in lines[1:]:
        s = line.strip()
        if s == "---":
            break
        if _META_START_RE.match(s):
            in_metadata = True
            continue
        if not in_metadata:
            continue
        m = _META_FIELD_RE.match(s)
        if m and m.group(1) == key:
            return m.group(2).strip() or None
    return None


def _event_last_seq(path):
    """读事件文件末尾段，解析最后一条合法 seq；缺失/不可读/损坏返回 0。

    与 kit/cli/task 的 _event_last_seq 同策略：只扫尾部 8 KiB，避免大文件全量读。
    """
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    seq = 0
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict) and isinstance(data.get("seq"), int) \
                    and not isinstance(data.get("seq"), bool):
                seq = data["seq"]
        except ValueError:
            continue
    return seq


class SchedulerState:
    """调度状态机：分配记录 + 事件流 + 转移（allocated→running→done/failed/stale/human）。

    用法（CLI 层）：
        st = SchedulerState.load(state_dir)
        st.allocate("proj-a", "TASK-001", worker="dispatcher")
        st.mark_running(...) / st.mark_done(...) / st.mark_failed(...)
        st.check_timeouts(task_timeout)
        st.save()          # 任意转移后持久化
        st.rebuild_from_projects(entries)   # kill 后重启恢复认知
    """

    def __init__(self, state_dir, clock=time.time, max_retries=DEFAULT_MAX_RETRIES):
        self.state_dir = state_dir
        self.clock = clock
        self.max_retries = max_retries
        self.allocations = {}  # key -> Allocation
        self.corrupt = False

    # ---------------- 持久化 ----------------

    def _allocations_path(self):
        return os.path.join(self.state_dir, STATE_FILE)

    def _events_path(self):
        return os.path.join(self.state_dir, EVENTS_FILE)

    @classmethod
    def load(cls, state_dir, clock=time.time, max_retries=DEFAULT_MAX_RETRIES):
        """读取 dispatcher-state.json；文件缺失 → 空状态；损坏 → StateError。"""
        st = cls(state_dir, clock=clock, max_retries=max_retries)
        path = st._allocations_path()
        if not os.path.isfile(path):
            return st
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise StateError(f"状态文件读取失败（{path}）: {e}") from e
        if not isinstance(data, dict):
            raise StateError(f"状态文件必须是 JSON 对象（{path}）")
        raw_allocs = data.get("allocations")
        if not isinstance(raw_allocs, dict):
            raise StateError(f"状态文件缺少 allocations 对象（{path}）")
        for key, raw in raw_allocs.items():
            if not isinstance(raw, dict):
                continue
            try:
                alloc = Allocation(
                    project_id=str(raw.get("project_id") or ""),
                    task_id=str(raw.get("task_id") or ""),
                    worker=str(raw.get("worker") or "unknown"),
                    started_at=float(raw.get("started_at") or 0.0),
                    status=str(raw.get("status") or "unknown"),
                    retry_count=int(raw.get("retry_count") or 0),
                    updated_at=float(raw.get("updated_at") or 0.0),
                    comment=str(raw.get("comment") or ""),
                )
            except (TypeError, ValueError):
                continue
            if alloc.project_id and alloc.task_id:
                st.allocations[alloc.key] = alloc
        return st

    def save(self):
        """原子写 dispatcher-state.json（tmp + os.replace）。"""
        os.makedirs(self.state_dir, exist_ok=True)
        payload = {
            "version": 1,
            "updated": self.clock(),
            "allocations": {
                key: self.allocations[key].to_dict()
                for key in sorted(self.allocations)
            },
        }
        path = self._allocations_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)

    # ---------------- 事件流 ----------------

    def append_event(self, ev, project_id=None, task_id=None, worker=None,
                     status=None, retry_count=None, comment=None):
        """向 dispatcher-events.jsonl 追加一条不可变事件（seq 单调递增）。

        失败只告警不阻断（与 kit/cli/task.append_event 一致）：状态文件已写，
        事件缺失不应阻塞调度主流程。
        """
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            path = self._events_path()
            seq = _event_last_seq(path) + 1
            event = {
                "seq": seq,
                "ts": self.clock(),
                "ev": ev,
                "project": project_id,
                "task": task_id,
                "worker": worker,
                "status": status,
                "retry_count": retry_count,
                "comment": comment,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as e:
            print(f"⚠ 调度事件追加失败（不影响状态机）: {e}", file=sys.stderr)

    # ---------------- 查询 ----------------

    @staticmethod
    def _key(project_id, task_id):
        return f"{project_id}|{task_id}"

    def get(self, project_id, task_id):
        return self.allocations.get(self._key(project_id, task_id))

    def active_count(self, statuses=ACTIVE_STATUSES):
        """活跃分配数（默认 allocated+running），供全局并发上限判断。"""
        return sum(1 for a in self.allocations.values() if a.status in statuses)

    def is_active(self, project_id, task_id):
        a = self.get(project_id, task_id)
        return bool(a and a.status in ACTIVE_STATUSES)

    def is_human(self, project_id, task_id):
        a = self.get(project_id, task_id)
        return bool(a and a.status == "human")

    # ---------------- 状态转移 ----------------

    def allocate(self, project_id, task_id, worker, ts=None, comment=None,
                 task_content=None, priority="", risk="", approval_ref="",
                 rework_count=0):
        """记录分配（分配指纹：project/task/worker/started_at）。

        TASK-075 治理闸门（先于分配执行）：
        - task_content 提供时优先从 frontmatter 读取治理字段（priority/risk/
          approval-ref/rework-count），否则用显式参数；
        - P0 无有效 approval-ref → 拒绝分配（p0-blocked，blocked 语义）；
        - rework-count ≥ 3 → 拒绝分配（rework-rejected，转人工）。
        被拒绝时写入 dispatcher.governance-blocked 事件并返回 None。

        其余返回语义不变：重复分配（同 key 已有活跃分配）或已转人工
        （human，不再自动重试）→ 返回 None。
        """
        if task_content is not None:
            priority = _metadata_field(task_content, "priority") or priority
            risk = _metadata_field(task_content, "risk") or risk
            approval_ref = _metadata_field(task_content, "approval-ref") or approval_ref
            rc_raw = _metadata_field(task_content, "rework-count")
            if rc_raw is not None:
                rework_count = governance.rework_count_int(rc_raw)
        decision, reason = governance.governance_check(
            priority, risk, approval_ref, rework_count
        )
        if decision != "ok":
            self.append_event(
                "dispatcher.governance-blocked", project_id, task_id, worker,
                status="blocked", retry_count=governance.rework_count_int(rework_count),
                comment=f"{decision}: {reason}",
            )
            return None  # 治理闸门：不分配（blocked 语义 / 转人工）

        now = self.clock() if ts is None else float(ts)
        existing = self.get(project_id, task_id)
        if existing and existing.status in ACTIVE_STATUSES:
            return None  # 指纹防重复：同任务已有活跃分配
        if existing and existing.status == "human":
            return None  # 连续失败已转人工，不再自动重试
        if existing and existing.status in ("failed", "stale"):
            # 失败/超时回收后可重新分配：连续失败计数延续（failed），stale 清零
            retry_count = existing.retry_count if existing.status == "failed" else 0
            base_comment = f"重新分配（前次 {existing.status}）"
        else:
            retry_count = 0
            base_comment = ""
        alloc = Allocation(
            project_id=project_id,
            task_id=task_id,
            worker=worker,
            started_at=now,
            status="allocated",
            retry_count=retry_count,
            updated_at=now,
            comment=comment or base_comment,
        )
        self.allocations[alloc.key] = alloc
        self.append_event("dispatcher.allocated", project_id, task_id, worker,
                          alloc.status, alloc.retry_count, alloc.comment)
        return alloc

    def mark_running(self, project_id, task_id, ts=None, comment=None):
        """allocated → running（下行执行开始）。"""
        a = self.get(project_id, task_id)
        if not a:
            return None
        now = self.clock() if ts is None else float(ts)
        a.status = "running"
        a.updated_at = now
        if comment:
            a.comment = comment
        self.append_event("dispatcher.running", project_id, task_id, a.worker,
                          a.status, a.retry_count, a.comment)
        return a

    def mark_done(self, project_id, task_id, ts=None, comment=None):
        """running → done（成功；重试计数清零）。"""
        a = self.get(project_id, task_id)
        if not a:
            return None
        now = self.clock() if ts is None else float(ts)
        a.status = "done"
        a.retry_count = 0
        a.updated_at = now
        a.comment = comment or "执行成功"
        self.append_event("dispatcher.done", project_id, task_id, a.worker,
                          a.status, a.retry_count, a.comment)
        return a

    def mark_failed(self, project_id, task_id, ts=None, comment=None):
        """running → failed（可重试）或 human（连续失败达上限，不再自动重试）。"""
        a = self.get(project_id, task_id)
        if not a:
            return None
        now = self.clock() if ts is None else float(ts)
        a.retry_count += 1
        a.updated_at = now
        if a.retry_count >= self.max_retries:
            a.status = "human"
            a.comment = comment or f"连续失败 {a.retry_count} 次，转人工（不再自动重试）"
            ev = "dispatcher.human"
        else:
            a.status = "failed"
            a.comment = comment or f"失败（第 {a.retry_count}/{self.max_retries} 次），可重试"
            ev = "dispatcher.failed"
        self.append_event(ev, project_id, task_id, a.worker, a.status,
                          a.retry_count, a.comment)
        return a

    def check_timeouts(self, timeout, now=None):
        """超时回收：活跃分配超过 timeout 秒 → 标记 stale（可重新分配）。

        返回本次标记 stale 的 Allocation 列表。timeout <= 0 视为全部立即可超时
        （测试/模拟用；正常运行传正数秒）。
        """
        now = self.clock() if now is None else float(now)
        stale = []
        for a in list(self.allocations.values()):
            if a.status in ACTIVE_STATUSES and now - a.started_at > timeout:
                a.status = "stale"
                a.updated_at = now
                a.comment = f"超时回收（分配已 {now - a.started_at:.0f}s 未完成）"
                self.append_event("dispatcher.stale", a.project_id, a.task_id,
                                  a.worker, a.status, a.retry_count, a.comment)
                stale.append(a)
        return stale

    # ---------------- 状态重建 ----------------

    def rebuild_from_projects(self, entries, snapshots=None, now=None):
        """从项目 runtime/tasks/ 重建状态（kill 后重启恢复认知，无状态设计）。

        - in-progress 任务 → running 分配（worker="recovered"，起点取
          autoloop-coder.heartbeat mtime，无心跳取当前时间）；
        - 旧状态中已不在活跃集的 allocated/running/stale 记录 → 清除；
        - done/failed/human 终态保留（审计可见）。
        返回 self；调用方需要时 save() 持久化。
        """
        now = self.clock() if now is None else float(now)
        snapshots = snapshots or {}
        active_keys = set()
        for entry in entries:
            if is_agent(entry):
                continue
            snapshot = snapshots.get(entry.id)
            if snapshot is None:
                snapshot = agent_runtime.read_project_runtime(entry.path)
            snapshot = snapshot or {}
            started_at = now
            for hb in snapshot.get("heartbeats") or []:
                if hb.get("file") == "autoloop-coder.heartbeat" and hb.get("mtime"):
                    started_at = float(hb["mtime"])
                    break
            for task in snapshot.get("tasks") or []:
                name = task.get("name") or ""
                m = TASK_FILE_RE.match(name)
                if not m:
                    continue
                task_id = f"TASK-{int(m.group(1)):03d}"
                if _metadata_field(task.get("content"), "status") != "in-progress":
                    continue
                key = self._key(entry.id, task_id)
                active_keys.add(key)
                existing = self.allocations.get(key)
                if not existing or existing.status not in ACTIVE_STATUSES:
                    self.allocations[key] = Allocation(
                        project_id=entry.id,
                        task_id=task_id,
                        worker="recovered",
                        started_at=started_at,
                        status="running",
                        retry_count=0,
                        updated_at=now,
                        comment="rebuild: runtime/tasks 中 in-progress",
                    )
        for key, a in list(self.allocations.items()):
            if a.status in ("allocated", "running", "stale") and key not in active_keys:
                del self.allocations[key]
        self.append_event("dispatcher.rebuilt", status=len(active_keys),
                          comment="rebuild_from_projects: 状态自 runtime/tasks/ 重建")
        return self
