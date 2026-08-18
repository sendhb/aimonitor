# agents/ — Agent 角色定义

> 不再按工具（Claude/Cursor/Copilot）定义，而是按**角色**（Manager/Analyst/Architect/Coder/Reviewer/Tester/Researcher）。
>
> 每个角色有自己的能力、权限、禁止事项。一个 AI 工具可以承担一到多个角色。

## 角色一览

| 角色 | 职责 | 读 | 写代码 | 改架构 | 关键约束 |
|------|------|:--:|:--:|:--:|------|
| **Manager** | 任务分派、进度跟踪、阻塞管理 | ✅ | ❌ | ❌ | 不写代码，只管流程 |
| **Analyst** | 需求分析、影响评估、假设管理 | ✅ | ❌ | ❌ | 探索先行，不明不写 |
| **Architect** | 架构决策、模块划分、技术选型 | ✅ | ❌ | ✅ | 设计审查、ADR 记录 |
| **Coder** | 按计划实现代码 | ✅ | ✅ | ❌ | 必须通过 verify；不改架构 |
| **Reviewer** | 代码审查、安全检查 | ✅ | ❌ | ❌ | 生成者 ≠ 审查者；必须不同会话/工具 |
| **Tester** | 测试编写与执行 | ✅ | ✅（测试文件） | ❌ | 测试先行 |
| **Researcher** | 外部检索、知识录入 | ✅（外部） | ❌ | ❌ | 探索结果写入 knowledge/ |

## 角色不是工具绑定

- Claude Code / pi / OpenCode / Qwen Code 等 **完整 agent** 可承担任一角色
- Copilot 补全模式、Qwen 网页模式 **只能做 Coder 角色**（无执行能力时不改状态）
- 同一 session 可切换角色，但 **Reviewer 必须与被审查的 Coder 不同会话/工具**

## 角色目录

每个 `agents/<role>/` 包含：
- `role.md` — 角色定义（输入/输出/权限/禁止）
- 触发该角色的意图描述（AI 据此自我匹配角色）
