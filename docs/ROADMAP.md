# ROADMAP — aimonitor 路线图（L2 执行层）

> 唯一权威：本文件（`docs/ROADMAP.md`），由 Manager 唯一写盘。
> 阶段是"方向航图"，执行一律以 `runtime/tasks/` 的 TASK 为准。
> 治理规则见 `kit/aios/governance/roadmap-policy.md`。

| 阶段 | 状态 | 目标 | owner | 对应 TASK | 完成标志 |
|------|------|------|-------|-----------|----------|
| Phase 1: 基础监控 | done | 本地 AIOS 工程监控：注册表/采集/API/前端/沙箱/趋势/告警/主题/响应式/并发锁 | hb-session | TASK-001..030 | 30/30 done，`/api/status` 全绿 |
| Phase 2: 多机监控-规格 | done | MONITOR-SPEC Agent 推送模式（transport/ingest 契约/鉴权/心跳/部署拓扑/多实例语义）+ ADR + ROADMAP | architect | TASK-031, 032 | 规格复审 pass |
| Phase 3: 多机监控-服务端 | done | ingest API + token 鉴权 + 采集层 FileReader 抽象（local/agent）+ 心跳/历史兼容，现有本地项目零回归 | coder | TASK-033..041 | server_smoke 全绿 + local 回归通过 |
| Phase 4: 多机监控-集成 | done | agent→ingest 集成测试 + 前端实例级展示 | coder | TASK-042, 043 | 推送与 local 模式结果一致 |
| Phase 5: 多机监控-部署 | done | README / 配置示例 / 运维手册；Windows(WSL2)+Linux 双机验证 | coder | TASK-044, 045 | 双机真实推送验证通过 |
| Phase 6: 注册审批 | done | 注册-审批-签发全链路：RegistrationStore + enrollment_code + admin-auth + 注册/轮询/审批/吊销/轮换端点 + 前端 UI + 测试 + 文档 | autoloop-coder | TASK-046..057, 060, 062, 063 | 全链路测试 pass + 文档完整 |
| Phase 7: 注册审批-收尾 | in-review | 审批通过自动登记 projects.json + 安全加固（挂起） | any | TASK-064, 065, 067, 068, 069 | TASK-069 in-review 待联测；TASK-064 blocked 待广域网 |
| Phase 8: 增强 | done | 清理 + 事件流 + webhook 告警通知 | any | TASK-070, 071, 072 | TASK-070 config 清理；TASK-071 事件流入库查询展示；TASK-072 webhook 4 规则触发验证 |

## 变更记录

- 2026-08-15：Phase 1 标记 done（30/30）；新增 Phase 2-5（Agent 推送模式，多机监控）。提议来源：架构分析（多平台工程监控：Windows + 远程 Linux）；人工确认：hb（P1 级阶段变更）。Manager 落盘。
- 2026-08-16：Phase 2 标记 done（TASK-031/032 规格复审 pass）；Phase 3-5 细化为 TASK-033..045（10 分钟 commit 粒度）。Manager 落盘。
- 2026-08-24：Phase 3-5 标记 done（TASK-033..045 全链路验证通过）；新增 Phase 6（注册审批全链路 TASK-046..063）+ Phase 7（收尾 TASK-064..069）+ Phase 8（增强 TASK-070..072）。Manager 落盘。
