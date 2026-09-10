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

采集已接入 `cli/task` 的天然落盘点（TASK-093，单源在 `cli/lib/tasklib.py`）：

1. 任务 closed（→ done）时自动追加一条 `metrics/task-metrics.jsonl`：
   `task/risk/priority/rework-count/created/updated/duration_days/done_at`
   （`task done` 与 `task approve` 两条关闭路径都触发，采集异常只告警不阻塞）
2. 验证失败时自动追加一条 `failures/failures.jsonl`：
   `task/date/failed_command/fail_log`（`fail_log` 指向 `runtime/logs/fail-<date>.log`）
3. `task metrics [--days N]` 汇总查询：done 率（runtime/tasks 实时口径）、
   平均 rework-count（优先 metrics 采集数据，无数据回退实时 frontmatter）、
   token 估算合计（聚合 `runtime/logs/token-usage.jsonl`；无数据输出 N/A 不报错）
4. 定期生成 `reports/`（周/月），对比模型版本、工作量趋势
5. `benchmarks/` 存放可复现的 AI 能力基准测试（如：同一个任务用 Claude Sonnet vs GPT-4o 对比）

人工补充的定性评估（模型名、审查发现数等）仍走 `reports/` 模板；
自动采集只负责机器可聚合的客观数据。

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
