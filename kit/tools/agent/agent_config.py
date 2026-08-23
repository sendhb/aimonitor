"""agent_config.py — agent.json 配置加载与校验（kit/tools/agent/ 配置层）。

零外部依赖（仅 stdlib），风格与 kit/cli/lib/config.py 一致：
schema 固定，缺失必填字段/非法值直接抛 AgentConfigError（CLI 层捕获后
stderr 报错 + exit 1），不静默接受、不为必填字段猜默认值。

agent.json 形状：

    {
      "server_url": "https://aimonitor.example.com/api/ingest",  # 必填
      "token": "secret",                                          # state=active 时必填
      "projects": [{"id": "proj-1", "path": "/srv/proj-1"}],      # 必填，至少 1 个
      "poll_interval_seconds": 30,                                # 可选，默认 30
      "state": "active",                                          # 可选，默认 active
      "req_id": null,                                             # 可选，state=pending 时使用
      "request_key": null                                         # 可选，state=pending 时使用
    }

约定：
- 空字符串 / 纯空白 / `<...>` 占位符视为"未填"（与 kit/cli/lib/config.py 一致）。
- 未知多余字段忽略（向前兼容后续任务新增字段，如日志级别、重试上限）。
"""
import json
import os

DEFAULT_POLL_INTERVAL_SECONDS = 30
VALID_STATES = frozenset({"unregistered", "pending", "active"})


class AgentConfigError(Exception):
    """agent.json 缺失/非法时抛出；CLI 层捕获后打印到 stderr 并 exit(1)。"""


def _is_unset(value):
    """None、空字符串/纯空白、或 <...> 占位符都视为未设置。"""
    if value is None:
        return True
    if isinstance(value, str):
        s = value.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
            s = s[1:-1].strip()
        return not s or (s.startswith("<") and s.endswith(">"))
    return False


def validate(cfg):
    """校验已解析的 agent.json（dict），返回带默认值的规范化配置。

    非法时抛 AgentConfigError，消息聚合列出全部问题（不只第一个），
    便于一次性修复。
    """
    if not isinstance(cfg, dict):
        raise AgentConfigError("agent.json 必须是 JSON 对象（{...}）")

    problems = []

    server_url = cfg.get("server_url")
    if _is_unset(server_url):
        problems.append("缺少必填字段 server_url（监控端 ingest 地址）")
    elif not isinstance(server_url, str):
        problems.append("server_url 必须是字符串")
    else:
        server_url = server_url.strip()

    # --- state 字段校验 ---
    state = cfg.get("state", "active")
    if state is None:
        state = "active"
    if not isinstance(state, str):
        problems.append("state 必须是字符串（unregistered/pending/active）")
        state = "active"
    else:
        state = state.strip()
        if state not in VALID_STATES:
            problems.append(
                f"state 必须为 unregistered/pending/active 之一（当前值: {state!r}）"
            )
            state = "active"

    # --- token 校验（依赖 state） ---
    token = cfg.get("token")
    token_out = None
    if state == "active":
        if _is_unset(token):
            problems.append("state=active 时 token 为必填（推送到监控端的 Bearer token）")
        elif not isinstance(token, str):
            problems.append("token 必须是字符串")
        else:
            token_out = token.strip()
    else:
        # state=unregistered 或 pending 时 token 可为空
        if not _is_unset(token):
            if isinstance(token, str):
                token_out = token.strip()
            else:
                problems.append("token 必须是字符串")

    # --- req_id / request_key 校验（仅在 state=pending 时关注） ---
    req_id = cfg.get("req_id")
    request_key = cfg.get("request_key")
    req_id_out = None
    request_key_out = None

    if state == "pending":
        if not _is_unset(req_id):
            if isinstance(req_id, str):
                req_id_out = req_id.strip()
            else:
                problems.append("req_id 必须是字符串")
        if not _is_unset(request_key):
            if isinstance(request_key, str):
                request_key_out = request_key.strip()
            else:
                problems.append("request_key 必须是字符串")

    projects = cfg.get("projects")
    projects_out = []
    if _is_unset(projects):
        problems.append("缺少必填字段 projects（被监控项目数组，至少 1 个）")
    elif not isinstance(projects, list):
        problems.append("projects 必须是数组")
    elif not projects:
        problems.append("projects 不能为空数组（至少配置 1 个项目）")
    else:
        for i, proj in enumerate(projects):
            if not isinstance(proj, dict):
                problems.append(f"projects[{i}] 必须是对象（{{id, path}}）")
                continue
            pid, pth = proj.get("id"), proj.get("path")
            valid = True
            if _is_unset(pid):
                problems.append(f"projects[{i}].id 缺少必填字段")
                valid = False
            elif not isinstance(pid, str):
                problems.append(f"projects[{i}].id 必须是字符串")
                valid = False
            if _is_unset(pth):
                problems.append(f"projects[{i}].path 缺少必填字段")
                valid = False
            elif not isinstance(pth, str):
                problems.append(f"projects[{i}].path 必须是字符串")
                valid = False
            if valid:
                projects_out.append({"id": pid.strip(), "path": pth.strip()})

    poll = cfg.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
    if poll is None:
        poll = DEFAULT_POLL_INTERVAL_SECONDS
    if isinstance(poll, bool) or not isinstance(poll, (int, float)):
        problems.append("poll_interval_seconds 必须是数字（秒）")
    elif poll <= 0:
        problems.append("poll_interval_seconds 必须大于 0（秒）")

    if problems:
        raise AgentConfigError("agent.json 配置不合法:\n" + "\n".join(f"  - {p}" for p in problems))

    return {
        "server_url": server_url,
        "token": token_out,
        "projects": projects_out,
        "poll_interval_seconds": poll,
        "state": state,
        "req_id": req_id_out,
        "request_key": request_key_out,
    }


def load_config(path):
    """读取并校验 agent.json。文件缺失或 JSON 非法同样抛 AgentConfigError。"""
    if not os.path.isfile(path):
        raise AgentConfigError(
            f"未找到配置文件 {path}（可复制 agent.json.example 为 agent.json 后填写）"
        )
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except json.JSONDecodeError as e:
        raise AgentConfigError(f"{path} 不是合法 JSON: {e}") from e
    return validate(cfg)
