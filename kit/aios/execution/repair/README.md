# Repair — 修复环

验证失败时：保存日志 → 回滚 checkpoint → 分析根因 → 重执行。

最多重复 3 次；3 次仍失败 → block 任务 + 通知人工。

→ 完整规范见 [aios/execution/engine.md](../engine.md) §6
