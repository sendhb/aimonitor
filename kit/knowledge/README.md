# knowledge/ — 项目知识图谱

> AI 最大的问题不是不会写代码，而是**不知道系统关系**。
>
> 知识图谱让 AI 不依赖"训练时的记忆"（可能过时），而是读"这个项目的实时真相"。

## 目录

| 目录 | 用途 | 维护者 |
|------|------|--------|
| `architecture/` | 系统架构设计（分层/模块/通信） | Architect |
| `modules/` | 模块清单：每个模块的职责、依赖、入口 | Architect + Coder |
| `dependencies/` | 模块间依赖关系图（自动/人工维护） | Analyst + Architect |
| `decisions/` | ADR：为什么这样设计（不重蹈覆辙） | Architect |
| `history/` | 已知问题、技术债、失败尝试（不要重试） | 所有角色 |
| `glossary/` | 领域术语表（统一语言） | Analyst + Researcher |

## 原则

1. **Knowledge invisible to the agent doesn't exist.**
   架构共识不在代码里的，写到 knowledge/。
2. **Module before change.**
   改代码前先查 `modules/<目标>.md` 了解它依赖谁、被谁依赖。
3. **ADR for every fork.**
   遇到"选 A 还是选 B"的决策，写 ADR。

## 模块文件模板

`knowledge/modules/<模块名>.md`：

```markdown
# Module: <模块名>

## 职责
<一句话>

## 依赖
- <依赖模块 A> — 原因
- <依赖模块 B> — 原因

## 被依赖
- <模块 C>
- <模块 D>

## 入口
- `<文件夹/文件>` — 说明

## 关键决策
- <为什么这样做而不是那样做>
```
