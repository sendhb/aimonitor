# aios/execution — 可靠执行引擎

不是"让 AI 写代码"，而是**确保 AI 做对事情**。

## 闭环

```
Intent  →  Impact Analysis  →  Plan  →  Execute  →  Reflect  →  Verify  →  Done
              ↑                                                      │
              └──────────────── Repair ←── Fail ←────────────────────┘
```

## 目录

| 目录 | 职责 |
|------|------|
| `planner/` | 任务分解为可执行计划（Plan First，Code Later） |
| `impact/` | 影响分析：修改 X 会影响 Y, Z，防止漏改 |
| `dependency/` | 依赖解析：任务排序与阻塞检测 |
| `executor/` | 执行引擎：按计划逐步实现 |
| `reflection/` | 自省：完成后检查遗漏、架构走偏、技术债 |
| `repair/` | 修复：验证失败时自动回滚+重新分析 |
| `verifier/` | 验证：编译/测试/lint/check 机械强制 |

## 工作流选择

实现前按 [`engine.md`](engine.md) 的“Workflow Selection”判断：仅当任务改变外部契约、业务规格、生成源或跨端协议时，才加载 [`sdd-workflow.md`](sdd-workflow.md) 的详细场景流程。

## 核心原则

1. **Plan First, Code Later** — 不规划不写代码
2. **Impact Before Change** — 改之前先算影响范围
3. **Verify Before Done** — 不通过 check 不算完成
4. **Reflect After Execute** — 完成后自问：遗漏/走偏/技术债？
5. **Repair on Failure** — 验证失败不是结束，是修复的起点
