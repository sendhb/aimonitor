"""policy.py — 选任务策略 v1（kit/tools/dispatcher/ 策略层）。

TASK-073：Phase 3 调度器下行执行链的选任务策略。
TASK-075：治理挂钩——选择前做治理判定（P0 无 approval-ref / rework-count ≥ 3
→ 跳过并暴露 reason），跨项目下治理闸门仍生效。

职责：
- 输入注册表条目（registry.RegistryEntry）与可选的运行时快照，输出本轮候选：
  Candidate(entry, task_id, status, priority, risk, updated, approval_ref,
  rework_count, decision, reason)。
- 选择规则 v1（与设计稿 §三「调度策略 v1」、autoloop 一致）：
  1. 只考虑本地条目（transport != agent），agent 传输条目跳过；
  2. 按注册表顺序 round-robin：逐项目轮询，每个项目最多 1 个候选
     （每项目 1 个执行 slot，复用 autoloop 进程锁天然防重）；
  3. 项目内只选 open/in-progress 任务，按 TASK 编号升序取最先；
  4. 全局并发上限 --max-workers N：累计「放行候选」达到 N 后停止；
  5. 治理闸门（TASK-075）：P0 无 approval-ref → p0-blocked；rework-count
     ≥ 3 → rework-rejected。被拦截候选不进入 select_candidates 结果，
     但 evaluate_candidates 会暴露其 decision/reason（--dry-run / 告警用）。
- 只读：不写任何文件，不执行任何命令。

零外部依赖（仅 stdlib + 复用 ../telemetry/agent_runtime.py 只读层 + governance 判定）。
"""
import os
import re
import sys
from dataclasses import dataclass, replace

# 复用 agent_runtime 的只读层（同目录层级：kit/tools/dispatcher/ → ../telemetry/）
AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "telemetry"
)
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)
import agent_runtime  # noqa: E402

import governance  # noqa: E402
from registry import RegistryEntry, is_agent  # noqa: E402

CANDIDATE_STATUSES = ("open", "in-progress")

# 与 kit/cli/task 的 TASK_RE / probe 的 TASK_FILE_RE 保持一致：只认标准任务文件
TASK_FILE_RE = re.compile(r"^TASK-(\d{3})-[a-z0-9-]+\.md$")
_META_START_RE = re.compile(r"^metadata:?\s*$")
_META_FIELD_RE = re.compile(r"^([a-z0-9-]+):\s*(.*)$")


@dataclass(frozen=True)
class Candidate:
    """单个任务候选（project/task_id/status/priority/risk/updated + 治理判定）。

    decision ∈ {"ok", "p0-blocked", "rework-rejected"}；ok 才可被
    select_candidates 返回（放行），非 ok 为治理拦截（blocked 语义）。
    """

    entry: RegistryEntry
    task_id: str
    status: str
    priority: str
    risk: str = ""
    updated: str = ""
    approval_ref: str = ""
    rework_count: int = 0
    decision: str = "ok"
    reason: str = ""


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


def _governed(candidate):
    """对候选执行治理判定，返回带 decision/reason 的新 Candidate（不修改入参）。"""
    decision, reason = governance.governance_check(
        candidate.priority, candidate.risk,
        candidate.approval_ref, candidate.rework_count,
    )
    return replace(candidate, decision=decision, reason=reason)


def _first_candidate(entry, snapshot):
    """从单个项目的运行时快照取第一个候选（open/in-progress、TASK 升序），
    并附加治理判定字段（priority/risk/approval-ref/rework-count/decision/reason）。

    快照形状与 agent_runtime.read_project_runtime 一致：tasks 为
    [{"name": "TASK-001-x.md", "content": "..."}, ...] 或 None。
    """
    tasks = (snapshot or {}).get("tasks") or []
    candidates = []
    for task in tasks:
        name = task.get("name") or ""
        m = TASK_FILE_RE.match(name)
        if not m:
            continue
        task_id = f"TASK-{int(m.group(1)):03d}"
        content = task.get("content") or ""
        status = _metadata_field(content, "status")
        if status not in CANDIDATE_STATUSES:
            continue
        candidates.append(_governed(Candidate(
            entry=entry,
            task_id=task_id,
            status=status,
            priority=_metadata_field(content, "priority") or "",
            risk=_metadata_field(content, "risk") or "",
            updated=_metadata_field(content, "updated") or "",
            approval_ref=_metadata_field(content, "approval-ref") or "",
            rework_count=governance.rework_count_int(
                _metadata_field(content, "rework-count")
            ),
        )))
    # 项目内按 TASK 编号升序（与 autoloop pick_task 一致）
    candidates.sort(key=lambda c: int(c.task_id.split("-")[1]))
    return candidates[0] if candidates else None


def evaluate_candidates(entries, max_workers=1, snapshots=None):
    """逐项目评估候选（含治理判定），返回本轮「考虑过」的候选列表。

    与 select_candidates 相同的 round-robin 选择顺序（每项目最多 1 个候选、
    累计「放行候选」不超过 max_workers），但保留被治理拦截的候选
    （decision != "ok"）供 --dry-run / 告警使用。

    参数:
        entries: registry.RegistryEntry 列表（保持注册表顺序）。
        max_workers: 全局并发上限；<=0 视为不选。
        snapshots: 可选 dict {entry.id: runtime 快照}；缺省时本地条目按需
            agent_runtime.read_project_runtime(entry.path) 读取。agent 条目
            （TASK-037）候选须经此注入 aimonitor 聚合快照（probe.snapshot_from_aimonitor）；
            未注入快照的 agent 条目跳过（caller 负责告警），与 v1 行为兼容。
    返回:
        Candidate 列表（ok 与治理拦截都包含；顺序 = 注册表顺序）。
    """
    if max_workers is None or max_workers <= 0:
        return []
    considered = []
    ok_count = 0
    snapshots = snapshots or {}
    for entry in entries:
        if ok_count >= max_workers:
            break
        if is_agent(entry) and entry.id not in snapshots:
            # agent 条目无注入快照（aimonitor 未配置/不可达）→ 跳过，保持 v1 边界
            continue
        snapshot = snapshots.get(entry.id)
        if snapshot is None:
            snapshot = agent_runtime.read_project_runtime(entry.path)
        candidate = _first_candidate(entry, snapshot)
        if candidate is not None:
            considered.append(candidate)
            if candidate.decision == "ok":
                ok_count += 1
    return considered


def select_candidates(entries, max_workers=1, snapshots=None):
    """按注册表顺序选出本轮可执行候选（治理放行），累计不超过 max_workers。

    与 TASK-073 语义兼容：只返回 decision == "ok" 的候选（P0 无
    approval-ref、rework-count ≥ 3 的候选被治理闸门拦截，不进入执行链）。
    被拦截详情见 evaluate_candidates（--dry-run / monitor 告警）。
    """
    if max_workers is None or max_workers <= 0:
        return []
    selected = []
    for c in evaluate_candidates(entries, max_workers=max_workers, snapshots=snapshots):
        if c.decision != "ok":
            continue
        selected.append(c)
        if len(selected) >= max_workers:
            break
    return selected
