# 架构设计说明

> 为什么 aibase 从"AI 配置模板"升级为"AI Agent Operating System + Reliable AI Engineering Framework"。

## 设计动机

第一代 AI 工程模板（ai-env 类）的核心假设：

```
AI 工具 → 读规则 → 理解任务 → 执行 → 验证 → 完成
```

这个模型成立的前提是：**AI 本质上是一个"会读规则的助手"**。

2026 年的现实是：AI agent 已经具备自主规划、多步执行、工具调用的能力。但如果没有治理约束，它们会：

- 修改不应该修改的文件（generated_dirs）
- 忽略系统架构关系（因为不在上下文里）
- 无法评估自己的工作质量（AI 无法自评）
- 在验证失败时没有修复机制

**未来正确的方向：**

```
Intent → Understanding → Knowledge → Planning
  → Execution Governance → Tool Action
  → Verification → Reflection → Memory Update
```

AI 不再是"收到命令→执行"，而是"理解目标→建模→规划→执行→验证→学习"。

## 三维升级

| 维度 | 问题 | 解决方案 |
|------|------|---------|
| **Governance** | AI 不知道什么不能做 | `aios/governance/` —— 任务政策、修改权限、安全红线、风险分级 |
| **Execution Engine** | AI 会写代码但不会做对 | `aios/execution/` —— Plan→Impact→Execute→Reflect→Verify→Repair 闭环 |
| **Knowledge Graph** | AI 不知道系统关系 | `knowledge/` —— 模块、依赖、架构决策、术语、失败历史 |

## 从工具适配到角色定义

旧版：`tools/claude.md`, `tools/cursor.md`, `tools/copilot.md`... 每个工具一个适配器。

新版：`agents/manager/`, `agents/coder/`, `agents/reviewer/`... 按角色定义，工具只是"执行器"。

```
旧:  Claude 能做什么？→ tools/claude.md
新:  Coder 角色需要什么能力？→ 任何有 git/shell/filesystem 能力的 AI 工具
```

这解决了一个根本问题：新工具（Codex CLI、Gemini CLI、Qwen Code...）不断出现，按角色定义不需要为每个工具写新适配器。

## 从环境配置到项目模板

旧版：`config.yaml` 是"环境"配置。

新版：`profiles/` 是"项目知识模板"。每种项目类型（backend/game-server/unity/unreal）有自己的技术栈约定，AI 初始化时加载模板就知道项目长什么样。

## 文件即数据库

所有运行时数据（任务、状态、验证、审查、日志、记忆）都在 `runtime/` 中以文件形式存储。不需要数据库、不需要服务。AI 通过文件系统读写协作状态。

这直接对应 **AIOS（LLM Agent OS）的 Memory Manager / Storage Manager** 设计理念，以及 **Ralph 模式**（文件系统作为 agent 状态载体）。

## 评价体系

AI 的工作也需要被评估。`evaluation/` 记录每次任务的 token 消耗、返工次数、bug 率、成功率。不同模型的表现可以横向对比。这对应了 **Harness Engineering** 的"评估工程"方向。

## 与标准的关系

- **AGENTS.md**：本框架的 AGENTS.md 是标准格式的 AGENTS.md，所有读取该标准的 AI 工具均可原生接入
- **AIOS（agiresearch/AIOS）**：设计理念（Governance/Context/Memory/Storage Manager）同源，但作为文件级治理层独立于 AIOS 运行时
- **MCP**：`tools/` 能力层可包装为 MCP server，使 agent 通过标准协议调用
- **A2A**：`agents/` 角色可通过 A2A 协议在不同框架的 agent 间协作（远期）
