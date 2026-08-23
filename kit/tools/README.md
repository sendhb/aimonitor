# tools/ — 工具能力层

> 不再按"Claude 能做什么/Cursor 能做什么"定义，而是按**能力抽象**。
>
> 每个工具目录定义一种能力的接口规范。AI agent 请求的是"能力"（need: git.commit），不是"调用哪个 AI 的什么函数"。

## 可用工具

| 工具 | 能力 | 接口 |
|------|------|------|
| `agent/` | AIOS 遥测推送（读被监控项目 runtime/ 状态 → 按间隔推送 ingest，含退避） | `python3 kit/tools/agent/agent.py [--once] [--interval N] [--quiet]`（配置见 `agent.json`） |
| `git/` | 版本控制（commit/branch/diff/checkpoint） | `git add/commit/push`, `git diff --stat`, `git stash` |
| `filesystem/` | 文件读写、目录遍历 | read/write/edit/ls |
| `shell/` | 命令行执行（编译/测试/构建） | bash <command> |
| `docker/` | 容器管理（隔离环境） | docker run/exec/build |
| `sandbox/` | 无网络容器沙箱（Rule of Two 执行层隔离） | `cli/sandbox-run` |
| `database/` | 数据库操作（读写/迁移） | 各 DB 客户端 |
| `unity/` | Unity 引擎操作（场景/预制体/构建） | Unity Editor CLI |
| `unreal/` | Unreal 引擎操作 | UAT/Editor CLI |
| `browser/` | 网页交互（截图/E2E/爬取） | Playwright/Puppeteer |

## 能力 vs AI 工具

一个 AI 工具（如 Claude Code、pi、Cursor）拥有多个能力。AI tool 只是 executor。

```
Agent 角色（Coder）
  ↓ 需要 "git.commit" 能力
  └→ AI 工具（pi / Claude Code / Cursor）
       └→ 执行 tools/git/commit
```

> 例外：`agent/` 是独立进程能力（不依赖 AI 工具 executor）——mkproject 自动分发到新项目，
> 部署方式见 [`agent/README.md`](agent/README.md)（systemd / nohup / Task Scheduler）。

## 如何添加新工具

1. 创建 `tools/<tool-name>/README.md`（接口规范）
2. 在 `profiles/<type>/` 中声明此工具是否可用
3. 任务执行时 Coder 按接口规范调用
