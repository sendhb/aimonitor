#!/usr/bin/env python3
"""交互式生成 aios.config.yaml：读 profile 模板，把占位符换成用户输入的真实值。

cli/init 在交互式终端里调用；非交互场景（CI/脚本）不会走到这里，
而是把模板原样复制过去，让人事后手动填。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg_lib  # noqa: E402


def ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or default


def main():
    template_src, config_dst, profile = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(template_src, encoding="utf-8") as f:
        parsed = cfg_lib.parse(f.read())

    print(f"\n=== 实例化 {profile} profile → {config_dst} ===")
    print("直接回车用方括号里的默认值（留空的没有默认值，需要真的填）。\n")

    source_dirs = ask("source_dirs（逗号分隔）", ",".join(parsed.get("source_dirs", [])) or "src/")
    generated_dirs = ask("generated_dirs（逗号分隔）", ",".join(parsed.get("generated_dirs", [])) or "dist/")

    commands = {}
    for key in ("build", "lint", "test", "check"):
        default = parsed.get("commands", {}).get(key, "")
        if default.startswith("<"):
            default = ""
        commands[key] = ask(f"commands.{key}", default)

    lines = ["version: 1", f"profile: {profile}", "source_dirs:"]
    lines += [f"  - {d.strip()}" for d in source_dirs.split(",") if d.strip()]
    lines.append("generated_dirs:")
    lines += [f"  - {d.strip()}" for d in generated_dirs.split(",") if d.strip()]
    lines.append("commands:")
    lines += [f"  {k}: {v}" for k, v in commands.items()]

    with open(config_dst, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"✓ 已写 {config_dst}")

    empty = [k for k, v in commands.items() if not v]
    if empty:
        print(f"⚠ commands.{', '.join(empty)} 留空了——cli/task verify 会拒绝跑，记得回来填。")


if __name__ == "__main__":
    main()
