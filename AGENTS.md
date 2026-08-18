# AGENTS.md — AIOS + Reliable AI Engineering Framework

> 本项目是 **AI Agent Operating System + Reliable AI Engineering Framework**。
> 所有 AI agent（pi / Claude Code / OpenCode / Qwen Code / Cursor / Copilot / Codex CLI 等）通过本文件进入项目。
>
> **多 AI 通用**：AGENTS.md 是 Linux Foundation / AAIF 标准，25+ 工具原生读取。Claude Code 通过 `ln -s AGENTS.md CLAUDE.md`（symlink）接入。pi 额外读取 `.pi/SYSTEM.md` 注入系统提示词。
>
> **aibase = kit 源仓库**：本仓库自身也是 kit 子目录布局（`kit/` = 框架内容，与 `kit/cli/mkproject` 生成的项目一致）。框架所有内容在 `kit/` 内，本文件是完整导航（路径均带 `kit/` 前缀）。

## 二层入口

| 工具 | 入口方式 | 说明 |
|------|---------|------|
| **pi / OpenCode / Qwen Code / Codex / Gemini CLI / Aider / goose** | 原生读 `AGENTS.md` | 零配置 |
| **Cursor / Windsurf / Copilot / Devin** | 原生读 `AGENTS.md` + 各自配置 | `.cursor/rules/` / `.github/copilot-instructions.md` |
| **Claude Code** | `CLAUDE.md` = symlink → `AGENTS.md` | 官方推荐 `ln -s AGENTS.md CLAUDE.md` |

## 快速导航（首次进入读这 3 个）

1. [`kit/aios/governance/`](kit/aios/governance/) — **先读**：你可以/不可以做什么（5 份治理协议）
2. [`kit/agents/README.md`](kit/agents/README.md) — 你的角色（Manager/Analyst/Architect/Coder/Reviewer/Tester/Researcher）
3. [`kit/aios/execution/engine.md`](kit/aios/execution/engine.md) — 执行闭环：Plan → Impact → Execute → Reflect → Verify → Repair

## 启动分流（实现前强制判断）

实现前先阅读 [`kit/aios/policy/principles.md`](kit/aios/policy/principles.md)，并判断任务是否改变外部契约、业务规格、生成源或跨端协议：

- **是**：按项目类型加载 [`kit/aios/execution/sdd-workflow.md`](kit/aios/execution/sdd-workflow.md) 的 contract、docs 或 protocol 详细流程，先更新规格再实现。
- **否**：仅按 `engine.md` 的通用闭环执行普通逻辑修改或 Bug 修复。

判断不清时，先作为规格影响处理并在 TASK 中记录假设。

## 进阶导航

- [`kit/knowledge/`](kit/knowledge/) — 框架知识库结构（项目知识在根 `knowledge/`）
- [`runtime/tasks/`](runtime/tasks/) — 当前任务列表（`kit/cli/task list`）
- [`kit/cli/README.md`](kit/cli/README.md) — 统一调度入口
- [`kit/profiles/`](kit/profiles/) — 项目类型模板（backend/game-server/unity/design/…）
- [`kit/docs/ARCHITECTURE.md`](kit/docs/ARCHITECTURE.md) — 架构设计说明

## 规则红线（每次动作前检查）

| 协议 | 约束 |
|------|------|
| [task-policy](kit/aios/governance/task-policy.md) | 所有开发项对应 TASK，状态机：open→in-progress→in-review→done |
| [modification-policy](kit/aios/governance/modification-policy.md) | 不编辑 `generated_dirs`；P0 文件需人工批准 |
| [security-policy](kit/aios/governance/security-policy.md) | 密钥不进提示词；Rule of Two；训练退出 |
| [risk-policy](kit/aios/governance/risk-policy.md) | P0（数据库/支付/auth）→人工批准；P1→reviewer审查 |
| [roadmap-policy](kit/aios/governance/roadmap-policy.md) | Roadmap 唯一权威在 `docs/ROADMAP.md`，Manager 唯一维护，与 TASK 不脱节 |

## 跨工具协作

- **状态共享**：所有工具通过 `runtime/` 文件系统读写协作状态
- **角色分离**：Coder 与 Reviewer **必须不同工具/会话**（AI 不可自评）
- **并发隔离**：多任务用 Git worktree（一任务一分支）
- **嵌套 AGENTS.md**：monorepo 子包可放私有 `AGENTS.md`，离代码最近的优先

## 上下文预算

- 单文件 ≤ 200 行；首次加载 ≤ 40% 上下文窗口
- 按需加载：从本文件逐层展开，不一次性全读 `kit/`

## 使用（创建新项目）

```bash
# mkproject（kit 布局，推荐）
bash kit/cli/mkproject ~/code/my-project --profile backend

# install 脚本（远程安装）
curl -fsSL https://<host>/install.sh | bash        # Linux/macOS
irm https://<host>/install.ps1 | iex                # Windows PowerShell
```

---

*AIOS Framework。项目安装：`bash kit/cli/mkproject <target-dir> --profile <type>`。*
