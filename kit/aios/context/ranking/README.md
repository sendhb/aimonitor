# ranking — 装配优先级（超预算截断顺序）

TASK 本体（受保护，永不截断）> depends-on TASK > checkpoint > 模块文档 > 源文件（diff 优先于提及）。
被截断文件显式标注 `[TRUNCATED 到第 N 行]`。
实现：[kit/cli/lib/context_loader.py](../../../cli/lib/context_loader.py)（TASK-094）
