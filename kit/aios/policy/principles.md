# Core Principles — 通用开发原则

> **所有 AI 工具必须阅读本文件；是否需要阅读 [`../execution/sdd-workflow.md`](../execution/sdd-workflow.md) 按 [`../execution/engine.md`](../execution/engine.md) §0 的启动分流判断。**
> 本文件定义"是什么"，`sdd-workflow.md` 定义命中规格变更时"怎么做"。
> 项目专属信息（路径、命令、技术栈）一律以 `profiles/<type>/config.yaml` 为准。

---

## 1. 单一真相来源 (Single Source of Truth)

- **contract 型**（`config.spec.type: contract`）：规格文件（`config.spec.file`）是 API/行为契约的**唯一权威**
- **docs 型**（`config.spec.type: docs`，工具/GUI 软件、游戏客户端）：`config.docs.domains`（功能/玩法规格）+ `config.docs.flows`（交互/玩法流程）是功能的**唯一权威**
- **protocol 型**（`config.spec.type: protocol`，CS 网游）：`config.docs.protocol`（消息定义、封包格式、交互序列）是客户端与服务器通信的**唯一权威**，变更必须两端同步
- 实现、文档、生成代码都必须与规格一致；不一致时以规格为准
- **规格变更必须先于实现**

## 2. 生成代码不可手动编辑

- `config.generated_dirs` 列出的目录由代码生成器写入，任何 AI **不得手动修改**
- 需要改生成代码 → 改规格 → 重新生成（`config.commands.generate`）
- 生成代码与规格不一致 = 生成器未运行或规格已过期 → 立即重新生成

## 3. 验证是流程的一部分

- 任何变更以 `config.commands.check`（及 `build` / `test`）通过为**完成标志**
- 验证结果必须记录到 `runtime/verification/`（见 `runtime/verification/VERIFY_WORKFLOW.md`）；优先用 `cli/task verify TASK-xxx` 生成——它真实执行 `aios.config.yaml` 里的命令，不是手写记录自证

## 4. 任务可追踪

- 每个开发项必须对应一个 `runtime/tasks/TASK-xxx.md`（固定格式，见 `aios/governance/task-policy.md`）
- 用 `cli/task` 管理状态；**任何 AI 接手任务先看状态，完成任务必须流转状态**
- 状态 6 种：`open` → `in-progress` → `in-review` → `done`，另有 `blocked` / `cancelled`（详见 `aios/governance/task-policy.md`）
- 任务含 `reviewer` 时，完成前必须由生成者之外的审查者审查（`in-review` + REVIEW 记录）

## 5. 最小改动

- 只修改任务范围内必要的文件；不做"顺手"重构
- 每完成一个逻辑单元即验证，不积累未验证的变更

## 6. 沟通与错误处理

- 回复语言：中文或英文；代码、注释、commit 用英文
- 错误信息不泄露内部细节（对外输出用户可理解的反馈，内部日志记录完整上下文）
- API/异步操作显示状态反馈

---

## 相关文件

- [`../execution/sdd-workflow.md`](../execution/sdd-workflow.md) — 命中规格变更时怎么做（场景 A/A'/A"/B/C），是否加载见 `engine.md` §0
- [`aios/governance/task-policy.md`](../governance/task-policy.md) — 任务生命周期与固定格式
- `profiles/<type>/config.yaml` — 项目专属信息（路径/命令/技术栈）
