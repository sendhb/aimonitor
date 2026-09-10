# loading — Load 策略（懒加载 / 渐进披露）

- 入口清单：`python3 kit/cli/context TASK-xxx`（预算内最小文件集，只给路径/行数/理由，不内联全文）
- 按需展开：AGENTS.md → governance → 涉及模块；先清单后详情
- 实现：[kit/cli/lib/context_loader.py](../../../cli/lib/context_loader.py)（TASK-094）
