# tools/agent

> 见 [tools/README.md](../README.md)

AIOS 通用遥测推送 agent：读取被监控项目 `runtime/` 状态（TASK / 焦点 /
心跳 / 事件 / 验证计数），按固定间隔推送到监控端（aimonitor ingest）。
随 mkproject 自动分发到新项目（TASK-029 集成验证）。

组件分层（自底向上，TASK-022..027）：

| 模块 | 职责 | 任务 |
|------|------|------|
| `agent_config.py` | agent.json 配置加载/校验 | TASK-022 |
| `agent_runtime.py` | 被监控项目 runtime/ 读取 | TASK-023 |
| `agent_payload.py` | 遥测 payload 构造与容量上限 | TASK-024 |
| `agent_http.py` | ingest HTTP 推送客户端 | TASK-025 |
| `agent_retry.py` | 指数退避状态机 | TASK-026 |
| `agent_loop.py` + `agent.py` | 主循环与 CLI 入口 | TASK-027 |

## 配置（agent.json）

示例见 [`agent.json.example`](agent.json.example)——占位符故意不可通过，
防止复制后不填写直接运行：

```json
{
  "server_url": "https://aimonitor.example.com/api/ingest",
  "token": "<your-ingest-token>",
  "projects": [
    {"id": "my-project", "path": "/absolute/path/to/my-project"}
  ],
  "poll_interval_seconds": 30
}
```

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `server_url` | ✅ | — | 监控端 ingest 完整地址（http/https，不含 userinfo） |
| `token` | ✅ | — | 推送到监控端的 Bearer token（机密；配置文件建议 `chmod 600`） |
| `projects` | ✅ | — | 被监控项目数组，至少 1 个 |
| `projects[].id` | ✅ | — | 项目唯一标识；多实例部署时在监控端全局唯一（见"多实例 id 约定"） |
| `projects[].path` | ✅ | — | 被监控项目绝对路径（项目根：含 `runtime/` 的目录） |
| `poll_interval_seconds` | — | 30 | 轮询间隔（秒），必须为正数；`--interval` 可临时覆盖 |

校验语义（`agent_config.validate`）：

- 缺失必填字段 / 非法类型 / 非正轮询间隔 → stderr 聚合报错并 **exit 1**；
- 空字符串与 `<...>` 占位符视为未填写（复制 `agent.json.example` 不填就跑会直接报错）；
- 未知多余字段忽略（向前兼容）。

## 使用（CLI）

```bash
python3 agent.py --check-config [--config agent.json]   # 只校验配置（exit 0/1）
python3 agent.py [--config agent.json]                  # 常驻：按 poll_interval_seconds 轮询
python3 agent.py --once                                 # 单轮后退出（cron / systemd timer）
python3 agent.py --interval 10                          # 覆盖轮询间隔（秒）
python3 agent.py --quiet                                # 只输出错误，常规日志静默
```

- `--config` 默认读取当前目录 `./agent.json`；
- SIGINT/SIGTERM → 干净退出（exit 0，无 traceback）；在途 HTTP 请求自然完成；
- 每项目独立退避：A 项目失败退避不阻塞 B 项目推送。

## 部署

### systemd（Linux 常驻，推荐）

`/etc/systemd/system/aios-agent.service`：

```ini
[Unit]
Description=AIOS telemetry agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=myuser
WorkingDirectory=/srv/my-project
ExecStart=/usr/bin/python3 /srv/my-project/kit/tools/agent/agent.py --config /etc/aios/agent.json
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aios-agent
sudo systemctl status aios-agent    # 查看状态
journalctl -u aios-agent -f         # 查看日志
```

### systemd timer（定时单轮）

用 `--once` 每轮独立推送；失败以 stderr 呈现（退出码恒 0，由日志/告警观察）：

```ini
[Unit]
Description=AIOS telemetry agent (once)
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /srv/my-project/kit/tools/agent/agent.py --once --config /etc/aios/agent.json
[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
```

### nohup（无 systemd 环境）

```bash
cd /srv/my-project
nohup python3 kit/tools/agent/agent.py --config /etc/aios/agent.json \
  >> /var/log/aios-agent.log 2>&1 &
echo $! > /var/run/aios-agent.pid
kill "$(cat /var/run/aios-agent.pid)"   # SIGTERM 干净退出
```

日志建议配合 logrotate 轮转；`--once` + cron 可代替常驻（等价 systemd timer）。

### Windows Task Scheduler

任务计划程序 GUI：创建任务 → 触发器（登录/启动/按间隔）→ 操作 → 启动程序 →
`python.exe` + 参数 `C:\srv\my-project\kit\tools\agent\agent.py --config C:\etc\agent.json`。

等价 schtasks 命令（开机启动常驻；建议 `python.exe` 用绝对路径，SYSTEM 账户
PATH 可能与交互用户不同）：

```bat
schtasks /Create /TN "AIOS Agent" /SC ONSTART /RU SYSTEM ^
  /TR "\"C:\Python312\python.exe\" C:\srv\my-project\kit\tools\agent\agent.py --config C:\etc\agent.json"
```

按间隔定时单轮（等价 systemd timer）：`/SC MINUTE /MO 5` + 参数加 `--once`。
所有路径必须用绝对路径；`agent.json` 含 token，注意文件访问权限。

## 与 aimonitor ingest 契约对应

### 请求

| 项 | 值 |
|----|----|
| 方法/地址 | `POST`（完整地址 = `server_url`，示例默认 `/api/ingest`） |
| 请求头 | `Authorization: Bearer <token>`、`Content-Type: application/json`、`User-Agent: aibase-agent/0.1` |
| 请求体 | `serialize_payload()` 输出的紧凑 JSON（UTF-8） |
| 超时 | connect 5s / read 30s（两阶段分离） |

### Payload（AIOS 通用遥测格式）

```json
{
  "project_id": "proj-1",
  "ts": 1786892400.0,
  "files": {
    "tasks": [{"name": "TASK-001.md", "content": "..."}],
    "focus": "...",
    "heartbeats": [{"file": "x.heartbeat", "mtime": 1786892280.0}],
    "events": [{"name": "x-events.jsonl", "content": "..."}],
    "verification_count": 3,
    "review_count": 1
  }
}
```

容量上限：tasks/events/heartbeats 各 ≤ 50 条；单条 content/focus ≤ 4096 字符
（超长截尾 + `…[truncated]` 标记）；整体 ≤ 256 KiB（超出抛 `PayloadTooLargeError`，本轮跳过推送）。

### 状态码（ingest 返回）

| 状态码 | agent 行为 | 可重试 |
|--------|-----------|--------|
| 2xx | 成功（`PushResult`） | — |
| 3xx | 拒绝，不跟随重定向（防 Authorization 异源泄露、防 POST→GET 降级） | 否 |
| 400/401/409/413 及其它 4xx | 拒绝——payload/token/配置需修复 | 否 |
| 429 | 限流——优先按 `Retry-After`（整数秒）退避 | 是 |
| 5xx | 服务端暂时故障 | 是 |
| 网络失败/超时 | 连接被拒 / DNS / 超时 | 是 |

### 重试退避

指数退避 1s→2s→4s→…→32s→60s（cap 60s 恒定）；每项目独立状态；成功 → 计数清零并解除退避；退避中 `can_push()` 跳过本轮，**不阻塞**下一轮轮询。

## 多实例 id 约定

监控端以 `project_id` 为键聚合遥测，因此：

- **全局唯一**：同一监控端下，所有 agent 实例的 `projects[].id` 必须互不相同，
  否则不同项目/主机的遥测互相覆盖混淆。
- **建议格式**：`<hostname>:<project>`（如 `web-01:my-project`）；
  多环境可加环境前缀（如 `prod-web-01:my-project`）。
- **稳定性**：id 一旦发布保持稳定（时间序列按 id 连续聚合），不要用随机值/时间戳。
- **多项目单实例**：一台主机监控多个项目 → 一个 agent.json 配多个 `projects`
  条目，各 id 仍须全局唯一。
- **单项目多实例（HA/负载均衡）**：每个实例用独立 id（如 `my-project-a` /
  `my-project-b` 或主机名后缀），不要共用同一 id。

## 注册（Registration）

> 从 v0.2 起，agent 支持**自助注册**：向 aimonitor 服务端发起注册申请，
> 管理员审批通过后自动获取 token，无需预先手工配置 token。

### 状态机

```
unregistered → pending → approved (active)
                       → rejected → retry
                       → expired → re-register
approved → revoked → re-register
```

- `state=unregistered`：初始状态，无 token，需要注册
- `state=pending`：已提交注册申请，等待管理员审批
- `state=active`：已有 token，正常运行（存量 agent 的缺省状态，零迁移）

### agent.json 新增字段

| 字段 | 必填 | 缺省 | 说明 |
|------|------|------|------|
| `state` | — | `active` | agent 状态：`unregistered` / `pending` / `active`。缺省 `active` 兼容存量 agent |
| `req_id` | — | — | 注册成功后服务端返回的申请 ID，pending 状态下存在 |
| `request_key` | — | — | agent 自生成的随机密钥，用于轮询时绑定身份（pending 状态下存在） |

`state=unregistered` 时，`token` 字段可为空；`state=active` 时，`token` 必填（与现有校验一致）。

示例（注册前）：
```json
{
  "server_url": "http://aimonitor.local:3113/api/ingest",
  "state": "unregistered",
  "projects": [
    {"id": "my-machine:my-project", "path": "/home/user/code/my-project"}
  ],
  "poll_interval_seconds": 30
}
```

示例（注册后，pending）：
```json
{
  "server_url": "http://aimonitor.local:3113/api/ingest",
  "state": "pending",
  "req_id": "<server-returned-uuid>",
  "request_key": "<agent-generated-secret>",
  "projects": [
    {"id": "my-machine:my-project", "path": "/home/user/code/my-project"}
  ],
  "poll_interval_seconds": 30
}
```

示例（注册成功，active）：
```json
{
  "server_url": "http://aimonitor.local:3113/api/ingest",
  "token": "aimon_my-machine:my-project_xxx_yyy",
  "state": "active",
  "projects": [
    {"id": "my-machine:my-project", "path": "/home/user/code/my-project"}
  ],
  "poll_interval_seconds": 30
}
```

### CLI 注册命令

```bash
python3 agent.py --register [--config agent.json]
# unregistered：构建申请 → POST /api/register 提交 → 成功写 pending（req_id/request_key）→ 进入轮询
#   （失败不修改 agent.json：409 已注册/已有申请、4xx 不可重试、5xx/网络可重试）
# pending：输出“正在等待审批（req_id: xxx）”并进入轮询
# active：输出“已注册，无需重复注册”

python3 agent.py --register --status [--config agent.json]
# 查看当前注册状态（unregistered/pending/active）

python3 agent.py --register --reset [--config agent.json]
# 恢复被污染/卡死的 pending → unregistered（清除 req_id/request_key）
```

> 注：`--enrollment-code` 尚未接线 CLI（`build_register_payload` 构造层已支持，后续版本开放）。

### 注册端点契约

> ✅ 契约状态：`POST /api/register` 提交胶水已接通（TASK-014）——`agent.py --register` 在
> state=unregistered 时构建 payload → `submit_register()` POST /api/register → 成功写
> pending（req_id/request_key 入 agent.json）→ 进入轮询（TASK-043）。

#### `POST /api/register`

向 aimonitor 服务端发起注册申请（CLI 已接线，TASK-014）。

URL 推导：从 `server_url`（如 `http://aimonitor.local:3113/api/ingest`）替换路径为 `/api/register`。

请求体（`build_register_payload()` 构造，对齐 aimonitor MONITOR-SPEC §3.2）：
```json
{
  "project_id": "my-machine-my-project",
  "path": "/home/user/code/my-project",
  "request_key": "<agent生成的随机密钥>",
  "host_info": "hostname:my-machine, ip:192.168.1.5",
  "enrollment_code": "ABC123-XYZ789"
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `project_id` | ✅ | 实例级唯一 id（**仅字母数字连字符**，服务端 `^[a-zA-Z0-9-]+$`），与 agent.json 中 projects[].id 一致 |
| `path` | ✅ | 被监控项目路径 |
| `request_key` | ✅ | 自生成随机密钥（token_urlsafe(24)，24 字节熵/32 字符），用于轮询时绑定身份 |
| `host_info` | ✅ | 机器标识**字符串**：`hostname:<h>, ip:<ip>`（ip 缺省可省略） |
| `enrollment_code` | — | 可选，预授权注册码（管理员提供） |

响应：

| 状态码 | 响应体 | agent 行为 |
|--------|--------|-----------|
| 201 | `{ "req_id": "<uuid>", "status": "pending", "pending_since": <epoch> }` | 保存 req_id，切换到 pending 状态，开始轮询 |
| 409 | `{ "error": "...", "existing": "active\|pending" }` | 已注册 → 提示用户；已存在 pending → 继续轮询旧 req_id |
| 400/429 | `{ "error": "..." }` | 退避重试 |

#### `GET /api/register/:req_id/status`

轮询审批结果。

URL：`GET <server_url_base>/api/register/<req_id>/status?request_key=<key>`

| 状态码 | 响应体 | agent 行为 |
|--------|--------|-----------|
| 200 pending | `{ "status": "pending", "pending_since": <epoch> }` | 继续轮询（间隔 30s） |
| 200 approved | `{ "status": "approved", "token": "<token>", "project_id": "<id>" }` | 保存 token，写入 agent.json，切换 state=active，开始正式推送 |
| 200 rejected | `{ "status": "rejected", "reason": "..." }` | 打印错误，退出（或等待人工介入） |
| 200 expired | `{ "status": "expired" }` | 提示重新注册 |
| 200 revoked | `{ "status": "revoked" }` | 提示重新注册 |
| 404 | — | request_key 不匹配或 req_id 不存在 → 退避重试 |

### Token 格式

```
aimon_{project_id}_{uuid4}_{random_hex}
```

示例：`aimon_my-machine:my-project_550e8400-e29b-41d4-a716-446655440000_a1b2c3d4`

- 前缀 `aimon_` 便于识别来源
- 中段 `project_id` 便于审计
- 后段 `uuid4` + `random_hex` 保证不可猜测

### 轮询失败处理

| 失败类型 | 行为 |
|---------|------|
| 网络错误/超时 | 指数退避（复用现有 `agent_retry.py`），最长 60s cap |
| 4xx（不含 404） | 不重试，打印错误，退出 |
| 404 | 退避重试（可能 req_id 尚未同步），3 次后退出 |
| pending TTL 超时（缺省 7 天） | 打印提示，退出（重新注册） |

### 依赖

agent 注册功能依赖 aimonitor 服务端 `TASK-046..054`（注册-审批-签发 API）。
注册端点契约与 aimonitor `docs/MONITOR-SPEC.md §3.2` 对齐。
