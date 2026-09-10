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

## 五策略 → 代码入口映射（TASK-094）

| 策略 | 代码入口 | 说明 |
|------|---------|------|
| **Write** | `kit/agents/<role>/role.md` + `AGENTS.md` | 角色定义 + 红线构成系统提示（人工/模板维护） |
| **Select** | `kit/cli/lib/context_loader.py` + `kit/cli/context` | 按 task 推导最小文件集，输出路径/行数/预算占比清单 |
| **Compress** | 宿主 CLI 承担（pi `/compact` 等） | 选型而非缺口：kit 不做摘要压缩，历史压缩交给宿主 |
| **Filter** | `context_loader.collect()` | 机械排除 `generated_dirs`、二进制/隐藏、不可读文件 |
| **Load** | `AGENTS.md` 渐进披露 + `context --budget N` | 先清单后按需展开；超预算按优先级截断，标注 `[TRUNCATED 到第 N 行]` |

装配优先级（超预算截断顺序）：TASK 本体（受保护）> depends-on TASK > checkpoint > 模块文档 > 源文件。
**例外（TASK-095）**：对 **in-progress** 任务，checkpoint 升至装配清单**最前**
（标注“断点恢复优先装载”，与 TASK 本体同为不截断保护）。
autoloop coder/reviewer 会话在 prompt 组装前自动注入装配清单（fail-open），
把“禁止全仓扫描”的规劝变成机械执行。

## 断点恢复（TASK-095）

“清洁上下文”（新任务新会话）意味着跨会话状态只能来自落盘产物。为此：

1. **写入（机械钩子，不依赖会话自觉）**：autoloop-coder 在每轮 LLM 会话收尾
   （正常结束 / 超时 124 / 异常退出，三条路径无条件）写
   `runtime/states/STATE-PROGRESS-<task>.md`：
   机械提取 TASK 本体（当前进度表 ✅/⏳/❌ + 验收标准勾选态）生成
   “已完成 / 下一步 / 阻塞”三段，附 per-task log 轮次数与尾部原始输出、
   last-exit 标签；写盘失败仅 stderr 告警，不阻塞 autoloop 主流程。
2. **装载（装配器机械执行）**：context loader 对 in-progress 任务把该
   checkpoint 排在装配清单首位（“断点恢复优先装载”）；新会话从 checkpoint
   而非零开始恢复上下文。checkpoint 目录另有 `runtime/checkpoints/`
   （TASK-094 预留兼容位，同样扫描）。
