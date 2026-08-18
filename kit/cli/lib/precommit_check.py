#!/usr/bin/env python3
"""
pre-commit 钩子的实际逻辑：generated_dirs 保护 + 跑 commands.check。

跟具体 AI 工具无关——任何东西（人、Claude Code、Cursor、脚本）提交前都会
经过 git 的 pre-commit 钩子，这是比某个 CLI 的 hook 系统更底层的强制点。
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg_lib  # noqa: E402


def main():
    root = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    try:
        cfg = cfg_lib.load_config(root)
    except cfg_lib.ConfigError as e:
        print(f"⚠ 跳过 pre-commit 检查（{e}）", file=sys.stderr)
        return 0

    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        cwd=root, capture_output=True, text=True,
    ).stdout.splitlines()

    gen_dirs = [d.rstrip("/") for d in cfg["generated_dirs"]]
    for f in staged:
        for d in gen_dirs:
            if f == d or f.startswith(d + "/"):
                print(f"✗ 拒绝提交：{f} 在 generated_dirs（{d}/）下，只能由生成器写入。", file=sys.stderr)
                print("  改规格 → 重新生成，而不是手改生成代码。", file=sys.stderr)
                return 1

    check_cmd = cfg["commands"]["check"]
    print(f"▶ pre-commit check: {check_cmd}")
    proc = subprocess.run(check_cmd, shell=True, cwd=root)
    if proc.returncode != 0:
        print(f"✗ commands.check 未通过（exit {proc.returncode}），拒绝提交", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
