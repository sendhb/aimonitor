# Cognition Layer — AI 理解目标的能力

> AI 不再"收到命令→执行"，而是**先理解目标，再行动**。

认知层是所有 agent 动作的第一站。Manager/Analyst 角色在执行前必须过认知层：

1. **Intent Analysis** — 用户的真实意图是什么？不是字面意思
2. **Requirement Analysis** — 拆解为可验证的技术需求
3. **Ambiguity Check** — 哪些是不确定的？默认假设是什么？
4. **Assumption Management** — 记录假设，等确认后再变为事实

认知层不写代码，**只产出理解**。理解错误比代码错误更难修复。
