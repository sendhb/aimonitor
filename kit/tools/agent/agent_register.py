"""agent_register.py — Agent 注册状态机（kit/tools/agent/ 注册层）。

注册状态机管理 agent.json 中的注册状态流转：

    unregistered ──→ pending ──→ active
         │                          │
         └──────（直接配置 active）──┘

状态语义：
- unregistered: 首次部署，尚未发起注册请求。token 可为空。
- pending: 已发起注册请求，等待审批/下发 token。req_id/request_key 记录请求标识。
- active: 已获得 token，可正常推送遥测。token 必填。

RegistrationPoller 轮询审批结果，领取 token 并自动切换为正式推送。

零外部依赖（仅 stdlib）。风格与 agent_config.py 一致。
"""
import json
import math
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 状态常量
STATE_UNREGISTERED = "unregistered"
STATE_PENDING = "pending"
STATE_ACTIVE = "active"

VALID_TRANSITIONS = {
    STATE_UNREGISTERED: (STATE_PENDING,),
    STATE_PENDING: (STATE_ACTIVE,),
    STATE_ACTIVE: (),
}


class RegistrationError(Exception):
    """注册状态机操作非法时抛出。"""


class RegisterConflictError(RegistrationError):
    """注册提交冲突（HTTP 409）：project_id 已注册（active）或已有 pending 申请。

    属性:
        existing: 冲突类型字符串，"active"（已注册）或 "pending"（已有待审批申请）
    """

    def __init__(self, existing, message=None):
        self.existing = existing
        if message is None:
            hint = "该项目已注册" if existing == "active" else "该项目已有待审批的注册申请"
            message = f"注册冲突（HTTP 409）: {hint}（existing={existing}）"
        super().__init__(message)


class RegistrationState:
    """注册状态机，封装 agent.json 的读取/写入与状态转换。"""

    def __init__(self, config):
        """从规范化配置 dict 初始化状态机。

        config 必须是 agent_config.validate 的返回值（含 state/req_id/request_key）。
        """
        self.state = config.get("state", STATE_ACTIVE)
        self.req_id = config.get("req_id")
        self.request_key = config.get("request_key")

    @classmethod
    def load(cls, path):
        """读取 agent.json 文件并返回 RegistrationState 实例。

        使用 agent_config.load_config 加载与校验，确保完整性。
        """
        import agent_config as cfg_lib  # noqa: E402
        cfg = cfg_lib.load_config(path)
        return cls(cfg)

    def save(self, path, token=None):
        """将当前状态写回 agent.json 文件（保留其他字段）。

        参数:
            path: agent.json 路径
            token: 可选，写入 token 字段（转入 active 时使用）
        """
        if not os.path.isfile(path):
            raise RegistrationError(f"配置文件不存在: {path}")

        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)

        cfg["state"] = self.state
        if self.req_id is not None:
            cfg["req_id"] = self.req_id
        else:
            cfg.pop("req_id", None)
        if self.request_key is not None:
            cfg["request_key"] = self.request_key
        else:
            cfg.pop("request_key", None)

        if token is not None:
            cfg["token"] = token
        elif self.state == STATE_UNREGISTERED:
            # state=unregistered 时清除 token（兼容后端下发前状态）
            cfg.pop("token", None)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")

    def transition_to(self, new_state, req_id=None, request_key=None, token=None):
        """执行状态转换。

        参数:
            new_state: 目标状态
            req_id: 转入 pending 时设置
            request_key: 转入 pending 时设置
            token: 转入 active 时设置
        返回:
            self（链式调用）
        异常:
            RegistrationError: 非法转换
        """
        allowed = VALID_TRANSITIONS.get(self.state, ())
        if new_state not in allowed:
            raise RegistrationError(
                f"非法状态转换: {self.state} → {new_state} "
                f"（允许: {', '.join(allowed) or '无'}）"
            )

        if new_state == STATE_PENDING:
            if req_id is None:
                raise RegistrationError("转入 pending 状态时必须提供 req_id")
            if request_key is None:
                raise RegistrationError("转入 pending 状态时必须提供 request_key")
            self.req_id = req_id
            self.request_key = request_key
            self.state = STATE_PENDING

        elif new_state == STATE_ACTIVE:
            if token is None:
                raise RegistrationError("转入 active 状态时必须提供 token")
            self.req_id = None
            self.request_key = None
            self.state = STATE_ACTIVE

        return self

    def is_registered(self):
        """是否已注册（active 状态）。"""
        return self.state == STATE_ACTIVE


def generate_request_key():
    """生成请求密钥（24 字节熵，URL 安全）。

    返回:
        str: token_urlsafe(24) → 32 字符 URL 安全随机字符串（24 字节熵）
    """
    return secrets.token_urlsafe(24)


def _get_host_info():
    """构造 host_info 字符串（契约：aimonitor MONITOR-SPEC §3.2）。

    服务端要求 `host_info` 为字符串（如 `"hostname:dev-box, ip:192.168.1.5"`），
    不是对象。返回: str，形如 `hostname:<h>, ip:<ip>`（ip 缺省时省略 ip 段）。
    """
    hostname = socket.gethostname()
    ip = None
    try:
        # 获取第一个非回环 IPv4 地址
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127."):
                ip = addr
                break
    except OSError:
        pass
    parts = [f"hostname:{hostname}"]
    if ip:
        parts.append(f"ip:{ip}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# RegistrationPoller — 轮询审批结果（TASK-043 范围）
#
# 归属声明（TASK-042 REVIEW-r3 ISSUE-04）：本文件同时承载 TASK-042 状态机与
# TASK-043 轮询器，按段归属；RegistrationPoller/轮询相关代码属 TASK-043。
# ---------------------------------------------------------------------------


class PollError(Exception):
    """轮询注册状态失败时抛出（网络错误、服务端错误等）。"""


class PollRejectedError(PollError):
    """服务端明确拒绝（4xx 不含 404），不可重试。"""


class PollRetryableError(PollError):
    """可重试的轮询错误（网络错误、404 等）。"""


REGISTER_POLL_INTERVAL = 30       # 轮询间隔（秒）
REGISTER_POLL_MAX_DAYS = 7        # pending TTL（天）
REGISTER_POLL_MAX_RETRIES = 3     # 连续网络错误/404 最大重试次数


class RegistrationPoller:
    """轮询注册审批结果，领取 token 并自动切换为正式推送。

    用法：
        poller = RegistrationPoller()
        result = poller.poll(server_url, req_id, request_key)
        # result == {'status': 'pending'}
        # result == {'status': 'approved', 'token': '...'}
        # result == {'status': 'rejected'} 等

    错误处理：
        - 网络错误：返回 PollRetryableError，调用方按指数退避重试
        - 4xx（不含 404）：PollRejectedError，不可重试
        - 404：PollRetryableError，可重试
    """

    def __init__(self, retry_state=None, log=None, time_fn=time.time):
        """初始化轮询器。

        参数:
            retry_state: PushRetryState 实例（缺省按 time_fn 新建）
            log: 日志器（打印到 stderr，缺省新建）
            time_fn: 时间来源（单测注入 FakeClock，与轮询循环 clock 保持一致）
        """
        if retry_state is None:
            from agent_retry import PushRetryState  # noqa: E402
            retry_state = PushRetryState(time_fn=time_fn)
        self.retry_state = retry_state
        self.log = log if log is not None else _PollLogger()

    @staticmethod
    def derive_register_url(server_url):
        """从 server_url 推导注册提交 URL（替换 /api/ingest 为 /api/register）。

        参数:
            server_url:  ingest 完整地址（如 http://host:3113/api/ingest）
        返回:
            str: 注册提交 URL（如 http://host:3113/api/register）
        """
        try:
            parts = urllib.parse.urlsplit(server_url)
        except ValueError:
            raise PollError(f"server_url 非法: {server_url!r}")

        path = parts.path.rstrip("/")
        if path.endswith("/api/ingest"):
            # 替换 /api/ingest 后缀
            base_path = path[: -len("/api/ingest")] + "/api/register"
        else:
            # 不含 /api/ingest，直接追加
            base_path = path + "/api/register"

        return urllib.parse.urlunsplit((
            parts.scheme, parts.netloc, base_path, "", ""
        ))

    @staticmethod
    def derive_status_url(server_url, req_id, request_key):
        """从 server_url 推导状态查询 URL。

        替换 server_url 路径中的 /api/ingest 为 /api/register/<req_id>/status，
        并追加 ?request_key=<key> 查询参数。

        参数:
            server_url:   ingest 完整地址（如 http://host:3113/api/ingest）
            req_id:       注册请求 ID
            request_key:  请求密钥
        返回:
            str: 状态查询 URL（如 http://host:3113/api/register/req-xxx/status?request_key=key）

        契约（TASK-015 对齐 aimonitor MONITOR-SPEC §3.2 / server/monitor_server.py STATUS_RE）：
        服务端路由为 `GET /api/register/<req_id>/status`，**必须带 /status 段**；
        本实现曾漏掉 /status 导致轮询 404（永远等不到 approved），已修复。

        权衡声明（TASK-042 REVIEW-r3 ISSUE-08）：request_key 明文进 URL query 会
        进入服务端/代理访问日志；header 传输方案由 aimonitor TASK-046 契约层评估，
        本实现暂按契约文档的 GET /api/register/<req_id>/status?request_key=<key> 形态落地。
        """
        base = RegistrationPoller.derive_register_url(server_url)
        status_path = f"{base}/{req_id}/status"
        query = urllib.parse.urlencode({"request_key": request_key})
        try:
            parts = urllib.parse.urlsplit(status_path)
        except ValueError:
            raise PollError(f"server_url 非法: {server_url!r}")
        return urllib.parse.urlunsplit((
            parts.scheme, parts.netloc, parts.path, query, ""
        ))

    def _read_response(self, resp):
        """读取响应体（上限 64KB，UTF-8 容错解码）。"""
        return resp.read(64 * 1024).decode("utf-8", errors="replace")

    def _parse_status_response(self, body):
        """解析状态查询响应体，返回规范化状态码。

        返回 dict:
            {'status': 'pending'}
            {'status': 'approved', 'token': '...', 'project_id': '...'}
            {'status': 'rejected'}
            {'status': 'expired'}
            {'status': 'revoked'}
        异常:
            PollError: 响应格式非法
        """
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            raise PollError(f"服务端返回非 JSON: {e}")

        if not isinstance(data, dict):
            raise PollError("服务端返回非对象")

        status = data.get("status")
        if status not in ("pending", "approved", "rejected", "expired", "revoked"):
            raise PollError(f"未知状态: {status!r}")

        result = {"status": status}
        if status == "approved":
            token = data.get("token")
            if not token:
                raise PollError("approved 状态缺少 token")
            result["token"] = token
            if "project_id" in data:
                result["project_id"] = data["project_id"]
        return result

    def _do_request(self, url):
        """执行一次 HTTP GET 请求，返回响应体字符串。

        异常:
            PollRejectedError: 4xx（不含 404）
            PollRetryableError: 网络错误/404/5xx
        """
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "aibase-agent/0.1")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return self._read_response(resp)
        except urllib.error.HTTPError as e:
            status = e.code
            body = self._read_response(e) if e.fp else ""
            e.close()
            if status == 404:
                raise PollRetryableError(f"注册请求未找到（HTTP 404），将重试: {body[:200]}")
            if 400 <= status < 500:
                raise PollRejectedError(f"服务端拒绝请求（HTTP {status}）: {body[:200]}")
            # 5xx
            raise PollRetryableError(f"服务端错误（HTTP {status}）: {body[:200]}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise PollRetryableError(f"轮询网络失败: {e}")

    def poll(self, server_url, req_id, request_key):
        """执行一次状态查询。

        参数:
            server_url:  ingest 完整地址（agent.json server_url）
            req_id:      注册请求 ID
            request_key: 请求密钥
        返回:
            dict: {"status": str, ...}
                - pending: {"status": "pending"}
                - approved: {"status": "approved", "token": "...", "project_id": "..."}
                - rejected/expired/revoked: {"status": "..."}
        异常:
            PollRejectedError: 不可重试的错误（4xx 不含 404）
            PollRetryableError: 可重试的错误（网络错误/404/5xx）
        """
        url = self.derive_status_url(server_url, req_id, request_key)
        body = self._do_request(url)
        return self._parse_status_response(body)


class _PollLogger:
    """轮询日志器：所有输出到 stderr，不抛异常。"""

    def info(self, message):
        print(f"agent: {message}", file=sys.stderr)

    def error(self, message):
        print(f"agent: {message}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 注册请求构造与提交
# ---------------------------------------------------------------------------


def submit_register(server_url, payload, timeout=30):
    """向服务端提交注册申请（POST /api/register）。

    参数:
        server_url:  ingest 完整地址（agent.json server_url）
        payload:     build_register_payload() 构造的请求体
        timeout:     连接/读取超时（秒，默认 30）
    返回:
        dict: 201 成功响应，如 {"req_id": "...", "status": "pending", "pending_since": ...}
    异常:
        RegisterConflictError: HTTP 409（project_id 已注册或已有 pending 申请）
        PollRejectedError:     其它 4xx，不可重试
        PollRetryableError:    5xx / 网络错误，可退避重试
    """
    url = RegistrationPoller.derive_register_url(server_url)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "aibase-agent/0.1")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(64 * 1024).decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PollError(f"服务端注册响应非 JSON: {e}")
        if not isinstance(data, dict):
            raise PollError("服务端注册响应非对象")
        return data
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read(64 * 1024).decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError):
            raw = ""
        finally:
            e.close()
        if status == 409:
            existing = "active"
            try:
                existing = json.loads(raw).get("existing", "active") or "active"
            except (json.JSONDecodeError, AttributeError):
                pass
            hint = "该项目已注册" if existing == "active" else "该项目已有待审批的注册申请"
            raise RegisterConflictError(
                existing,
                message=f"注册冲突（HTTP 409）: {hint}（existing={existing}）: {raw[:200]}",
            )
        if 400 <= status < 500:
            raise PollRejectedError(f"服务端拒绝注册请求（HTTP {status}）: {raw[:200]}")
        raise PollRetryableError(f"服务端错误（HTTP {status}）: {raw[:200]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PollRetryableError(f"提交注册请求网络失败: {e}")


def build_register_payload(project, request_key, enrollment_code=None):
    """构造注册请求 payload。

    参数:
        project: dict，含 id 和 path
        request_key: str，请求密钥
        enrollment_code: str or None，注册码（可选）
    返回:
        dict: 注册请求体
    """
    payload = {
        "project_id": project["id"],
        "path": project["path"],
        "request_key": request_key,
        "host_info": _get_host_info(),
    }
    if enrollment_code is not None:
        payload["enrollment_code"] = enrollment_code
    return payload
