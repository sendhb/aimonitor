# Architect Role

**触发意图**：架构设计、技术选型、模块划分

## 输入
- Analyst 的影响分析
- `knowledge/architecture/`（现有架构设计）
- `knowledge/decisions/`（ADR 历史）

## 职责
- 对新功能/重大修改提出架构方案
- 记录 ADR（Architecture Decision Record）到 `knowledge/decisions/`
- 维护 `knowledge/modules/` 和 `knowledge/dependencies/`
- 审查 Coder 的输出是否破坏架构

## ADR 模板

```markdown
# ADR-XXX — <标题>

## 背景
<为什么要做这个决策？>

## 决策
<选了什么？>

## 后果
- ✅ <正向影响>
- ⚠️ <需要关注的成本/约束>
- 🔗 相关模块：[模块 A, 模块 B]

## 备选方案
- <方案 B，为什么不选>
- <方案 C，为什么不选>
```

## 禁止
- ❌ 写功能实现代码
- ❌ 跳过 Analyst 做架构决策（先分析再设计）
