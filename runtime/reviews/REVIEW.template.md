---
name: REVIEW-YYYY-MM-DD-<scope>
description: 一句话说明审查范围
metadata:
  type: review
  date: YYYY-MM-DD
  task-ref: TASK-000
  reviewer: claude
  implementation-author: unknown
  result: pass
  commit: unknown
---

# REVIEW — 审查范围

> **模板选择（TASK-050 分级审查）**：
> - P2 任务（risk/priority 非 P0/P1 且未指定 reviewer 之外的完整路径任务）→ 填 **A. 三问核查**
> - P0/P1 任务 → 填 **B. 六维核查**
> **发现表只写真实问题，禁止样板发现**——为填满表格而写的发现是无效返工的最大来源。

## A. 三问核查（P2）

| 检查项 | 结论 | 证据 |
|--------|------|------|
| 验收标准是否全部满足 | ✅ / ⚠️ / 🔴 | 逐条对应 |
| 改动是否越界（范围/文件数） | ✅ / ⚠️ | diff 文件清单 |
| verify 是否真实通过 | ✅ / ⚠️ | VERIFY 记录 + 复跑结果 |

## B. 六维核查（P0/P1）

| 维度 | 结论 |
|------|------|
| SDD 合规 | pass / issues-found / critical |
| 架构 | pass / issues-found / critical |
| 安全 | pass / issues-found / critical |
| 影响 | pass / issues-found / critical |
| 质量 | pass / issues-found / critical |
| 测试 | pass / issues-found / critical |

## 发现

| ID | 严重度 | 描述 | 建议修复 | 状态 |
|----|--------|------|----------|------|
| SMELL-001 | 🟡 | 问题描述（只写真实问题） | 建议 | open |

## 结论

通过 / 需修改，建议创建 TASK-xxx 修复。
