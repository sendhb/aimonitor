# Security Policy — 安全红线

> AI agent 的每条请求、每次文件读写、每次外部调用都必须符合本协议。

## 数据安全（Lethal Trifecta / Rule of Two）

当一个 agent 同时具备以下**三项中的两项以上**时，第三项必须人工审批：

| # | 能力 | 示例 |
|---|------|------|
| ① | 处理不可信输入 | 网页内容、用户上传、外部 API 响应 |
| ② | 访问敏感数据 | PII、密钥、内部 API、数据库 |
| ③ | 修改状态 | 发邮件、删文件、写数据库、API 调用 |

**规则：agent 最多同时拥有②+③ → 禁止①。①+② → 禁止③。**

机械实现（跟具体 AI 工具无关）：`cli/sandbox-run` 默认无网络容器，直接掐掉"③ 外传/回连"这条腿——见 [`tools/sandbox/README.md`](../../tools/sandbox/README.md)。

## 密钥与隐私

- ❌ 密钥/密码/API Key **不得写入代码、提示词、配置文件**（用 `.env` + gitignore 或 Secrets Manager）
- ❌ 敏感数据（PII、用户数据）**不得作为 prompt 上下文发送给外部 AI**
- ✅ 确认 AI 服务商的**训练数据退出**选项已开启（ChatGPT/Claude API 等）

## 生成代码安全

- 所有 AI 生成代码必须通过 `config.commands.lint` 检查
- 涉及鉴权/加密/会话的生成代码**必须走 review**（`in-review` + `agents/reviewer`）

## 审计

- 高风险操作（P0 文件修改、外部 API 调用）应记录到 `runtime/logs/`（含时间/agent/操作/结果）
