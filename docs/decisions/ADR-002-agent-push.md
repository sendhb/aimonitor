# ADR-002 — 多机监控：Agent 推送模式（transport 抽象 + ingest + last_seen）

> 关联 TASK-031（规格：docs/MONITOR-SPEC.md §3.1）
> 日期：2026-08-16

## 背景

aimonitor 需要监控**远程机器**（Windows+WSL2、另一台 Linux）上的 AIOS 工程。现有架构
（ADR-001）为服务端**本地直读**被监控项目文件系统（`transport: local`），无法到达远程机器。

候选方案：
- A) 网络挂载（SMB/NFS/sshfs 把远程 runtime 当本地路径）
- B) SSH 拉取（服务端主动 SSH 读远端文件）
- C) Agent 推送（被监控机器跑 agent，主动 POST 状态到服务端）

## 决策

- **采用 C — Agent 推送模式**（`transport: "agent"`），保留 `transport: "local"` 为默认
- **agent 只搬运不解析**：agent 读取被监控工程 `runtime/` 原始文件内容并打包推送；
  解析全部留在服务端（复用现有 `collect_tasks`/`read_heartbeat`/`read_events`），
  保证两种 transport 结果一致、解析逻辑单一事实源
- **部署粒度：每台被监控机器 1 个 agent**（独立守护进程，不在任何项目内），管理该机多个工程；
  被监控项目零部署、零改动
- **心跳语义（方案 a，保持 local 语义）**：`files.heartbeats[]` 携带 `{file, mtime}`（file = 远端
  心跳文件名，服务端按文件名识别 role），`heartbeat.age_seconds = now - mtime`（远端 role 卡死可检测，
  与 local 模式一致）；
  **agent 整体离线用 `last_seen`**：`now - last_seen > 阈值` → `error="agent 离线"`（区分角色卡死与 agent 失联）
- **ingest 契约格式（2026-08-17 修订）**：ingest 请求体对齐 **AIOS 通用遥测格式**（file-oriented 文件条目数组：
  `files.tasks`/`files.heartbeats`/`files.events`），服务端按文件名识别 role（见 MONITOR-SPEC §3.1.3，TASK-042）
- **鉴权**：每 agent 一个 Bearer token（`config/agents.json`，权限 600）；
  **授权范围校验**：请求体 `project_id` 必须在 token 授权项目集合内，否则 401/403；未注册 project_id → 400/404；
  校验失败 401 不泄露数据；跨公网建议 HTTPS 反代；`config/agents.json` 含密钥**必须 gitignore 不入库**
- **多实例语义（2026-08-16 修订）**：同一逻辑项目可部署在**多台机器**（dev/prod/CI），
  各机器 runtime 状态独立 → `projects[].id` 为**实例级唯一**（如 `baseline-dev` / `baseline-prod`）；
  同一 `project_id` **只允许一个 agent 推送**（双 agent 抢同一 id → 409，防 INSERT OR REPLACE 互相覆盖污染）
- **agent 归属（2026-08-16 修订）**：agent 为**框架通用组件**，放 `aibase/kit/tools/agent/`，
  随 `mkproject` 自动分发到新项目（零额外安装）；程序随项目分发（副本），**运行时每机器 1 实例**（机器级配置）；
  协议解耦：agent 推送 **AIOS 通用遥测格式**（runtime 文件内容 + 元数据），aimonitor ingest 为消费方之一；
  **不放入任何被监控项目内运行**（只读 runtime/）
- **agent 代码归属（原 ADR-002 决策，已修订为上述）**：第一版曾计划放 aimonitor `tools/agent/`；
  2026-08-16 用户提议后改为**框架通用组件放 `aibase/kit/tools/agent/`**（mkproject 分发消除安装动作）；
  存量项目（本地 7 个）为 `transport: local` 无需 agent，远程新项目生成即带
- **Windows 前提**：AIOS 工具链（autoloop）依赖 bash + util-linux flock →
  被监控 Windows 工程须在 WSL2 内运行，agent 在 WSL 内以独立进程运行

## 后果

- ✅ 跨平台/跨机器：agent 纯 Python 标准库，Windows(WSL)/Linux 通用
- ✅ 现有 7 个本地项目零迁移（`transport` 缺省 = `local`）
- ✅ 对外 API 契约不变：前端/告警/历史/趋势无需改动
- ✅ 可穿透防火墙（被监控端出向 HTTP，无需服务端入向 SSH/SMB）
- ⚠️ 新增写端点 `POST /api/ingest` → 必须鉴权 + 输入校验（401/400/413/429）
- ⚠️ 需要新增 SQLite 表 `ingest_state`（服务重启不丢远端状态）
- ⚠️ agent 断线期间数据有缺失窗口 → 趋势缺口语义已覆盖
- ⚠️ 弃用方案 A/B 的理由：A 运维重/暴露大/NFS 明文；B 轮询性能瓶颈（每文件一次 SSH）
- 🔗 相关模块：`config/projects.json`（transport 字段）、`config/agents.json`（token）、
  `server/monitor_server.py`（ingest 端点 + FileReader 抽象）、`aibase/kit/tools/agent/`（agent，mkproject 分发）
