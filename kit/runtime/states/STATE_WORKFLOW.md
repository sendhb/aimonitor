# State Workflow — 跨会话状态规范

> **适用对象：所有 AI 工具。**
> `_state/` 存放跨会话工作状态与上下文记忆，是任务文件（做什么）与验证记录（做完了吗）之间的"正在进行"信息。

---

## 文件类型

| 文件 | 用途 | 维护方式 |
|------|------|----------|
| `CURRENT_FOCUS.md` | 当前焦点、优先级队列、下一个动作 | 任务开始/结束时人工或 AI 更新 |
| `PROGRESS.md` | 全部任务完成进度总览 | **`task index` 自动重新生成** |
| `BLOCKERS.md` | 阻塞项与待解决问题 | 发现阻塞时立即记录 |
| `CONTEXT.md` | 跨会话关键决策、约束（可选） | 重要决策时记录 |

---

## 更新规则

1. **任务开始** → 更新 `CURRENT_FOCUS.md`（当前任务、下一个动作）
2. **任务流转** → `task start/done` 会自动更新 `PROGRESS.md` 与 `CURRENT_FOCUS.md`
3. **发现阻塞** → 立即记录到 `BLOCKERS.md`（`task block TASK-xxx "原因"`）
4. **重要决策** → 记录到 `CONTEXT.md`

模板见 `_templates/STATE-*.template.md`。

---

## CURRENT_FOCUS.md 格式

```markdown
---
name: CURRENT_FOCUS
metadata:
  type: state
  updated: YYYY-MM-DD
---

# Current Focus — YYYY-MM-DD

## 当前任务
TASK-001 — <名称>，阶段：in-progress

## 优先级队列
| 优先级 | 任务 | 状态 |
|--------|------|------|
| P1 | TASK-001 — xxx | in-progress |

## 下一个动作
1. <具体要做的事>
```

## PROGRESS.md 格式

由 `task index` 自动生成，**不要手动编辑**：

```markdown
# Progress — <项目名>

## 统计
| 状态 | 数量 |
| open | 3 |
| in-progress | 1 |
| blocked | 0 |
| done | 42 |
| cancelled | 1 |

## 进行中
| 任务 | 描述 | assignee |
|------|------|----------|

## 已完成（最近 10 条）
| 任务 | 描述 | 完成日期 |
```

## BLOCKERS.md 格式

```markdown
# Blockers

### BLOCKER-001 — <描述>
| 项 | 内容 |
|----|------|
| 严重度 | 🔴 / 🟡 |
| 阻塞任务 | TASK-001 |
| 记录日期 | YYYY-MM-DD |
| 解除方式 | <怎么做才能解除> |
| 状态 | open / resolved |
```

---

## 与其他目录的关系

```
_tasks/  → 定义要做什么（任务文件）
_state/  → 记录当前在做什么、做到哪里（本目录）
_verify/ → 记录做完后的验证结果
_reviews/ → 记录代码审查反馈
```
