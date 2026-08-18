"""
config.py — 最小依赖的 aios.config.yaml 解析器

不依赖 PyYAML（保持 cli/ 全 stdlib、零外部依赖的设计），只支持
profiles/*/config.template.yaml 这个固定形状：

    version: 1
    profile: <name>
    source_dirs:
      - a/
      - b/
    generated_dirs:
      - dist/
    commands:
      build: <cmd>
      lint: <cmd>
      test: <cmd>
      check: <cmd>

不是通用 YAML 解析器，遇到这个形状之外的语法会忽略而不是报错——
配置文件复杂度超出这个形状时应该扩展本文件的 schema，而不是引入 PyYAML。
"""
import os

REQUIRED_TOP = ("source_dirs", "generated_dirs", "commands")
REQUIRED_COMMANDS = ("build", "lint", "test", "check")
PLACEHOLDER_RE_PREFIX = "<"  # 未填写的占位符形如 <build-command>


def _scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    return s


def parse(text):
    cfg = {"source_dirs": [], "generated_dirs": [], "commands": {}}
    section = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            if stripped.startswith("- "):
                continue
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                cfg[key] = _scalar(val)
                section = None
            else:
                section = key if key in ("source_dirs", "generated_dirs", "commands") else None
                cfg.setdefault(section, [] if section != "commands" else {})
        else:
            if stripped.startswith("- ") and section in ("source_dirs", "generated_dirs"):
                cfg[section].append(_scalar(stripped[2:]))
            elif section == "commands" and ":" in stripped:
                k, _, v = stripped.partition(":")
                cfg["commands"][k.strip()] = _scalar(v.strip())
    return cfg


class ConfigError(Exception):
    pass


def load_config(root_dir, path=None):
    """读取 <root_dir>/aios.config.yaml。缺失或未填占位符时抛 ConfigError（调用方决定怎么呈现）。"""
    path = path or os.path.join(root_dir, "aios.config.yaml")
    if not os.path.isfile(path):
        raise ConfigError(
            f"未找到 {path}。请从 profiles/<type>/config.template.yaml 复制一份到项目根目录，"
            f"改名 aios.config.yaml 并填好占位符（或跑 cli/init 的实例化向导）。"
        )
    with open(path, encoding="utf-8") as f:
        cfg = parse(f.read())

    missing = [k for k in REQUIRED_TOP if not cfg.get(k)]
    if missing:
        raise ConfigError(f"{path} 缺少必填字段: {', '.join(missing)}")

    placeholders = [
        f"commands.{k}" for k in REQUIRED_COMMANDS
        if not cfg["commands"].get(k) or cfg["commands"][k].strip().startswith(PLACEHOLDER_RE_PREFIX)
    ]
    if placeholders:
        raise ConfigError(f"{path} 里以下命令还是占位符，没有填成真实命令: {', '.join(placeholders)}")

    return cfg
