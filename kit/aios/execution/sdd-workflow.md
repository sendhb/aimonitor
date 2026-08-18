# SDD Workflow — 规格驱动开发流程

> **适用对象：涉及外部契约、业务规格、生成源或跨端协议变更的任务。**
> 启动分流由 [`engine.md`](engine.md) §0 执行；命中后加载本文件按对应形态操作。
>
> **与 engine.md 闭环的关系**：本文件的规格变更场景(Phase1 规格层)嵌入 engine.md 的 SDD 分支，详见 engine.md 的[闭环全景图](engine.md)和[阶段产物表](engine.md)。完成后汇入 engine.md 的 §4-§6(Reflect/Verify/Repair)。
>
> 所有 `config.xxx` 引用指向项目根目录的 `aios.config.yaml`；profile 提供可复制的 `config.template.yaml`。

---

## 核心约束（任何场景都不得违反）

| 约束 | 说明 |
|------|------|
| 规格是唯一真相 | contract 型：`config.spec.file`；docs 型：`config.docs.domains` + `config.docs.flows`；protocol 型：`config.docs.protocol` |
| 生成代码不可手动编辑 | `config.generated_dirs` 只能由生成器写入 |
| 规范变更先于实现 | 新增功能/改契约：先改规格 → 再生成(如有) → 再实现 |
| 验证是流程的一部分 | 每次变更以 `config.commands.check` 通过作为完成标志 |
| 任务可追踪 | 每个开发项对应一个 TASK，状态由 `cli/task` 管理 |

---

## 项目形态判断（开工前）

```
config.project.kind？
    ├── api-service（spec.type: contract）→ 规格 = 中央契约，有代码生成
    ├── tool-software / game-client（spec.type: docs）→ 规格 = 文档，无代码生成
    └── game-network（spec.type: protocol）→ 规格 = 协议文档（客户端+服务器共同契约）
```

---

## 场景判断

```
任务是否涉及规格变更（路径/参数/功能/消息定义）？
    ├── 是 → contract 型走 [场景 A]；docs 型走 [场景 A']；protocol 型走 [场景 A"]
    └── 否 → 只改实现逻辑？
              ├── 是 → 走 [场景 B：仅逻辑修改]
              └── 修复 bug？ → 走 [场景 C：Bug 修复]
```

---

## 场景 A/A'/A" — 规格变更（共享步骤 + 形态差异表）

以下 11 步为三种形态的共享骨架；差异部分（规格来源、校验方式等）见下方[形态差异表](#形态差异表)。

```
0. 确认任务已创建（cli/task list 中有对应 TASK）

1. 阅读现有规格 — 来源见[形态差异表]

2. 更新规格（先于实现）— 目标见[形态差异表]

3. 校验规格 — 方式见[形态差异表]
   （contract 型：校验通过后执行 generate，产出入 generated_dirs）

4. 实现业务逻辑 — 只改 config.source_dirs

5. 编译验证 — config.commands.build

6. 合规验证 — config.commands.check

7. 形态专属验证 — 见[形态差异表]

8. 记录验证结果 — runtime/verification/VERIFY-xxx.md

9. 审查（任务含 reviewer 或需要时）
   cli/task review TASK-xxx          # in-progress → in-review
   有缺陷 → cli/task start TASK-xxx   # 返工（in-review → in-progress）

10. 关闭任务
    cli/task approve TASK-xxx（走审查）或 cli/task done TASK-xxx
```

### 形态差异表

| 维度 | contract (A) | docs (A') | protocol (A") |
|------|-------------|-----------|---------------|
| **规格来源** | `config.spec.file` | `config.docs.domains` + `config.docs.flows` | `config.docs.protocol` + `config.docs.domains` |
| **更新目标** | 契约文件 | `docs/domains/`, `docs/flows/` | `docs/protocol/` |
| **校验方式** | `config.commands.validate_spec` | 文档审查(逐域/逐流程) | 协议审查(逐消息/序列) |
| **重新生成** | `config.commands.generate`(入 `generated_dirs`) | 无 | generate(如有 schema，入 `generated_dirs`) |
| **专属验证** | — | 按交互流程功能清单 + 截图 | 联调(CS 消息收发验证) |

---

## 场景 B — 仅逻辑修改（不改规格）

1. 影响分析：对照 `knowledge/modules/` 确认修改波及的调用方和下游模块（→ engine §2 Impact）

2. 直接修改 `config.source_dirs` 中的实现

3. 编译验证 — `config.commands.build`

4. 合规验证 — `config.commands.check`

5. 记录验证结果 — `runtime/verification/VERIFY-xxx.md`

**不需要**重新生成代码。完成后回到 engine §4 Reflect 自省。

---

## 场景 C — Bug 修复

1. 确认 bug 位于实现层（不是规格问题）

2. 影响分析：此修复是否改变调用方行为或边界语义？（→ engine §2 Impact）

3. 只修改允许编辑的目录：
   ✅ `config.source_dirs`
   ❌ `config.generated_dirs`

4. 编译验证 — `config.commands.build`

5. 合规验证 — `config.commands.check`

6. 记录验证结果 — `runtime/verification/VERIFY-xxx.md`

如果 bug 根因在规格与实现不一致 → 升级为场景 A/A'/A"。完成后回到 engine §4 Reflect 自省。

---

## 完成标准

任何任务，满足以下**全部**条件才算完成：

- [ ] `config.commands.build` 零错误
- [ ] `config.commands.check` 全部通过
- [ ] 未手动编辑任何 `config.generated_dirs`
- [ ] 规格已先于实现更新（形态差异见[形态差异表](#形态差异表)）
- [ ] `config.commands.generate` 已执行（contract/protocol 型需要时）
- [ ] 验证结果已记录到 `runtime/verification/`
- [ ] TASK 状态已流转为 `done`（`cli/task done TASK-xxx`）

---

## 相关文件

- [`../policy/principles.md`](../policy/principles.md) — 通用原则
- [`aios/governance/task-policy.md`](../governance/task-policy.md) — 任务生命周期
- [`runtime/verification/VERIFY_WORKFLOW.md`](../../runtime/verification/VERIFY_WORKFLOW.md) — 验证记录规范
- `profiles/<type>/config.yaml` — 项目专属命令与路径
