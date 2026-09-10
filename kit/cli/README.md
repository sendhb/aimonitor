# cli/ — 统一控制入口

> 已实现的 CLI 负责初始化、任务生命周期与框架健康检查；计划、影响分析和执行流程由 `aios/` 文档与角色定义约束，本身不提供确定性命令 —— `autoloop-*` 只是调度壳，实际的 Plan/Impact/Execute/Verify 仍由被调用的 agent 会话按文档执行，没有额外的机械校验。

## 核心命令

| 命令 | 功能 | 实现 |
|------|------|------|
| `mkproject` | 用 kit/ 布局创建新项目；支持 `--profile`、`--persona <name>`（从人格库激活）、`--no-persona`（零加载）、`--from <kit-root>` | `cli/mkproject` |
| `persona` | 人格切换（按需加载）：`list` / `use <name>` / `off` / `show` | `cli/persona`（Python 3） |
| `init` | 安装模板到目标项目（Python 跨平台：Windows 可 `python cli/init`；`--install-deps` 自动按平台装 git/python） | `cli/init` |
| `re-init` | 重建目标 `kit/`（框架升级路径：备份旧版 → 删除 → 用新版重装；只动 `kit/`，人格保真，init 失败自动回滚，报告 新增/删除/更新 对账） | `cli/re-init`（Python 3；第一个参数为目标，其余选项原样透传给 init） |
| `task` | 任务生命周期管理（`block` 原因必填，缺失则非零退出）；`task review [--wake]`（reviewer 不在岗时告警/唤醒）、`task stale [--hours N]`（in-review 滞留检测，有滞留 exit 2 供监控/CI 消费，TASK-107） | `cli/task`（Python 3，用 `./cli/task` 调用，勿用 `bash cli/task`） |
| `task verify` | 真实执行 `aios.config.yaml` 的 build/lint/test/check，通过才生成 VERIFY 记录（不是自证） | `cli/task verify TASK-xxx` |

> **调用注意**：`cli/task` 是 Python 3 脚本，请用 `./cli/task <子命令>` 或 `python3 cli/task <子命令>`；
> **Windows**：请用 `python cli\task <子命令>`（`python3` 常是 Microsoft Store stub，不可用）；
> 不要用 `bash cli/task <子命令>` —— bash 会把 Python 源码当 shell 逐行解析，产生巨量输出并死循环（脚本已自带防护）。
| `check` | 框架健康与 TASK 格式检查 | `cli/check` |
| `protect` | 把 `generated_dirs` chmod 成只读（`--unlock` 反向解锁） | `cli/protect [--unlock]` |
| `sandbox-run` | 无网络容器沙箱跑任意命令（Rule of Two 隔离） | `cli/sandbox-run -- <command>` |

## 无人值守模式（可选/实验性）

| 命令 | 功能 |
|------|------|
| `autoloop-coder` | 轮询 `open`/`in-progress` 任务，起独立无头会话按 `agents/coder/role.md` 实现并提交审查 |
| `autoloop-reviewer` | 轮询 `in-review` 任务，起独立无头会话按 `agents/reviewer/role.md` 审查并 approve/打回 |

两者是两个独立进程，只通过 `runtime/tasks/` 文件状态耦合（不直接通信），满足"生成者 ≠ 审查者、必须不同会话"的约束。每轮循环默认 `timeout 1800s`（`--timeout` 可调）杀死卡死的子会话，并产出机器可读的健康信号供监控系统读：

- `runtime/logs/autoloop-{coder,reviewer}.heartbeat` — 每轮循环开始时间戳，判断循环是否还活着
- `runtime/logs/autoloop-{coder,reviewer}-events.jsonl` — 每轮一行 JSON `{ts, task, outcome}`，outcome ∈ `no_task/blocked_p0/ok/error/timeout`
- `runtime/logs/tasks/<TASK-ID>.log` — LLM 会话输出按 task 落盘（TASK-100）：路径是 task ID 的纯函数，单文件追加 + 轮次头 `=== [ISO时间] <role> round N | provider <p> | PID <pid> ===`（rework/reviewer 多轮共存一个文件保任务完整一生，`tail -f` 实时可看）；`task verify` 输出经 prompt 指示 tee 进同一文件。循环层行仍在 `autoloop-{coder,reviewer}-<date>.log`（广度）与 per-task log（纵深）互补
- `runtime/logs/sessions/<TASK-ID>/` — pi 会话全量 transcript（TASK-103）：pi `--session-dir` 自建 `<时间戳>_<UUID>.jsonl`（消息/tool call/thinking/usage），与 per-task log（人读摘要，`-p` 模式只有最终回复）互补；`pi --export <file>` 回放 HTML。仅 pi 生效；no_task 空转轮与非 pi provider 不落 session；目录创建失败降级 `--no-session` 不阻塞会话
- `runtime/locks/autoloop-llm.pid` — 当前 LLM 子进程记录（`{pid, llm, task, ts}`；daemon 退出收编与 status 孤儿检测的数据源）
- `runtime/locks/task-events.lock` — task-events.jsonl 追加/任务编号分配的跨进程锁（并发 `task new` 防 seq 撞号）

用法：

```bash
python kit/cli/autoloop coder    --interval 300 --unattended --id coder-1
python kit/cli/autoloop reviewer --interval 300 --unattended --id reviewer-1
python kit/cli/autoloop status   --interval 300   # 单屏聚合：壳死活/LLM 子进程/in-progress/最近事件
python kit/cli/autoloop ensure                    # 看门狗：幂等探活+拉起（供定时器高频调用）
python kit/cli/autoloop watchdog                  # 本仓自看看门狗：常驻薄壳定时调 ensure（TASK-111）
```

> **status（TASK-099）**：单屏回答三个问题 —— 循环壳死活（PID + heartbeat 年龄）、
> 哪个 task 有 LLM 在跑（PID 记录 + ps 扫描孤儿，无记录的存活进程标记为「孤儿」交人工决策）、
> in-progress 任务清单与最近事件。daemon（both）正常退出（SIGTERM/stop）时会收编
> 记录中的 LLM 子进程；`kill -9` 残留的孤儿由 status 暴露，人工决策兑底。

> **per-task log（TASK-100）**：任务执行中实时观察走
> `tail -f runtime/logs/tasks/<TASK-ID>.log`（路径由 task ID 直接推导，
> `autoloop_coder.task_log_path(task_id)` 为纯函数）；no_task 空转轮不产生
> task log（仍走 both.log/daily）。events/heartbeat/both.log 保留不替代——
> per-task log 管单任务纵深，它们管循环整体广度，判活仍靠 heartbeat。
> 会话全过程回放（TASK-103）：`runtime/logs/sessions/<TASK-ID>/`（
> `autoloop_coder.task_session_dir(task_id)` 为纯函数）落 pi 原生全量
> transcript，per-task log 管人读摘要、session 管全量回放，二者互补。

> 兼容 shim（TASK-026）：`python kit/cli/autoloop-coder ...` / `python
> kit/cli/autoloop-reviewer ...` 等价于上面的 `autoloop coder\|reviewer ...`。

### 看门狗（自愈）：autoloop ensure（TASK-107）

常驻循环无外部看门狗 = 死了永远死（实录：LLM 网关欠费卡满 timeout 后整个 both
循环停摆 3 天）。`autoloop ensure` 是幂等探活+拉起入口，供系统定时器高频调用；
多次/并发调用至多一个实例胜出（内层 `autoloop-both.lock` 防重），健康时 no-op：

| 判定（`ensure_decision`） | 动作 |
|------|------|
| PID 文件缺失或进程已死 | **spawn**：直接后台拉起 `autoloop both`（锁随进程死亡已由 OS 释放） |
| 壳存活但最新心跳停滞（> 阈值） | **restart**：SIGTERM 僵死实例（daemon 收尾收编 LLM 子进程、释放锁）后再拉起 |
| 壳存活且心跳新鲜（或进程太新无心跳，防误杀） | **no-op**：返回 0 |

- 阈值默认 `2×interval + timeout`（`ensure_threshold`，默认 30/1800 → 1860s），
  `--max-age S` 可覆盖；循环活性取 coder/reviewer 两心跳中最新者。
- 返回码：0 = 健康或已拉起；1 = 拉起失败/参数非法（并发锁竞争拒启属预期：胜出
  实例已在跑，定时器可忽略）。

**本仓自看看门狗（TASK-111）**：`autoloop watchdog` —— 每仓一个常驻薄壳，定时调
`ensure` 探活拉起本仓，**不依赖系统定时器/计划任务**（容器、受限环境、多平台统一的
最低公共分母）：

```bash
python kit/cli/autoloop watchdog                 # 常驻（每 120s 巡检；Ctrl-C 停止）
python kit/cli/autoloop watchdog --interval 60   # 自定义间隔；--max-age S 透传 ensure
python kit/cli/autoloop watchdog --once          # 单轮自检（不持锁）
python kit/cli/autoloop stop                     # 同时停止 both + 看门狗
```

- **薄壳零判定**：生死判定 100% 复用 `ensure`（子进程调用，30s 超时——ensure 悬挂
  只损失一轮）；本层只做调度/心跳/事件，判定逻辑出现第二份 = 未来事故。
- **自描述存活**：`runtime/locks/autoloop-watchdog.{lock,pid}` +
  `runtime/logs/autoloop-watchdog.heartbeat` + 每轮事件（ok/error）→ 事件流停滞
  = 看门狗死亡，远端监控可见（aimonitor 现有事件链路免费接走）。
- **每仓自看，零配置**：cwd 即项目根，无项目清单/注册表；与系统定时器**可并存**
  （ensure 幂等 + both 锁防重，叠加无冲突）。
- **残余风险（知情）**：无 OS 兜底——看门狗自身死亡或机器重启后需人工重启
  （现有批量启动脚本每仓加一行 `autoloop watchdog` 即可；`@reboot` /
  `Restart=always` 可选加固不强制）。每轮一行事件（默认 120s ≈ 720 行/天/仓）
  是"死亡远端可见"的设计价格。

**Windows（schtasks）**：每 2 分钟探活 + 开机自启：

```bat
schtasks /Create /TN "autoloop-watchdog" /TR "python C:\path\to\kit\cli\autoloop ensure" /SC MINUTE /MO 2
schtasks /Create /TN "autoloop-boot"     /TR "python C:\path\to\kit\cli\autoloop ensure" /SC ONSTART
```

**Linux（systemd timer）**：`/etc/systemd/system/autoloop-watchdog.service` + `.timer`：

```ini
# autoloop-watchdog.service
[Unit]
Description=autoloop ensure watchdog
[Service]
Type=oneshot
WorkingDirectory=/path/to/project
ExecStart=/usr/bin/python3 kit/cli/autoloop ensure

# autoloop-watchdog.timer
[Unit]
Description=Run autoloop ensure every 2 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now autoloop-watchdog.timer
```

**Linux（cron）**：`crontab -e`：

```cron
*/2 * * * * cd /path/to/project && python3 kit/cli/autoloop ensure >> runtime/logs/watchdog.log 2>&1
@reboot            cd /path/to/project && python3 kit/cli/autoloop ensure >> runtime/logs/watchdog.log 2>&1
```

配套自愈面（同 TASK-107）：

- **快速失败**：LLM 网关不可恢复错误（`gateway_error`/`403004`/欠费/HTTP 401/403，
  见 `llm.FATAL_PATTERNS`）命中即杀会话、事件记 `error`（非 timeout），不再拖满
  timeout 拖垮循环；超时重试语义不变。
- **`task review [--wake]`**：转 in-review 时机械检查 reviewer 心跳，不在岗则醒目
  告警并指引 `autoloop ensure`；`--wake` 额外后台拉起一次单轮审查（reviewer 内层锁
  防重入，已在跑自然跳过）；末尾刷新 INDEX/PROGRESS（对齐 approve，治索引漂移）。
- **`task stale [--hours N]`**（默认 2h）：扫描 in-review 任务按 `metadata.updated`
  计滞留时长（日期粒度，自当日 00:00 起算，宁早勿晚），有滞留 exit 2，可接监控/CI。

**⚠️ `--unattended` 会给 `claude -p` 传 `--dangerously-skip-permissions`，agent 将不经确认执行任意文件写/shell 操作。仅在隔离环境（容器/git worktree/一次性沙箱）中启用，并确保有独立版本控制可随时回滚。** 用 `cli/sandbox-run -- python kit/cli/autoloop coder --once --unattended` 就是这样的隔离环境。P0 风险任务（`aios/governance/risk-policy.md`）不会被自动实现或自动 approve —— 缺少 `approval-ref` 时脚本会把任务转 `blocked` 并停止，等待人工。

## 机械强制层（跟具体 AI 工具无关）

| 层 | 机制 | 挡什么 |
|---|------|--------|
| 进程沙箱 | `cli/sandbox-run`（默认无网络容器） | Rule of Two：敏感数据 + 不可信输入时禁止外传 |
| 文件系统权限 | `cli/protect`（chmod 锁 `generated_dirs` 只读） | 手动改生成代码 |
| Git hook（本地） | `.githooks/pre-commit`、`.githooks/commit-msg` | 没有 TASK 引用的提交、碰了 generated_dirs、check 没过 |
| CI（服务端，本地 hook 被 `--no-verify` 绕过也挡不住） | `.github/workflows/verify.yml` | 验证被跳过；配合分支保护（需仓库管理员在 GitHub 网页开）还能挡"生成者=审查者" |

这四层跟用 Claude Code、Cursor、pi 还是人手改代码无关——都会经过文件系统、git、CI，这是它们和 `aios/` 下纯文档治理规则的本质区别。

## 设计原则

- CLI 是已自动化流程的入口；尚未实现的流程必须显式读写其 Markdown 记录，不得假定存在对应命令
- 所有命令零网络依赖（纯本地）；`autoloop-*` 例外 —— 它们调用 `claude -p`，网络/模型调用发生在被调起的会话内部
- 输出格式：成功时安静（或 ✓），失败时详细（✗ + 原因）
