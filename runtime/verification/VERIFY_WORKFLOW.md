# Verify Workflow — 验证记录规范

> **适用对象：所有 AI 工具。**
> 每个任务在关闭前必须产生验证记录，证明"做完了且是对的"。验证记录与任务文件通过 `task-ref` 关联。

---

## 文件命名

```
VERIFY-<YYYY-MM-DD>-<范围>.md
```

示例：`VERIFY-YYYY-MM-DD-taskXXX-feature-name.md`

## 固定格式

路径：`runtime/verification/VERIFY-<日期>-<范围>.md`，模板见 `runtime/verification/VERIFY.template.md`

```markdown
---
name: VERIFY-YYYY-MM-DD-xxx       # 与文件名一致
description: 一句话说明验证范围
metadata:
  type: verify
  date: YYYY-MM-DD
  task-ref: TASK-001               # 必填：关联任务
  verifier: claude                 # 验证者：ai 工具名或 human
  result: pass                     # 仅 pass 可作为关闭证据
  commit: <git-sha-or-unknown>     # 被验证的版本
---

# VERIFY — <范围>

## 执行环境
| 项 | 值 |
|----|----|
| 日期 | YYYY-MM-DD |
| 验证者 | claude |

## 验证结果
| 检查项 | 结果 | 说明 |
|--------|------|------|
| config.commands.build | ✅ PASS / ❌ FAIL | |
| config.commands.check | ✅ PASS / ❌ FAIL | |
| <其他测试项> | ✅ PASS / ❌ FAIL | |

## 发现问题
| ID | 严重度 | 描述 | 状态 |

## 结论
通过 / 失败，下一步...
```

---

## 验证检查项（通用）

| 检查项 | 来源 | 说明 |
|--------|------|------|
| 编译/构建 | `config.commands.build` | 零错误 |
| 合规检查 | `config.commands.check` | 全部通过 |
| 测试 | `config.commands.test` | 如配置 |
| 端点/接口测试 | 项目脚本 | 如适用 |
| 截图/UI 验证 | `config.docs.screenshots` | 截图必须保存到该项目录，命名 `<task-id>-<功能>-<状态>.png` |

---

## 规则

1. `cli/task done TASK-xxx` 会检查：任务引用至少一条日期为当天、`result: pass` 且格式有效的 VERIFY 记录
2. 验证失败 → 回到实现，修复后重新验证；不得伪造 PASS
3. 验证记录是不可变审计信息，完成后只允许追加修正说明
4. `metadata.commit` 语义：`task verify` 在提交前运行，stamp 的是**验证时 HEAD**（即被验证工作树的前一个提交）；验证实际覆盖工作树全部改动（含未提交部分），故返工提交后无需重跑。需要精确钉住实现提交时，可在实现提交后重跑 verify 覆盖同名记录。
