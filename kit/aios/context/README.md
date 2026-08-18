# aios/context — 上下文工程

> Prompt Engineering → Context Engineering → Harness Engineering
>
> 核心不再是"怎么跟 AI 说"，而是"给 AI 看什么"。
> 
> Context 是 AI 的 RAM。装多了溢出，装少了瞎猜。

## 五大策略（Anthropic）

| 策略 | 说明 | 示例 |
|------|------|------|
| **Write** | 写清楚结构化的系统提示 | 角色定义 + 强制约束 + 禁止项 |
| **Select** | 只选相关上下文注入 | 不把整个代码库 dump 进 prompt |
| **Compress** | 压缩历史/冗余信息 | 摘要而非全文 |
| **Filter** | 过滤噪音 | 排除 node_modules、日志、二进制文件 |
| **Load** | 懒加载 | 先索引，后按需加载详情 |

## 加载顺序（Progressive Disclosure）

```
1. AGENTS.md          — 入口（必读清单 + 红线）
2. aios/governance/  — 权限/安全/风险
3. 按需加载：
   ├── knowledge/     — 模块关系、架构、决策
   ├── runtime/tasks/ — 当前任务
   └── profiles/      — 项目类型模板
```

## 约束

- **上下文预算**：单文件 ≤ 200 行；首次加载不超过 40% 上下文窗口（超过进入 "dumb zone"）
- **按需读取**：不要一次性加载 `aios/` 全部；从入口逐层展开
- **清洁上下文**：新任务新会话（或 /compact 后重新加载）
