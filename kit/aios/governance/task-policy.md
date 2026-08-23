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

---

## 分级治理（fast-path）

**原则：治理强度随风险分级，不搞一刀切。** 低风险任务跳过独立 reviewer 会话与 Impact 文档，**但 verify（build/lint/test/check + VERIFY 记录）与 TASK 状态流转对任何任务都不可省略**。

### fast-path 判定（机械规则，与 `cli/task` 的 needs_review 逻辑一致）

任务为 **fast-path** 当且仅当 **同时满足**：

| 条件 | 值 |
|------|----|
| `metadata.risk` | P2 或 P3（非 P0/P1） |
| `metadata.priority` | P2 或 P3（非 P0/P1） |
| `metadata.reviewer` | 未指定（`any`/空） |

**fast-path 流程**：`verify 通过 → task done`，跳过 `task review`、REVIEW 记录、Impact 文档。

**完整路径**：命中任一条件则必须走完整路径（`task verify → task review → task approve`）。

### 创建任务时的切分与声明（反返工指导）

- **一个任务一个可独立验证的交付**：预计超过一个 coder 会话（约 >30 分钟）或改动 >5 文件，应拆分为多个 TASK。
- **验收标准必须具体可验证**：写命令/断言/文件与预期结果，禁止占位符（如"可验证的条件 1"）。
- 预计改动 >3 文件或跨 >2 模块时，创建者应主动指定 reviewer 或提高 risk——否则默认按 fast-path 处理，后果由创建者承担。

---

## 返工上限（rework-count）

**原则：自动返工最多 2 轮，超限升级人工，不无限烧循环。**

| 字段 | 语义 |
|------|------|
| `metadata.rework-count` | 打回次数。默认 0，TASK 模板自带 |
| 0 → 1 | 第 1 次打回，允许自动返工 |
| 1 → 2 | 第 2 次打回，允许自动返工 |
| 2 → 3 | 第 3 次打回，**`task start` 机械拒绝**，必须人工介入（拆分任务或人工批准继续） |

**机械强制**：
- `task start` 在 `in-review → in-progress`（打回）时递增 `rework-count`；`open → in-progress`（首次开始）不递增。
- `rework-count ≥ 3` 时，`autoloop-coder` 不自动实现，直接 `task block` 并通知人工。
- 人工解除：明确记录介入原因后 `task unblock` 或人工编辑 `rework-count`。

## 并发规则

- 多任务并发时用 git worktree 隔离（一任务一分支）
- 同任务抢占：先到先得，后到者读最新状态后决定

## 文件格式

见 `runtime/tasks/TASK.template.md`。模板含 `rework-count` 字段与验收标准写法引导。
