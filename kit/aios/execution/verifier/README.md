# Verifier — 验证

机械强制检查：编译 build → lint → test → check。全部通过才允许 done。

跑 `cli/task verify TASK-xxx`：真实执行 build/lint/test/check,全部通过才自动写 `runtime/verification/VERIFY-xxx.md`；不要手写这份记录冒充已验证。

→ 完整规范见 [aios/execution/engine.md](../engine.md) §5
