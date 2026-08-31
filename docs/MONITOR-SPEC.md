# MONITOR-SPEC — aimonitor 监控规格

> 规格来源：TASK-001（用户需求 A1-A7 确认版）。
> 类型：docs 型（sdd-workflow 场景 A'）。
> 本文件是监控功能的**唯一真相**：后端采集、API、前端展示均以此为准。
> 被监控项目的 runtime 格式约定见 `aios/` 框架（TASK frontmatter / PROGRESS / CURRENT_FOCUS / heartbeat / events.jsonl / VERIFY / REVIEW）。

## 1. 目标

aimonitor 读取多个 AIOS 治理项目的执行状态（任务、进度、心跳、事件流、验证/审查记录），以网页仪表盘展示。

- 项目注册：手动配置文件（A1.a）
- 展示内容：任务全景 / 统计 / 当前焦点 / 心跳存活 / 事件流 / 验证审查记录（A3 全选）
- 刷新：前端定时自动刷新（A4.b）
- 依赖：尽量零依赖；后端用 Python 3.12 标准库（A5）
- 运行：当前服务器即运营环境，暴露端口（A6）；先用 aimonitor 自身 dogfooding（A7）
- 多机：监控 Windows / 远程 Linux 上的 AIOS 工程（Agent 推送，§3.1，TASK-031 规格）

## 2. 项目注册表（`config/projects.json`）

```json
{
  "poll_interval_seconds": 30,
  "heartbeat_stale_threshold_seconds": 300,
  "history_retention_days": 90,
  "alert_blocked_ratio_threshold": 0.2,
  "alert_stale_task_days": 14,
  "projects": [
    { "id": "aimonitor", "name": "aimonitor", "path": "/home/hb/code/aimonitor" }
  ]
}
```

| 字段 | 说明 | 默认 |
|------|------|------|
| `poll_interval_seconds` | 后端轮询各项目的间隔 | 30 |
| `heartbeat_stale_threshold_seconds` | 心跳超过该秒数视为"卡死/离线" | 300（5 分钟） |
| `history_retention_days` | 历史快照保留天数（超出则轮询时惰性清理） | 90 |
| `alert_blocked_ratio_threshold` | blocked 占任务总数比例超过该阈值触发 `blocked-ratio` 告警（TASK-026） | 0.2 |
| `alert_stale_task_days` | 非终态任务 `updated` 距今超过该天数触发 `task-stale` 告警（TASK-026） | 14 |
| `projects[].id` | **实例级唯一**（URL 中使用）：同一逻辑项目部署在多台机器时各机器为一个实例，id 各自唯一（如 `baseline-dev` / `baseline-prod`）；同逻辑项目多实例是独立监控单元（各自 runtime 状态独立），禁止多 agent 共享同一 id（见 §3.1.6） | 必填 |
| `projects[].name` | 展示名 | = id |
| `projects[].group` | 逻辑分组（可选）：同逻辑项目的多个实例共享 `group`（如 `baseline`），供前端分组展示；缺省 = `id` | = id |
| `projects[].path` | AIOS 项目根目录（含 runtime/）；`transport: local` 时为服务端本地路径；`transport: agent` 时为展示用途（agent 侧配置实际路径） | 必填 |
| `projects[].transport` | 采集方式：`local`（服务端直读本地文件系统，现状）/ `agent`（远端 agent 推送，§3.1） | `local` |

## 3. 采集规范（后端对每个项目只读）

后端**只读**以下文件，不写任何东西进被监控项目。

| 数据 | 路径（相对项目根） | 解析方式 |
|------|-------------------|---------|
| 任务列表 | `runtime/tasks/TASK-*.md` | frontmatter：`name`/`metadata.status`/`priority`/`risk`/`assignee`/`reviewer`/`updated` |
| 任务详情（TASK-024） | 同上（正文）+ `runtime/verification/VERIFY-*.md`、`runtime/reviews/REVIEW-*.md` | 正文 `## ` 章节（目标/范围/计划/风险与审批/验收标准/当前进度/子任务/备注 等，见 §4.4）；frontmatter `metadata.depends-on`；VERIFY/REVIEW 记录按 frontmatter `metadata.task-ref` 匹配该任务 |
| 统计 | 由任务列表自行聚合（不依赖 PROGRESS.md，防止生成滞后） | 各状态计数 |
| 当前焦点 | `runtime/states/CURRENT_FOCUS.md` | 标题下正文（"当前任务"/"下一个动作"两段） |
| 心跳-存活 | `runtime/logs/autoloop-{coder,reviewer}.heartbeat` | 存在性 + 文件 mtime 年龄（epoch 秒） |
| 事件流 | `runtime/logs/autoloop-{coder,reviewer}-events.jsonl` | 逐行 JSON：总数、最近一条 `{ts,task,outcome}`、outcome 分布；时间线 API（§4.3）按需读取同一文件返回每 role 最近 N 条 |
| 验证记录 | `runtime/verification/VERIFY-*.md` | 计数 |
| 审查记录 | `runtime/reviews/REVIEW-*.md` | 计数 |

**读取容错**：任一文件缺失/解析失败 → 该字段置 `null`/空，项目级 `error` 记录首次失败原因，**不中断其他项目**。单项目读取异常不拖垮整个 API。

**只读例外（TASK-022）**：后端在**自己**的 `data/` 目录写入历史快照库 `data/history.db`（SQLite，见 §4.1）。这是监控器自身存储，不属于被监控项目的 runtime 状态；对被监控项目的只读约束不变。

## 3.1 Agent 推送模式（多机监控，规格扩展）

> 目标：监控**远程机器**（如 Windows+WSL2、另一台 Linux）上的 AIOS 工程。
> 服务端仍然只读；被监控机器上运行 **agent** 负责把本机各工程 `runtime/` 状态推送到服务端。

### 3.1.1 数据流

```
被监控机器（每机器 1 个 agent，独立进程，不在任何项目内）
  agent 读取本机各 AIOS 工程 runtime/（TASK-*.md / focus / heartbeat / events.jsonl / VERIFY / REVIEW）
        │  POST /api/ingest（Bearer token 鉴权，打包原始文件内容）
        ▼
aimonitor 服务端
  ingest 处理器校验鉴权 → 落库 ingest_state（SQLite 新表）→ 写历史快照（TASK-040：趋势即时反映推送，采样/缺口语义与轮询一致）
        ▼
  采集层 AgentReader 从 ingest_state 读 → 复用现有 collect_tasks/read_heartbeat/read_events 解析
        ▼
  /api/status / /api/history / /api/projects/:id/events（对外契约不变）
```

**agent 只搬运不解析**：agent 不解析 runtime 内容，只读取原始文件并打包推送；
解析全部留在服务端（单一事实源），与 `transport: local` 共用同一套解析逻辑，保证两种模式结果一致。

**请求体格式（TASK-042）**：ingest 请求体 = **AIOS 通用遥测格式**（file-oriented 文件条目数组，
`files.tasks`/`files.heartbeats`/`files.events`），见 §3.1.3；服务端按文件名识别 role，与 agent 组件
（`aibase/kit/tools/agent/`，§3.1.5）输出零转换对接（协议解耦：aimonitor ingest 为通用遥测格式的消费方之一）。

### 3.1.2 配置（`config/projects.json`）

```json
{
  "projects": [
    { "id": "local-proj",  "name": "本地工程", "path": "/home/hb/code/x", "transport": "local" },
    { "id": "win-proj",    "name": "Windows工程", "path": "/展示路径", "transport": "agent" },
    { "id": "remote-linux","name": "远端Linux工程", "path": "/展示路径", "transport": "agent" }
  ]
}
```

- `transport` 缺省 = `local`（现有项目零迁移）
- `transport: agent` 项目的 `path` 仅作展示；真实路径在 agent 侧配置
- agent token 存 `config/agents.json`（权限 600）：`{ "<project_id>": "<token>" }` 或 `{ "<agent_id>": { "token": "...", "projects": ["win-proj", ...] } }`；每 token 的授权项目集合即该 agent 可推送的 `project_id` 白名单
- ⚠️ `config/agents.json` 含密钥：**必须 gitignore、禁止入库**（security-policy：密钥不进代码/提示词/配置文件；`.gitignore` 已含条目）

### 3.1.3 ingest API 契约（`POST /api/ingest`）

| 项 | 契约 |
|----|------|
| 端点 | `POST /api/ingest` |
| 鉴权 | `Authorization: Bearer <token>`；每 agent 一 token；校验失败 → `401`（不泄露任何状态数据） |
| 授权范围 | 请求体 `project_id` 必须在 token 授权项目集合内（§3.1.2），否则 `401/403`（防跨项目污染）；`project_id` 未在 `config/projects.json` 注册 → `400/404` |
| Content-Type | `application/json` |
| 请求体 | `{ "project_id": "<id>", "ts": <epoch秒>,\n  "files": {\n    "tasks": [ { "name": "TASK-001-xxx.md", "content": "<原文>" }, ... ],\n    "focus": "<CURRENT_FOCUS 原文>" | null,\n    "heartbeats": [ { "file": "autoloop-coder.heartbeat", "mtime": <远端心跳文件mtime epoch> }, ... ],\n    "events": [ { "name": "autoloop-coder-events.jsonl", "content": "<events.jsonl 原文>" }, ... ],\n    "verification_count": <int> | null,\n    "review_count": <int> | null\n  } }`（TASK-042：请求体为 **AIOS 通用遥测格式**——aibase 组件 agent（`aibase/kit/tools/agent/`）输出的文件条目数组，role 由文件名识别；`heartbeats[].mtime` 为远端 `runtime/logs/autoloop-<role>.heartbeat` 文件 mtime，语义见 §3.1.4） |
| 响应 | `200 { "ok": true, "project_id": "<id>" }`；失败 `400`（schema 错）/ `401`（鉴权失败）/ `409`（同 id 已被另一 agent 占用，见 §3.1.6）/ `413`（payload 超限） |
| 幂等 | 同 `project_id` 同一 agent 重复推送覆盖写（`INSERT OR REPLACE` 语义；实现用 `INSERT ... ON CONFLICT DO UPDATE` 保留 `task_cursor`，见下），无副作用；**不同 agent 推同一 id → 409** |
| 任务事件流（TASK-066/071，可选） | payload 顶层可选 `events`（数组，task 事件增量）与 `cursor`（整数，已确认覆盖最大 seq）。`events[]` 为 `{seq, ts, ev, task, from, to, actor, commit, dispatch_ref?, reason?}`；seq 整数 ≥1 且批内严格单调递增；单批 ≤ 200（超限 `400`，不静默截断）；`cursor` 整数 ≥0 且 ≥ 批内最大 seq。服务端按 `(project_id, seq)` 幂等去重；`cursor` 只推进不倒退（事件启用后混入旧 payload 重推不清空已确认 cursor） |
| 限流 | 每 agent 每分钟 N 次（可配置），超限 `429` |

> TASK-042（契约对齐）：请求体为 **AIOS 通用遥测格式**（aibase 组件 agent 输出，file-oriented）。
> 旧 role-oriented 格式不再接受——`files.heartbeat`（dict）键显式 `400`（避免「200 但心跳数据丢失」），
> `files.tasks`/`files.events` 旧 dict 形状按新 schema 校验拒绝（`必须为数组`）。

> TASK-071（任务事件流）：ingest 可选消费 aibase `cli/task` 写入的 task 级事件
> （`runtime/logs/task-events.jsonl`，TASK-065 增量落地 + TASK-066 传输）。
> seq/cursor 规则与 aibase `kit/tools/agent/README.md` §事件流契约一致；旧 payload
> （无 `events`/`cursor` 键）仍 `200`，`files` 快照照常落库，task 事件表不写入。
> 事件落库失败 → `500`（不静默丢弃，审计级真相）。

### 3.1.4 心跳与离线语义

**角色心跳 = payload 每 role epoch（方案 a，保持 local 语义）**：
- 请求体 `files.heartbeats[]` 携带 `{file: "autoloop-<role>.heartbeat", mtime}`（file = 远端
  `runtime/logs/autoloop-<role>.heartbeat` 文件名，mtime = 该文件 **mtime（epoch）**）；
  服务端按文件名识别 role，`heartbeat.<role>.exists = mtime 非 null`、`age_seconds = now - mtime`（沿用现有字段模型）
- 远端 role 进程死亡/卡死（mtime 变旧）→ 该 role `alive = false`，派生 `heartbeat-stale` 告警——**与 `transport: local` 语义一致**

**agent 整体离线 = `last_seen`（区分角色卡死与 agent 失联）**：
- 服务端记录每 agent 项目最近成功推送时间 `last_seen`
- `now - last_seen > heartbeat_stale_threshold_seconds` → 该项目 `error = "agent 离线"`（展示层可区分
  “远端 role 卡死”与“agent 失联”；`heartbeat-stale` 告警仍由 role epoch 驱动）
- agent 断线重连后自动恢复；期间历史快照缺失段表现为趋势缺口（现有缺口语义）

### 3.1.5 agent 部署拓扑与归属

| 项 | 决策 |
|----|------|
| 部署粒度 | **每台被监控机器 1 个 agent**（独立守护进程），管理该机多个工程；不在任何项目内运行 |
| 被监控项目 | 零部署、零改动（agent 只读 runtime/，与 local 模式相同的只读约束） |
| agent 代码归属 | **aibase/kit/tools/agent/**（框架通用组件，纯 Python 标准库跨平台）；随 `mkproject` 自动分发到新项目（零额外安装） |
| 程序分发 vs 运行 | 程序随项目分发（副本，每项目一份同一代码）；**运行时每机器 1 实例**，机器级配置 `agent.json`；项目内仅放 `agent.example.json` 模板 |
| 协议解耦 | agent 推送 **AIOS 通用遥测格式**（runtime 文件内容 + 元数据），aimonitor ingest（§3.1.3）为其消费方之一 |
| 存量项目 | 本地 7 个为 `transport: local` 无需 agent；远程新项目由 mkproject 生成即带 |
| Windows 前提 | AIOS 工具链（autoloop）依赖 bash + util-linux flock → 被监控 Windows 工程须在 WSL2 内运行；agent 在 WSL 内以独立进程运行 |
| 常驻方式 | Linux: systemd / nohup；Windows/WSL: nohup 或 Task Scheduler |

### 3.1.6 多实例语义（同逻辑项目多机器）

同一逻辑项目（如 baseline）可部署在**多台机器**（dev/prod/CI），各机器 runtime 状态彼此独立：

- **实例级监控**：每台机器上的项目实例 = 独立监控单元，`projects[].id` 各自唯一（`baseline-dev` / `baseline-prod`），
  各自有独立的任务列表/心跳/事件/历史——与 `transport: local` 的多 path 多记录语义完全一致
- **禁止共享 id**：同一 `project_id` 只允许**一个 agent** 推送；若两个 agent 抢推同一 id（`INSERT OR REPLACE` 互相覆盖），
  服务端检测到冲突 → `409 Conflict`（记录 agent 来源，后到者拒绝）
- **agent 配置**：每台机器 agent 的 `projects` 列表用实例 id + 路径：`{ "id": "baseline-dev", "path": "/home/dev/code/baseline" }`；
  token 授权按实例 id
- **展示**：项目总览一行一实例（同 `group` 多行可见）；`group` 字段供前端分组/折叠（可选项，非本期必做）
- **趋势/告警**：实例独立采样/告警；跨实例对比属前端可选项

## 3.2 注册-审批-签发（自助注册，动态签发 token）

> 目标：让被监控机器通过**自助注册 + 管理员审批**的方式加入 aimonitor，
> 无需预先手工编辑 `config/projects.json` 和 `config/agents.json`。
> 管理员通过 dashboard 页面确认/拒绝，审批通过后自动签发 token。

### 3.2.1 数据流

```
被监控机器 agent（未注册）
  │  POST /api/register { project_id, path, host_info, request_key, enrollment_code? }
  ▼
服务端 → 校验 → 写入 registration_request(status=pending)
  │  返回 { req_id, status: "pending" }
  ▼
agent 定期轮询 GET /api/register/:req_id/status?request_key=xxx
  │  （pending 状态 → 继续轮询）
  ▼
管理员在 dashboard 看到申请队列
  │  POST /api/register/:req_id/approve { admin_password }
  ▼
服务端 → 校验 admin_password → TokenIssuer 签发 token（scope=该 project_id 唯一）
  │  → 写入 config/agents.json（权限 600）→ 标记 status=approved
  ▼
agent 轮询到 status=approved → 拿到 token → 写入 agent.json → 切换 state=active
  │  → 开始正式推送（复用现有 ingest 链路）
  ▼
POST /api/ingest（正常推送，与现有行为一致）
```

### 3.2.2 状态机

```
                  ┌──────────────────┐
                  │  unregistered    │  agent 初始状态，无 token
                  └────────┬─────────┘
                           │ 注册
                           ▼
                  ┌──────────────────┐
                  │  pending         │  等待管理员审批
                  └────────┬─────────┘
                      ┌───┴───┐
                      ▼       ▼
              ┌──────────┐ ┌──────────┐
              │ approved │ │ rejected │
              │ (active) │ │          │
              └────┬─────┘ └────┬─────┘
                   │            │ 冷却后重试
                   ▼            ▼
              ┌──────────┐ ┌──────────┐
              │ revoked  │ │ expired  │  pending TTL 超时
              └──────────┘ └──────────┘
```

- `pending` → `approved`（管理员确认）→ `revoked`（管理员吊销）
- `pending` → `rejected`（管理员拒绝）→ 可重新注册
- `pending` → `expired`（TTL 超时，缺省 7 天）→ 可重新注册
- `approved` → `revoked`（管理员吊销 token）

### 3.2.3 注册申请存储（`registration_request` 表）

SQLite 表 `registration_request`，存放于 `data/registration.db`：

```sql
CREATE TABLE registration_request (
  req_id          TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL,
  path            TEXT,                   -- 申请 payload 的被监控项目路径（展示/自动登记用，TASK-069）
  enrollment_code TEXT,
  host_info       TEXT NOT NULL,          -- JSON: {hostname, ip, user_agent, ts}
  request_key     TEXT NOT NULL,          -- 客户端 secret，用于轮询绑定身份（服务端存 hash）
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|expired|revoked
  issued_token    TEXT,                   -- 审批通过时签发的 token（hash 存储）
  token_delivered INTEGER DEFAULT 0,      -- 单次交付标记（status 端点首次返回后置 1）
  renew_count     INTEGER DEFAULT 0,      -- 已轮换次数（renew 防重复守卫，>0 时 renew → 409）
  created_at      REAL NOT NULL,
  decided_at      REAL,
  expire_at       REAL NOT NULL,           -- pending TTL
  UNIQUE(project_id)                      -- 同 id 只能有一个活跃申请
);
```

状态机约束：
- `pending → approved|rejected|expired`
- `approved → revoked`
- `rejected → noop`（可重新注册）
- `expired → noop`（可重新注册）

### 3.2.4 注册码（Enrollment Code）表

SQLite 表 `enrollment_code`，存放于同一 DB：

```sql
CREATE TABLE enrollment_code (
  code                    TEXT PRIMARY KEY,
  description             TEXT,
  allowed_project_pattern TEXT,            -- 可选 glob: "baseline-*"
  max_uses                INTEGER DEFAULT 1,
  use_count               INTEGER DEFAULT 0,
  created_at              REAL NOT NULL,
  expire_at               REAL,
  revoked                 INTEGER DEFAULT 0
);
```

注册码是预授权信任锚：注册请求携带注册码时，管理员在审批页面看到 🔵 预授权标记；
无码时为 🟡 盲申请。注册码不是 token，泄露后管理员可吊销重发。

### 3.2.5 管理员认证

简化版（局域网适用）：
- 配置文件 `config/admin.json`（权限 600，gitignored）：`{ "admin_password": "<随机密码>" }`
- 首次启动时若文件不存在，自动生成 32 字符随机密码并 stdout 打印一次
- 审批端点均要求 `Authorization: Bearer <admin_password>`
- 前端审批区要求输入一次密码，sessionStorage 暂存

未来迁广域网时可升级为 Bearer token 系统（含签发/吊销/轮换）。

### 3.2.6 API 端点契约

#### `POST /api/register`（公开，无 token 鉴权）

注册端点，接收 agent 注册申请。

| 项 | 值 |
|----|----|
| 方法 | `POST /api/register` |
| 鉴权 | 无（公开端点，全局限流 60 次/分钟，可配置） |
| Content-Type | `application/json` |

请求体：
```json
{
  "project_id": "baseline-dev",
  "path": "/home/dev/code/baseline",
  "host_info": "hostname:dev-box, ip:192.168.1.5",
  "request_key": "<agent-生成的随机密钥>",
  "enrollment_code": "ABC123-XYZ789"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `project_id` | ✅ | 实例级唯一 id（如 baseline-dev） |
| `path` | ✅ | 被监控项目路径（展示用） |
| `host_info` | ✅ | 机器标识信息（hostname, IP, user-agent, ts） |
| `request_key` | ✅ | 客户端自生成随机密钥，用于轮询时绑定身份（最小 16 字节） |
| `enrollment_code` | — | 可选，预授权注册码 |

响应：

| 状态码 | 条件 | 响应体 |
|--------|------|--------|
| 201 | 注册成功 | `{ "req_id": "<uuid>", "status": "pending", "pending_since": <epoch> }` |
| 400 | 请求体格式错误 | `{ "error": "..." }` |
| 409 | project_id 已存在（已注册/活跃中） | `{ "error": "project_id 已存在", "existing": "active|pending" }` |
| 429 | 限流 | `{ "error": "rate limit" }` |

校验逻辑：
1. 全局限流（60 次/分钟，可配置）
2. Schema 校验：project_id 格式（字母数字连字符）、request_key 最小长度
3. project_id 已存在于 projects.json 或 ingest_state 活跃 → 409
4. 同 project_id 已有 pending 申请 → 409
5. enrollment_code 存在 → 校验并标记"预授权"
6. 写入 registration_request → 返回 201

#### `GET /api/register/:req_id/status`（公开，需 request_key 绑定）

Agent 轮询审批结果。

| 项 | 值 |
|----|----|
| 方法 | `GET /api/register/:req_id/status?request_key=<key>` |
| 鉴权 | 无（公开，但 request_key 绑定 req_id） |

响应：

| 状态 | 响应体 |
|------|--------|
| pending | `{ "status": "pending", "pending_since": <epoch> }` |
| approved | `{ "status": "approved", "token": "<token>", "project_id": "<id>" }`（token 仅返回一次） |
| rejected | `{ "status": "rejected", "reason": "..." }` |
| expired | `{ "status": "expired" }` |
| revoked | `{ "status": "revoked" }` |

安全设计：
- request_key 不匹配 → 404（不泄露 req_id 存在）
- token 仅在首次返回 approved 时交付一次，标记 `token_delivered=true`，后续不再返回
- token 仅在 HTTPS 响应中传输（局域网 HTTP 可用，但建议局域网内也注意）

#### `POST /api/register/:req_id/approve`（管理员认证）

管理员确认注册申请。

| 项 | 值 |
|----|----|
| 方法 | `POST /api/register/:req_id/approve` |
| 鉴权 | `Authorization: Bearer <admin_password>` |
| 请求体 | `{ "note": "可选备注" }` |

处理逻辑：
1. 校验 admin_password → 401
2. req_id 不存在 → 404
3. status 不是 pending → 409（已处理）
4. TokenIssuer 签发 token（scope=该 project_id 唯一）
5. 写入 config/agents.json（600）
6. 更新 status=approved, issued_token, decided_at
7. 如 enrollment_code 存在 → 调用 consume（+1 use_count）
8. **自动登记 projects.json**（TASK-069）：把 project_id 写入 config/projects.json
   （`{id, name, path, transport: "agent"}`，path 取申请 payload 的 path，缺省兜底为
   project_id），并同步内存 config → 审批后 agent 推送不再 400 "project_id 未注册"，
   且无需重启服务即被轮询/ingest 识别。幂等（已存在不重复追加）；失败 best-effort
   告警不阻断审批（与步骤 7 语义一致）。
9. 返回 `{ "status": "approved", "req_id": "<id>", "project_id": "<id>" }`

#### `POST /api/register/:req_id/reject`（管理员认证）

管理员拒绝注册申请。

| 项 | 值 |
|----|----|
| 方法 | `POST /api/register/:req_id/reject` |
| 鉴权 | `Authorization: Bearer <admin_password>` |
| 请求体 | `{ "reason": "拒绝原因" }` |

返回 `{ "status": "rejected", "req_id": "<id>" }`。

#### `POST /api/register/:req_id/revoke`（管理员认证）

吊销已批准的 token。

| 项 | 值 |
|----|----|
| 方法 | `POST /api/register/:req_id/revoke` |
| 鉴权 | `Authorization: Bearer <admin_password>` |

处理逻辑：
1. status 必须是 approved → 否则 409
2. 从 config/agents.json 移除该 token
3. 更新 status=revoked, decided_at
4. 返回 `{ "status": "revoked" }`

#### `POST /api/register/:req_id/renew`（管理员认证）

轮换 token：吊销旧 + 签发新。

| 项 | 值 |
|----|----|
| 方法 | `POST /api/register/:req_id/renew` |
| 鉴权 | `Authorization: Bearer <admin_password>` |

处理逻辑：
1. 同 revoke 逻辑吊销旧 token
2. TokenIssuer 签发新 token（同 project_id）
3. 写入 agents.json
4. 返回 `{ "status": "approved", "note": "新 token 已签发，agent 下次推送时收到 401 后自动轮询领取" }`

状态守卫：仅当 status='approved' 且 `renew_count=0` 时可轮换（重复 renew → 409）；
轮换成功后 `renew_count` +1、`token_delivered` 重置为 0（agent 通过 status 端点领取新 token）。

### 3.2.7 Token 签发

`TokenIssuer` 类：

- `issue(project_id) → { token, project_id, scope }`
- token 格式：`aimon_{project_id}_{uuid4}_{random_hex}`（可识别来源，可审计）
- 写入 `config/agents.json`（600），格式兼容现有：
  ```json
  { "baseline-dev": "aimon_baseline-dev_xxx_yyy" }
  ```
- 单次交付：token 仅通过审批响应 / status 轮询返回一次，不落页面存储、不入日志、不入提示词
- 吊销时从 agents.json 移除
- 生成用 `secrets.token_urlsafe(32)`（密码学安全随机）

### 3.2.8 前端页面

侧边栏新增「📋 注册申请」入口（badge 显示 pending 数量），包含：

**申请队列**（只读，TASK-055）：
- 列表：project_id、host_info（hostname + IP）、时间、状态
- 标记：🔵 预授权（有 enrollment_code）/ 🟡 盲申请（无码）/ ✅ 已批准 / ❌ 已拒绝 / ⏳ 已过期 / 🔒 已吊销
- 数据来源：`GET /api/register/list?status=pending`（需 admin_password 认证）

**审批操作**（TASK-056）：
- 点击行展开详情：project_id、path、host_info、申请时间、注册码（如有）、状态
- 操作按钮：✅ 确认 / ❌ 拒绝 / 🔒 吊销 / 🔄 轮换
- 首次打开审批区时，弹出 admin_password 输入框，sessionStorage 暂存

**注册码管理**（TASK-057）：
- 子 tab：申请队列 / 注册码管理
- 注册码列表：code、description、allowed_project、max_uses/use_count、expire_at、status
- 操作：生成注册码（表单）、吊销

### 3.2.9 安全假设（局域网）

| 项 | 假设 |
|----|------|
| 网络 | 局域网内 HTTP 通信，不强制 HTTPS |
| 威胁模型 | 内部用户误操作 > 恶意攻击；不防 LAN 内撞库/嗅探 |
| 管理员认证 | 简单密码，够防手滑即可 |
| 限流 | 全局限流防 bug 死循环，不防 DDoS |
| 未来迁移 | 迁广域网时需补充：HTTPS 反代、IP 级限流、枚举防护、token 系统升级 |

### 3.2.10 aibase 接口契约（供 aibase 侧实现 agent 注册功能）

> 本附录仅包含 aibase 的 agent 组件需要实现的 API 契约和状态机定义。
> aimonitor 内部实现细节（存储表结构、前端 UI、admin 认证）不在此列出。

#### Agent 状态机

```
unregistered → pending → approved (active)
                       → rejected → retry
                       → expired → re-register
approved → revoked → re-register
```

#### agent.json 新增字段

| 字段 | 必填 | 缺省 | 说明 |
|------|------|------|------|
| `state` | — | `active` | agent 状态：`unregistered` / `pending` / `active`。缺省 `active` 兼容存量 agent |
| `req_id` | — | — | 注册成功后服务端返回的申请 ID，pending 状态下存在 |
| `request_key` | — | — | agent 自生成的随机密钥，用于轮询时绑定身份（pending 状态下存在） |

`state=unregistered` 时，`token` 字段可为空；`state=active` 时，`token` 必填（与现有校验一致）。

#### CLI 新增子命令

```bash
python3 agent.py --register [--enrollment-code CODE] [--config agent.json]
  # 注册流程：构造请求 → POST /api/register → 进入 pending 状态 → 开始轮询

python3 agent.py --register --status
  # 查看当前注册状态（unregistered/pending/active）
```

#### POST /api/register 契约（agent 视角）

| 项 | 值 |
|----|----|
| URL | `POST <server_url>/../register`（server_url 的 `/api/ingest` 替换为 `/api/register`） |
| Content-Type | `application/json` |

请求体：
```json
{
  "project_id": "baseline-dev",
  "path": "/home/dev/code/baseline",
  "host_info": "hostname:dev-box, ip:192.168.1.5",
  "request_key": "<随机密钥>",
  "enrollment_code": "ABC123-XYZ789"
}
```

响应：

| 状态码 | 响应体 | agent 行为 |
|--------|--------|-----------|
| 201 | `{ "req_id": "<uuid>", "status": "pending", "pending_since": <epoch> }` | 保存 req_id，切换到 pending 状态，开始轮询 |
| 409 | `{ "error": "...", "existing": "active|pending" }` | 已注册 → 提示用户；已存在 pending → 继续轮询旧 req_id |
| 400/429 | `{ "error": "..." }` | 退避重试 |

#### GET /api/register/:req_id/status 契约（agent 视角）

| 项 | 值 |
|----|----|
| URL | `GET <server_url>/../register/<req_id>?request_key=<key>` |

响应：

| 状态码 | 响应体 | agent 行为 |
|--------|--------|-----------|
| 200 pending | `{ "status": "pending", "pending_since": <epoch> }` | 继续轮询（间隔 30s） |
| 200 approved | `{ "status": "approved", "token": "<token>", "project_id": "<id>" }` | 保存 token，写入 agent.json，切换 state=active，开始正式推送 |
| 200 rejected | `{ "status": "rejected", "reason": "..." }` | 打印错误，退出（或等待人工介入） |
| 200 expired | `{ "status": "expired" }` | 提示重新注册 |
| 200 revoked | `{ "status": "revoked" }` | 提示重新注册 |
| 404 | — | request_key 不匹配或 req_id 不存在 → 退避重试 |

#### Token 格式

```
aimon_{project_id}_{uuid4}_{random_hex}
```

示例：`aimon_baseline-dev_550e8400-e29b-41d4-a716-446655440000_a1b2c3d4`

- 前缀 `aimon_` 便于识别来源
- 中段 `project_id` 便于审计
- 后段 `uuid4` + `random_hex` 保证不可猜测

#### 轮询失败处理

| 失败类型 | 行为 |
|---------|------|
| 网络错误/超时 | 指数退避（复用现有 `agent_retry.py`），最长 60s cap |
| 4xx（不含 404） | 不重试，打印错误，退出 |
| 404 | 退避重试（可能 req_id 尚未同步），3 次后退出 |
| pending TTL 超时 | 打印提示，退出（重新注册） |

---

## 4. 聚合 JSON 模型（`GET /api/status`）

```json
{
  "generated_at": 1722590000,
  "poll_interval_seconds": 30,
  "heartbeat_stale_threshold_seconds": 300,
  "alerts": { "count": 0, "items": [] },
  "projects": [
    {
      "id": "aimonitor",
      "name": "aimonitor",
      "path": "/home/hb/code/aimonitor",
      "error": null,
      "last_read_at": 1722590000,
      "summary": { "total": 0, "open": 0, "in-progress": 0, "in-review": 0,
                   "blocked": 0, "done": 0, "cancelled": 0 },
      "tasks": [
        { "id": "TASK-001", "slug": "monitor-spec", "name": "TASK-001-monitor-spec",
          "description": "", "status": "in-progress", "priority": "P1",
          "risk": "P2", "assignee": "analyst", "reviewer": "any", "updated": "2026-08-02",
          "detail": { "sections": [ { "heading": "目标", "body": "…" } ],
                       "acceptance": [ { "text": "可验证的条件 1", "checked": false } ],
                       "dependencies": [],
                       "verification": [], "reviews": [] } }
      ],
      "focus": { "current": "", "next": "" },
      "heartbeat": {
        "coder":    { "exists": false, "age_seconds": null },
        "reviewer": { "exists": false, "age_seconds": null }
      },
      "events": {
        "coder":    { "count": 0, "last": null, "outcomes": {} },
        "reviewer": { "count": 0, "last": null, "outcomes": {} }
      },
      "verification_count": 0,
      "review_count": 0,
      "alerts": []
    }
  ]
}
```

- `age_seconds`：`now - heartbeat mtime`；`null` = 文件不存在
- `events.last`：`{ts, task, outcome}` 或 `null`（无事件）
- `events.outcomes`：`{"no_task": 5, "ok": 2, ...}` 计数分布

## 4.4 任务详情模型（`tasks[].detail`，TASK-024）

```json
{
  "detail": {
    "sections": [ { "heading": "目标", "body": "要做什么，以及为什么。" } ],
    "acceptance": [ { "text": "可验证的条件 1", "checked": false } ],
    "dependencies": [ "TASK-000" ],
    "verification": [ { "name": "VERIFY-2026-08-15-task-023", "result": "pass",
                          "date": "2026-08-15", "verifier": "cli/task-verify", "commit": "dd509ef" } ],
    "reviews": [ { "name": "REVIEW-2026-08-15-TASK-023", "result": "issues-found",
                     "date": "2026-08-15", "reviewer": "autoloop-reviewer", "commit": "2e4ffcf" } ]
  }
}
```

- `sections`：TASK 正文 frontmatter 之后按 `## ` 标题切分的全部章节（`{heading, body}`，body 为章节原文，纯文本）。空正文的章节不产生条目。
- `acceptance`：`## 验收标准` 章节内 `- [ ]`/`- [x]` 清单项 → `{text, checked}`；`checked` 为 `[x]` 时为 `true`。无验收标准章节 → 空数组。
- `dependencies`：frontmatter `metadata.depends-on`（YAML 列表文本，如 `[]` / `[TASK-001, TASK-002]`）→ id 数组。
- `verification` / `reviews`：`runtime/verification/VERIFY-*.md` / `runtime/reviews/REVIEW-*.md` 中 `metadata.task-ref` 等于该任务 id 的记录摘要（只含 frontmatter：name/result/date/verifier|reviewer/commit，不读全文）。无匹配 → 空数组。
- 容错：TASK/VERIFY/REVIEW 任一文件缺失、frontmatter 解析失败 → 对应字段空值，不中断整个任务/项目读取。

## 4.5 任务筛选查询参数（`GET /api/status`，TASK-025）

`/api/status` 支持可选查询参数，对响应中每个项目的 `tasks[]` 做**服务端筛选**；`summary`/`focus`/`heartbeat`/`events` 等聚合字段**保持全量不变**（筛选语义只作用于任务列表，指标卡/总览/状态分布仍反映项目整体）。

| 参数 | 语义 | 示例 |
|------|------|------|
| `status` | 精确匹配任务 `status` | `?status=in-progress` |
| `priority` | 精确匹配任务 `priority` | `?priority=P1` |
| `assignee` | 精确匹配任务 `assignee` | `?assignee=coder` |
| `q` | 大小写不敏感子串搜索，匹配 `id`/`name`/`description`/`assignee` 拼接文本 | `?q=history` |

- 多参数为 AND 关系（同时满足才返回）；同名参数重复时取第一个值（`parse_qs` 首值）。
- 空值/缺失参数不参与筛选；全部参数为空时返回未筛选的完整 `tasks[]`（与 TASK-025 之前行为完全兼容）。
- 筛选值无枚举校验（宽松语义）：不存在的状态/优先级/assignee 只产生空 `tasks[]`（200，不 400）——`status`/`priority`/`assignee` 均为数据驱动字段，项目可能使用非标准值。
- 实现位置：请求时基于轮询缓存的未筛选 payload 派生筛选视图（不缓存每个筛选组合，不修改轮询缓存本身）。

## 4.6 告警派生模型（`alerts`，TASK-026）

后端在每轮轮询时为每个项目派生告警，写入项目对象 `alerts` 字段；顶层 `alerts` 聚合全部项目的告警（导航告警计数、顶栏横幅直接消费）。`alerts` 是**只读派生数据**：不落库、不写入被监控项目，随轮询缓存刷新。

```json
{
  "alerts": {
    "count": 3,
    "items": [
      { "project": "aimonitor", "level": "error", "kind": "read-error", "text": "读取失败: …" },
      { "project": "aimonitor", "level": "error", "kind": "heartbeat-stale", "role": "coder", "text": "Coder 心跳卡死" },
      { "project": "aimonitor", "level": "warn", "kind": "blocked-ratio", "blocked": 3, "total": 9, "threshold": 0.2, "text": "blocked 占比 33% 超阈值 20%" }
    ]
  },
  "projects": [
    { "id": "aimonitor", "…": "…", "alerts": [ /* 该项目在 items 中的子集 */ ] }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `alerts.count` | 全部项目告警条目总数（= `items` 长度） |
| `alerts.items[]` | 全部项目告警条目（顺序 = 项目顺序 × 项目内告警顺序） |
| `projects[].alerts[]` | 该项目告警条目数组（`items` 中 `project` 等于该项目 id 的子集） |

告警条目公共字段：`project`（项目 id）、`level`（`error`/`warn`）、`kind`、`text`（展示文案）。`kind` 明细：

| kind | level | 触发条件 | 附加字段 |
|------|-------|---------|---------|
| `read-error` | `error` | 项目读取失败（`error` 非空）；该条件下不再派生该项目其他告警 | — |
| `heartbeat-stale` | `error` | 心跳文件存在且 `age_seconds > heartbeat_stale_threshold_seconds`（卡死）；心跳缺失**不告警**（视为"无进程"） | `role`（coder/reviewer） |
| `blocked-ratio` | `warn` | `total > 0` 且 `blocked/total > alert_blocked_ratio_threshold` | `blocked`/`total`/`threshold` |
| `task-stale` | `warn` | 存在**非终态**（open/in-progress/in-review/blocked）任务且 `updated` 距今超过 `alert_stale_task_days` 天 | `count`/`days`/`tasks`（任务 id 列表） |

- `updated` 解析：`YYYY-MM-DD`；缺失/非法 → 不参与（该任务不触发 `task-stale`）。
- 阈值配置见 §2：`alert_blocked_ratio_threshold`（默认 0.2）、`alert_stale_task_days`（默认 14）。
- 筛选（§4.5）不影响 `alerts`：任务筛选只作用于 `tasks[]`，告警为轮询时基于全量派生（聚合字段保持全量）。

## 4.7 告警通知渠道（webhook，TASK-072）

告警派生后除在仪表盘/API 可见（§4.6）外，可配置外部 webhook 把「有新告警」推送给外部系统（IM/邮件网关/自建接收器），使 blocked/stale 状态有人关注。通知是**增强能力**：未配置时轮询与 API 完全不受影响（fail-open）。

### 4.7.1 配置

两种方式（环境变量优先于文件）：

**方式 A：环境变量**（部署注入，最简）

| 环境变量 | 说明 |
|---------|------|
| `AIMONITOR_NOTIFY_WEBHOOK_URL` | webhook URL（必填以启用，仅接受 `http://`/`https://`） |
| `AIMONITOR_NOTIFY_WEBHOOK_TOKEN` | 可选 Bearer token（无需鉴权的 webhook 可省略） |

**方式 B：配置文件 `config/notify.json`**（权限 600，**gitignored，不入库**——url/token 为机密，security-policy）

```json
{
  "webhook": {
    "enabled": true,
    "url": "https://example.com/your-webhook",
    "token": ""
  }
}
```

- `enabled: false` 或文件缺失/JSON 非法/URL 非 http(s) → 通知禁用（fail-open，不抛异常、不拖垮轮询）
- 环境变量设置后忽略文件中的 `enabled: false`（部署注入优先）
- 示例仅文档展示；真实配置请在本机 `config/notify.json`（gitignored）或环境变量设置

### 4.7.2 投递语义

- **时机**：每轮轮询后，聚合全部项目告警（`alerts.items` 同序）；**告警集合变化时**才投递（防抖：相同告警不每轮重复轰炸）
- **恢复**：告警清空时重置指纹，下次再出现会再次通知（不做专门 recovery 消息，保持最小实现）
- **失败重试**：投递失败只记日志、保留旧指纹，下一轮自动重试（最终一致）；超时 5s 防卡死；任何失败不抛异常
- **请求格式**：`POST <webhook_url>`，`Content-Type: application/json`，可选 `Authorization: Bearer <token>`

```json
{
  "event": "alerts.changed",
  "ts": 1787000000.0,
  "count": 2,
  "items": [
    { "project": "x1design", "level": "warn", "kind": "blocked-ratio", "blocked": 10, "total": 32, "threshold": 0.2, "text": "blocked 占比 31% 超阈值 20%" },
    { "project": "proj-x", "level": "error", "kind": "heartbeat-stale", "role": "coder", "text": "Coder 心跳卡死" }
  ]
}
```

- 条目字段 = §4.6 告警条目公共字段（`project`/`level`/`kind`/`text` + kind 附加字段），接收方可直接按 `kind` 路由
- 端到端验证记录（TASK-072）：本地接收器收到 `alerts.changed` POST，见任务备注

## 4.1 历史快照存储（SQLite，TASK-022）

后端每轮轮询为每个**读取成功**的项目写入一行快照（SQLite `data/history.db`，stdlib `sqlite3`，零第三方依赖）：

| 列 | 类型 | 说明 |
|------|------|------|
| `ts` | INTEGER | 轮询轮次的 epoch 秒（主键之一） |
| `project` | TEXT | 项目 id（主键之一） |
| `summary_json` | TEXT | §4 `summary` 的 JSON（total + 各状态计数） |
| `coder_alive` | INTEGER | Coder 心跳存活（0/1） |
| `reviewer_alive` | INTEGER | Reviewer 心跳存活（0/1） |

- **心跳存活定义**：heartbeat 文件存在且 `age_seconds ≤ heartbeat_stale_threshold_seconds`。
- **采样规则**：项目读取失败（`error` 非空）时**本轮不采样**，时间序列留缺口——避免把"读不到"伪装成"全部归零"误导趋势图。
- **保留期**：`history_retention_days`（默认 90 天），每轮轮询后惰性清理过期行。
- **存储位置**：aimonitor 自身 `data/`（gitignore 排除）；不写入被监控项目。

## 4.2 历史趋势 API 模型（`GET /api/history`）

```json
{
  "project": "aimonitor",
  "hours": 24,
  "generated_at": 1722590000,
  "points": [
    { "ts": 1722500000,
      "summary": { "total": 28, "open": 3, "in-progress": 2, "in-review": 1,
                    "blocked": 0, "done": 22, "cancelled": 0 },
      "coder_alive": true,
      "reviewer_alive": false }
  ]
}
```

- 查询参数：`project`（必填，项目 id）、`hours`（可选，默认 24，范围 (0, 8760]）。
- `points` 按 `ts` 升序，窗口为 `[now - hours*3600, now]`。
- 非法参数（缺 `project`、`hours` 非数字、非有限数（`NaN`/`inf`）或超范围）→ `400` + JSON 错误。
- 项目无数据 → `points: []`（200，而非 404）。

## 4.3 事件时间线 API 模型（`GET /api/projects/:id/events`，TASK-023）

```json
{
  "project": "aimonitor",
  "limit": 10,
  "generated_at": 1722590000,
  "counts": { "coder": 4, "reviewer": 123 },
  "events": {
    "coder":    [ { "ts": 1722590000, "task": "TASK-022", "outcome": "ok" } ],
    "reviewer": [ { "ts": 1722589000, "task": "TASK-021", "outcome": "error" } ]
  },
  "task_events": {
    "count": 3,
    "cursor": 2,
    "events": [ { "seq": 2, "ts": "2026-08-27T09:00:00", "ev": "task.started", "task": "TASK-012", "from": "open", "to": "in-progress", "actor": "cli/task", "commit": null, "dispatch_ref": null, "reason": null } ]
  }
}
```

- 路径参数：`id` = 项目 id（`config/projects.json` 注册表中的 `projects[].id`）。
- 查询参数：`limit`（可选，默认 10，范围 (0, 100] 的**正整数**）。
  - `limit` 按 **role 生效**：`events.coder` 与 `events.reviewer` 各返回最近 `limit` 条，避免单一 role 事件淹没时间线。
  - `events.<role>` 为 `{ts, task, outcome}` 数组，按 `ts` **降序**（最近在前）；无事件或文件缺失 → 空数组。
  - `counts.<role>` = 该文件有效事件总数（前端用于"最近 X / total"展示）。
  - `task_events`（TASK-071，agent 项目）：`{count, cursor, events}`——task 事件流总条数、
    已确认覆盖最大 seq（未推送 → `null`）、最近 `limit` 条按 `seq` **降序**（每条含完整事件字段
    `seq/ts/ev/task/from/to/actor/commit/dispatch_ref/reason`）；`transport: local` 项目 → `(0, null, [])`。
  - 向后兼容：`task_events` 为**追加键**，旧客户端忽略不影响；`counts`/`events` 形状不变。
- 错误码：
  - 项目 id 未注册 → `404` + JSON 错误（资源型路由；与 §4.2 的"无数据 200"区分——项目存在但无事件仍 `200` + 空数组）。
  - `limit` 非数字 / 非有限数（`NaN`/`inf`，沿用 TASK-022 BUG-001 修复模式）/ 非整数 / 超出范围 → `400` + JSON 错误。

## 5. 后端服务（`server/monitor_server.py`）

- **语言/依赖**：Python 3.12 **标准库**（`http.server` + `json` + `os` + `time` + `threading`），零第三方依赖
- **进程模型**：单进程 = ThreadingHTTPServer + 一个后台轮询线程
  - 后台线程每 `poll_interval_seconds` 秒遍历 `config/projects.json` 全部项目 → 聚合 → 存内存缓存
  - 首轮在启动时立即执行一次
  - API 线程只读缓存返回（响应 O(1)，不阻塞轮询）
- **端点**：
  - `GET /api/status` → 第 4 节聚合 JSON（`Content-Type: application/json`）；支持 §4.5 筛选参数（`status`/`priority`/`assignee`/`q`，仅作用于 `tasks[]`，聚合字段保持全量）；含 §4.6 告警派生（顶层 `alerts` + 每项目 `alerts`，轮询时派生，不受筛选影响）
  - `GET /api/history?project=<id>&hours=<n>` → 第 4.2 节历史时间序列（`Content-Type: application/json`）
  - `GET /api/projects/<id>/events?limit=<n>` → 第 4.3 节事件时间线（`Content-Type: application/json`）
  - `POST /api/ingest` → §3.1.3 Agent 推送入口（Bearer token 鉴权；落库 `ingest_state`）
  - `GET /` 及其他静态路径 → 服务前端页面（默认 `dist/`；`--dev` 时服务 `src/`）
- **端口**：默认 `3113`（`--port` 可覆盖；31xx 段空闲，避开 3010/3011/3012 Docmost 与其它已用端口）
- **日志**：启动信息、每轮轮询耗时、错误到 stderr/stdout，`--quiet` 可关

## 6. 前端仪表盘（`src/`）

零依赖原生 JS（沿用现有骨架，不引入框架）。页面为**现代化监控布局**（TASK-021，依据 `tmp/ui-mockup.html` 高保真原型），
整体为 CSS 变量双主题（暗色/浅色），`<html data-theme="dark|light">` 切换。

### 6.1 页面结构

| 区块 | 内容 | 数据来源 |
|------|------|---------|
| 侧边栏导航 | 左侧固定：logo、监控导航（总览/任务/执行流/趋势/告警，含计数）、项目列表（点击切换当前项目）、底部状态（项目数/轮询间隔/系统健康） | `projects[]` / `summary` / 告警计数（TASK-026：`alerts.count` 服务端派生） |
| 顶栏 | 页面标题 + 更新时间、时间范围分段（24h/7d/30d，UI 占位，数据由 TASK-022/027 接入）、项目下拉筛选（`#project-select`）、刷新按钮（`#refresh-btn`）、主题切换按钮 | `projects` / `generated_at` / `poll_interval_seconds` |
| 告警横幅 | 红色告警条：心跳卡死 / blocked 占比超阈值 / 任务长期未更新 / 项目读取错误（TASK-026：服务端派生 `alerts.items`，前端直接消费） | `alerts.items` |
| 指标卡 | 当前项目 6 卡：总任务(done/total)、完成率、进行中、审查中、阻塞、Coder 心跳 | `summary` / `heartbeat` |
| 趋势面板（完成率 / 事件速率） | 面板 `#trend-panel`：手写 SVG 折线图（零依赖），数据源 `GET /api/history`（TASK-022）；顶栏 24h/7d/30d 分段控件（`#range-seg`）与项目切换驱动刷新；两图：完成率趋势（done/total%）与事件速率（任务状态迁移事件/小时，由相邻快照 `summary` 差推导）；详见 §6.5 | `/api/history` |
| 项目总览表 | 所有项目一行一条：项目名、总任务、完成、完成率进度条、open/in-progress/in-review/blocked 计数、**告警列**（TASK-026：`projects[].alerts` 计数，悬浮显示条目明细）、心跳、VERIFY/REVIEW 计数（TASK-015）；**行可点击切换当前项目**（与顶部选择器联动，TASK-017） | `projects[]` / `summary` / `heartbeat` / 验证审查计数 / `alerts` |
| 任务列表 | TASK 表格：任务、状态、优先级、风险、assignee、updated；筛选行：搜索框 + 状态/优先级/assignee 下拉（TASK-025 后端化：筛选/搜索变化触发 `GET /api/status?status=&priority=&assignee=&q=`，任务行由服务端筛选结果渲染） | `tasks`（服务端筛选，§4.5） |
| 事件时间线 | 事件流面板：按 role 渲染最近事件（outcome 圆点 + 时间 + 任务），outcome 计数；数据源 `GET /api/projects/:id/events?limit=10`（TASK-023），不再只用 `events.*.last` | `/api/projects/:id/events` |
| 当前焦点 | CURRENT_FOCUS 文本（当前/下一步） | `focus` |
| 任务状态分布 | 各状态数量分布（色块条 + 图例） | `summary` |
| 详情抽屉 | 右侧固定抽屉：点击任务行显示 slug/名称/描述/状态/优先级/风险/assignee/reviewer/updated + **完整正文**（目标/范围/计划/风险与审批/当前进度/子任务/备注）、验收标准 checklist（含勾选态）、依赖、关联 VERIFY/REVIEW 记录（TASK-024） | `tasks[].detail` |
| 刷新 | 自动定时 `setInterval` fetch `/api/status`（间隔 = `poll_interval_seconds`）+ 手动刷新按钮 + 最后更新时间 | — |

### 6.2 主题系统（TASK-021 基础 + TASK-028 完善）

- CSS 变量定义在 `:root[data-theme="dark"]` 与 `:root[data-theme="light"]` 两组（`src/css/style.css`）
- TASK-021：提供基础切换按钮（设置 `documentElement.dataset.theme`）
- TASK-028：默认主题 / localStorage 记忆 / `prefers-color-scheme` 跟随

### 6.3 容错

API 请求失败 → 显示错误横幅 + 保留上次数据，不白屏。

### 6.4 契约元素（test 依赖，不得删除）

- `#project-select`、`#task-table`（class `task-table`）、`#status-bars`、`#refresh-btn`、`#trend-panel`、`#range-seg`
- `main.js` 必须 `fetch("/api/status")` 且 `fetch("/api/history")`（趋势图，TASK-027）
- `trend.js` 必须提供纯函数 `completionRateSeries` / `eventRateSeries`（Node 单测依赖）
- `dist/` 为构建产物，只由 `scripts/build.sh` 生成，禁止手动编辑

### 6.5 趋势图（TASK-027，手写 SVG 折线图）

趋势面板 `#trend-panel` 由前端**手写 SVG 折线图**填充（零第三方依赖），数据源 `GET /api/history`（§4.2，TASK-022）。项目切换或时间范围切换时按当前项目重新拉取；随轮询周期自动刷新。

- **时间范围**：顶栏分段 `#range-seg`（24h / 7d / 30d）→ `hours` 参数 24 / 168 / 720（默认 24h）。
- **完成率趋势**：每个快照 `value = round(summary.done / summary.total × 100)`（1 位小数）；`total` 缺失或为 0 → 该点为空（缺口断开折线）。y 轴固定 0–100%。
- **事件速率**：任务状态迁移事件/小时，**分桶统计**（固定时间桶）：相邻快照 `Σ|Δstatus|`（六个状态计数绝对差之和）/ 2 = 状态迁移次数，按相邻快照中点归入所在时间桶；每桶速率 = 桶内迁移次数 / 桶小时数。桶长从 1/2/3/6/12/24 小时选取（目标 ≥20 桶：24h → 1h 桶、7d → 6h 桶、30d → 24h 桶）。相邻 `summary` 缺失、Δt≤0 或 Δt 超 2 倍桶长（采样缺口）→ 该对不归桶；桶内无迁移 → 0（有意义值而非缺口）。y 轴 0 起、按数据峰值自动取整（nice ceiling）。
- **缺口处理**：空值点断开折线（不跨缺口连线），避免"读不到"被画成下跌。
- **降采样**：点数超 400 时等距抽样（保留首尾），控制 SVG 规模（24h @30s 轮询 ≈ 2880 点）。
- **渲染**：`src/js/trend.js` 提供纯函数（序列推导 / 缩放几何 / 格式化，UMD 双形态供 Node 单测）；`src/js/main.js` 负责 `fetch` + `createElementNS` 构建 SVG（网格线 / 时间轴标签 / 折线 / 面积填充 / 数据点 tooltip）。颜色走 CSS 变量，暗/浅主题自适应。
- **空态**：无历史点 → 面板显示"暂无历史数据（等待轮询采样）"；有序列但全部为空值 → 单图显示空提示。
- **容错**：请求失败且无旧数据 → 显示错误占位；有旧数据 → 保留旧图（错误横幅已提示）。

## 7. 验收标准（TASK-002/003/004 共用基线）

1. `config/projects.json` 指向 aimonitor 自身，后端启动后 `/api/status` 返回第 4 节结构且任务数据与 `runtime/tasks/` 一致
2. 心跳文件缺失时 `heartbeat.*.exists=false`，手动创建后轮询更新为 `true` 且 `age_seconds` 合理
3. 前端定时刷新，展示全 6 类内容，无 JS 报错
4. 单项目 path 配错时，该项目 `error` 有值，其他项目正常
5. `aios.config.yaml` 增加 `server/` 与文档配置，`cli/task verify` 通过

## 8. 决策记录

| 决策 | 选择 | 理由 |
|------|------|------|
| 后端 vs 纯静态 | 轻量后端 | A3 全选 + A4 定时刷新 + A6 运营环境；单进程单端口最简 |
| 语言 | Python 3.12 标准库 | 零依赖（A5），`http.server` 够用 |
| 统计来源 | 任务列表自聚合 | 不依赖 PROGRESS.md 的生成时机 |
| 数据存储 | 内存缓存，无数据库 | 文件即数据库；重启即重扫，符合框架原则 |
| 历史趋势存储（TASK-022） | 内存缓存 + SQLite（stdlib `sqlite3`）`data/history.db` | 趋势需跨重启持久化；stdlib 保持零依赖；仅监控自身存储，被监控项目仍只读 |
| 事件时间线 API（TASK-023） | 独立端点 `GET /api/projects/:id/events?limit=`（每 role 最近 N 条、ts 降序），`/api/status` 聚合不变 | 时间线可能很长，不宜塞进轮询缓存；按需读文件保持 `/api/status` 轻量；limit 按 role 生效避免单 role 淹没 |
| 任务详情（TASK-024） | `tasks[].detail` 随 `/api/status` 聚合返回（正文章节 + 验收 checklist + 依赖 + VERIFY/REVIEW 摘要） | 详情随轮询缓存，前端抽屉保持单数据源单请求；VERIFY/REVIEW 每项目单遍建索引避免重复扫盘；关联记录只取 frontmatter 摘要控制 payload |
| 任务筛选/搜索（TASK-025） | 服务端筛选：`/api/status` 支持 `status`/`priority`/`assignee`/`q` 查询参数，仅作用于 `tasks[]`；`summary` 等聚合字段保持全量 | 任务列表筛选是核心交互，后端化避免前端重复拉全量并保持单数据源；筛选视图在请求时由未筛选缓存派生，不污染轮询缓存；`summary` 保持全量使指标卡/总览语义不变（筛选只影响任务列表） |
| 告警派生（TASK-026） | 服务端每轮轮询派生告警：顶层 `alerts`（count+items）+ 每项目 `alerts[]`；规则 = 读取错误 / 心跳卡死 / blocked 占比超阈值 / 非终态任务长期未更新；阈值配置在 `config/projects.json` | 告警规则集中在后端单一事实源（可测试、可扩展），前端横幅/告警列/导航计数直接消费服务端字段；由 TASK-021 的客户端轻量派生升级，避免前后端规则不一致 |
| 前端布局（TASK-021） | 侧边栏导航 + 顶栏筛选 + 指标卡/横幅/表/时间线/抽屉（依据 ui-mockup） | 高保真原型驱动；零依赖原生 JS 保持一致 |
| 趋势图（TASK-027） | 前端手写 SVG 折线图（`src/js/trend.js` 纯函数 + `main.js` DOM 渲染），数据源 `GET /api/history`；事件速率由相邻快照 `summary` 差**分桶统计**（状态迁移事件/小时） | 零依赖（A5）；趋势面板从占位升级为真实图表；事件速率无需后端改动（快照 summary 已含各状态计数），且事件时间线 API 的 limit（≤100）不足以支撑 30 天速率；分桶统计避免 30s 轮询下瞬时速率锯齿 |
| 主题（TASK-021/028） | CSS 变量双主题（dark/light），`data-theme` 切换 | 零依赖实现暗/浅色；持久化与系统跟随由 TASK-028 完善 |
| 采集方式抽象（TASK-031 规格） | `transport: local \| agent`；agent 推送模式（§3.1） | 多机监控（Windows/远程 Linux）需求；agent 只搬运不解析（解析单一事实源）、心跳用 last_seen、每机器一 agent；归属见 TASK-032 修订（aibase/kit/tools/agent/） |
| 多实例语义（TASK-032 修订） | 同逻辑项目多机器 = 多实例（id 唯一，如 baseline-dev/prod）；同一 project_id 只允许一个 agent（双 agent → 409）；`group` 字段可选分组 | 各机器 runtime 状态独立，必须实例级监控；禁止共享 id 互相覆盖污染；与 local 多 path 语义一致 |
| agent 归属修订（TASK-032 修订） | agent 从 aimonitor `tools/agent/` 改为**框架通用组件** `aibase/kit/tools/agent/`，随 mkproject 自动分发（零安装）；程序随项目分发、运行时每机器 1 实例（机器级配置）；协议解耦为 AIOS 通用遥测格式 | 用户提议消除被监控端额外安装；与 mkproject“生成即带”哲学一致；存量本地项目 transport=local 无需 agent |
