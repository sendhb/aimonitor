# aios/governance — 治理协议

定义 AI agent 的行为边界。不是"建议"，是**强制规则** —— AI 在执行前必须先检查 governance。

| 文件 | 用途 |
|------|------|
| `task-policy.md` | 任务生命周期与状态机（open→in-progress→in-review→done） |
| `modification-policy.md` | 文件修改权限（哪些目录可写/禁写，高风险文件需批准） |
| `security-policy.md` | 安全红线（密钥不进提示词；Rule of Two；训练退出） |
| `risk-policy.md` | 风险分级与审批（P0-P3，含审批条件） |
| `roadmap-policy.md` | 项目路线图治理（位置/使用者/修改者/变更控制） |

## 核心约束

所有 AI agent 在**每次动作前**必须检查：此动作是否违反 governance 协议。违反 → 拒绝执行并通知人工。
