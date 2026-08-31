"""monitor.py — 调度事件 + 心跳 → aimonitor（kit/tools/dispatcher/ 观测层）。

TASK-074：调度状态机的可观测性，与 agent（kit/tools/agent/）同构：
- 心跳：`state_dir/dispatcher.heartbeat`（内容为 epoch 秒，mtime 同样可判活，
  与 autoloop-*.heartbeat 语义一致——监控端靠 mtime 判断调度器是否卡死）。
- 事件流：`state_dir/dispatcher-events.jsonl`（state.py 追加写入），按游标
  `state_dir/.push-cursor` 增量推送（与 agent 的 task-events.jsonl / .push-cursor
  同构）。
- payload：复用 agent_payload（project_id="dispatcher"，files.tasks = 当前分配
  快照，events = 调度事件增量，cursor = 本次覆盖最大 seq）；TASK-075 起额外
  携带 governance 派生字段（blocked/stale 告警，对齐 aimonitor
  derive_project_alerts 的可读形状；通知渠道由 TASK-072 负责）。
- 推送：复用 agent_http（Bearer token / connect-read 超时分离 / 可重试分类）。
- 未配置 server_url/token（--monitor-config 缺省）→ dry-run：只写心跳与本地
  事件，不推送、不推进游标。

零外部依赖（仅 stdlib + ../agent/ 的 agent_payload/agent_http/agent_config）。
"""
import json
import os
import re
import sys
import time

# 复用 agent 的 payload/HTTP 推送层（同目录层级：kit/tools/dispatcher/ → ../agent/）
AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "agent"
)
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)
import agent_http  # noqa: E402
import agent_payload  # noqa: E402

import governance  # noqa: E402
from state import (  # noqa: E402
    CURSOR_FILE,
    EVENTS_FILE,
    HEARTBEAT_FILE,
    SchedulerState,
)

# 与 policy/state 的 TASK_FILE_RE 保持一致：只认标准任务文件
TASK_FILE_RE = re.compile(r"^TASK-(\d{3})-[a-z0-9-]+\.md$")
_META_START_RE = re.compile(r"^metadata:?\s*$")
_META_FIELD_RE = re.compile(r"^([a-z0-9-]+):\s*(.*)$")
CANDIDATE_STATUSES = ("open", "in-progress")


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

DISPATCHER_PROJECT_ID = "dispatcher"


# ---------------- 心跳 ----------------

def write_heartbeat(state_dir, ts=None):
    """写调度器心跳文件（epoch 秒）；返回文件路径。"""
    os.makedirs(state_dir, exist_ok=True)
    ts = time.time() if ts is None else float(ts)
    path = os.path.join(state_dir, HEARTBEAT_FILE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{ts:.3f}\n")
    return path


def read_heartbeat(state_dir):
    """读心跳 mtime（epoch 秒）；文件缺失 → None。"""
    path = os.path.join(state_dir, HEARTBEAT_FILE)
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


# ---------------- 事件流（本地读取 + 游标） ----------------

def read_events(state_dir):
    """读 dispatcher-events.jsonl → [parsed dict]（跳过损坏行）。

    容错语义与 agent_runtime.read_task_events 一致：
    - 文件缺失 → None（无数据）；
    - 文件存在但为空/全损坏 → []（有数据但为空）。
    每行必须是含整数 seq 的 JSON 对象才被接受。永不抛异常。
    """
    path = os.path.join(state_dir, EVENTS_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError:
        return None
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("seq"), int) \
                and not isinstance(data.get("seq"), bool) and data.get("seq") >= 1:
            events.append(data)
    return events


def read_cursor(state_dir):
    """读 .push-cursor → int（已确认推送的最大 seq）；缺失/非法 → None。"""
    path = os.path.join(state_dir, CURSOR_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    seq = data.get("seq") if isinstance(data, dict) else None
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


def write_cursor(state_dir, seq):
    """原子写推送游标（tmp + os.replace）。seq 必须为非负整数。"""
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError(f"推送游标 seq 必须是非负整数（got {seq!r}）")
    os.makedirs(state_dir, exist_ok=True)
    target = os.path.join(state_dir, CURSOR_FILE)
    tmp = os.path.join(state_dir, CURSOR_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seq": seq, "updated": time.time()}, f, sort_keys=True)
    os.replace(tmp, target)


# ---------------- 治理派生 ----------------

def _task_governance(content):
    """解析单任务 frontmatter 的治理字段 → (priority, risk, approval_ref, rework_count)。"""
    return (
        _metadata_field(content, "priority") or "",
        _metadata_field(content, "risk") or "",
        _metadata_field(content, "approval-ref") or "",
        governance.rework_count_int(_metadata_field(content, "rework-count")),
    )


def derive_governance(allocations, snapshots=None):
    """派生治理状态：blocked 任务/项目计数 + stale 分配告警。

    对齐 aimonitor `derive_project_alerts` 的可读形状（v1 以派生字段上报，
    通知渠道由 TASK-072 负责，本任务不新增告警规则）。

    - blocked：open/in-progress 任务中治理判定非 ok（P0 无 approval-ref /
      rework-count ≥ 3），判定规则与 policy/state 完全一致（只读 frontmatter）；
    - blocked_ratio：blocked / (open + in-progress)，无候选任务时为 0.0；
    - stale：当前分配中 status=stale（超时回收未处理 → 卡死信号）。

    参数:
        allocations: iterable of state.Allocation（当前分配快照）。
        snapshots: 可选 dict {project_id: runtime 快照}；缺省（None/空）时
            只派生 stale，不统计 blocked（避免不必要的全项目扫描）。
    返回:
        dict（blocked_tasks / blocked_projects / blocked_ratio /
        stale_allocations / alerts）。
    """
    blocked_tasks = []
    blocked_projects = []
    total = 0
    for project_id, snapshot in (snapshots or {}).items():
        snapshot = snapshot or {}
        for task in snapshot.get("tasks") or []:
            name = task.get("name") or ""
            m = TASK_FILE_RE.match(name)
            if not m:
                continue
            content = task.get("content") or ""
            status = _metadata_field(content, "status")
            if status not in CANDIDATE_STATUSES:
                continue
            total += 1
            priority, risk, approval_ref, rework_count = _task_governance(content)
            decision, reason = governance.governance_check(
                priority, risk, approval_ref, rework_count
            )
            if decision != "ok":
                task_id = f"TASK-{int(m.group(1)):03d}"
                blocked_tasks.append({
                    "project": project_id,
                    "task": task_id,
                    "decision": decision,
                    "reason": reason,
                })
                if project_id not in blocked_projects:
                    blocked_projects.append(project_id)

    stale_allocations = []
    for a in allocations or []:
        if getattr(a, "status", "") == "stale":
            stale_allocations.append({
                "project": a.project_id,
                "task": a.task_id,
                "worker": a.worker,
                "since": a.started_at,
                "comment": a.comment,
            })

    alerts = []
    for bt in blocked_tasks:
        alerts.append({"type": "blocked", **bt})
    for sa in stale_allocations:
        alerts.append({"type": "stale", **sa})

    return {
        "blocked_tasks": blocked_tasks,
        "blocked_projects": blocked_projects,
        "blocked_ratio": (len(blocked_tasks) / total) if total else 0.0,
        "stale_allocations": stale_allocations,
        "alerts": alerts,
    }


# ---------------- payload 构造 ----------------

def build_payload(allocations, events, cursor=None, ts=None,
                  project_id=DISPATCHER_PROJECT_ID, governance=None):
    """构造调度遥测 payload（复用 agent_payload 形状）。

    - files.tasks：当前分配快照（name="project|task"，content=分配 JSON）；
    - events / cursor：调度事件增量（与 agent 的 task 事件流同构）；
    - governance：TASK-075 派生治理状态（derive_governance 返回；None 时不携带
      该键，保持向后兼容）。
    """
    tasks = []
    for a in sorted(allocations, key=lambda x: (x.project_id, x.task_id)):
        tasks.append({
            "name": f"{a.project_id}|{a.task_id}",
            "content": json.dumps(a.to_dict(), ensure_ascii=False, sort_keys=True),
        })
    snapshot = {
        "tasks": tasks,
        "focus": None,
        "heartbeats": None,
        "events": None,
        "verification_count": None,
        "review_count": None,
    }
    payload = agent_payload.build_payload(
        project_id, snapshot, ts=ts, task_events=events, cursor=cursor
    )
    if governance is not None:
        payload["governance"] = governance
    return payload


def incremental_events(events_all, cursor, batch_limit=None):
    """从事件全量中筛出待推送增量，返回 (events, cursor_out)。

    语义与 agent_loop._incremental_events 一致：
    - None（无事件流文件）→ (None, None)：payload 不携带事件字段；
    - cursor is None（从未推送）→ 全量，cursor_out = 批量最大 seq；
    - cursor 已设 → 只推 seq > cursor 的增量；游标大于文件最大 seq
      （文件被截断/重建）→ 全量重推（服务端按 (project, seq) 去重）。
    """
    if batch_limit is None:
        batch_limit = agent_payload.MAX_TASK_EVENTS
    if events_all is None:
        return None, None
    if cursor is None:
        batch = events_all
    else:
        batch = [e for e in events_all if e.get("seq", 0) > cursor]
        max_all = max((e.get("seq", 0) for e in events_all), default=0)
        if cursor > max_all:
            batch = events_all
    batch = batch[:batch_limit]
    if batch:
        batch_max = max(e.get("seq", 0) for e in batch)
        cursor_out = batch_max if cursor is None else max(cursor, batch_max)
    else:
        cursor_out = cursor
    return batch, cursor_out


# ---------------- 单轮推送 ----------------

def monitor_once(state_dir, state=None, cfg=None, clock=time.time,
                 push_fn=agent_http.push_payload,
                 events_fn=read_events, cursor_read_fn=read_cursor,
                 cursor_write_fn=write_cursor, snapshots=None):
    """执行一轮调度监控：写心跳 → 组装分配+事件增量 → 推 aimonitor。

    参数:
        state_dir: 调度状态目录（dispatcher-state.json / events / heartbeat / 游标）
        state:     SchedulerState（缺省从 state_dir 加载）
        cfg:       规范化 agent.json（server_url/token）；None → dry-run 不推送
        clock:     时间来源（单测注入）
        push_fn / events_fn / cursor_read_fn / cursor_write_fn: 可注入替身
        snapshots: 可选 dict {project_id: runtime 快照}；提供时派生 governance
            blocked 计数（缺省只派生 stale）
    返回:
        (pushed, skipped, failed)
    """
    write_heartbeat(state_dir, ts=clock())
    if state is None:
        state = SchedulerState.load(state_dir, clock=clock)
    allocations = sorted(state.allocations.values(),
                         key=lambda a: (a.project_id, a.task_id))
    governance = derive_governance(allocations, snapshots=snapshots)
    events_all = events_fn(state_dir)
    cursor = cursor_read_fn(state_dir)
    events, cursor_out = incremental_events(events_all, cursor)
    payload = build_payload(allocations, events, cursor=cursor_out, ts=clock(),
                            governance=governance)
    body = agent_payload.serialize_payload(payload)

    if cfg is None or not cfg.get("server_url") or not cfg.get("token"):
        # dry-run：只写心跳与本地事件，不推送、不推进游标
        return 0, 1, 0

    try:
        push_fn(cfg["server_url"], cfg["token"], body)
    except agent_http.PushError as e:
        print(f"monitor: 推送失败: {e}", file=sys.stderr)
        return 0, 0, 1

    if cursor_out is not None:
        try:
            cursor_write_fn(state_dir, cursor_out)
        except (OSError, ValueError) as e:
            print(f"monitor: 游标写入失败（下次重推，幂等可容忍）: {e}", file=sys.stderr)
    return 1, 0, 0
