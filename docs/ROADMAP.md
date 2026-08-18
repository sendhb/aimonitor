# ROADMAP — aimonitor 路线图（L2 执行层）

> 唯一权威：本文件（`docs/ROADMAP.md`），由 Manager 唯一写盘。
> 阶段是"方向航图"，执行一律以 `runtime/tasks/` 的 TASK 为准。
> 治理规则见 `kit/aios/governance/roadmap-policy.md`。

| 阶段 | 状态 | 目标 | owner | 对应 TASK | 完成标志 |
|------|------|------|-------|-----------|----------|
| Phase 1: 基础监控 | done | 本地 AIOS 工程监控：注册表/采集/API/前端/沙箱/趋势/告警/主题/响应式/并发锁 | hb-session | TASK-001..030 | 30/30 done，`/api/status` 全绿 |
| Phase 2: 多机监控-规格 | done | MONITOR-SPEC Agent 推送模式（transport/ingest 契约/鉴权/心跳/部署拓扑/多实例语义）+ ADR + ROADMAP | architect | TASK-031, 032 | 规格复审 pass |
| Phase 3: 多机监控-服务端 | open | ingest API + token 鉴权 + 采集层 FileReader 抽象（local/agent）+ 心跳/历史兼容，现有本地项目零回归 | coder | TASK-033..041 | server_smoke 全绿 + local 回归通过 |
| Phase 4: 多机监控-集成 | open | agent→ingest 集成测试 + 前端实例级展示 | coder | TASK-042, 043 | 推送与 local 模式结果一致 |
| Phase 5: 多机监控-部署 | open | README / 配置示例 / 运维手册；Windows(WSL2)+Linux 双机验证 | coder | TASK-044, 045 | 双机真实推送验证通过 |

## 变更记录

- 2026-08-15：Phase 1 标记 done（30/30）；新增 Phase 2-5（Agent 推送模式，多机监控）。提议来源：架构分析（多平台工程监控：Windows + 远程 Linux）；人工确认：hb（P1 级阶段变更）。Manager 落盘。
- 2026-08-16：Phase 2 标记 done（TASK-031/032 规格复审 pass）；Phase 3-5 细化为 TASK-033..045（10 分钟 commit 粒度）。Manager 落盘。
