# Copilot — 项目入口

本项目是 AIOS + Reliable AI Engineering Framework。

**入口**：`AGENTS.md`（所有规则的总入口）。

Coding Agent 模式：遵循 `kit/aios/execution/engine.md` 闭环，可承接 Coder 角色。
补全模式：只做单文件补全，不修改任务状态。

**对话人格**（按需加载 · 表达层设定）：当前激活人格见项目根 `personas/active.md`（未激活则零加载），切换用 `kit/cli/persona`；此处不重复。
