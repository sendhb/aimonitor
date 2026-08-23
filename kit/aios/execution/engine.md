# Execution Engine - Reliable AI Loop

> **前置知识**:`config.commands` 等引用指向项目配置中的命令定义。
> 配置规范见 `profiles/README.md`--每个项目类型(backend/game-server/unity/...)有自己的 `config.yaml`,
> 定义 `commands.build`、`commands.test`、`commands.lint`、`commands.check`、`generated_dirs` 等。

## 闭环全景

```
                   ┌──────────────────────┐
                   │      Task 进入        │
                   └──────────┬───────────┘
                              │
                              ▼
                   ┌──────────────────────┐
                   │ §0 Workflow Selection │
                   │ 改契约/规格/生成源    │
                   │ 或跨端协议?          │
                   └────┬──────────┬──────┘
                    是  │          │  否
                        ▼          ▼
        ┌───────────────────┐  ┌──────────────┐
        │ SDD 分支          │  │ 通用路径     │
        │ Phase1 规格层     │  │ §1 Plan      │
        │  改规格→校验→生成 │  │ §2 Impact    │
        │  失败≤3次→block   │  │ §3 Execute   │
        │ Phase2 代码层     │  └──────┬───────┘
        │  实现→build→check │         │
        └────────┬──────────┘         │
                 └────────┬───────────┘
                          │
                          ▼
               ┌──────────────────┐
               │ §4 Reflect       │
               └────────┬─────────┘
                        │
                        ▼
               ┌──────────────────┐
               │ §5 Verify        │
               └────────┬─────────┘
                        │
                 失败   │   通过
               ┌────────┴─────────┐
               ▼                  ▼
      ┌──────────────┐   ┌──────────────┐
      │ §6 Repair    │   │ Done         │
      │ 分析→重试    │   │ VERIFY→关闭  │
      │ ≤3次→block   │   └──────────────┘
      └──────┬───────┘
             │ 修复后 → §3 Execute
```

---

## 0. Workflow Selection - 启动分流

在规划前判断:任务是否改变外部契约、业务规格、生成源或跨端协议?

- **是**:走 SDD 分支(Phase1 规格层 → Phase2 代码层),细节见 [`sdd-workflow.md`](sdd-workflow.md) 的共享步骤与形态差异表。
- **否**：走通用路径，直接执行本文件 §1-§6 全部阶段。适用于内部逻辑修改、Bug 修复、测试、性能优化和不改变既有行为的重构。此时也必须走 Impact(改了哪些调用方?)和 Verify(build + test + check)，不可省略。
- **不确定**：按"是"处理，并在 TASK 的计划中记录待确认假设。

> **分级治理（fast-path）**：
> 低风险任务可跳过部分仪式，判定规则与流程见 [`governance/task-policy.md`](../governance/task-policy.md) 的"分级治理（fast-path）"。
> 要点：**fast-path 只跳过 Impact 文档与独立 reviewer 会话，verify 与 TASK 状态流转不可省略**；
> 机械规则：risk/priority 均非 P0/P1 且 reviewer 未指定 → fast-path。
> 简单任务（scope < 3 文件、无 P0 文件）可直接执行，无需显式计划（见 §1）。

---

## 阶段产物表

> 每一步"读什么、产出什么、写在哪个文件"。

| 阶段 | 输入 | 产出 | 记录位置 |
|------|------|------|----------|
| **Workflow Selection (§0)** | TASK 描述, `config.project.kind` | 分流决策(SDD / 通用) | TASK `## 计划` 段 |
| **Plan (§1)** | 规格/契约(如 SDD), `knowledge/` 模块文档 | 可执行计划(步骤/文件/预期产物) | TASK `## 计划` 段 |
| **Impact (§2)** | 计划修改的文件列表, `knowledge/modules/` | 受影响模块清单 + 风险等级（fast-path 任务可跳过） | TASK 或 impact 记录 |
| **SDD Phase1** | 现有规格(contract/docs/protocol) | 更新后规格 + 校验结果 + (重新生成的代码) | 规格文件, 生成代码入 `generated_dirs` |
| **Execute (§3)** | 计划步骤 + 规格 | 修改后的源码 | Git commit |
| **Reflect (§4)** | 修改 diff + impact 分析 | 自省清单(遗漏/架构违规/技术债) | TASK 备注 或 `knowledge/history/` |
| **Verify (§5)** | 修改后的代码 | build/lint/test/check 结果 | `runtime/verification/VERIFY-xxx.md` |
| **Repair (§6)** | 失败日志 | 根因分析 + 修复方案 | `runtime/logs/fail-<date>.log`, TASK 备注 |

---

## 1. Planner - 计划先行

**原则:Plan First, Code Later。**

在执行前,AI 必须:
- **规格变更任务**:先阅读契约(`config.spec.file`)、业务规则(`config.docs.domains/`)、交互流程(`config.docs.flows/`)
- **纯逻辑/修复任务**:阅读 `knowledge/` 中的架构/模块/依赖/决策记录
- 分析 impact(见下)
- 输出可执行计划(步骤/文件/预期产物)
- 计划写入 TASK 的 `## 计划` 段

**简单任务(TASK scope < 3 文件、无 P0 文件)可直接执行,无需显式计划。**

**fast-path 任务**（判定见 task-policy 分级治理）：跳过 Impact 文档与独立 reviewer 会话，实现后 verify 通过即 `task done`。

**反返工要求**：验收标准必须具体可验证；一个任务一个可独立验证的交付（详见 task-policy）。

---

## 2. Impact - 影响分析

**原则:改 X 之前算清楚 Y。**

修改前分析受影响模块:

```python
# 伪代码
impact = analyze_impact(modified_files, knowledge/modules/*.md)
print(impact.affected_modules)  # 如: [Player, Save, Shop, Quest]
print(impact.risk_level)        # 如: P1
```

发现未预见的依赖 → 触发任务依赖创建。

---

## 3. Executor - 按计划执行

### 通用规则

1. 创建 Git checkpoint(commit 或 stash)
2. 逐步执行计划(每步验证 → 通过才下一步)
3. 遵守 modification-policy(跳过高风险文件)

### SDD 任务:两阶段执行

规格变更任务按**规格先于代码**原则分两阶段。步骤细节见上方[阶段产物表](#阶段产物表)的"SDD Phase1"和"Execute"行,以及 [`sdd-workflow.md`](sdd-workflow.md) 的共享步骤 + 形态差异表。

关键规则:Phase1 规格层失败最多退回重试 3 次,超出则 block 任务并通知人工。

非 SDD 任务(逻辑修改/Bug 修复)直接进入 Execute,但仍须走 §2 Impact 和 §5 Verify。

---

## 4. Reflection - 自省

**执行完成后,AI 必须回答:**

**通用自省(所有任务):**
- [ ] 是否有遗漏的模块?(对照 impact 分析结果)
- [ ] 是否违反了架构约定?(对照 `knowledge/architecture/`)
- [ ] 是否引入了技术债?("只是为了让它跑起来"的代码)
- [ ] 测试是否覆盖了关键路径?
- [ ] 错误处理是否完整?

**SDD 任务额外自省(规格变更):**
- [ ] 实现是否与更新后的规格逐项对齐?(逐字段/逐端点/逐消息对照)
- [ ] 是否有规格写了但未实现的端点/字段/消息?
- [ ] 是否有实现做了但规格未记录的行为?(如有 → 回补规格)

发现任何问题 → 创建 TASK 或标记为已知债(TODO 注释 + `knowledge/history/` 记录)

---

## 5. Verify - 验证

**机械强制(deterministic feedback),agent 无法跳过:**

| 检查 | 命令 | 失败后果 |
|------|------|---------|
| 编译/构建 | `config.commands.build` | 回滚 → 修复 |
| lint | `config.commands.lint` | 自动修复 |
| 测试 | `config.commands.test` | 回滚 → 修复 |
| 合规 | `config.commands.check` | 回滚 → 修复 |

跑 `cli/task verify TASK-xxx`:读 `aios.config.yaml` 真实执行上述四条命令,全部通过才自动写 `runtime/verification/VERIFY-xxx.md`;任何一步失败直接 die 并把完整输出记到 `runtime/logs/fail-<date>.log`,不生成记录。
不要手写 VERIFY 文件冒充已验证--`cli/task done/approve` 只检查记录格式,无法分辨记录是真跑出来的还是手写的,这道防线只在使用 `cli/task verify` 时才成立。

---

## 6. Repair — 修复环

验证失败时:

1. 保存失败日志到 `runtime/logs/fail-<date>.log`
2. 回滚前先取得人工批准;不得自动执行破坏性回滚
3. 分析根因(`reflection` 追问)
4. 修改计划 → 重新执行
5. **返工上限（机械强制）**：打回由 `task start` 自动累计 `metadata.rework-count`，最多 2 次自动返工；第 3 次打回（2→3）被拒绝，必须人工介入（拆分任务或人工批准继续）。`autoloop-coder` 捡到 `rework-count ≥ 3` 的任务直接 block，不再自动实现。
