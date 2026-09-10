# compression — Compress 策略（宿主承担）

选型而非缺口：历史/会话压缩由宿主 CLI 承担（pi `/compact` 等），kit 不实现摘要压缩。
kit 侧对应能力是 Select 的预算截断（见 [../README.md](../README.md) 五策略映射表）。
