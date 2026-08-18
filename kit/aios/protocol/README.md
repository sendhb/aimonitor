# aios/protocol — 协议规范

> Agent 之间通信约定（如 A2A）。

## 状态

远期规划，尚未实现。当前跨工具协作通过 `runtime/` 文件系统读写共享状态（见 [AGENTS.md](../../AGENTS.md#跨工具协作)），不依赖运行时通信协议。

未来若不同框架的 agent 需要直接协议级协作（而非通过文件系统），协议定义放在此目录。参见 [docs/ARCHITECTURE.md](../../docs/ARCHITECTURE.md) 的 A2A 说明。
