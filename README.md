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

```bash
python3 kit/tools/agent/agent.py --config /etc/aios/agent.json   # 常驻推送
python3 kit/tools/agent/agent.py --check-config --config ...     # 只校验配置
```

systemd / systemd timer / nohup / Windows Task Scheduler 部署方式见 `aibase/kit/tools/agent/README.md`。

### 验证

- 仪表盘 `/api/status`：agent 项目状态与 local 项目一致（任务/焦点/心跳/事件/计数）
- agent 整体离线 → 项目 `error: "agent 离线"`（`last_seen` 超 `heartbeat_stale_threshold_seconds`，本项目配置 900s）
- 多实例：同一逻辑项目多机器 = 多 id（`baseline-dev` / `baseline-prod`，可选 `group` 分组）；禁止共享 id（双 agent 抢推 → 409）

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
