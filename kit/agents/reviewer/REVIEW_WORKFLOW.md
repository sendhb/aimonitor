# Review Workflow — 代码审查规范

> **适用对象：所有 AI 工具。**
> 审查用于发现实现与规格/架构/安全的偏差。审查结论可作为新任务（修复项）的输入。

---

## 文件命名

```
REVIEW-<YYYY-MM-DD>-<范围>.md
```

示例：`REVIEW-YYYY-MM-DD-feature-name.md`

## 固定格式

路径：`runtime/reviews/REVIEW-<日期>-<范围>.md`，模板见 `runtime/reviews/REVIEW.template.md`

```markdown
---
name: REVIEW-YYYY-MM-DD-xxx
description: 一句话说明审查范围
metadata:
  type: review
  date: YYYY-MM-DD
  task-ref: TASK-001        # 可选：关联任务
  reviewer: claude          # 审查者
  implementation-author: codex # 必填：实现者，必须不同于 reviewer
  result: pass              # 仅 pass 可关闭任务
  commit: <git-sha-or-unknown>
---

# REVIEW — <范围>

## 审查结果
| 维度 | 结论 |
|------|------|
| SDD 合规 | pass / issues-found / critical |
| 架构层次 | pass / issues-found / critical |
| 安全性 | pass / issues-found / critical |
| 代码质量 | pass / issues-found / critical |
| 性能 | pass / issues-found / critical |

## 发现
| ID | 严重度 | 描述 | 建议修复 | 状态 |
|----|--------|------|----------|------|
| SMELL-001 | 🟡 | <描述> | <建议> | open |

## 结论
通过 / 需修改，建议创建 TASK-xxx 修复
```

---

## 审查维度

| 维度 | 检查内容 |
|------|----------|
| SDD 合规 | 实现是否与 `config.spec.file` 一致；是否动了 `config.generated_dirs` |
| 架构层次 | 是否遵循项目分层（handler → service → model 等，见项目架构文档） |
| 安全性 | 鉴权、输入验证、敏感数据、错误信息泄露 |
| 代码质量 | 错误处理、命名、注释、重复代码 |
| 性能 | 查询效率、N+1、无谓计算 |

## 严重度定义

| 严重度 | 含义 | 处理 |
|--------|------|------|
| 🔴 | 功能错误 / 安全漏洞 | 必须修复 |
| 🟡 | 设计/代码质量问题 | 建议修复 |
| 🔵 | 信息/改进建议 | 可选 |

---

## 规则

1. 审查发现的问题记入 `runtime/reviews/`，修复项创建为 TASK 跟踪（`cli/task new`）
2. 严重问题（🔴）不得在无 TASK 跟踪的情况下关闭
