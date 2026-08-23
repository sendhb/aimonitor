# cli/ — 统一控制入口

> 已实现的 CLI 负责初始化、任务生命周期与框架健康检查；计划、影响分析和执行流程由 `aios/` 文档与角色定义约束，本身不提供确定性命令 —— `autoloop-*` 只是调度壳，实际的 Plan/Impact/Execute/Verify 仍由被调用的 agent 会话按文档执行，没有额外的机械校验。

## 核心命令

| 命令 | 功能 | 实现 |
|------|------|------|
| `mkproject` | 用 kit/ 布局创建新项目；支持 `--profile`、`--persona <name>`（从人格库激活）、`--no-persona`（零加载）、`--from <kit-root>` | `cli/mkproject` |
| `persona` | 人格切换（按需加载）：`list` / `use <name>` / `off` / `show` | `cli/persona`（Python 3） |
| `init` | 安装模板到目标项目（Python 跨平台：Windows 可 `python cli/init`；`--install-deps` 自动按平台装 git/python） | `cli/init` |
| `task` | 任务生命周期管理 | `cli/task`（Python 3，用 `./cli/task` 调用，勿用 `bash cli/task`） |
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

用法：

```bash
bash cli/autoloop-coder    --interval 300 --unattended --id coder-1
bash cli/autoloop-reviewer --interval 300 --unattended --id reviewer-1
```

**⚠️ `--unattended` 会给 `claude -p` 传 `--dangerously-skip-permissions`，agent 将不经确认执行任意文件写/shell 操作。仅在隔离环境（容器/git worktree/一次性沙箱）中启用，并确保有独立版本控制可随时回滚。** 用 `cli/sandbox-run -- bash cli/autoloop-coder --once --unattended` 就是这样的隔离环境。P0 风险任务（`aios/governance/risk-policy.md`）不会被自动实现或自动 approve —— 缺少 `approval-ref` 时脚本会把任务转 `blocked` 并停止，等待人工。

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
