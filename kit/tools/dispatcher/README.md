# tools/dispatcher — Phase 3 调度器

> 中央调度器：跨项目读状态（只读）→ 按策略选任务 → 触发目标项目本地
> 执行链（下行）。设计依据 `docs/PHASE3-SCHEDULER-DESIGN.md` §三/§五
> （TASK-068）。

## 组件

| 模块 | 职责 | 任务 |
|------|------|------|
| `registry.py` | 读 `aimonitor/config/projects.json` 注册表，暴露 transport（缺失默认 local）；`is_local(entry)`/`is_agent(entry)`；顶层 `aimonitor.server_url`（agent 通道地址，TASK-037） | TASK-069/037 |
| `probe.py` | 只读探测项目状态（六种计数 + 最近事件）；本地条目读 `runtime/tasks/`（复用 `../agent/agent_runtime.py`）；agent 条目经 aimonitor `/api/status` 聚合（TASK-037，不再无脑 skip；未配 aimonitor 或失联时仍 skipped + 告警） | TASK-069/037 |
| `agent_adapter.py` | 下行传输适配器抽象：语义命令（task_start/autoloop_coder/autoloop_reviewer）→ LocalAdapter（本地 subprocess，与 v1 逐字节一致）/ AgentAdapter（aimonitor 指令队列：dedup_key 幂等入队、409 复用防双派、轮询至终态、等待超时 timed_out）；A2A 适配器为预留挂载点 | TASK-037 |
| `policy.py` | 选任务策略 v1：注册表顺序 round-robin、项目内 TASK 升序、每项目 1 候选、全局 `--max-workers` 上限；治理判定（P0 无 approval-ref / rework ≥ 3 → 拦截） | TASK-073/075 |
| `governance.py` | 治理判定：P0 无 approval-ref → p0-blocked；rework-count ≥ 3 → rework-rejected（机械执行，单源） | TASK-075 |
| `downlink.py` | 本地 subprocess 下行适配器：校验路径属于注册表 → 在项目目录执行命令 → 收集 exit code/输出 | TASK-073 |
| `state.py` | 调度状态机：分配指纹（project/task/worker/started_at）、超时回收（stale → 可重分配）、连续失败 3 次转人工、全局并发上限、从 `runtime/tasks/` 重建状态；分配前治理闸门 | TASK-074/075 |
| `monitor.py` | 调度事件 + 心跳 → aimonitor（与 agent 同构：`dispatcher.heartbeat` + `dispatcher-events.jsonl` 增量推送）；payload 含 governance 派生（blocked/stale 告警） | TASK-074/075 |
| `dispatcher.py` | CLI 入口：`list` / `scan` / `allocate` / `run` / `dispatch --once` / `status` / `monitor` / `downlink`；`--dry-run` 只报治理判定不执行 | TASK-069/073/074/075 |

边界（传输处理边界，TASK-037 更新）：
- **同机本地条目**（transport 为空/默认）：probe 直接读 runtime/tasks/，downlink 本地执行（LocalAdapter）。
- **远端 agent 传输条目**（`"transport": "agent"`，如 `D:/share/*`）：下行走 aimonitor 指令队列（AgentAdapter），
  probe/候选经 aimonitor `/api/status` 聚合快照；注册表顶层需 `"aimonitor": {"server_url": "http://<hub>:<port>"}`，
  token 从环境变量 `AIOS_DOWNLINK_TOKEN` 读取（不入注册表/日志）；未配 aimonitor 或失联 → skipped/unreachable + 告警（保持可观测）。
- **中央不直写远端 FS**（v1 铁律不变）：agent 条目的执行仍由目标机 agent 拾取指令后本地执行（含双重白名单闸）。

## 使用（CLI）

```bash
CFG=/home/hb/code/aimonitor/config/projects.json

# 项目清单：id / path / transport / 可达性
python3 kit/tools/dispatcher/dispatcher.py list --config $CFG

# 本地项目读 runtime/tasks/ 统计；agent 条目经 aimonitor 聚合出计数（未配 aimonitor → skipped 告警）
python3 kit/tools/dispatcher/dispatcher.py scan --config $CFG

# agent 通道 token（agent 条目执行需要；不入文件）
export AIOS_DOWNLINK_TOKEN=<dispatcher-token>

# 选任务策略 v1：打印候选（不执行）；--max-workers 全局并发上限
python3 kit/tools/dispatcher/dispatcher.py allocate --config $CFG --max-workers 1

# 下行执行链：对候选在项目目录内执行 task start / autoloop-coder / autoloop-reviewer
python3 kit/tools/dispatcher/dispatcher.py run --config $CFG --max-workers 1

# 单轮执行链（allocate 读快照选候选 → run 执行 → 收结果）——v1 推荐入口
# 注意：v1 的 dispatch 不输出独立 scan 汇总（scan 是单独子命令，见上）
python3 kit/tools/dispatcher/dispatcher.py dispatch --once --config $CFG --max-workers 1

# 治理 dry-run：只报判定（[ok] 放行 / [governance] P0 阻塞或返工超限 /
# [skip-state] 活跃/人工），不执行任何命令、不修改调度状态
python3 kit/tools/dispatcher/dispatcher.py dispatch --once --dry-run --config $CFG --max-workers 1

# 调度状态机：当前分配（project/task/worker/started_at/状态）+ 超时回收
#   --task-timeout 1800（默认）：活跃分配超过该秒数 → 标记 stale（可重新分配）
#   --rebuild：从各项目 runtime/tasks/ 重建状态（kill 后重启恢复认知）
python3 kit/tools/dispatcher/dispatcher.py status --config $CFG --state-dir <dir> --task-timeout 1800
python3 kit/tools/dispatcher/dispatcher.py status --config $CFG --state-dir <dir> --rebuild

# 调度观测：心跳 + 事件增量 → aimonitor（与 agent 同构；无 --monitor-config 则 dry-run）
python3 kit/tools/dispatcher/dispatcher.py monitor --config $CFG \
  --state-dir <dir> --monitor-config ~/code/aimonitor/config/agent.json

# 手动下行执行（路径校验防越界的可见验证；伪造 path 会报错退出不执行）
python3 kit/tools/dispatcher/dispatcher.py downlink --config $CFG \
  --path /home/hb/code/aimonitor --command bash --arg kit/cli/check
```

- `--state-dir` 默认 `<项目根>/runtime/logs/dispatcher`（state 文件/事件流/心跳/游标）。
- `run`/`dispatch` 指定 `--state-dir` 时接线调度状态机：
  - 先做超时回收（`--task-timeout` 内未完成的活跃分配 → stale，可重新分配）；
  - 活跃/人工任务不重复分配（分配指纹防双跑）；全局并发 = 历史活跃 + 本轮新启动 ≤ `--max-workers`；
  - 结果落盘 done/failed，连续失败 3 次 → human（不再自动重试，`status` 可见）。

- `--config` 默认 `~/code/aimonitor/config/projects.json`。
- `list` 输出 10 条注册，本地条目标 `local`，`D:/share/*` 标 `agent`。
- `scan` 本地项目读 runtime/tasks/（六种计数 + 最近事件）；agent 条目经 aimonitor `/api/status` 聚合出计数（TASK-037）；
  未配 aimonitor.server_url → `skipped(agent-transport)`、失联 → `[unreachable]`，均 stderr 告警；整体 exit 0。
- `allocate`：policy v1 只选 open/in-progress、项目内 TASK 编号升序、每项目最多 1 个候选、累计不超过 `--max-workers`。
- `run` / `dispatch --once`：policy 读 runtime 快照选候选（本地条目读 `runtime/tasks/`，agent 条目经 aimonitor 聚合快照），
  对每个候选执行语义命令链（agent_adapter.candidate_commands）——
  - open → `task_start`（`task start <id>`）+ `autoloop_coder`（`autoloop-coder --once`）
  - in-progress → `autoloop_coder`
  - in-review → `autoloop_reviewer`（**保留分支**：v1 policy 只选
    open/in-progress，allocate/run/dispatch 当前不会触发，仅留给未来 policy 扩展）
  本地条目 → LocalAdapter（subprocess，行为同 v1）；agent 条目 → AgentAdapter
  （aimonitor 指令队列：dedup_key 幂等入队，409 复用在途指令防双派，轮询至终态，
  等待超时 timed_out → state.py stale 回收语义与本地一致）。
  只触发既有工具链，不直接写任务文件（中央不直写远端 FS）。
- **治理挂钩（TASK-075）**：跨项目继承单项目分级治理——
  - P0 任务（priority/risk 任一 P0）无有效 `approval-ref` → 不分配（blocked 语义），
    自动跳过并告警（`[governance] p0-blocked` / `dispatcher.governance-blocked` 事件）；
  - `rework-count ≥ 3` → 拒绝自动实现、转人工（`rework-rejected`）；
  - 判定以任务 frontmatter 为准，机械执行；只读 frontmatter，不修改任务文件。
- `--dry-run`（allocate/run/dispatch）：只输出治理判定（`[ok]` / `[governance]` /
  `[skip-state]`），不执行任何命令、不修改调度状态。
- `downlink`：先校验 `--path` 属于注册表（规范化路径精确匹配，`../` 伪造无效；agent 条目拒绝），再在项目目录执行命令。
- 注册表路径不存在 / 格式错误 → stderr 明确报错 + exit 1。

## 安全边界（TASK-073 / TASK-037）

- downlink 只调 `task`/`autoloop-*` CLI，不直接写任何任务文件。
- 执行前校验项目路径：非注册表路径 / 目录不存在 → DownlinkError，报错退出不执行；
  手动 `downlink` 子命令对 agent 传输条目仍拒绝（本地执行越权），agent 条目请用 `run`/`dispatch`（走指令队列）。
- 语义命令白名单（TASK-037）：dispatcher 侧只发 task_start / autoloop_coder / autoloop_reviewer
  （与 agent 侧白名单各自独立枚举，防一处被改两处失守）；白名单外命令在任何适配器上直接拒绝。
- dispatcher token 只经 Authorization 头传递（环境变量 `AIOS_DOWNLINK_TOKEN`），不入注册表/日志/代码（Rule of Two）。
- 失败以 exit code + stdout/stderr 回报，不静默吞错。

## 状态机（TASK-074）

- 分配记录：`state_dir/dispatcher-state.json`（project/task/worker/started_at/status/retry_count）。
- 事件流：`state_dir/dispatcher-events.jsonl`（seq 单调递增：allocated/running/done/failed/stale/human/rebuilt/governance-blocked）。
- 治理闸门（TASK-075）：分配前判定——P0 无 approval-ref / rework ≥ 3 → 不分配，
  写 `dispatcher.governance-blocked` 事件（blocked 语义 / 转人工）。
- 超时回收：`status`/`run`/`monitor` 按 `--task-timeout`（默认 1800s）把超时活跃分配标记 `stale` → 可重新分配。
- 重试上限：同一任务连续失败 3 次 → `human`（不再自动重试）。
- 全局并发：`--max-workers N` = 历史活跃（allocated/running）+ 本轮新启动 ≤ N（跨轮持久生效）。
- 状态重建：`--rebuild` 从各项目 `runtime/tasks/` 重建——in-progress → running（worker=recovered，起点取心跳 mtime），kill 后重启恢复认知。

## 治理告警（TASK-075）

- `monitor` payload 额外携带 `governance` 派生字段（对齐 aimonitor `derive_project_alerts` 可读形状）：
  - `blocked_tasks` / `blocked_projects` / `blocked_ratio`：P0 无 approval-ref 或
    rework ≥ 3 的候选任务计数（只读 frontmatter，规则与 policy/state 一致）；
  - `stale_allocations`：当前分配中 `stale`（超时回收未处理 → 卡死信号）；
  - `alerts`：派生告警条目（type=blocked / stale）。
- 通知渠道本身由 TASK-072（aimonitor 告警闭环）负责；本任务只产生告警数据。

## 状态（TASK-076 已完成）

- TASK-076：集成验证（2 个真实项目跑通闭环）+ 文档完善 ✅

### 集成验证记录（TASK-076，2026-08-28）

在 2 个真实本地项目（westhill、x1design）各跑通一轮 `dispatch --once` 闭环，
调度状态机事件轨迹完整（allocated → running → done）：

| 轮次 | 项目 | 任务 | 事件轨迹（`dispatcher-events.jsonl` seq） | 结果 |
|------|------|------|------------------------------------------|------|
| 1 | westhill | TASK-020（临时演示任务） | allocated(3) → running(4) → done(5) | ✅ 任务 open → in-progress → done；VERIFY 由 `task verify` 真实生成 |
| 2 | x1design | TASK-033（临时演示任务） | allocated(6) → running(7) → done(8) | ✅ 任务 open → in-progress → done；VERIFY 由 `task verify` 真实生成 |

- 下行链实际执行：`task start <id>` + `autoloop-coder --once`（真实本地 agent 会话），
  演示任务明确「不实现任何功能、不修改源码」，验证后任务文件已清理（无残留伪任务）。
- **治理闸门（真实任务验证，P0 + 返工超限 2 例）**：
  - westhill 临时 P0 任务（priority=P0, risk=P0, approval-ref=none）→
    `[governance] p0-blocked`（dry-run 与操作路径一致），写
    `dispatcher.governance-blocked` 事件，不执行下行链；
  - x1design 临时任务 `rework-count: 3` → `[governance] rework-rejected`
    （`rework-count=3 ≥ 3，拒绝自动实现（转人工）`），同样拦截 + 事件；
  - 验证用临时任务均已清理。
- 验证用注册表子集（westhill + x1design）：`runtime/logs/dispatcher-demo/projects.demo.json`
  （仅含同机本地条目；完整注册表含 aibase 开放任务，为避免 autoloop 捡到本任务自身，
  闭环验证选两个下游真实项目；aibase dogfood 可在 TASK-076 结束后单独执行）。
