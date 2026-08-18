# Task Policy — 任务生命周期与状态机

> **强制约束**：所有开发项必须对应一个 TASK。状态流转由 `cli/task` 管理。

## 状态机

```
open → in-progress → in-review → done
  │        │              ↑
  │        ▼              │（返工）
  └──→ blocked ──→ in-progress ──┘
  │
  └──→ cancelled
```

| 状态 | 含义 | 转换 |
|------|------|------|
| `open` | 已定义，未开始 | → in-progress, blocked, cancelled |
| `in-progress` | 正在执行 | → in-review, done, blocked, cancelled |
| `in-review` | 等待审查（生成者≠审查者） | → done, in-progress（返工） |
| `blocked` | 被阻塞 | → in-progress |
| `done` | 完成并验证 | 终态 |
| `cancelled` | 取消 | 终态 |

## 关闭前置条件

1. 验收标准全部勾选
2. VERIFY 记录存在（`runtime/verification/`）
3. P1/P0 任务、指定 reviewer 的任务或进入 `in-review` 的任务，必须有通过的 REVIEW 记录，且 reviewer 不得等于实现者
4. P0 任务必须在 TASK 的 `approval-ref` 中引用人工批准记录

`--force` 不得用于绕过关闭前置条件。异常关闭必须由人工在 TASK 中明确记录风险、原因和批准引用。

## 并发规则

- 多任务并发时用 git worktree 隔离（一任务一分支）
- 同任务抢占：先到先得，后到者读最新状态后决定

## 文件格式

见 `runtime/tasks/TASK.template.md`。
