# ADR-003 — 注册-审批-签发：自助注册 + 管理员审批 + 动态 token 签发

> 关联 TASK-046（规格：docs/MONITOR-SPEC.md §3.2）
> 日期：2026-08-18

## 背景

当前，新增一台被监控机器需要**手工编辑三处**：服务端 `config/projects.json` 注册项目、
`config/agents.json` 预置 token、客户端 `agent.json` 配置 token。这在小规模下可行，
但每增加一台机器就多一次人工操作，且 token 的生成/分发/存储完全依赖人工。

候选方案：
- A) 维持现状，继续手工编辑三处配置
- B) 注册码即签：管理员预生成注册码，agent 一次请求即签发 token（无需审批队列）
- C) 申请-审批全流程：agent 自助注册 → 管理员页面审批 → 签发 token

## 决策

- **采用 C — 申请-审批全流程**
- **注册码（enrollment code）作为可选预授权信任锚**，有码的申请标记 🔵 预授权，无码标记 🟡 盲申请
- **管理员认证简化版**（局域网适用）：`config/admin.json` 放固定密码，无需 token 生命周期
- **HTTPS 安全加固挂起**，创建占位 TASK，待迁广域网时激活
- **Agent 端代码在 aibase 仓库实现**（`kit/tools/agent/`），aimonitor 只负责服务端 + 前端

### 为什么选 C 而不是 B

| 维度 | B（注册码即签） | C（申请-审批） |
|------|----------------|--------------|
| 实现复杂度 | 低（1 个端点 + 1 个表） | 中（4 个端点 + 2 个表 + 前端） |
| 用户体验 | 管理员需预先生成码并离线传递 | 机器直接注册，管理员在页面点确认 |
| 安全边界 | 注册码是唯一信任锚，泄露即失守 | 管理员审批增加一层人工审核 |
| 适用场景 | 小团队，机器少，管理员直接操作 | 机器多，或机器管理员不是同一个人 |

C 覆盖 B 的场景（有码 = 快速审批），且多了无码申请的兜底路径。B 可后续作为 C 的简化子集补充。

### 为什么 admin 认证用简化版

局域网内威胁模型是"防手滑"而非"防攻击"。简单密码够用，且实现量仅为 Bearer token 系统的 1/3。
未来迁广域网时再升级。

## 后果

- ✅ 新增被监控机器零手工配置：注册 → 审批 → 自动发 token → 自动推送
- ✅ 兼容现有 `transport: local` 项目（零迁移）
- ✅ 兼容现有 `transport: agent` 项目（存量 token 不受影响，state 缺省 active）
- ✅ 注册码作为预授权信任锚，审批页面可区分"已知机器"和"陌生人"
- ⚠️ 新增 4 个写端点 + 1 个读端点 → 需鉴权 + 输入校验 + 限流
- ⚠️ 新增 2 个 SQLite 表（registration_request + enrollment_code）
- ⚠️ 引入第一个管理写面（dashboard 审批操作）→ 需 admin 认证
- ⚠️ Agent 端需在 aibase 仓库实现注册状态机 + 轮询逻辑
- ⚠️ 跨仓库协调：aibase 侧需等待 aimonitor API 契约锁定后才能开始实现
- 🔗 相关模块：`server/monitor_server.py`（新端点 + 新存储）、`src/`（审批 UI）、
  `config/admin.json`（admin 密码）、`config/agents.json`（动态签发）、
  `aibase/kit/tools/agent/`（agent 注册状态机 + 轮询）