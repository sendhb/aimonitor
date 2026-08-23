# Autoloop Boot — 无人值守会话速查

> **用途**：autoloop-coder / autoloop-reviewer 的无人值守会话专用速查（TASK-050）。
> 压缩自框架规范；**与原始文档冲突时以原始文档为准**。原始规范：
> `AGENTS.md` → `kit/aios/governance/*.md`、`kit/aios/execution/engine.md`、`kit/aios/execution/sdd-workflow.md`、`kit/agents/<role>/role.md`、`kit/aios/policy/principles.md`。
> 本会话只读本文件 + TASK + 相关模块；命中规格变更等场景才展开对应原始文档（最小读取，TASK-047 #6）。

## 1. 会话任务

- 从 `runtime/tasks/TASK-xxx.md` 读目标/范围/验收标准。
- **最小读取**：只读 TASK + 涉及模块；禁止全仓扫描、禁止 knowledge/ 通读；需要时按需展开。

## 2. 状态机（cli/task 管理）

```
open → in-progress → in-review → done
  │        │              ↑（返工）
  └→ blocked → in-progress┘
```

调用：`./kit/cli/task <start|verify|review|approve|done|block|unblock|cancel> TASK-xxx`。
⚠️ task 是 Python 3：用 `./kit/cli/task` 或 `python3 kit/cli/task`，**勿用 bash kit/cli/task**（巨量输出死循环）。

## 3. 关闭前置条件

1. 验收标准全部勾选
2. VERIFY 记录存在——**必须由 `task verify` 真实执行生成，禁止手写冒充**
3. P0/P1、指定 reviewer、或进入 in-review 的任务 → 必须有 REVIEW 记录且 reviewer ≠ 实现者
4. P0 → `approval-ref` 必填

## 4. 分级治理（fast-path）

| 条件 | 路径 |
|------|------|
| risk/priority 均非 P0/P1 **且** reviewer 未指定（any/空） | **fast-path**：verify → `task done`（无 review、无 Impact 文档） |
| 其余（P0/P1 或指定 reviewer） | 完整路径：verify → `task review` → `task approve` |

**verify（build/lint/test/check + VERIFY 记录）对任何任务不可省略。**

## 5. 返工上限

- `metadata.rework-count` 打回自动累计（`task start` 在 in-review→in-progress 时 +1）。
- 0→1、1→2 允许；**2→3 被拒绝**（需人工介入：拆分任务或调整 rework-count）。
- rework ≥ 3：不自动实现，`task block` 升级人工。

## 6. 修改权限

- ✅ 只改 `aios.config.yaml` 的 `source_dirs`；❌ 绝不手动改 `generated_dirs`（OS 只读锁）。
- 🔴 P0 文件（db schema/支付/账号/权限）→ 人工批准，否则 `task block`。
- 🟡 P1 文件（API 契约/共享类型/CI-CD）→ TASK 声明。
- 风险升级：改动 >5 文件 / 跨 >2 模块 / 多语言 / 有未解阻塞依赖 → 升一级。

## 7. 执行闭环（极简）

`Plan → Impact（fast-path 可省）→ Execute → Reflect → Verify → Repair（≤2 次返工）`
规格变更任务：规格先于实现（sdd-workflow），生成代码不可手编。

## 8. Coder 职责（autoloop-coder）

1. 读 boot + TASK + 相关模块，制定计划
2. 实现（只改 source_dirs）
3. `./kit/cli/task verify TASK-xxx`（真实执行，不手写 VERIFY）
4. 通过后：fast-path → `task done`；完整路径 → `task review`
5. 完整路径任务**不自行 approve/done**（生成者 ≠ 审查者）
6. 需动 P0 文件 → `task block` 等人工，不继续实现

## 9. Reviewer 职责（autoloop-reviewer）

1. 捡 `in-review` 且 assignee ≠ 自己的任务
2. 读 boot + TASK + diff；**禁止修改被审查代码**（审查 ≠ 修复）
3. 分级审查（见 REVIEW.template.md）：
   - **P2 → 三问**：验收标准满足？改动越界？verify 真实通过？（不自由审计）
   - **P0/P1 → 六维**：SDD 合规/架构/安全/影响/质量/测试
   - 发现只写真实问题，**禁样板发现**；无问题写"无"
4. 通过 → `task approve`；需返工 → 写 REVIEW 后 `task start`（自动计 rework；2→3 被拒则 `task block`）
5. P0 无 approval-ref → `task block`，不绕过

## 10. 安全红线

- 密钥/密码/API Key 不进代码、提示词、配置（用 `.env` + gitignore）。
- 敏感数据不进 prompt 上下文。
- Rule of Two：不可信输入/敏感数据/状态修改 三能力同时具备两项以上 → 第三项人工审批。
- 生成代码必须过 `config.commands.lint`。

## 11. 常见坑

- `task` 是 Python 3，勿用 bash 调用（见 §2）。
- 不手写 VERIFY/REVIEW 冒充证据（`task verify` / reviewer 会话才生成）。
- 生成代码改了规格不生效 → 改规格 → `config.commands.generate` 重新生成。
