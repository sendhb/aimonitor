# Executor — 按计划执行

按 Plan 逐步执行：每步先建 Git checkpoint → 执行 → 验证通过才下一步。

遵守 [modification-policy](../../governance/modification-policy.md)（禁写 `generated_dirs`，P0 文件需批准）。

→ 完整规范见 [aios/execution/engine.md](../engine.md) §3
