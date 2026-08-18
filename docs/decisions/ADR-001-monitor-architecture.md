# ADR-001 — aimonitor 监控架构：单进程 Python 标准库后端 + 零依赖前端

> 关联 TASK-001（规格：docs/MONITOR-SPEC.md）
> 日期：2026-08-02

## 背景

aimonitor 需要读取多个 AIOS 项目的 runtime 状态并网页展示。需求约束：零依赖优先、当前服务器即运营环境（端口可暴露）、简单稳定、先用自身 dogfooding。

候选形态：A) 纯静态（cron 生成 JSON + 静态页）；B) 轻量常驻后端；C) 引入框架后端（FastAPI/Express 等）。

## 决策

- **形态**：B — 轻量常驻后端（单进程单端口）
- **技术栈**：Python 3.12 **标准库**（`http.server` + `json` + `os` + `time` + `threading`），零第三方依赖
- **进程模型**：`ThreadingHTTPServer`（服务 API + 静态页）+ 单个后台轮询线程（内存缓存聚合结果）
- **数据存储**：无数据库，聚合结果存内存；重启即重扫
- **前端**：零依赖原生 JS，定时 fetch `/api/status` 渲染
- **项目注册**：`config/projects.json` 手动配置（A1.a）
- **监控协议**：只读被监控项目文件系统，见 `docs/MONITOR-SPEC.md` §3

## 后果

- ✅ 零依赖：`pip install` 不需要，`nohup python3 server/monitor_server.py` 即可运行
- ✅ 简单稳定：单进程、单端口（默认 3113）、重启无状态
- ✅ 实时：后台线程定时轮询，API 响应 O(1)
- ⚠️ 并发上限：ThreadingHTTPServer 适合低流量内部监控页，不适合高并发公网
- ⚠️ 内存缓存：多实例部署时各实例独立缓存（本场景单实例，可接受）
- ⚠️ 被监控项目需对运行账号可读（只读权限即可）
- 🔗 相关模块：`config/`（注册表）、`server/`（采集+API）、`src/`（展示）、`aios.config.yaml`（命令配置）

## 备选方案

- **A 纯静态 + cron**：不选——多组件（cron + 静态服务）协调更复杂；心跳"新鲜度"判断需要时基，cron 方案要在脚本里带时间戳，反而绕
- **C 框架后端（FastAPI/Express）**：不选——违背"尽量零依赖"；杀鸡用牛刀

## 语言选型详细对比（2026-08-02 补充）

> 背景：用户追问"为什么不用 Node/Go/PHP/其他"。环境探测结果 + 四重过滤如下，作为本 ADR 的决策依据存档。

### 环境探测结果（服务器）

| 语言 | 本机状态 |
|------|---------|
| Python | ✅ 3.12.3 |
| Node.js | ✅ v20.20.0 |
| Go | ✅ 1.26.3 |
| Perl | ✅ 有（版本信息为空） |
| PHP | ❌ 未安装 |
| Ruby | ❌ 未安装 |
| Rust (cargo/rustc) | ❌ 未安装 |
| .NET (dotnet) | ❌ 未安装 |
| Java | ⚠️ 仅有运行时 openjdk 21，无 javac（不能编译） |
| Web 框架 (flask/fastapi) | ❌ 未安装（pip 可用，但违背零依赖） |

### 四重过滤（选型收敛逻辑）

```
① 零依赖约束    → 排除需安装框架/编译链：Rust(Cargo)、C#/Java(编译)、Node 框架、PHP Swoole
② 已装环境      → 只剩 Python / Node / Perl
③ 混合负载模型  → 排除 Node（单线程事件循环，同步读文件会阻塞 API）、Perl（工程风险）
④ 简单稳定      → Python 标准库胜出（单进程、线程隔离、无构建）
```

### 候选对比表（零依赖口径）

| 语言/方案 | 部署 | 混合负载（轮询+API） | 本项目匹配 | 不选理由 |
|---|---|---|---|---|
| **Python stdlib（选中）** | nohup 一行 | ✅ 线程天然隔离 | ★★★ | — |
| Node 原生 http | nohup 一行 | ⚠️ 单线程，需防阻塞设计 | ★★ | 同步读文件阻塞 API，复杂度上升 |
| Go stdlib net/http | 需 go build | ✅ goroutine | ★★ | 引入编译链；无 yaml 需手写 frontmatter 解析；并发过剩 |
| PHP（`php -S`/FPM） | 多组件或开发级 server | ❌ 请求-响应模型，无后台轮询 | ★ | 模型错配；需 Swoole/cron+缓存才可行 |
| Rust | 需编译 | ✅ | ★ | 标准库无 HTTP 需引 crate；性能过剩；学习曲线 |
| Java | 需编译+JVM | ✅ | ★ | 本机无 javac；JVM ~200MB；样板多 |
| C#/.NET | 需 SDK+编译 | ✅ | ★ | 未安装；依赖重 |
| Ruby | gem 依赖 | ✅ | ★ | 未安装；生态偏 Web 框架 |
| Perl | 可零依赖 | ⚠️ | ★ | 语法老旧、生态萎缩、维护风险 |
| 框架方案（Flask/FastAPI/Express/Fastify/Gin/Echo） | 需依赖 | — | ★★ | 功能过剩：本项目无需高并发/类型校验/自动文档 |

### 换选触发器（当前均不满足）

| 触发条件 | 应换 |
|---|---|
| 被监控项目 >50、轮询变慢 | Go（并发遍历）或 Python 异步 |
| WebSocket 实时推送/认证/多用户 | FastAPI 或 Node+Express |
| 容器最小镜像部署 | Go 单二进制（~15MB） |
| 模板渲染型传统 Web 应用 | PHP/传统栈 |
| frpc 暴露为公网高并发服务 | Go + 反向代理 |

**结论**：本场景（低流量内部监控 + 零依赖 + 简单稳定）下 Python 3.12 标准库是唯一同时满足已安装 / 零依赖 / 常驻混合负载 / 开发快 的选项。

## 追加：sandbox-review 网络权衡（2026-08-02，方案 D，TASK-005/006）

### 背景
无人值守审查需要 claude 在容器内联网调用 Anthropic API，与 cli/sandbox-run 的 --network none（Rule of Two 机械落地）冲突。

### 决策
新增 `cli/sandbox-review`：--network bridge + 挂载 ~/.claude 凭据 + aios-sandbox-review 镜像（预装 claude）。

### Rule of Two 分析（Lethal Trifecta，security-policy.md）
| 能力 | 审查可信项目代码时 |
|---|---|
| ① 不可信输入 | ❌ 无（输入 = 本项目/受信项目代码，受控） |
| ② 敏感数据 | ✅ 有（~/.claude 凭据） |
| ③ 修改状态 | ✅ 有（写 REVIEW、执行命令） |

②+③ 且无① → **不违反 Rule of Two**，bridge 可接受。

### 硬性边界（违反即回退）
- ❌ **禁止**用 sandbox-review 审查不可信外部输入（外部下载代码/内容）——那构成 ①+②，必须用 cli/sandbox-run（无网络）或人工审查。
- 其余隔离保留：--read-only 根 FS、只挂载项目目录、容器 --rm 一次性。
- 凭据只读挂载进一次性容器，容器销毁后无残留。

### 后果
- ✅ 无人值守审查自动化可用
- ⚠️ 网络隔离被削弱（仅限审查可信代码场景）
- ⚠️ 镜像 1.37GB（基础 337MB + node/npm/claude）
- 🔗 相关：cli/sandbox-review、tools/sandbox/Dockerfile.review、cli/autoloop-*

## 追加：历史趋势存储引入 SQLite（2026-08-15，TASK-022）

### 背景
TASK-022 需要跨重启持久化的历史采样（每轮轮询快照）供趋势图（TASK-027）使用；纯内存缓存重启即丢，无法支撑 24h/7d/30d 趋势。

### 决策
- **存储**：在"内存缓存当前状态"基础上，追加 SQLite（Python 3.12 **stdlib** `sqlite3`）持久化历史快照，文件位于 aimonitor 自身 `data/history.db`（gitignore 排除，不进仓库）。
- **采样**：每轮轮询为每个读取成功的项目写一行 `snapshots(ts, project, summary_json, coder_alive, reviewer_alive)`；读取失败不采样（留缺口，不写假 0）。
- **保留**：`history_retention_days`（默认 90 天），轮询时惰性清理。
- **只读约束不变**：对被监控项目 runtime 仍只读；`data/` 是监控器自身存储（MONITOR-SPEC §3 例外说明）。

### 理由
- 零第三方依赖（A5）：`sqlite3` 是标准库，不引入 pip 依赖。
- 趋势必须跨重启：文件 DB 是唯一零依赖持久化选项（JSON 文件需自行索引/并发控制，SQLite 已解决）。
- 低流量场景：单进程 + 单写者（轮询线程），SQLite WAL 足够；不做多实例共享。

### 后果
- ✅ 趋势图数据可用（TASK-027 消费 `/api/history`）
- ⚠️ 增加 `data/` 目录与轮询落库写路径（原为纯读+内存）；`/api/status` 契约不变
- 🔗 相关：`docs/MONITOR-SPEC.md` §4.1/§4.2、`server/monitor_server.py`、TASK-027
