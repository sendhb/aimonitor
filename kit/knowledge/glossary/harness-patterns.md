# Glossary: Harness 模式全景

> 概念出处：`kit/aios/context/README.md` —— Prompt Engineering → Context Engineering → **Harness Engineering**。
> Harness = 围绕模型的**外部工程设施**：状态怎么存、质量怎么量、危险怎么隔离、边界怎么定、状态怎么被看见。
> 维护：Analyst + Researcher。

## 一、状态载体类模式（agent 状态/数据如何承载与流通）

Ralph 模式所在的维度：解决"agent 状态用什么载体、怎么跨进程/跨机流通"。

| 模式 | 载体 | 特点 | 适合场景 | 本项目落点 |
|------|------|------|---------|-----------|
| **Ralph 模式** | 本地文件系统 | 零依赖、可移植、可审计、离线可用；状态即文件 | 单机 agent、多工具共享状态 | ✅ `runtime/` 文件即数据库（唯一真相） |
| **Event Sourcing**（事件溯源） | 事件日志 | 真相 = 事件流；可无限重放重建状态 | 审计、回放、状态重建 | 🟡 `runtime/logs/*-events.jsonl`（雏形） |
| **Message Queue / Pub-Sub** | 消息总线 | 异步、解耦、跨机器、高吞吐 | 多机协作、任务分发、链式触发 | 🔵 演进路线第③阶段（规划中） |
| **Shared Database** | 中心数据库 | 强一致、强查询、多写者 | 多人多服务、复杂关联查询 | ❌ 刻意不用（零依赖哲学） |
| **Outbox 模式** | 本地表 + 消息 | 状态写入与事件发布**原子一致** | 本地状态 + 对外推送的可靠性桥 | ✅ agent 推送链路（思想同源：先落盘再投递） |
| **WAL（预写日志）** | 日志先行 | 崩溃恢复、顺序可靠 | 可靠性底层机制 | 🟡 概念参考（事件流顺序约定） |

## 二、执行模式类模式（agent 行为层）

不同维度：解决"agent 怎么干活"。

| 模式 | 一句话 | 本项目对应 |
|------|--------|-----------|
| **ReAct** | 推理 ↔ 行动交替循环 | `engine.md` 执行闭环（同源思想） |
| **Reflexion** | 干完反思，教训入记忆，下轮改进 | 闭环 §4 Reflect + 返工机制 |
| **Plan-and-Execute** | 先规划（Plan）再执行（Execute），两阶段 | §0 启动分流 + §1 Plan → §3 Execute |
| **Tree of Thoughts** | 多路径搜索，不一条道走到黑 | —（可探索） |
| **RAG** | 检索外部知识增强生成 | `knowledge/` 按需加载（Select/Load 策略） |

## 三、Harness Engineering 外围方向

| 方向 | 要解决的问题 | 本项目落点 |
|------|-------------|-----------|
| 状态载体 | 状态怎么存 | Ralph（`runtime/` 文件即数据库） |
| 评估工程 | 质量怎么量 | `evaluation/`（token/返工/成功率/模型对比） |
| 沙箱隔离 | 危险操作怎么隔离 | `sandbox-run`（无网络容器） |
| 护栏治理 | 边界怎么定 | `governance/` 5 份协议 |
| 可观测性 | 状态怎么被看见 | agent 遥测 → aimonitor（心跳/事件流/告警） |
| 工具抽象 | 能力怎么接 | MCP（能力层包装标准协议） |

## 选型结论（关键决策）

1. **状态载体选型 = Ralph 模式 + Outbox 思想**：本地文件是唯一真相（可审计、可移植、零依赖），对外传播走"先落盘、后投递"（agent 推送），本地永远完整，中央永远可重建。
2. **多项目交换的扩展原则**：中央（aimonitor/dispatcher）**不直写远端文件系统**；下行指令投递到目标项目，由目标项目本地 agent 落盘 `runtime/tasks/`——Ralph 模式在跨项目场景下依然成立。
3. **数据库为"刻意不用"而非"没想到"**：Shared Database 被零依赖哲学排除；复杂度通过文件 + 事件流 + 消息语义承载。

## 相关

- [architecture/](../architecture/) — 文件即数据库设计
- [context/](../../aios/context/README.md) — Harness Engineering 定义
- [ARCHITECTURE.md](../../docs/ARCHITECTURE.md) — Ralph 模式出处
