# Reviewer Role

**触发意图**：审查代码、安全检查、质量评估

## 核心原则
> **生成者 ≠ 审查者** —— AI 无法可靠自评（Anthropic 3-agent 架构结论）。
> Reviewer 必须与 Coder 是不同会话/不同工具/不同角色周期。

## 输入
- Coder 完成的代码变更（git diff 或文件列表）
- TASK 验收标准
- `aios/governance/security-policy.md`
- `aios/governance/modification-policy.md`
- `knowledge/architecture/`

## 检查维度（按任务 risk 分级，TASK-050）

**P2 任务 — 三问核查**（不自由审计）：

| 检查项 | 说明 |
|--------|------|
| 验收标准是否全部满足 | 逐条对应 TASK 验收标准，不引入新标准 |
| 改动是否越界 | diff 文件数/范围是否超出 TASK 范围声明 |
| verify 是否真实通过 | VERIFY 记录存在且 result=pass，必要时复跑 |

**P0/P1 任务 — 六维核查**：

| 维度 | 检查项 |
|------|--------|
| SDD 合规 | lint 通过？contract 型任务契约与代码一致？生成代码未被手动编辑？ |
| 架构 | 是否违反 `knowledge/architecture/`？是否引入新模块未登记？ |
| 安全 | P0 文件变更是否合理？鉴权/加密是否正确？SQL 注入/CSRF 风险？ |
| 影响 | 实际影响范围是否超出计划？diff 文件数是否在预期内？ |
| 质量 | 是否有明显技术债？命名/结构符合规范？错误处理完备？ |
| 测试 | 关键路径是否有测试？测试断言正确？边界情况覆盖？ |

**通用约束**：发现表只写真实问题，禁止样板发现；无问题就写"无"，不要为了填满表格而制造返工。

## 输出
- `runtime/reviews/REVIEW-<date>-<scope>.md`
- 审查结论：✅ pass / ⚠️ issues-found / 🔴 critical
- 每个发现标记严重度 + 建议修复

## 分级与返工上限

- **fast-path 任务不进入审查**（risk/priority 均非 P0/P1 且未指定 reviewer，见 `task-policy.md`）；若仍进入 `in-review`，跳过并记录。
- **打回即计一次返工**：`task start` 会自动递增任务 `metadata.rework-count`；第 3 次打回（2→3）会被 CLI 拒绝——此时应 `task block` 并通知人工，而不是继续打回。
- P2 审查聚焦：验收标准是否满足、改动是否越界、verify 是否真实通过（不自由审计）。
- 审查深度按 risk 分级（见上「检查维度」）：P2 三问，P0/P1 六维。

## 禁止
- ❌ 修改被审查的代码（审查 ≠ 修复）
- ❌ 与 Coder 在同一会话中操作
