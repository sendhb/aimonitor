# OPERATIONS — aimonitor 运维手册（多机监控）

> 本文档是**部署/运维操作指引**，不定义契约。契约唯一真相 = `docs/MONITOR-SPEC.md`（§3 Agent 推送 + 注册审批）；
> 架构决策见 `docs/decisions/ADR-002-agent-push.md` 与 `docs/decisions/ADR-003-registration-approval.md`；agent 组件说明见 `aibase/kit/tools/agent/README.md`。
> 本文档与实现行为逐项对照（2026-08-20，TASK-044/063）。

## 1. 架构速览

```
被监控机器（每机器 1 个 agent，独立进程，不在任何项目内）
  agent 读取本机各 AIOS 工程 runtime/（TASK-*.md / focus / heartbeat / events.jsonl / VERIFY / REVIEW）
        │  POST /api/ingest（Bearer token 鉴权，AIOS 通用遥测格式 file-oriented）
        ▼
aimonitor 服务端
  鉴权 → 限流 → 授权范围 → 落库 ingest_state（data/ingest.db，含 last_seen）→ 写历史快照
        ▼
  采集层 AgentReader 从 ingest_state 读 → /api/status / /api/history / /api/projects/:id/events
```

关键语义（详情见 MONITOR-SPEC §3.1）：

| 项 | 语义 |
|----|------|
| agent 只搬运不解析 | agent 读取 runtime/ 原始文件打包推送；解析全在服务端（两种 transport 共用同一解析逻辑） |
| 角色心跳 | `files.heartbeats[]` 携带 `{file, mtime}`，服务端按文件名识别 role，`age = now - mtime` |
| agent 整体离线 | ingest_state 无记录 或 `now - last_seen > heartbeat_stale_threshold_seconds` → 项目 `error = "agent 离线"` |
| 多实例 | 同逻辑项目多机器 = 多 id；同一 `project_id` 只允许一个 agent 推送（双 agent → 409） |

## 2. 配置示例

### 2.1 `config/projects.json`（服务端项目注册）

现有 local 项目缺省 `transport: local`，零迁移。新增远程项目加 `transport: "agent"`（`path` 仅展示用）：

```json
{
  "poll_interval_seconds": 30,
  "heartbeat_stale_threshold_seconds": 900,
  "ingest_rate_limit_per_minute": 60,
  "projects": [
    { "id": "aimonitor", "name": "aimonitor", "path": "/home/hb/code/aimonitor" },
    { "id": "win-proj",  "name": "Windows工程", "path": "/展示路径", "transport": "agent" },
    { "id": "remote-linux", "name": "远端Linux工程", "path": "/展示路径", "transport": "agent" }
  ]
}
```

多实例示例（同逻辑项目 `baseline` 两台机器，`id` 实例级唯一，可选 `group` 分组）：

```json
{
  "projects": [
    { "id": "baseline-dev",  "name": "baseline-dev",  "group": "baseline", "path": "/dev/code/baseline",  "transport": "agent" },
    { "id": "baseline-prod", "name": "baseline-prod", "group": "baseline", "path": "/prod/code/baseline", "transport": "agent" }
  ]
}
```

⚠ 同一 `project_id` 只允许一个 agent 推送；两个 agent 抢推同一 id → 服务端 `409`（记录 agent 来源，后到者拒绝）。

### 2.2 `config/agents.json`（服务端 agent token）

路径 `config/agents.json`，**权限必须 600**（`chmod 600 config/agents.json`）；含密钥，已 gitignore、禁止入库。
服务端启动时加载一次（改后需重启服务端）。两种格式：

**扁平格式**（每项目一专属 token，天然单 agent）：

```json
{
  "win-proj": "tok_A_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "baseline-dev": "tok_B_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
}
```

**agent 格式**（一 token 管多项目，token 授权项目集合 = `projects` 白名单）：

```json
{
  "web-01": {
    "token": "tok_C_cccccccccccccccccccccccccccccccc",
    "projects": ["win-proj", "remote-linux"]
  }
}
```

| 场景 | 推荐格式 |
|------|---------|
| 每项目一个独立 agent/token | 扁平 `{ "<project_id>": "<token>" }` |
| 一台机器多项目共用一 agent/token | agent `{ "<agent_id>": { "token", "projects" } }` |

fail-closed 语义（`load_agents_config`）：文件缺失、权限非 600、JSON 非法、顶层非对象 → 按 `{}` 处理，
即**全部 ingest 401**（服务端只服务 local 项目，不半载密钥）。

### 2.3 `agent.json`（被监控机器）

机器级配置（参考 `aibase/kit/tools/agent/agent.json.example`；`<...>` 占位符故意不可通过，复制后必须填写）：

```json
{
  "server_url": "https://aimonitor.example.com/api/ingest",
  "token": "tok_C_cccccccccccccccccccccccccccccccc",
  "projects": [
    { "id": "win-proj", "path": "/absolute/path/to/my-project" }
  ],
  "poll_interval_seconds": 30
}
```

字段说明与 CLI（`--check-config` / `--once` / `--interval` / `--quiet`）见 `aibase/kit/tools/agent/README.md`。

### 2.4 端到端最小示例

1. 服务端 `config/projects.json` 添加 `{ "id": "win-proj", "transport": "agent", ... }`
2. 服务端新建 `config/agents.json`：`{ "win-proj": "<token>" }`，`chmod 600`
3. 重启服务端（`bash scripts/stop.sh && bash scripts/start.sh`）——agents.json 启动时加载
4. 被监控机器写 `agent.json`（`server_url` 指向服务端 `/api/ingest`，token 同 `<token>`），`chmod 600`
5. 被监控机器启动：`python3 kit/tools/agent/agent.py --config agent.json`
6. 验证：`curl http://<aimonitor>:3113/api/status` 中 `win-proj` 与 local 项目同结构（任务/焦点/心跳/事件/计数）

## 3. Token 轮换

### 3.1 背景

- 服务端 `config/agents.json` **启动时加载一次** → 服务端侧轮换需重启服务端
- agent 侧 `agent.json` **启动时加载一次** → agent 侧轮换需重启 agent（`--once`/cron/timer 模式每轮重新加载，无需重启）
- agent 对 `401/4xx` 分类为**不可重试**错误（token 错 = 配置需修复；失败虽仍进入退避，但 4xx 每次重试都失败，等效持续 401）
  → 若先改 agent 侧，agent 会持续 401，直到服务端与新 token 对齐

### 3.2 步骤（顺序：服务端先、agent 后）

```bash
# 1) 生成新 token（也可用其它安全随机源）
openssl rand -hex 32
```

1. **服务端**：更新 `config/agents.json` 中对应条目的 token → `chmod 600 config/agents.json`
   → 重启服务端（`bash scripts/stop.sh && bash scripts/start.sh`）。
   重启瞬间 `/api/ingest` 短暂不可达；重启后旧 token 立即失效（新 token 生效）。
2. **被监控机器**：更新 `agent.json` 的 `token` → 重启 agent 进程
   （systemd：`sudo systemctl restart aios-agent`；nohup：`kill $(cat /var/run/aios-agent.pid)` 后重新启动）。
3. **验证**：agent 侧先 `python3 kit/tools/agent/agent.py --check-config --config agent.json`，
   再 `python3 kit/tools/agent/agent.py --once --config agent.json`（单轮推送）→ 确认无 401；
   服务端 `/api/status` 该项目 `last_seen` 刷新、无 `"agent 离线"`。

### 3.3 回滚

任一步失败 → 两端改回旧 token，重启服务端与 agent，确认恢复推送后再重试。

### 3.4 多 agent 轮换

逐个 agent 执行（每完成一个验证一个），避免同时中断全部远程项目。

## 4. Agent 离线排查

### 4.1 症状与定位

| 症状 | 含义 | 判定 |
|------|------|------|
| 项目 `error = "agent 离线"` | **agent 整体失联**（进程死/网络断/token 失效/从未推送） | ingest_state 无记录 或 `last_seen` 超 `heartbeat_stale_threshold_seconds` |
| `heartbeat-stale` 告警（role=coder/reviewer） | **远端角色进程卡死**，但 agent 在线 | 心跳文件存在且 `age > heartbeat_stale_threshold_seconds` |
| 项目 `error` 为其它文本 | 读取/解析失败 | 看具体 error 文本 |

> 两者可同时存在；`agent 离线` 时数据为空、历史快照留缺口（趋势缺口语义，不伪造成归零）。

### 4.2 检查清单（自底向上）

1. **agent 进程存活？** `ps -ef | grep agent.py` / `systemctl status aios-agent`
2. **agent 日志最近输出？**（位置见 §5）找 `401` / `网络失败` / `退避中` / `payload 构造失败`
3. **网络可达？** 从被监控机器 `curl -v -X POST http://<aimonitor>:3113/api/ingest`（预期 401 而非超时/拒连）；
   检查防火墙/反向代理/端口
4. **配置正确？** `agent.py --check-config`；`agent.json` 的 `server_url`/`token`/`projects[].id` 与服务端一致
5. **服务端日志？**（`runtime/logs/monitor-server.log`）服务端**不记录**逐请求 401/429/403/409（鉴权/限流失败仅返回状态码，无日志）；日志仅含启动告警（如 agents.json 缺失/权限非 600 的 fail-closed ⚠）、轮询/落库异常与 500。排查 401/429 以 **agent 侧日志**为主信号；服务端日志顺带确认 500/落库失败即可
6. **服务端 agents.json 权限？** 非 600 或文件缺失 → fail-closed 全部 401（`monitor-server.log` 有 ⚠ 提示）

### 4.3 常见根因

| 现象 | 根因 | 处置 |
|------|------|------|
| agent 日志持续 401 | token 与服务端不一致（轮换不同步/复制错误） | 按 §3 重新对齐两端 token |
| 服务端 429 | 超 `ingest_rate_limit_per_minute`（默认 60/min） | 降 agent 频率或调大限流；agent 自动退避（服务端返回 `Retry-After` 时按其退避，否则指数退避 1s→60s 兜底） |
| agent 日志“连接被拒/超时” | 网络/防火墙/服务端未启动 | 修网络；确认服务端监听 `0.0.0.0:<port>` |
| `/api/status` 一直 `agent 离线` 但 agent 在跑 | agent 配的 `project_id` 与服务端注册不一致（未推送成功过） | 核对 `agent.json` 与 `config/projects.json` 的 id |

### 4.4 恢复

agent 断线重连后**自动恢复**（服务端按下次成功推送刷新 `last_seen`）；
断线期间历史快照缺失段表现为趋势缺口（现有缺口语义）。若为 token/配置问题（4xx 不可重试——
虽进入退避但每次重试仍失败），需人工修复后重启 agent。

## 5. 日志位置

| 组件 | 位置 | 说明 |
|------|------|------|
| 服务端运行日志 | `runtime/logs/monitor-server.log` | `scripts/start.sh` 把 stdout+stderr 重定向至此；`--quiet` 只关轮询日志，错误仍输出 |
| 服务端数据 | `data/history.db` | 历史快照（SQLite，TASK-022） |
| 服务端数据 | `data/ingest.db` | agent 推送状态 `ingest_state`（含 `last_seen`/`agent_id`，TASK-033/034） |
| agent 日志 | 部署方式决定 | 本身无文件日志，输出走 stdout/stderr：systemd → `journalctl -u aios-agent -f`；nohup → 重定向文件（如 `/var/log/aios-agent.log`） |
| 被监控项目心跳 | `<project>/runtime/logs/autoloop-{coder,reviewer}.heartbeat` | 角色心跳文件（agent 只读并推送 mtime） |
| 被监控项目事件 | `<project>/runtime/logs/autoloop-{coder,reviewer}-events.jsonl` | 角色事件流（agent 只读并推送原文） |
| autoloop 框架日志 | `<project>/runtime/logs/autoloop-{coder,reviewer}-<date>.log` | 被监控项目自身的 AI 执行日志（与监控无关，排查被监控端常用） |

## 6. 安全注意

- `config/agents.json` / `agent.json` 含密钥：权限 `600`、gitignore、禁止入库/入提示词/入日志
- 服务端 `agents.json` 权限非 600 → fail-closed 全部 401（宁可不可用，不半载密钥）
- 401 不泄露任何状态数据（不区分“缺失”与“错误”token）
- token 按 §3 定期轮换；跨公网部署建议服务端前置 HTTPS 反向代理
- 同一 `project_id` 双 agent → 409（防跨 agent 互相覆盖污染）；多实例用不同 id

## 7. 注册审批流程（自助注册）

> 规格见 `docs/MONITOR-SPEC.md` §3.2（注册-审批-签发）。
> 决策记录见 `docs/decisions/ADR-003-registration-approval.md`。
> **能力边界**：服务端注册/审批/签发 API 与前端审批页已实现；**agent 端自动注册客户端
> 尚未实现**（`--register`/`--status` 子命令、`state=unregistered` 配置、自动轮询/自动
> 领取 token 属于 aibase agent 组件待实现契约，见 MONITOR-SPEC §3.2.10 与 ADR-003）。
> 当前按本章 API/页面步骤手工完成注册；等 aibase 交付后，被监控端可改为一行 `agent.py --register`。

### 7.1 流程概览

当前实现的完整链路（服务端 + 前端已实现，被监控端步骤为手工 API 操作）：

```
被监控机器（运维/脚本）
  │  curl POST /api/register（注册申请，可选携带注册码）
  ▼
服务端 → registration_request（status=pending）→ 返回 req_id
  ▼
管理员在 dashboard 申请队列看到申请（🔵 预授权 / 🟡 盲申请）
  │  输入 admin_password → ✅ 确认（POST /api/register/:req_id/approve）
  ▼
服务端 → 签发 token 写入 config/agents.json → status=approved
         └ 自动登记 config/projects.json（transport=agent，path=申请 path，TASK-069）
  ▼
申请方用 request_key 轮询 GET /api/register/:req_id/status
      首次 approved 响应携带 token（单次交付）
  ▼
配置 agent.json（token）→ 启动 agent 推送（projects.json 已自动登记，无需手工编辑）
```

> aibase agent 注册客户端落地后，"申请方"由 agent 自动完成（自动轮询、自动领 token、
> 自动写 agent.json、自动切换 active），流程即与 MONITOR-SPEC §3.2 数据流一致。

### 7.2 管理员初始化

首次启动时，若 `config/admin.json` 不存在，服务端自动生成 32 字符随机密码，并以**单行**
形式输出到 stdout（`ensure_admin_config()`，TASK-049）。已存在时不覆盖（幂等）。

**步骤：**

1. 启动服务端：`bash scripts/start.sh`
2. 在启动输出中找单行 `Admin password: <hex>`（示例）：

   ```
   aimonitor 监听 http://0.0.0.0:3113  (static: /home/hb/code/aimonitor/dist)
   Admin password: a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6
   ```

   （密码是单行输出；示例中的密码仅为演示，实际每次随机）
3. 立即保存到密码管理器。确认文件与权限：

   ```bash
   ls -la config/admin.json
   # -rw------- 1 user user 51 Aug 20 12:00 config/admin.json
   cat config/admin.json
   # {"admin_password": "a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6"}
   ```

> ⚠ 密码仅首次启动输出一次。**遗失后重建密码不会吊销已签发 token**：删除
> `config/admin.json` 并重启只会生成新密码，`config/agents.json` 中的 token 仍有效。
> 若确需撤销访问，按 §7.5 手动吊销/轮换对应申请。

### 7.3 生成注册码（可选）

注册码是预授权信任锚：携带注册码的申请标记 🔵 预授权，无码标记 🟡 盲申请。注册码不是
token，泄露后管理员可吊销重发。

**页面操作步骤：**

1. 打开仪表盘 `http://localhost:3113/`
2. 侧边栏点击「📋 注册申请」→ 默认「申请队列」tab → 切换「注册码管理」tab
3. 点击「生成注册码」，填写表单：
   - **描述**（必填）：如 "dev 团队机器"
   - **允许的 project_id 模式**（可选）：如 `baseline-*`（Glob 模式）
   - **最大使用次数**（默认 1）
   - **过期时间**（可选）
4. 确认后复制注册码（格式 `XXXXXXXX-XXXXXXXX`）

**命令行**（需 `Authorization: Bearer <admin_password>`）：

```bash
curl -X POST http://localhost:3113/api/register/codes/generate \
  -H "Authorization: Bearer <admin_password>" \
  -H "Content-Type: application/json" \
  -d '{"description": "dev 团队", "allowed_project": "baseline-*", "max_uses": 5}'
# → 200 {"code": "AbCd1234-EfGh5678", "description": "dev 团队",
#        "allowed_project_pattern": "baseline-*", "max_uses": 5,
#        "created_at": 1755691200.0, "expire_at": null}
```

请求体字段：`description`（必填字符串）、`allowed_project`（可选 Glob）、`max_uses`
（可选整数，默认 1）、`expire_at`（可选时间戳）。其它管理端点：
`GET /api/register/codes`（列表）、`POST /api/register/codes/:code/revoke`（吊销）。

### 7.4 审批流程

#### 管理员端（dashboard，已实现）

1. 侧边栏「📋 注册申请」badge 显示 pending 数量
2. 申请队列列表：project_id、host_info（hostname + IP）、申请时间、状态
   （🔵 预授权 / 🟡 盲申请 / ✅ 已批准 / ❌ 已拒绝 / ⏳ 已过期 / 🔒 已吊销）
3. 点击行展开详情（project_id、host_info、申请时间、注册码（如有）、状态、拒绝原因）
4. 首次操作弹出 admin_password 输入框（sessionStorage 暂存）
5. 操作按钮：
   - ✅ **确认**（approve）：签发 token 写入 `config/agents.json`，状态 → approved
   - ❌ **拒绝**（reject）：填写原因，状态 → rejected（原因在列表/详情展示）
   - 🔒 **吊销**（revoke）：仅 approved，从 `config/agents.json` 移除 token
   - 🔄 **轮换**（renew）：仅 approved，吊销旧 token + 签发新 token（状态保持 approved）

#### 被监控机器端（当前用 API 手工发起；agent 端 `--register` 未实现）

agent 端没有自动注册客户端，运维/脚本按以下步骤发起注册申请：

```bash
# 1) 生成 request_key（≥16 字节随机串，用于绑定申请身份；务必保存，领取 token 需要）
openssl rand -hex 16

# 2) 发起注册申请（enrollment_code 可选）
curl -X POST http://localhost:3113/api/register \
  -H "Content-Type: application/json" \
  -d '{"project_id": "baseline-dev",
       "path": "/home/user/code/baseline",
       "host_info": "hostname:dev-box, ip:192.168.1.20",
       "request_key": "<上一步生成的随机串>",
       "enrollment_code": "AbCd1234-EfGh5678"}'
# → 201 {"req_id": "R000001", "status": "pending", "pending_since": 1755691200.0}
```

字段：`project_id`（字母/数字/连字符，实例级唯一）、`path`（展示用）、`host_info`
（机器标识）、`request_key`（≥16 字节）、`enrollment_code`（可选）。
冲突（409）：project_id 已在 `config/projects.json`、已有活跃推送记录、或已有 pending
申请；rejected/expired 后可重新注册。

#### 审批后领取 token 并上线

```bash
# 3) 轮询审批结果（request_key 绑定；首次 approved 返回 token，仅一次）
curl "http://localhost:3113/api/register/R000001/status?request_key=<key>"
# → {"status": "approved", "token": "aimon_baseline-dev_<uuid4>_<urlsafe32>",
#    "project_id": "baseline-dev"}
```

各状态响应：pending → `{"status":"pending","pending_since":...}`；approved 首次 → 含 token；
approved 已交付 → 无 token；rejected → 含 reason；expired / revoked → 状态文本。
request_key 缺失或不匹配 → 404（不泄露 req_id 是否存在）。

**上线（agent 端不会自动完成，需手工）：**

1. 把 token 写入被监控机器 `agent.json`（普通推送配置，见 §2.3），`chmod 600`
2. **projects.json 已自动登记**（TASK-069：审批通过即写入 `transport: "agent"`，无需手工编辑/重启）；
   若确认未登记，可手工补 `config/projects.json` 后重启服务端
3. 启动 agent 推送：`python3 kit/tools/agent/agent.py --config agent.json`
4. 验证：`/api/status` 中该项目与 local 项目同结构、`last_seen` 刷新

> agent 端自动轮询 / 自动写 agent.json / 自动切换 state=active **尚未实现**（aibase
> 组件契约，MONITOR-SPEC §3.2.10），需人工执行上面 1-4 步。

### 7.5 吊销与轮换

场景：机器退役、token 泄露、管理员误批准、定期轮换。

#### 吊销（撤销已批准 token）

**操作步骤：**

1. dashboard 申请队列找到已批准申请 → 点击「🔒 吊销」
   （命令行：`POST /api/register/:req_id/revoke`，Bearer admin_password）
2. 服务端：从 `config/agents.json` 移除该 token → 状态 revoked

**后果（重要）**：agent **不会自动重新注册**。旧 token 下次推送收到 401 —— agent 将
401 视为不可重试错误（PushRejectedError），退避报错，不会自动恢复。吊销后如需该机器
继续监控，需重新走 §7.4 注册流程。

#### 轮换（更换 token）

**操作步骤：**

1. dashboard 找到已批准申请 → 点击「🔄 轮换」
   （命令行：`POST /api/register/:req_id/renew`，Bearer admin_password）
2. 服务端：吊销旧 token（从 agents.json 移除）+ 签发新 token（写入 agents.json），
   状态保持 approved、`token_delivered` 重置（status 端点可再次领取）
3. 用原 request_key 轮询 status 端点领取新 token（单次交付）：

   ```bash
   curl "http://localhost:3113/api/register/R000001/status?request_key=<key>"
   # → {"status": "approved", "token": "aimon_baseline-dev_<新token>", "project_id": "baseline-dev"}
   ```

4. 手动更新 agent.json 的 token → 重启 agent

**后果**：agent **不会自动领取新 token**（401 不可重试、退避报错），必须按第 3-4 步手工完成。

### 7.6 安全注意事项

- **admin_password 保管**：首次启动仅 stdout 输出一次，立即保存到密码管理器。
  **重建密码（删除 admin.json 重启）不吊销已签发 token**，如需吊销请按 §7.5 操作
- **config/admin.json 权限**：必须 600（`chmod 600 config/admin.json`），已 gitignore；
  非 600 / 缺失 / JSON 非法 → fail-closed（审批端点 401）
- **config/agents.json 权限**：必须 600；动态签发同样遵守；缺失或权限非 600 → 全部 ingest 401
- **token 单次交付**：token 仅通过 status 轮询首次返回一次，不落页面存储、不入日志、不入提示词
- **request_key 保管**：领取 token 的唯一凭证，等同 token 级别的秘密；丢失后只能吊销重新注册
- **局域网 HTTP 通信**：当前假设局域网内 HTTP，不强制 HTTPS；迁广域网需前置 HTTPS 反代
  （占位 TASK-064）
- **限流保护**：注册端点全局限流 60 次/分钟（可配置），防 bug 死循环
- **project_id 唯一性**：同一 project_id 只能有一个活跃申请（pending 或 approved），
  且已在 projects.json / 有活跃推送记录时注册返回 409
