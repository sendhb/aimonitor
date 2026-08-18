# evaluation/ — AI 质量体系

> AI 的工作也需要被评估。不评估 = 不知道 AI 做得好不好。

## 评估维度

| 维度 | 指标 | 记录位置 |
|------|------|---------|
| **任务成功率** | 完成任务数 / 总分配任务数；done 率；cancelled 率 | `metrics/` |
| **返工次数** | in-review → in-progress 返工次数 / 任务 | `failures/` |
| **Bug 率** | 审查发现的问题数 / 变更文件数 | `metrics/` |
| **Token 消耗** | 平均 token/任务；超额 token（重复执行） | `metrics/` |
| **耗时** | 平均 wall time / 任务 | `reports/` |
| **模型表现** | 按模型分类的成功率/失败原因 | `benchmarks/` |

## 使用方法

1. 每个任务 closed 时自动记录 `metrics/`（通过 `cli/task index` 或 CI）
2. 每次验证失败自动记录 `failures/`（`aios/execution/engine.md` 的 Repair 环）
3. 定期生成 `reports/`（周/月），对比模型版本、工作量趋势
4. `benchmarks/` 存放可复现的 AI 能力基准测试（如：同一个任务用 Claude Sonnet vs GPT-4o 对比）

## 模板

```markdown
# Task Metrics — TASK-001

| 指标 | 值 |
|------|-----|
| 分配 agent | pi |
| 模型 | Claude Sonnet 4 |
| 返工次数 | 1 |
| 审查发现 | 3 个问题（2 质量 + 1 安全） |
| Token | ~45K |
| 耗时 | 12 分钟 |
| 结果 | done |
```
