# tools/shell

> 见 [tools/README.md](../README.md)

> **处置（TASK-098）**：由宿主 CLI 承担——宿主 shell 工具即实现入口（bash <command>）。
> 框架侧纪律：确定性验证 `cli/task verify`（build/lint/test/check 四道）；
> 需要无网络隔离的执行用 `cli/sandbox-run`（见 [sandbox/](../sandbox/README.md)，Rule of Two）。
