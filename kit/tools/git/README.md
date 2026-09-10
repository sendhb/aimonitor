# tools/git

> 见 [tools/README.md](../README.md)

> **处置（TASK-098）**：由宿主 CLI 承担——git 本体即接口（add/commit/push、`git diff --stat`、
> `git stash`，接口约定见 tools/README 接口列），kit 不另封 wrapper。
> 框架侧补充：仓库级保护由 `cli/protect`（generated_dirs OS 只读锁）承担；checkpoint 语义由
> tasklib + git 实现（`kit/cli/lib/tasklib.py`）。
