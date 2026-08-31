"""governance.py — 跨项目调度治理判定（kit/tools/dispatcher/ 治理层）。

TASK-075：Phase 3 治理挂钩（P0 阻塞、返工上限、告警）。
- P0（priority/risk 任一为 P0）且无有效 approval-ref → p0-blocked（blocked 语义）；
- rework-count ≥ 3 → rework-rejected（机械拒绝转人工，不自动实现）。

对齐 `kit/aios/governance/task-policy.md`：
- P0 任务必须在 TASK 的 `approval-ref` 中引用人工批准记录；
- 自动返工最多 2 轮，`rework-count ≥ 3` 时拒绝自动实现、升级人工。

治理判定只读任务 frontmatter，不修改任务文件（中央不直写远端 FS）。
机械执行：判定以字段值精确匹配，无自由裁量。
零外部依赖（仅 stdlib）。
"""

REWORK_LIMIT = 3

# 无效 approval-ref 值（TASK 模板缺省 / 常见占位，视为未批准）
_EMPTY_APPROVALS = {"", "none", "n/a", "na", "-", "null", "0"}


def approval_ok(value):
    """approval-ref 是否为有效人工批准引用（非空且不是占位缺省值）。"""
    v = (value or "").strip().lower()
    return v not in _EMPTY_APPROVALS


def is_p0(priority="", risk=""):
    """是否 P0 级任务（priority 或 risk 任一为 P0，大小写不敏感）。"""
    return (priority or "").strip().upper() == "P0" \
        or (risk or "").strip().upper() == "P0"


def rework_count_int(value):
    """rework-count 解析为 int；缺失/非法 → 0（防御，模板缺省 0）。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def governance_check(priority="", risk="", approval_ref="", rework_count=0):
    """返回 (decision, reason)。

    decision ∈ {"ok", "p0-blocked", "rework-rejected"}。
    P0 优先判定（即使同时 rework 超限，P0 阻塞更早暴露——都是人工闸门，
    但 P0 无批准是根本性阻塞，先报告）。
    """
    if is_p0(priority, risk) and not approval_ok(approval_ref):
        return (
            "p0-blocked",
            f"P0 任务无 approval-ref（priority={priority or '?'}, risk={risk or '?'}），"
            "跳过（blocked 语义，等待人工批准）",
        )
    rc = rework_count_int(rework_count)
    if rc >= REWORK_LIMIT:
        return (
            "rework-rejected",
            f"rework-count={rc} ≥ {REWORK_LIMIT}，拒绝自动实现（转人工）",
        )
    return ("ok", "")
