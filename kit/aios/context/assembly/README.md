# assembly — 装配入口

`python3 kit/cli/context TASK-xxx [--budget N]` → 路径/行数/预算占比三列清单；
autoloop coder/reviewer prompt 自动注入装配清单（fail-open）。
实现：[kit/cli/lib/context_loader.py](../../../cli/lib/context_loader.py)（TASK-094）
