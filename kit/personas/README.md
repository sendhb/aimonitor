# personas — 人格库（按需加载）

> 人格 = 表达层设定（语气/修辞/叙事），**绝不改变**推理、结论、工程判断与治理纪律。
> 本目录为**框架自带人格库**（只读，随 kit 升级整体替换）；项目自定义人格放项目根 `personas/`（可写）。

## 机制

- **一人格一文件**：本目录下每个 `.md` 是一个完整人格（含身份/风格/硬边界/生效范围）。
- **激活 = 复制**：`kit/cli/persona use <name>` 将所选人格内容复制到项目根 `personas/active.md`。
- **强制激活**：进入任何 AI CLI 会话时先 `kit/cli/persona ensure`——active.md 存在则保持，缺失则随机激活一个（AI 入口薄壳均已指路）。
- **按需加载**：工具只读 `personas/active.md` 一个文件；未激活（文件不存在）则零加载。
- **AGENTS.md 与工具薄壳均指路、不内嵌**，避免双源漂移。

## 命令

```bash
kit/cli/persona list                  # 列出可用人格（kit/personas/ + 项目 personas/）
kit/cli/persona use <name>            # 激活人格 → 写入 personas/active.md
kit/cli/persona ensure                # 确保已激活：缺失时从人格库随机激活一个（AI CLI 进入时调用）
kit/cli/persona off                   # 关闭人格 → 删除 personas/active.md（零加载）
kit/cli/persona show                  # 显示当前激活人格
```

## 已有备选

- 默认人格：`dongfang-bubai.md` — 东方不败（金庸《笑傲江湖》）
- **49 位备选角色**（曹操 / 孙悟空 / 小丑 / 杰洛特 / 皇帝 / 太监…）：见 [`INDEX.md`](INDEX.md)，由 `_generate.py` 生成（数据在 `_data.py`，增删角色改数据后重跑即可）

## 新增人格

1. 在 `kit/personas/`（框架自带）或项目根 `personas/`（项目自定义）新建 `<name>.md`，参考 [`kit/templates/persona.template.md`](../templates/persona.template.md) 填写，**保留「硬边界」5 条不动**（框架安全网）。
2. `kit/cli/persona use <name>` 激活。

## mkproject 集成

- `--persona <name>`：从人格库选名字激活（不传文件路径）。
- `--no-persona`：不激活任何人格（零加载）。
- 默认：继承源项目激活状态（复制源 `personas/active.md`）。
