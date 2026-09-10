#!/usr/bin/env python3
"""dispatcher.py — Phase 3 调度器 CLI 入口（骨架 + 下行执行链 + 调度状态机）。

TASK-069：list/scan（只读侧：项目清单 + 任务状态统计）。
TASK-073：allocate/run/dispatch/downlink（下行执行链：选任务策略 →
本地 subprocess 执行既有工具链 → 收集结果）。
TASK-074：status/monitor + run 状态机接线（分配指纹/超时回收/重试上限/
全局并发上限/状态重建；事件流 + 心跳 → aimonitor，与 agent 同构）。
TASK-075：治理挂钩——P0 无 approval-ref / rework-count ≥ 3 → 跳过并告警；
monitor 上报 blocked/stale 派生信息；allocate/run/dispatch 支持 --dry-run
（只报治理判定不执行）。
TASK-037：agent 传输适配器（PHASE3-V2-CROSSMACHINE-DESIGN §五-4）——
语义命令（task_start/autoloop_coder/autoloop_reviewer）→ LocalAdapter（本地
subprocess，与 v1 逐字节一致）/ AgentAdapter（aimonitor 指令队列：POST 入队
dedup_key 幂等、409 复用防双派、轮询至终态；token 经环境变量
AIOS_DOWNLINK_TOKEN，不入注册表/日志）；scan/allocate/run 的 agent 条目经
aimonitor /api/status 聚合快照（不再无脑跳过）；A2A 适配器为预留挂载点。

用法:
    python3 kit/tools/dispatcher/dispatcher.py list --config <projects.json>
    python3 kit/tools/dispatcher/dispatcher.py scan --config <projects.json>
    python3 kit/tools/dispatcher/dispatcher.py allocate --config <projects.json> --max-workers N
    python3 kit/tools/dispatcher/dispatcher.py run --config <projects.json> --max-workers N [--state-dir <dir>] [--task-timeout S]
    python3 kit/tools/dispatcher/dispatcher.py dispatch --once --config <projects.json> --max-workers N
    python3 kit/tools/dispatcher/dispatcher.py status --config <projects.json> [--state-dir <dir>] [--task-timeout S] [--rebuild]
    python3 kit/tools/dispatcher/dispatcher.py monitor --config <projects.json> [--state-dir <dir>] [--monitor-config <agent.json>]
    python3 kit/tools/dispatcher/dispatcher.py downlink --config <projects.json> --path <项目路径> --command <cmd> [--arg A]...

- list：项目清单 + transport 标注（local/agent）+ 可达性；
- scan：本地项目读 runtime/tasks/ 统计（六种计数 + 最近事件）；agent 传输
  条目经 aimonitor /api/status 聚合读计数（TASK-037）；未配置
  aimonitor.server_url 或 aimonitor 不可达时输出 skipped/unreachable +
  stderr 告警；整体 exit 0；
- allocate：policy 选任务策略 v1（注册表顺序 round-robin、项目内 TASK
  升序、每项目 1 候选、全局 --max-workers 上限），只打印候选不执行；
  治理拦截候选（P0 无 approval-ref / rework-count ≥ 3）打印 [governance] 行；
- run / dispatch --once：allocate（policy 读 runtime 快照选候选，agent 条目
  经 aimonitor 聚合快照）后对每个候选执行 task start / autoloop-coder --once
  ——本地条目 subprocess，agent 条目经 aimonitor 指令队列（适配器抽象，
  TASK-037），输出每条命令的 exit code 与输出；指定 --state-dir 时接线调度状态机——超时回收先行、
  活跃/人工任务不重复分配、全局并发上限（活跃 + 本轮新启动 ≤ --max-workers）、
  执行结果落盘 done/failed（连续失败 3 次 → human）；
  --dry-run：只报治理判定（[ok]/[governance]/[skip-state]）不执行任何命令、
  不修改调度状态；
  （注意：v1 的 dispatch 不做显式 scan 步骤/输出——scan 是独立子命令；
  in-review → autoloop-reviewer 分支为保留分支，v1 policy 只选
  open/in-progress，不会触发。）
- status：调度状态机查看——当前分配（project/task/worker/started_at/状态 +
  重试计数），并执行超时回收（超过 --task-timeout 的活跃分配标记 stale，
  可重新分配）；--rebuild 从项目 runtime/tasks/ 重建状态；
- monitor：单轮观测——写心跳 + 调度事件增量 → aimonitor（payload 与 agent
  同构，含 governance 派生 blocked/stale 信息；--monitor-config 缺省时
  dry-run 只写本地）；
- downlink：手动下行执行（校验 --path 属于注册表后在项目目录执行
  --command，用于路径越界防护的可见验证）；
- 注册表缺失/格式错误 → stderr 明确报错 + exit 1。
"""
import argparse
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_adapter as agent_adapter_lib  # noqa: E402
import downlink as downlink_lib  # noqa: E402
import monitor as monitor_lib  # noqa: E402
import policy as policy_lib  # noqa: E402
import probe as probe_lib  # noqa: E402
import registry as registry_lib  # noqa: E402
import state as state_lib  # noqa: E402

# agent_config 用于 monitor 的推送配置加载（agent.json 形状）；
# agent_runtime 用于 monitor 的本地项目快照读取（governance blocked 派生）
AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "telemetry")
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)
import agent_config as agent_config_lib  # noqa: E402
import agent_runtime as agent_runtime_lib  # noqa: E402

DEFAULT_CONFIG = os.path.expanduser("~/code/aimonitor/config/projects.json")
DEFAULT_TIMEOUT = 1800


def default_state_dir():
    """调度状态目录缺省值：<项目根>/runtime/logs/dispatcher（runtime/logs 已 gitignore）。

    dispatcher.py 位于 <root>/kit/tools/dispatcher/，向上 4 级到项目根。
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )))
    return os.path.join(root, "runtime", "logs", "dispatcher")


def _aimonitor_url(args):
    """注册表顶层 aimonitor.server_url（TASK-037）；缺失 → None（list/scan 兼容旧行为）。"""
    return registry_lib.load_aimonitor_config(
        getattr(args, "config", None) or DEFAULT_CONFIG).get("server_url")


def _agent_snapshots(args, entries):
    """agent 条目 → aimonitor 聚合快照（probe.snapshot_from_aimonitor，TASK-037）。

    aimonitor 未配置 → 空表（agent 条目被 policy 跳过，保持 v1 边界）；
    已配置但不可达/未登记 → stderr 告警 + 跳过该条目（不崩溃）。
    """
    url = _aimonitor_url(args)
    snaps = {}
    if not url:
        return snaps
    for entry in entries:
        if not registry_lib.is_agent(entry):
            continue
        snap = probe_lib.snapshot_from_aimonitor(url, entry)
        if snap is None:
            print(f"dispatcher: WARN agent 条目快照不可达（aimonitor 未登记/失联）: "
                  f"{entry.id}（{entry.path}）", file=sys.stderr)
            continue
        snaps[entry.id] = snap
    return snaps


def _evaluated(args, entries):
    """evaluate_candidates + agent 快照注入（cmd_allocate/dry_run/run 共用，TASK-037）。"""
    return policy_lib.evaluate_candidates(
        entries, max_workers=args.max_workers,
        snapshots=_agent_snapshots(args, entries))


def _adapter_for(entry, aimonitor_url):
    """按条目传输选适配器（TASK-037）：local → LocalAdapter（复用 downlink.run，
    与 v1 逐字节一致）；agent → AgentAdapter（aimonitor 指令队列）。

    token 从环境变量 AIOS_DOWNLINK_TOKEN 读（Rule of Two：不入注册表/日志/代码）；
    拾取余量可经 AIOS_DOWNLINK_ACK_MARGIN 覆盖（秒，默认 90；集成验证/烟幕用短值）。
    agent 配置缺失时 AgentAdapter 报 DownlinkError，由 caller 沿用
    cmd_run 的干净失败路径（✗ 下行错误 + 标 failed）。
    """
    if not registry_lib.is_agent(entry):
        return agent_adapter_lib.LocalAdapter()
    try:
        ack_margin = float(os.environ.get(agent_adapter_lib.ACK_MARGIN_ENV,
                                          agent_adapter_lib.ACK_MARGIN))
    except (TypeError, ValueError):
        ack_margin = agent_adapter_lib.ACK_MARGIN
    return agent_adapter_lib.AgentAdapter(
        aimonitor_url, os.environ.get("AIOS_DOWNLINK_TOKEN", ""),
        ack_margin=ack_margin)


def _load_or_die(args):
    """加载注册表；失败打印 stderr + exit 1。返回 entries 列表。"""
    path = args.config or DEFAULT_CONFIG
    try:
        return registry_lib.load_registry(path)
    except registry_lib.RegistryError as e:
        print(f"dispatcher: 注册表错误: {e}", file=sys.stderr)
        sys.exit(1)


def _reachable_label(entry):
    """可达性：本地条目按目录是否存在；agent 远端条目显示 '-'（不探测）。"""
    if registry_lib.is_local(entry):
        return "yes" if os.path.isdir(entry.path) else "no"
    return "-"


def cmd_list(args, entries):
    print(f"{'ID':<22} {'PATH':<42} {'TRANSPORT':<10} REACHABLE")
    for e in entries:
        print(f"{e.id:<22} {e.path:<42} {e.transport:<10} {_reachable_label(e)}")
    return 0


def cmd_scan(args, entries):
    aimonitor_url = _aimonitor_url(args)
    fetcher = None
    if aimonitor_url:
        fetcher = (lambda e: probe_lib.fetch_aimonitor_counts(aimonitor_url, e.id))
    results = probe_lib.scan_projects(entries, status_fetcher=fetcher)
    totals = {status: 0 for status in probe_lib.STATUSES}
    skipped = 0

    for res in results:
        entry = res["entry"]
        if res["skipped"]:
            skipped += 1
            if res.get("reason") == "aimonitor-unreachable":
                print(f"[unreachable] {entry.id}  {entry.path}  aimonitor 失联")
                print(f"dispatcher: WARN agent 条目 aimonitor 不可达/未登记，跳过: "
                      f"{entry.id} ({entry.path})", file=sys.stderr)
            else:
                print(f"[skipped] {entry.id}  {entry.path}  skipped(agent-transport)")
                print(
                    f"dispatcher: WARN agent 传输条目跳过（未配置 aimonitor.server_url）: "
                    f"{entry.id} ({entry.path}) [agent-transport]",
                    file=sys.stderr,
                )
            continue

        counts = res["counts"]
        for status in probe_lib.STATUSES:
            totals[status] += counts[status]
        print(
            f"{entry.id:<22} "
            f"open={counts['open']} in-progress={counts['in-progress']} "
            f"in-review={counts['in-review']} blocked={counts['blocked']} "
            f"done={counts['done']} cancelled={counts['cancelled']}"
        )
        latest = res["latest_event"]
        if latest:
            print(
                f"    latest: seq={latest.get('seq')} ev={latest.get('ev')} "
                f"task={latest.get('task')}"
            )

    print("-" * 60)
    print(
        f"scan 完成: 本地项目 {len(entries) - skipped} 个，"
        f"agent 传输跳过 {skipped} 个；"
        f"任务合计 open={totals['open']} in-progress={totals['in-progress']} "
        f"in-review={totals['in-review']} blocked={totals['blocked']} "
        f"done={totals['done']} cancelled={totals['cancelled']}"
    )
    return 0


def cmd_allocate(args, entries):
    """打印本轮候选；治理拦截候选（P0 无 approval-ref / rework ≥ 3）打印 [governance]。"""
    considered = _evaluated(args, entries)
    candidates = [c for c in considered if c.decision == "ok"]
    for c in considered:
        if c.decision != "ok":
            print(f"[governance] {c.entry.id} {c.task_id}: "
                  f"{c.decision} — {c.reason}")
    if not candidates:
        print("allocate: 无候选任务（无 open/in-progress 任务）")
        return 0
    print(f"{'PROJECT':<22} {'TASK':<16} {'STATUS':<12} {'PRIORITY':<10} UPDATED")
    for c in candidates:
        print(
            f"{c.entry.id:<22} {c.task_id:<16} {c.status:<12} "
            f"{c.priority or '-':<10} {c.updated or '-'}"
        )
    return 0


def _print_result(res):
    """打印单条下行命令结果（stdout/stderr 修剪到尾部，防巨量刷屏）。"""
    tail = 400
    print(f"  rc={res.exit_code}" + (" timed_out" if res.timed_out else ""))
    if res.stdout.strip():
        out = res.stdout.strip()
        if len(out) > tail:
            out = "…(截断)…\n" + out[-tail:]
        print(f"  stdout: {out}")
    if res.stderr.strip():
        err = res.stderr.strip()
        if len(err) > tail:
            err = "…(截断)…\n" + err[-tail:]
        print(f"  stderr: {err}", file=sys.stderr)


def _run_candidate(entry, candidate, timeout=DEFAULT_TIMEOUT, adapter=None):
    """对单个候选执行标准下行链（传输无关，TASK-037 适配器抽象），返回
    [(label, CommandResult), ...]。

    语义命令序列（agent_adapter.candidate_commands）：
    - open：先 task start（状态落盘），再 autoloop-coder --once；
    - in-progress：autoloop-coder --once（继续实现 + verify）；
    - in-review → autoloop-reviewer --once 是【保留分支】：v1 policy
      只选 open/in-progress，该分支仅留给未来扩展。
    adapter=None → LocalAdapter（与 v1 逐字节一致）；agent 条目由 cmd_run
    注入 AgentAdapter。只触发既有工具链，不直接写任务文件（硬约束）。
    """
    if adapter is None:
        adapter = agent_adapter_lib.LocalAdapter()
    results = []
    for name, args_, label in agent_adapter_lib.candidate_commands(candidate):
        results.append((label, adapter.execute(entry, name, args_, timeout=timeout)))
    return results


def _load_state(args, entries, task_timeout, rebuild=False):
    """加载调度状态；损坏时告警按空状态处理；--rebuild 从 runtime/tasks/ 重建。

    返回 (state, stale_list)。直接以 args.state_dir 缺省（None）调用时
    返回 (None, [])——保持旧的无状态调用路径（单测直接调 cmd_run 的兼容）。
    """
    state_dir = getattr(args, "state_dir", None)
    if not state_dir:
        return None, []
    try:
        state = state_lib.SchedulerState.load(state_dir)
    except state_lib.StateError as e:
        print(f"dispatcher: WARN 状态文件损坏（{e}），按空状态处理；可用 --rebuild 重建",
              file=sys.stderr)
        state = state_lib.SchedulerState(state_dir)
    if rebuild:
        state.rebuild_from_projects(entries)
        state.save()
    stale = state.check_timeouts(task_timeout)
    if stale:
        state.save()
        for a in stale:
            print(f"  ⚠ 超时回收 stale: {a.project_id} {a.task_id}"
                  f"（分配于 {a.started_at:.0f}，超过 {task_timeout}s）", file=sys.stderr)
    return state, stale


def cmd_status(args, entries):
    """调度状态机查看：当前分配 + 超时回收；--rebuild 从 runtime/tasks/ 重建。"""
    task_timeout = getattr(args, "task_timeout", DEFAULT_TIMEOUT)
    state, stale = _load_state(args, entries, task_timeout, rebuild=args.rebuild)
    print(f"{'PROJECT':<22} {'TASK':<16} {'WORKER':<20} {'STARTED_AT':<19} "
          f"{'STATUS':<10} {'RETRY':<3} COMMENT")
    for a in sorted(state.allocations.values(),
                    key=lambda x: (x.project_id, x.task_id)):
        started = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a.started_at))
                   if a.started_at else "-")
        print(f"{a.project_id:<22} {a.task_id:<16} {a.worker:<20} {started:<19} "
              f"{a.status:<10} {a.retry_count:<3} {a.comment}")
    if not state.allocations:
        print("（无分配记录）")
    print("-" * 60)
    print(f"status: 分配记录 {len(state.allocations)} 条，活跃 {state.active_count()} 条，"
          f"本次超时回收 {len(stale)} 条"
          f"（max-workers 全局并发上限由 run/dispatch 生效）")
    return 0


def cmd_monitor(args, entries):
    """调度观测：心跳 + 事件增量 → aimonitor（无 --monitor-config 则 dry-run）。"""
    task_timeout = getattr(args, "task_timeout", DEFAULT_TIMEOUT)
    state, stale = _load_state(args, entries, task_timeout, rebuild=args.rebuild)
    cfg = None
    if args.monitor_config:
        try:
            cfg = agent_config_lib.load_config(args.monitor_config)
        except agent_config_lib.AgentConfigError as e:
            print(f"dispatcher: monitor 配置错误: {e}", file=sys.stderr)
            return 1
    # 本地项目快照 → monitor 派生 governance（blocked 计数 + stale 告警）
    snapshots = {}
    for e in entries:
        if registry_lib.is_local(e) and os.path.isdir(e.path):
            snapshots[e.id] = agent_runtime_lib.read_project_runtime(e.path)
    pushed, skipped, failed = monitor_lib.monitor_once(
        args.state_dir, state=state, cfg=cfg, snapshots=snapshots,
    )
    if cfg is None:
        print(f"monitor: 未配置推送（--monitor-config 缺省），dry-run——"
              f"心跳已写、事件已本地记录（pushed=0 skipped=1）")
    else:
        print(f"monitor: 推送成功 {pushed}，跳过 {skipped}，失败 {failed}")
    return 0 if failed == 0 else 1


def cmd_dry_run(args, entries):
    """--dry-run：只报治理判定（policy 评估 + 状态机可见性），不执行任何命令、
    不修改调度状态。"""
    considered = _evaluated(args, entries)
    state = None
    state_dir = getattr(args, "state_dir", None)
    if state_dir:
        try:
            state = state_lib.SchedulerState.load(state_dir)
        except state_lib.StateError as e:
            print(f"dispatcher: WARN 状态文件损坏（{e}），按空状态处理", file=sys.stderr)
            state = state_lib.SchedulerState(state_dir)
    for c in considered:
        if c.decision != "ok":
            print(f"[governance] {c.entry.id} {c.task_id}: "
                  f"{c.decision} — {c.reason}")
            continue
        if state is not None and (state.is_active(c.entry.id, c.task_id)
                                  or state.is_human(c.entry.id, c.task_id)):
            print(f"[skip-state] {c.entry.id} {c.task_id}："
                  f"活跃/人工任务不重复分配（{c.status}）")
        else:
            print(f"[ok] {c.entry.id} {c.task_id} ({c.status}) "
                  f"priority={c.priority or '-'} risk={c.risk or '-'} "
                  f"rework-count={c.rework_count}")
    if not considered:
        print("dry-run: 无候选任务（无 open/in-progress 任务）")
    print("dry-run: 仅输出判定，未执行任何命令、未修改调度状态")
    return 0


def cmd_run(args, entries):
    if getattr(args, "dry_run", False):
        return cmd_dry_run(args, entries)
    # 治理挂钩（TASK-075 FIND-001 修复）：用 evaluate_candidates 保留被拦截
    # 候选——[governance] 行与 dispatcher.governance-blocked 事件是「跳过并
    # 记录告警」的操作路径（与 cmd_allocate / README 一致），不静默丢弃。
    considered = _evaluated(args, entries)
    candidates = [c for c in considered if c.decision == "ok"]
    blocked = [c for c in considered if c.decision != "ok"]
    state_dir = getattr(args, "state_dir", None)
    state = None
    if state_dir:
        state, stale = _load_state(args, entries,
                                   getattr(args, "task_timeout", DEFAULT_TIMEOUT),
                                   rebuild=getattr(args, "rebuild", False))
        # 治理拦截候选：写 governance-blocked 事件（allocate 治理闸门返回
        # None，不产生分配；只留事件供 monitor/审计）——不占 max-workers 额度
        for c in blocked:
            state.allocate(c.entry.id, c.task_id, worker="dispatcher",
                           priority=c.priority, risk=c.risk,
                           approval_ref=c.approval_ref, rework_count=c.rework_count)
        remaining = args.max_workers - state.active_count()
        if remaining <= 0:
            candidates = []
        else:
            # 全局并发上限：活跃（历史/其它项目）+ 本轮新启动 ≤ max_workers；
            # 活跃/人工任务不重复分配（分配指纹防双跑）
            candidates = [c for c in candidates
                          if not state.is_active(c.entry.id, c.task_id)
                          and not state.is_human(c.entry.id, c.task_id)][:remaining]
    for c in blocked:
        print(f"[governance] {c.entry.id} {c.task_id}: "
              f"{c.decision} — {c.reason}")
    if not candidates:
        if blocked:
            print(f"run: 无放行候选（治理拦截 {len(blocked)} 个并告警；"
                  f"无 open/in-progress 可执行任务）")
        else:
            print("run: 无候选任务（无 open/in-progress 任务）")
        return 0
    failed = 0
    for c in candidates:
        print(f"▶ {c.entry.id} {c.task_id} ({c.status})")
        if state is not None:
            state.allocate(c.entry.id, c.task_id, worker="dispatcher",
                           priority=c.priority, risk=c.risk,
                           approval_ref=c.approval_ref, rework_count=c.rework_count)
            state.mark_running(c.entry.id, c.task_id)
            state.save()
        cand_failed = False
        fail_reason = ""
        try:
            results = _run_candidate(
                c.entry, c, timeout=getattr(args, "timeout", DEFAULT_TIMEOUT),
                adapter=_adapter_for(c.entry, _aimonitor_url(args)))
        except downlink_lib.DownlinkError as e:
            # 干净报错（目录被删/命令二进制缺失等），记失败并继续，不抛 traceback
            print(f"  ✗ 下行错误（{c.task_id}）: {e}", file=sys.stderr)
            cand_failed = True
            fail_reason = f"下行错误: {e}"
        else:
            for label, res in results:
                print(f"  $ {res.command}   # {label}")
                _print_result(res)
                if res.exit_code != 0:
                    cand_failed = True
                    fail_reason = f"{label} 失败（rc={res.exit_code}）"
        if cand_failed:
            failed += 1
        if state is not None:
            if cand_failed:
                state.mark_failed(c.entry.id, c.task_id,
                                  comment=fail_reason or f"下行执行失败（{c.task_id}）")
            else:
                state.mark_done(c.entry.id, c.task_id,
                                comment=f"下行执行成功（{c.task_id}）")
            state.save()
    print("-" * 60)
    print(f"run 完成: 候选 {len(candidates)} 个，失败命令 {failed} 条")
    return 1 if failed else 0


def cmd_downlink(args, entries):
    """手动下行执行：先校验 --path 属于注册表，再在项目目录执行命令。"""
    try:
        entry = downlink_lib.find_entry(entries, args.path)
    except downlink_lib.DownlinkError as e:
        print(f"dispatcher: downlink 拒绝: {e}", file=sys.stderr)
        return 1
    try:
        res = downlink_lib.run(entry, args.exec_command, args.arg, timeout=args.timeout)
    except downlink_lib.DownlinkError as e:
        print(f"dispatcher: downlink 错误: {e}", file=sys.stderr)
        return 1
    print(f"{entry.id}  {res.command}")
    _print_result(res)
    return 0 if res.exit_code == 0 else 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="dispatcher",
        description="Phase 3 调度器（list/scan/allocate/run/dispatch/status/monitor/downlink）",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None,
                        help="projects.json 路径（默认 ~/code/aimonitor/config/projects.json）")
    common.add_argument("--max-workers", type=int, default=1,
                        help="全局并发上限（默认 1，v1 保守）")
    common.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="单条下行命令超时秒数（默认 1800）")
    common.add_argument("--state-dir", default=default_state_dir(),
                        help="调度状态目录（默认 <项目根>/runtime/logs/dispatcher）")
    common.add_argument("--task-timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="任务超时秒数（默认 1800）：活跃分配超时标记 stale 可回收")
    common.add_argument("--rebuild", action="store_true",
                        help="状态重建：从各项目 runtime/tasks/ 重建调度状态")
    common.add_argument("--dry-run", action="store_true",
                        help="（allocate/run/dispatch）只报治理判定，不执行任何命令")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", parents=[common], help="项目清单 + transport 标注 + 可达性")
    sub.add_parser("scan", parents=[common], help="本地项目任务状态统计 + agent 条目跳过告警")
    sub.add_parser("allocate", parents=[common],
                   help="policy 选任务策略 v1：打印候选不执行；--dry-run 等价（本身不执行）")
    sub.add_parser("run", parents=[common],
                   help="allocate 后对候选执行下行链（task/autoloop-*）；--dry-run 只报判定")
    dispatch_p = sub.add_parser(
        "dispatch", parents=[common],
        help="单轮 dispatch：scan→allocate→run→收结果（--once 兼容参数；--dry-run 只报判定）",
    )
    dispatch_p.add_argument("--once", action="store_true",
                            help="单轮执行（v1 默认即单轮，参数仅作兼容）")
    sub.add_parser("status", parents=[common],
                   help="调度状态机：当前分配（project/task/worker/started_at/状态）+ 超时回收")
    monitor_p = sub.add_parser(
        "monitor", parents=[common],
        help="调度观测：心跳 + 事件增量 → aimonitor（无 --monitor-config 则 dry-run）",
    )
    monitor_p.add_argument("--monitor-config", default=None,
                           help="agent.json 形状的推送配置（server_url/token；缺省 dry-run）")
    downlink_p = sub.add_parser(
        "downlink", parents=[common],
        help="手动下行执行：校验路径属于注册表后在项目目录执行命令",
    )
    downlink_p.add_argument("--path", required=True, help="目标项目路径（必须在注册表中）")
    downlink_p.add_argument("--command", dest="exec_command", required=True,
                            help="可执行命令字（如 bash / python3）")
    downlink_p.add_argument("--arg", action="append", default=[],
                            help="命令参数（可重复），如 --arg 'kit/cli/task' --arg start")
    args = parser.parse_args(argv)

    entries = _load_or_die(args)
    if args.command == "list":
        return cmd_list(args, entries)
    if args.command == "scan":
        return cmd_scan(args, entries)
    if args.command == "allocate":
        return cmd_allocate(args, entries)
    if args.command in ("run", "dispatch"):
        return cmd_run(args, entries)
    if args.command == "status":
        return cmd_status(args, entries)
    if args.command == "monitor":
        return cmd_monitor(args, entries)
    if args.command == "downlink":
        return cmd_downlink(args, entries)
    parser.error(f"未知子命令: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
