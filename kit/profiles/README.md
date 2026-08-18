# profiles/ — 项目类型模板

> 不是"环境配置"，是**项目知识模板**。
>
> 每个 profile 定义一种项目类型：AI 初始化时读此模板，就知道项目用什么技术栈、有什么约束、常见工具有哪些。

## 可用 profiles

| Profile | 适用项目 |
|---------|---------|
| `backend/` | 后端服务（Go/Node/Python/Java…） |
| `game-server/` | 网游服务器（Go+Redis+Mongo+帧同步+网关） |
| `unity/` | Unity 客户端（Assets/Prefab/Scene/Material） |
| `unreal/` | Unreal 客户端（Blueprints/C++） |
| `frontend/` | 前端（React/Vue/Next.js…） |
| `data/` | 数据工程（ETL/pipeline/warehouse） |
| `design/` | 游戏策划（玩法/系统设计、数值配置、剧情文案） |
| `novel/` | AI 创作（长篇小说/文本类内容） |

## Profile 文件内容

每个 profile 提供 `config.template.yaml`。`cli/init` 在交互式终端下会自动问这些字段并生成实例化好的 `aios.config.yaml`；非交互环境（CI/脚本）退化为原样复制模板，此时需要手动把占位符换成真实命令和目录。

配置必须包含：`source_dirs`、`generated_dirs`、`commands.build`、`commands.lint`、`commands.test`、`commands.check`。

`commands` 还接受额外键（如 `start` / `dev` / `run`），用于登记工程启动命令——见各 `config.template.yaml` 底部的"启动命令（可选）"注释块。`cli/lib/config.py` 只强制 4 个必填命令，额外键会被保留，AI 通过 `aios.config.yaml` 即可发现启动入口。启动脚本放 `source_dirs` 指向的源码区（或按技术栈惯例：`bin/`、`scripts/`、`package.json scripts` 等），模板不预置具体目录。

任务命中 [`aios/execution/engine.md`](../aios/execution/engine.md) §0 的启动分流（改契约/规格/生成源/跨端协议）时，还需要 [`aios/execution/sdd-workflow.md`](../aios/execution/sdd-workflow.md) 场景 A/A'/A" 用到的 `project.kind`、`spec.*`、`docs.*`、`commands.validate_spec`、`commands.generate`。这组字段默认不启用——各 `config.template.yaml` 底部有按项目类型预置的注释块，需要时取消注释并按需修改；纯逻辑修改/Bug 修复（场景 B/C）不需要它们。

例如 `game-server/config.template.yaml`：

```markdown
## 技术栈
- 语言: Go
- 数据库: MongoDB（业务数据）+ Redis（缓存/会话）
- RPC: gRPC
- 消息队列: Kafka
```

详见各 profile 目录。
