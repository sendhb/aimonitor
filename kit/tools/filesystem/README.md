# tools/filesystem

> 见 [tools/README.md](../README.md)

> **处置（TASK-098）**：由宿主 CLI 承担——宿主原生工具（read/write/edit/ls/glob）即实现入口，
> 接口约定见 tools/README。框架侧的机械约束是 `aios.config.yaml` 的
> `source_dirs`/`generated_dirs`（context_loader Filter 排除与 reflect 越界检查机械生效）。
