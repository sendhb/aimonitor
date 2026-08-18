# aios/memory — 跨会话记忆系统

> 上下文窗口是 RAM（一次会话的短期记忆）。Memory 是不挥发存储（跨会话的长期知识）。

## 记忆类型

| 类型 | 位置 | 内容 |
|------|------|------|
| 事实记忆 | `knowledge/` | 架构、模块、决策、术语（人工/AI 确认后写入） |
| 任务记忆 | `runtime/tasks/` + `runtime/states/` | 任务状态（每个会话都是新的上下文窗口，任务记忆是恢复点） |
| 偏好记忆 | `runtime/memory/` | AI 学习到的偏好：代码风格、常用命令、你纠正过的事 |
| 失败记忆 | `runtime/logs/` + `evaluation/failures/` | 失败日志、修复记录（避免重复错误） |
| 审计记忆 | `runtime/reviews/` + `runtime/verification/` | 不可变审计记录 |

## Memory Update 协议

每次会话结束时，AI 应自问：

1. 有没有学到新的偏好？（用户纠正了我什么？）
2. 有没有学到新的模式？（这个项目有什么特殊约定？）
3. 有没有遇到需要记录的失败？（什么操作失败了？根因是什么？教训？）

→ 写入 `runtime/memory/` 或 `evaluation/failures/`。

## 原则

- Memory ≠ Context：不要往上下文窗口塞记忆（吃 token、无缓存命中）
- Memory = File：通过文件系统实现，零依赖
- 不冗余：已在 `knowledge/` 中的不重复写入
