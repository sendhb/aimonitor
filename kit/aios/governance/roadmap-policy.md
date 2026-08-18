# Roadmap Policy — 项目路线图治理

> Roadmap 是"方向航图"，TASK 是"执行单元"。Roadmap **不进入任务状态机**。
> 任务状态机见 [`task-policy.md`](task-policy.md)。

## 分层

| 层 | 内容 | 决策者（拍板方向） | 写入者（落盘） |
|----|------|-------------------|---------------|
| **L1 方向层** | 产品/战略/技术演进路线（可不在本仓库：会议、文档、Issue） | 人类用户、产品、**Tech Lead、Architect** | 各自载体 |
| **L2 执行层** | `docs/ROADMAP.md`：阶段 + owner + TASK 范围 | 阶段 owner | **Manager（唯一写盘者）** |

> **核心区分**：Manager 是唯一**写盘者**，不是唯一**决策者**。
> 技术规划由 Tech Lead / Architect 发起，Manager 负责落盘为阶段并拆 TASK。

## 位置

- L2 唯一权威：`docs/ROADMAP.md`（项目根目录）
- 治理规则本体：本文件（`aios/governance/roadmap-policy.md`，跟随 kit 分发）

## 使用者

| 角色 | 动作 |
|------|------|
| 人类用户 / 产品 | 阅读方向、批准阶段调整 |
| **Tech Lead / Architect** | **发起技术规划阶段**（架构演进、技术栈迁移、技术债消除）→ 提议给 Manager 落盘 |
| **Manager** | 唯一写盘者：把提议落成 ROADMAP.md 阶段；维护阶段与 TASK 对应 |
| Analyst | 读：做影响分析时对齐阶段目标 |
| Coder | 读：了解所处阶段（**执行以 TASK 为准**） |
| Reviewer | 读：审查 TASK 是否偏离 roadmap |

## 写入规则（single writer）

- **Manager 是唯一写盘者**：`docs/ROADMAP.md` 的写入/更新只能由 Manager 执行（防并发冲突）
- **Manager 不是唯一决策者**：阶段内容由来源提议——
  - 技术规划 → Tech Lead / Architect 提议
  - 产品方向 → 人类用户 / 产品提议
- **流程**：`提议（任何来源）→ 人类确认（P0/P1 阶段）→ Manager 落盘 → 拆 TASK`
- 任何阶段变更必须**同时**创建/调整对应 TASK —— roadmap 与 task 不得脱节

## 规则

1. Roadmap 是"方向"不是"承诺"—— AI 执行一律以 TASK 为准，roadmap 只做背景参考
2. 技术细节不进 roadmap → 放 `knowledge/architecture/` 或 `knowledge/decisions/`（ADR）
3. 阶段完成 → 标记 `[done]`，不删除（保留历史）
4. 变更控制：P0/P1 阶段变更需人工确认（对照 [`risk-policy.md`](risk-policy.md)）
5. `docs/ROADMAP.md` 是项目特有内容，**不得放入 kit 的 docs/ 当模板**（会被 `cli/init` 整目录复制污染新工程）
6. ROADMAP.md 阶段表必须含 **owner** 列（该阶段由谁负责推进）

## ROADMAP.md 阶段表模板

```markdown
| 阶段 | 状态 | 目标 | owner | 对应 TASK | 完成标志 |
|------|------|------|-------|-----------|----------|
| Phase 1: 契约与规格 | in-progress | 定义核心契约 | tech-lead | TASK-001, 002 | 规格评审通过 |
| Phase 2: 核心实现 | open | 后端+前端 | coder-lead | TASK-003..005 | check 全绿 |
| Phase 3: 技术债清理 | open | 消除 X 模块债 | architect | TASK-008 | ADR 落地 |
```

## 关系

```
L1 方向层（产品/技术/战略）─── 提议 ───┐
                                     ▼
                    L2 docs/ROADMAP.md（阶段 + owner）
                        │ Manager 唯一写盘
                        ▼
            runtime/tasks/TASK-xxx.md（执行）
                        │
        Architect ADR / knowledge/ 提供技术约束
```

## 相关文件

- [`task-policy.md`](task-policy.md) — 任务生命周期与状态机
- [`risk-policy.md`](risk-policy.md) — 风险分级与审批
- `docs/ROADMAP.md` — 项目路线图内容（各项目自建）
