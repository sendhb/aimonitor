---
name: TASK-000-<slug>
description: 一句话说明任务目标
metadata:
  type: task
  status: open
  created: YYYY-MM-DD
  updated: YYYY-MM-DD
  priority: P2
  risk: P2
  approval-ref: none
  assignee: any
  reviewer: any
  parent: TASK-000
  depends-on: []
  rework-count: 0
  tags: []
---

# TASK-000 — 任务标题

## 目标
要做什么，以及为什么。

## 范围
涉及的文件/模块/端点；明确"不做什么"。

## 计划
1. 分析影响、风险与依赖。
2. 实现并逐项验证。
3. 记录 VERIFY；P1/P0 任务交由独立 reviewer 审查。

## 风险与审批
- 风险级别：P2
- P0 人工批准记录：不适用（P0 时填写审批工单、PR 或变更单链接）

## 验收标准

> 每条必须**具体可验证**：写明命令/断言/文件与预期结果，禁止占位符（如"可验证的条件 1"）。
> 验收标准应尽量做到“不靠 LLM 主观判断”：能写命令就写命令，能写断言就写断言，能写预期输出就写预期输出。
> 范围必须明确列出允许改动/禁止改动的文件或目录，避免 Coder 越界和 Reviewer 反复判断。
> 一个任务一个可独立验证的交付；预计超过一个 coder 会话（约 >30 分钟）或改动 >5 文件 → 拆任务。
> 预计改动 >3 文件或跨 >2 模块 → 指定 reviewer 或提高 risk（否则按 fast-path 处理）。

- [ ] 可验证的条件 1（示例：`python3 -c "import x; assert x.y == 1"` 通过）
- [ ] `config.commands.build` 零错误
- [ ] `config.commands.check` 通过

## 当前进度

| 项 | 状态 | 位置 |
|----|------|------|
| 功能 1 | ⏳ | — |

## 子任务
- → TASK-001: 说明

## 备注
已知问题、关键决策。

> `metadata.rework-count`：打回自动累计（`task start` 在 in-review→in-progress 时 +1），
> 超过 2 次（2→3）被拒绝，需人工介入。详见 `aios/governance/task-policy.md`。
