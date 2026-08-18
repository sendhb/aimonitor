# runtime/ — 项目运行时数据

> AI 的每一步操作都应留下 trace。这里存放所有运行时产生的数据（任务、状态、验证、审查、日志、记忆）。

## 目录

| 目录 | 写入时机 | 内容 |
|------|----------|------|
| `tasks/` | 任务创建/流转时 | TASK-xxx.md + INDEX.md |
| `states/` | 任务开始/阻塞/结束时 | CURRENT_FOCUS / PROGRESS / BLOCKERS |
| `executions/` | 每次 AI 执行时 | 执行 trace（可选：时间/agent/模型/token/结果） |
| `reviews/` | 审查通过时 | REVIEW-xxx.md |
| `verification/` | 验证通过时 | VERIFY-xxx.md |
| `logs/` | 高风险操作/异常时 | 审计日志 |
| `memory/` | 跨会话知识更新时 | AI 自我学习记录（preferences / patterns / fixes） |

## 原则

1. **不可变审计**：VERIFY 和 REVIEW 完成后只允追加修正说明
2. **自动索引**：`cli/task index` 自动生成 INDEX.md 和 PROGRESS.md
3. **可追溯**：每条记录有时间戳 + agent + 操作目标
