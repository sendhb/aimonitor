# aimonitor — AI 项目执行监控

读取多个 AIOS 治理项目的执行状态（任务、进度、心跳、事件流、验证/审查记录），以网页仪表盘实时展示。

- 零第三方依赖：后端使用 Python 3.12 标准库
- 项目注册：`config/projects.json`（手动配置被监控项目）
- 展示内容：任务全景 / 统计 / 当前焦点 / 心跳存活 / 事件流 / 验证审查记录
- 规格来源：`docs/MONITOR-SPEC.md`（唯一真相）

## 目录结构

| 路径 | 说明 |
|------|------|
| `src/` | 前端源码（`index.html` / `css/style.css` / `js/main.js`） |
| `dist/` | 前端构建产物（由 `scripts/build.sh` 生成） |
| `server/monitor_server.py` | 后端采集服务（Python 3.12 标准库） |
| `config/projects.json` | 被监控项目注册表 |
| `docs/` | 架构与监控规格文档 |
| `runtime/` | 项目运行时数据（TASK/REVIEW/VERIFY/状态/日志） |
| `kit/` | AIOS 治理框架（只读，可整体升级；`aios/` `agents/` `cli/` 等均在其中） |
| `tmp/` | 项目内临时目录（截图/一次性产物，git 忽略，不进版本库） |

## 启动/停止（推荐用脚本）

```bash
./start.sh                        # 根目录 symlink：构建 + 启动 + 健康检查（默认 3113，服务 dist/）
bash scripts/start.sh             # 等价写法（指向同一脚本）
./start.sh --dev                  # 开发模式：服务 src/，不构建也可用 --no-build
./start.sh --port 8080            # 自定义端口
./stop.sh                         # 停止服务
```

`start.sh` 会：自动构建前端（`--no-build` 跳过）→ 后台启动 → 健康检查 → 记录 PID（`/tmp/aimonitor-monitor-server.pid`）→ 防重复启动。

## 手动启动（高级）

### 1. 构建前端（可选）

生产模式服务的是 `dist/` 构建产物，源码改动后需重新构建：

```bash
bash scripts/build.sh    # src/ → dist/，确定性构建，零网络、无依赖
```

### 2. 启动后端服务

```bash
python3 server/monitor_server.py [--port 3113] [--dev] [--quiet]
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `--port` | `3113` | 监听端口，绑定 `0.0.0.0` |
| `--dev` | 关 | 静态文件服务 `src/`（开发模式）；默认服务 `dist/` |
| `--quiet` | 关 | 关闭后台轮询日志 |

启动后后台线程按 `config/projects.json` 的 `poll_interval_seconds`（默认 30 秒）轮询各项目状态，聚合为 JSON 缓存。

### 3. 停止

```bash
bash scripts/stop.sh     # 或手动 kill PID
```

## 访问

- 仪表盘：`http://localhost:3113/`
- API 调试：`http://localhost:3113/api/status`（聚合 JSON）

## 多机监控（Agent 推送）

除本地项目（`transport: local`，服务端直读文件系统）外，aimonitor 还支持监控**远程机器**（Windows+WSL2 / 另一台 Linux）上的 AIOS 工程：被监控机器运行 **agent**（`aibase/kit/tools/agent/`，AIOS 通用遥测组件，随 `mkproject` 分发），只读本机工程 `runtime/` 并定时推送到服务端 `POST /api/ingest`。规格见 `docs/MONITOR-SPEC.md` §3.1；部署/运维详见 `docs/OPERATIONS.md`。

### 服务端注册（2 步）

1. `config/projects.json` 添加 `transport: "agent"` 项目（`path` 仅展示用，真实路径在 agent 侧配置）：

```json
{ "projects": [ { "id": "win-proj", "name": "Windows工程", "path": "/展示路径", "transport": "agent" } ] }
```

2. `config/agents.json` 添加 agent token（权限 `600`；含密钥，已 gitignore，不入库）：

```json
{ "win-proj": "<token>" }
```

> 扁平格式 `{ "<project_id>": "<token>" }` = 每项目一专属 token；agent 格式
> `{ "<agent_id>": { "token": "...", "projects": ["win-proj", ...] } }` = 一 token 管多项目。
> ⚠ `config/agents.json` 缺失或权限非 600 → 全部 ingest 401（fail-closed）。

### 被监控机器部署 agent

每台被监控机器运行 **1 个 agent 实例**（独立进程，不在任何项目内），机器级配置 `agent.json`：

```json
{
  "server_url": "http://<aimonitor-host>:3113/api/ingest",
  "token": "<token>",
  "projects": [ { "id": "win-proj", "path": "/home/user/code/my-project" } ],
  "poll_interval_seconds": 30
}
```

> 自助注册（审批后动态签发 token）已上线服务端与审批页，见下文「快速添加被监控机器」
> 与 `docs/OPERATIONS.md` §7。**agent 端自动注册客户端（`--register` 子命令、
> `state=unregistered` 配置、自动轮询/自动领取 token）尚未实现**——当前 agent 仅支持
> 已有 token 的推送，注册需按 API 手工完成（见下文）。

```bash
python3 kit/tools/agent/agent.py --config /etc/aios/agent.json   # 常驻推送（已有 token）
python3 kit/tools/agent/agent.py --check-config --config ...     # 只校验配置
python3 kit/tools/agent/agent.py --once --config agent.json      # 单轮推送（cron/timer）
```

systemd / systemd timer / nohup / Windows Task Scheduler 部署方式见 `aibase/kit/tools/agent/README.md`。

### 验证

- 仪表盘 `/api/status`：agent 项目状态与 local 项目一致（任务/焦点/心跳/事件/计数）
- agent 整体离线 → 项目 `error: "agent 离线"`（`last_seen` 超 `heartbeat_stale_threshold_seconds`，本项目配置 900s）
- 多实例：同一逻辑项目多机器 = 多 id（`baseline-dev` / `baseline-prod`，可选 `group` 分组）；禁止共享 id（双 agent 抢推 → 409）

## 快速添加被监控机器（自助注册）

> 规格见 `docs/MONITOR-SPEC.md` §3.2（注册-审批-签发）。
> 运维详情见 `docs/OPERATIONS.md` §7（注册审批流程）。
> ⚠ **能力边界**：服务端注册/审批/签发 API 与审批页已实现；**agent 端自动注册客户端
> （`--register` / `state=unregistered` / 自动轮询领 token）尚未实现**（依赖 aibase
> agent 组件，MONITOR-SPEC §3.2.10）。以下按当前可用 API 手工完成。

### 5 步流程

**第 1 步：管理员首次启动**

服务端首次启动自动生成 `config/admin.json`（32 字符随机密码），stdout **单行**输出
`Admin password: <hex>`，立即保存到密码管理器。

**第 2 步：管理员生成注册码（可选）**

dashboard → 📋 注册申请 → 注册码管理 → 生成注册码；
或命令行 `POST /api/register/codes/generate`（Bearer admin_password，见 OPERATIONS §7.3）。
有码的申请标记 🔵 预授权，审批更快。

**第 3 步：被监控机器发起注册申请**

agent 端自动注册客户端未实现，当前用 API 发起（`request_key` 为 ≥16 字节随机串，务必保存）：

```bash
curl -X POST http://<aimonitor-host>:3113/api/register \
  -H "Content-Type: application/json" \
  -d '{"project_id": "baseline-dev", "path": "/home/user/code/baseline",
       "host_info": "hostname:dev-box, ip:192.168.1.20",
       "request_key": "<随机串>", "enrollment_code": "<注册码，可选>"}'
# → 201 {"req_id": "R000001", "status": "pending", "pending_since": 1755691200.0}
```

**第 4 步：管理员审批**

dashboard → 📋 注册申请 → 申请队列 → 查看详情 → ✅ 确认（输入 admin_password）；
或命令行 `POST /api/register/R000001/approve`（Bearer admin_password）。

**第 5 步：领取 token 并上线**

```bash
# 轮询审批结果（首次 approved 返回 token，仅一次）
curl "http://<aimonitor-host>:3113/api/register/R000001/status?request_key=<随机串>"
# → {"status": "approved", "token": "aimon_...", "project_id": "baseline-dev"}
```

1. 将 token 写入被监控机器 `agent.json`（普通推送配置，见上节），`chmod 600`
2. **projects.json 已自动登记**（TASK-069：审批通过即写入 `transport: "agent"`，无需手工编辑/重启）；
   若确认未登记，可手工补 `config/projects.json` 后重启服务端
3. 启动 agent 推送：`python3 kit/tools/agent/agent.py --config agent.json`
4. 仪表盘 `/api/status` 中该项目的状态与 local 项目一致

> 等 aibase 交付 agent 注册客户端后，被监控端可简化为一行 `agent.py --register`，
> 自动轮询审批结果并领取 token；当前需按第 5 步手工完成。

## 常见问题

### 注册被拒绝

管理员拒绝后，该申请的 `status=rejected`，原因在申请列表/详情中展示
（`GET /api/register/list`）。

**处置：**

1. 确认拒绝原因（如 project_id 冲突、非授权机器）
2. 修改 `agent.json` 中的 project_id 或补充 enrollment_code
3. 重新发起注册申请（`POST /api/register`；rejected 后可复用同 project_id）

### Token 丢失

agent 拿到 token 后写入 `agent.json` 本地文件。若文件损坏或丢失，agent 会持续收到 401
（401 是**不可重试**错误，agent 退避报错，**不会自动恢复**）。

**处置：**

1. 管理员在 dashboard 找到该项目的已批准申请，点击「🔄 轮换」
   （或命令行 `POST /api/register/:req_id/renew`）
2. 轮换后新 token 已写入 agents.json；用原 request_key 轮询 status 端点领取（单次交付）：
   `curl "http://<aimonitor-host>:3113/api/register/R000001/status?request_key=<key>"`
3. 手动更新 `agent.json` 的 token → 重启 agent

### 机器更换

被监控机器故障或更换硬件后，原 project_id 对应的 token 在新机器上不存在。

**处置：**

1. 管理员在 dashboard 中吊销旧机器的 token（🔒 吊销）
2. 新机器上用**新的 project_id** 配置 `agent.json` 并走「快速添加被监控机器」流程注册
3. 注意：复用原 project_id 需先吊销旧 token，且该 id 不能在 projects.json 或有活跃
   推送记录（否则注册返回 409）；当前实现无清除历史记录的接口，机器更换建议使用新 id

## 配套命令

定义于 `aios.config.yaml`：

| 命令 | 作用 |
|------|------|
| `bash scripts/build.sh` | 构建前端 src/ → dist/ |
| `node --check src/js/main.js && python3 -m py_compile server/monitor_server.py` | lint 语法检查 |
| `node test/hello.test.js && python3 test/server_smoke.py` | 单元/冒烟测试 |
| `test -f dist/index.html && ...` | 校验 dist 构建产物 |

## 环境要求

- Python ≥ 3.12（仅标准库，无 pip 依赖）
- Node.js（仅用于 lint/test 前端 JS）

*维护：AIOS Framework。项目结构说明见 `AGENTS.md`。*

## 署名

- **作者 / Maintainer**：hb <sendhb@21cn.com>

## 许可证

本项目采用 [MIT License](LICENSE) 发布。

Copyright (c) 2026 hb <sendhb@21cn.com>
