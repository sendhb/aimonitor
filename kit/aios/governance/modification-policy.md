# Modification Policy — 文件修改权限

> AI agent 在执行任何文件写操作前，必须检查本协议。

## 禁写目录（generated_dirs）

以下目录**只能由生成器写入**，AI agent **禁止手动编辑**：

- 引擎生成物（Unity: `Library/ Temp/`；Unreal: `Binaries/ Intermediate/`）
- 代码生成产物（`gen/ src/generated/`）
- 构建产物（`dist/ build/`）
- 依赖目录（`node_modules/ vendor/`）

> 需要改生成代码？→ 改规格 → 重新生成。

**这条红线现在有机械强制**：`cli/protect` 会把 `aios.config.yaml` 里 `generated_dirs` 列出的目录 `chmod -R a-w`，任何工具（不只 AI）的写操作在 OS 层直接被拒绝，不再只靠 agent 读文档自觉遵守。生成器重新生成前先 `cli/protect --unlock`，生成后再跑一次 `cli/protect` 锁回去。

## 高风险文件（修改前需检查）

| 类型 | 示例 | 级别 |
|------|------|------|
| 数据库 schema | `migrations/`, `schema.sql` | 🔴 P0 |
| 支付逻辑 | `payment/`, `billing/` | 🔴 P0 |
| 账号/认证 | `auth/`, `login/`, `oauth/` | 🔴 P0 |
| 权限/授权 | `rbac/`, `permissions/` | 🔴 P0 |
| API 契约 | `openapi.yaml`, `.proto`, `GraphQL schema` | 🟡 P1 |
| 共享类型定义 | `types/`, `interfaces/` | 🟡 P1 |
| CI/CD 配置 | `.github/workflows/`, `Dockerfile` | 🟡 P1 |

**P0 级修改必须人工批准**；P1 级修改需在 TASK 中明确声明。

## 实现目录（source_dirs）

只有 `config.source_dirs` 列出的目录允许直接编辑实现代码。
