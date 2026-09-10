"""registry.py — projects.json 注册表读取（kit/tools/dispatcher/ 注册层）。

TASK-069：Phase 3 调度器骨架的注册表组件。

职责：
- 读取 aimonitor 权威注册表 `config/projects.json`（CLI 的 --config 可覆盖）。
- 暴露每条注册的 transport 字段；transport 缺失/为空视为本地默认。
- `is_local(entry)`：非 agent 传输的条目判定为本地（v1 只处理本地条目）。
- 注册表缺失 / JSON 格式错误 / 缺必填字段 → 抛 RegistryError
  （CLI 层捕获后打印到 stderr 并 exit 1，不静默接受、不为缺项猜默认值）。

注册表形状（与 aimonitor/config/projects.json 一致）：

    {
      "poll_interval_seconds": 30,
      ...
      "projects": [
        {"id": "aimonitor", "name": "aimonitor", "path": "/home/hb/code/aimonitor"},
        {"id": "hb-share-aibase", "name": "hb-share-aibase",
         "path": "D:/share/the5/aibase", "transport": "agent"}
      ]
    }

零外部依赖（仅 stdlib）。风格与 kit/tools/telemetry/agent_config.py 一致。
"""
import json
import os
from dataclasses import dataclass

DEFAULT_TRANSPORT = "local"
AGENT_TRANSPORT = "agent"


class RegistryError(Exception):
    """注册表缺失/非法时抛出；CLI 层捕获后打印到 stderr 并 exit(1)。"""


@dataclass(frozen=True)
class RegistryEntry:
    """单条项目注册（id/name/path/transport，transport 已规范化）。"""

    id: str
    name: str
    path: str
    transport: str


def _normalize_transport(value):
    """transport 规范化：缺失/空 → local（默认）；其他字符串 → 小写去空白。"""
    if value is None:
        return DEFAULT_TRANSPORT
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    # 非字符串或纯空白 → 按缺失处理，不猜 agent
    return DEFAULT_TRANSPORT


def load_registry(path):
    """读取注册表文件并返回 RegistryEntry 列表（保持文件内顺序）。

    注册表缺失、JSON 解析失败、缺少 projects 数组、条目缺 id/path
    → 抛 RegistryError（消息含具体原因与路径，便于一次性修复）。
    """
    if not path:
        raise RegistryError("注册表路径为空（需要 --config <projects.json>）")
    if not os.path.isfile(path):
        raise RegistryError(f"注册表不存在: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise RegistryError(f"注册表读取失败（{path}）: {e}") from e

    if not isinstance(data, dict):
        raise RegistryError(f"注册表必须是 JSON 对象（{path}）")

    projects = data.get("projects")
    if not isinstance(projects, list):
        raise RegistryError(f"注册表缺少 projects 数组（{path}）")

    entries = []
    for i, item in enumerate(projects):
        if not isinstance(item, dict):
            raise RegistryError(f"注册表第 {i + 1} 条不是 JSON 对象（{path}）")

        entry_id = item.get("id")
        if not entry_id or not isinstance(entry_id, str) or not entry_id.strip():
            raise RegistryError(f"注册表第 {i + 1} 条缺少 id（{path}）")

        entry_path = item.get("path")
        if not entry_path or not isinstance(entry_path, str) or not entry_path.strip():
            raise RegistryError(
                f"注册表第 {i + 1} 条缺少 path（id={entry_id}）: {path}"
            )

        entry_name = item.get("name") or entry_id
        entries.append(RegistryEntry(
            id=entry_id.strip(),
            name=str(entry_name).strip() if entry_name else entry_id.strip(),
            path=entry_path.strip(),
            transport=_normalize_transport(item.get("transport")),
        ))
    return entries


def is_local(entry):
    """本地条目判定：transport 非 agent 即视为本地（v1 只处理本地条目）。"""
    return entry.transport != AGENT_TRANSPORT


def load_aimonitor_config(path):
    """读注册表文件顶层 aimonitor 段（TASK-037，agent 通道接入点）。

    形状：{"projects": [...], "aimonitor": {"server_url": "http://..."}}。
    段缺失/非法/文件读不到 → {"server_url": None}（不抛——list/scan 照常工作，
    agent 条目在执行时才报错）；token 绝不入注册表（环境变量 AIOS_DOWNLINK_TOKEN）。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"server_url": None}
    section = data.get("aimonitor") if isinstance(data, dict) else None
    url = section.get("server_url") if isinstance(section, dict) else None
    if isinstance(url, str) and url.strip():
        return {"server_url": url.strip().rstrip("/")}
    return {"server_url": None}


def is_agent(entry):
    """agent 传输条目判定（v1 跳过并告警，不尝试读不存在的路径）。"""
    return entry.transport == AGENT_TRANSPORT
