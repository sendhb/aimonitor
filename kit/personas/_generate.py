#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 personas 库（v2 按需加载架构）：读 _data.py 生成 <slug>.md + INDEX.md。

用法：python3 _generate.py
增删角色：编辑 _data.py 后重跑本脚本。
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _data import PERSONAS

# 分类映射（slug → 分类）；增删角色时同步维护
CATS = {
    # 帝王将相 · 政坛
    "caocao": "帝王将相 · 政坛", "kaisa": "帝王将相 · 政坛", "bismarck": "帝王将相 · 政坛",
    "ekaterina": "帝王将相 · 政坛", "lincoln": "帝王将相 · 政坛", "churchill": "帝王将相 · 政坛",
    # 东方古典 · 武侠
    "sunwukong": "东方古典 · 武侠", "lindaiyu": "东方古典 · 武侠", "wangxifeng": "东方古典 · 武侠",
    "luzhishen": "东方古典 · 武侠", "linghuchong": "东方古典 · 武侠", "huangrong": "东方古典 · 武侠",
    "xiaolongnv": "东方古典 · 武侠", "saodiseng": "东方古典 · 武侠", "weixiaobao": "东方古典 · 武侠",
    # 诗人 · 哲人
    "libai": "诗人 · 哲人", "zhuangzi": "诗人 · 哲人", "nietzsche": "诗人 · 哲人",
    # 国漫
    "nezha": "国漫", "fengbaobao": "国漫", "wangye": "国漫",
    # 日漫
    "gintoki": "日漫", "luluxiu": "日漫", "l": "日漫", "saitama": "日漫",
    "gojo": "日漫", "levi": "日漫", "luffy": "日漫", "ayanami": "日漫", "haibara": "日漫",
    # 欧美文学 · 影视
    "holmes": "欧美文学 · 影视", "frankenstein": "欧美文学 · 影视", "jacksparrow": "欧美文学 · 影视",
    "hannibal": "欧美文学 · 影视", "tyler": "欧美文学 · 影视", "house": "欧美文学 · 影视",
    "gump": "欧美文学 · 影视", "ladymacbeth": "欧美文学 · 影视",
    # 欧美漫画
    "ironman": "欧美漫画", "deadpool": "欧美漫画", "batman": "欧美漫画", "joker": "欧美漫画",
    # 游戏
    "geralt": "游戏", "arthas": "游戏", "claptrap": "游戏",
    # 身份原型
    "emperor": "身份原型", "eunuch": "身份原型", "fortune-teller": "身份原型", "storyteller": "身份原型",
}


def cat_of(p):
    return CATS.get(p["slug"], "其他")

HARD_BOUNDS = """## 硬边界（不可违反）
1. **表达层 ≠ 认知层**：语气可以个性，结论必须准——工程结论、任务状态、架构判断与实际一致。
2. **治理纪律不变**：TASK 状态机、风险分级、审查规则、文件权限照常执行。
3. **紧急切换**：用户说「严谨模式」，立即去人格化；说角色名或默认，回到人格。
4. **不歪曲**：不得为了像角色而夸大、虚构事实或改变数据。
5. **度**：个性而不过，不冒犯用户、不贬低真实的人。"""


def render(p):
    s = p["style"]
    fm = (f"---\nname: {p['slug']}\nname_cn: {p['name']}\nsrc: {p['src']}\ntag: {p['tag']}\ncat: {cat_of(p)}\n---\n\n")
    return fm + f"""# 人格：{p['name']}（{p['src']}）

> 用户指定：**{p['name']}**（{p['src']}）作为可选人格，`kit/cli/persona use {p['slug']}` 激活后以
> 此人个性/语气展开。本设定仅作用于**表达层**（语气/修辞/叙事），**绝不改变**推理、结论、工程判断与治理纪律。

## 身份与性格
{chr(10).join('- ' + t for t in p['traits'])}

## 说话风格（可操作特征）
- **自称**：{s['self']}；**称呼对方**：{s['addr']}。
- **笑声/口头禅**：{s['catch']}
- **核心意象（务必高频）**：{s['img']}
- **标志用词**：{s['sig']}

## 一句话见骨
> {p['quote']}

{HARD_BOUNDS}

## 生效范围
- **单源**：本文件（`kit/personas/{p['slug']}.md`）是此人格唯一真相源；激活时由 `kit/cli/persona use {p['slug']}` 复制到项目根 `personas/active.md`，工具只读 active.md，不重复内容。
- **薄壳指路**：AGENTS.md 与工具薄壳（`.pi/SYSTEM.md`、`.cursor/rules/persona.mdc`、`.github/copilot-instructions.md`）只指路不复制，避免双源漂移。
- **按需加载**：未激活（`personas/active.md` 不存在）则零加载；激活则仅加载该一个文件。
"""


def main():
    os.makedirs(HERE, exist_ok=True)
    rows = []
    for p in PERSONAS:
        fn = os.path.join(HERE, p["slug"] + ".md")
        with open(fn, "w", encoding="utf-8") as f:
            f.write(render(p))
        rows.append((p["slug"], p["name"], p["src"], p["tag"]))
        print(f"  ✓ {p['slug']}.md — {p['name']}")

    lines = [
        "<!-- 本索引由 _generate.py 生成；增删角色请编辑 _data.py 后重跑。 -->",
        "# Personas 索引 — 人格备选（v2 按需加载）",
        "",
        f"> 共 {len(rows)} 位（另有默认人格 `dongfang-bubai.md` 未在此列）。",
        "> 激活：`kit/cli/persona use <slug>`；关闭：`kit/cli/persona off`；mkproject：`--persona <slug>`。",
        "",
        "| slug | 角色 | 出处 | 个性 |",
        "|------|------|------|------|",
    ]
    lines += [f"| {s} | {n} | {src} | {tag} |" for s, n, src, tag in rows]
    with open(os.path.join(HERE, "INDEX.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"✓ 共 {len(rows)} 位角色 + INDEX.md")


if __name__ == "__main__":
    main()
