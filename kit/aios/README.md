# aios/ — AI Agent Operating System 内核

> 整个工程模板的大脑。定义 AI agent 如何认知、规划、执行、验证、学习。
>
> 目录分层（从抽象到具体）：
>
> - `governance/` — 治理协议：AI 可以/不可以做什么
> - `cognition/` — 认知层：AI 如何理解目标、分析需求、管理假设
> - `execution/` — 可靠执行引擎：规划→影响→执行→反思→修复→验证
> - `context/` — 上下文工程：装配/排序/过滤/压缩/加载
> - `memory/` — 记忆系统：跨会话知识持久化
> - `policy/` — 通用原则
> - `protocol/` — 协议规范（agent 之间通信约定）

## 设计理念

> AI 不是"收到命令→执行"的工具，而是"理解目标→建模→规划→执行→验证→学习"的执行者。

参见 [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)。
