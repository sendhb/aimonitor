"""agent_http.py — AIOS 遥测 HTTP 推送客户端（kit/tools/agent/ 推送层）。

消费 TASK-024 `serialize_payload()` 输出的 JSON 字符串，POST 到监控端 ingest
（默认路径 /api/ingest，完整地址来自 agent.json `server_url`）：

    Authorization: Bearer <token>
    Content-Type:   application/json

零外部依赖（仅 stdlib urllib/http.client），风格与 agent_config.py / agent_payload.py 一致。
使用 urllib.request 完成请求（自定义 HTTP/HTTPS handler 实现超时分离）：
- connect 阶段超时：`connect_timeout`（默认 5s）——建连被拒/黑洞/超时都算网络失败；
- read 阶段超时：`read_timeout`（默认 30s）——覆盖响应头与响应体读取。
http.client 的 timeout 参数会同时约束 connect 与 read；这里在 connect() 完成后把
socket 超时切换为 read_timeout，实现两阶段分离（见 _SplitTimeoutMixin）。

状态码语义（与 aimonitor ingest 契约，TASK-025 首次定义）：
- 2xx                 → 成功，返回 PushResult(status, body)
- 3xx                 → PushRejectedError —— 不跟随重定向（ingest 端点对 POST 返回 3xx
  属配置/契约异常；跟随会把 Authorization 头原样发给异源目标，且 301/302/303 会把
  POST 静默降级为 GET——客户端报成功但数据未 ingest，见 REVIEW FIND-001）
- 400/401/409/413 及
  其它 4xx           → PushRejectedError —— payload/token/配置需修复，不可重试
- 429                → PushRateLimitedError —— 可重试（优先按 Retry-After，缺省指数退避 TASK-026）
- 5xx                → PushServerError —— 服务端暂时故障，可重试
- 网络层失败/超时     → PushNetworkError —— 可重试（timeout 属性标记是否为超时）

`is_retryable(err)` 供 TASK-026 退避循环分类：429 / 5xx / 网络失败可重试，
4xx 客户端错误不重试（避免对已确定的错误空转）。
"""
import collections
import http.client
import urllib.error
import urllib.parse
import urllib.request

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0
USER_AGENT = "aibase-agent/0.1"
MAX_RESPONSE_BODY = 64 * 1024  # 响应体读取上限（防御异常大响应，正常 ingest 响应远小于此）

# 成功响应的返回载体：status（int）+ body（str，解码后的响应体）
PushResult = collections.namedtuple("PushResult", ["status", "body"])
PushResult.__doc__ = "推送成功（2xx）返回：status 状态码 + body 响应体。"


class PushError(Exception):
    """HTTP 推送失败基类。"""


class PushRejectedError(PushError):
    """4xx 客户端错误（400/401/409/413 等）——payload/token/配置需修复，不可重试。"""

    def __init__(self, status, body=None, message=None):
        self.status = status
        self.body = body
        if message is None:
            message = f"服务器拒绝请求（HTTP {status}）"
            if body:
                message += f": {body[:200]}"
        super().__init__(message)


class PushRateLimitedError(PushError):
    """429 限流——可重试（优先按 Retry-After，缺省走 TASK-026 指数退避）。"""

    def __init__(self, status=429, body=None, retry_after=None, message=None):
        self.status = status
        self.body = body
        self.retry_after = retry_after
        if message is None:
            message = f"服务器限流（HTTP {status}）"
            if retry_after:
                message += f"，Retry-After {retry_after}s"
        super().__init__(message)


class PushServerError(PushError):
    """5xx 服务端错误——可重试。"""

    def __init__(self, status, body=None, message=None):
        self.status = status
        self.body = body
        if message is None:
            message = f"服务器错误（HTTP {status}）"
            if body:
                message += f": {body[:200]}"
        super().__init__(message)


class PushNetworkError(PushError):
    """网络层失败（连接被拒/超时/DNS 等）——可重试；timeout=True 表示超时。"""

    def __init__(self, message, timeout=False):
        self.timeout = bool(timeout)
        super().__init__(message)


RETRYABLE_ERRORS = (PushRateLimitedError, PushServerError, PushNetworkError)


def is_retryable(error):
    """推送失败是否值得重试（TASK-026 退避循环据此分类）。"""
    return isinstance(error, RETRYABLE_ERRORS)


# ---------------------------------------------------------------------------
# 超时分离的 urllib handler（http.client 连接扩展）
# ---------------------------------------------------------------------------

class _SplitTimeoutMixin:
    """http.client 连接扩展：connect 阶段用 connect_timeout，read 阶段用 read_timeout。

    urllib do_open 总会以 timeout=req.timeout（None）调用连接类构造；这里用
    _connect_timeout 覆盖为 connect 阶段超时，connect() 完成后把 socket 超时
    切换为 _read_timeout，实现 connect/read 两阶段分离。
    """

    def __init__(self, *args, _connect_timeout=None, _read_timeout=None, **kwargs):
        if _connect_timeout is not None:
            kwargs["timeout"] = _connect_timeout
        super().__init__(*args, **kwargs)
        self._read_timeout = _read_timeout

    def connect(self):
        super().connect()
        if self._read_timeout is not None and getattr(self, "sock", None) is not None:
            self.sock.settimeout(self._read_timeout)


class _SplitTimeoutHTTPConnection(_SplitTimeoutMixin, http.client.HTTPConnection):
    """HTTP 连接：connect/read 超时分离。"""


class _SplitTimeoutHTTPSConnection(_SplitTimeoutMixin, http.client.HTTPSConnection):
    """HTTPS 连接：connect/read 超时分离（证书校验保持 http.client 默认行为）。"""


class _SplitTimeoutHTTPHandler(urllib.request.HTTPHandler):
    """urllib HTTP handler：请求使用 _SplitTimeoutHTTPConnection。"""

    def __init__(self, connect_timeout, read_timeout):
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        super().__init__()

    def http_open(self, req):
        return self.do_open(
            _SplitTimeoutHTTPConnection,
            req,
            _connect_timeout=self._connect_timeout,
            _read_timeout=self._read_timeout,
        )


class _SplitTimeoutHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib HTTPS handler：请求使用 _SplitTimeoutHTTPSConnection。"""

    def __init__(self, connect_timeout, read_timeout):
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        super().__init__()

    def https_open(self, req):
        return self.do_open(
            _SplitTimeoutHTTPSConnection,
            req,
            _connect_timeout=self._connect_timeout,
            _read_timeout=self._read_timeout,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向：ingest 端点对 POST 返回 3xx 属配置/契约异常（REVIEW FIND-001）。

    urllib 默认 HTTPRedirectHandler 会跟随 301/302/303/307/308，并把原请求头
    （含 Authorization、User-Agent）原样复制到重定向目标——跨源重定向会把
    Bearer token 泄露给任意主机；且 301/302/303 会把 POST 静默降级为 GET
    （客户端报成功但数据未 ingest）。这里在入口直接以 HTTPError 拒绝，连
    Location 都不解析（默认实现会在调用 redirect_request 之前 urlparse
    Location，畸形 Location 会先抛 ValueError 逃逸），3xx 交给状态码分类
    → PushRejectedError。
    """

    def _reject(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = _reject


def _build_opener(connect_timeout, read_timeout):
    """构造带超时分离 handler 的 opener（其余默认 handler 由 build_opener 补齐）。

    _NoRedirectHandler 是 HTTPRedirectHandler 子类，build_opener 检测到同族 handler
    已提供时不会追加默认的 HTTPRedirectHandler（重定向被整体禁止）。
    """
    return urllib.request.build_opener(
        _SplitTimeoutHTTPHandler(connect_timeout, read_timeout),
        _SplitTimeoutHTTPSHandler(connect_timeout, read_timeout),
        _NoRedirectHandler(),
    )


# ---------------------------------------------------------------------------
# 参数校验与状态码分类
# ---------------------------------------------------------------------------

def _redact_url(url):
    """剔除 URL 中的 userinfo（防止密码进入异常文本，REVIEW NOTE-001）。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<非法 URL>"
    if parts.username is None and parts.password is None:
        return url
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urllib.parse.urlunsplit(
        (parts.scheme, netloc, parts.path, parts.query, parts.fragment)
    )


def _validate_args(server_url, token, body, connect_timeout, read_timeout):
    """入参校验；非法时抛 PushError（URL/token/body/超时）。"""
    if not isinstance(server_url, str) or not server_url.strip():
        raise PushError("server_url 必须是非空字符串（agent.json server_url）")
    try:
        parts = urllib.parse.urlsplit(server_url)
    except ValueError as e:
        raise PushError(f"server_url 非法: {e}") from e
    if parts.username is not None or parts.password is not None:
        raise PushError("server_url 不得包含 userinfo（用户名/密码）")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PushError(f"server_url 必须是 http/https 完整地址: {_redact_url(server_url)!r}")
    try:
        parts.port  # 触发端口解析；非数字端口会抛 ValueError
    except ValueError as e:
        raise PushError(f"server_url 端口非法: {e}") from e

    if not isinstance(token, str) or not token:
        raise PushError("token 必须是非空字符串（agent.json token）")
    if not isinstance(body, str):
        raise PushError("body 必须是字符串（serialize_payload 输出）")
    for name, value in (("connect_timeout", connect_timeout),
                        ("read_timeout", read_timeout)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PushError(f"{name} 必须是数字（秒）")
        if value <= 0:
            raise PushError(f"{name} 必须大于 0（秒）")


def _parse_retry_after(headers):
    """解析 Retry-After 响应头（整数秒）；缺失/非整数返回 None（退避循环走指数退避兜底）。"""
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    return None


def _read_body(resp):
    """读取响应体（上限 MAX_RESPONSE_BODY 字节，UTF-8 容错解码）。"""
    return resp.read(MAX_RESPONSE_BODY).decode("utf-8", errors="replace")


def _raise_for_status(status, body, headers):
    """按状态码分类：2xx 返回 PushResult，否则抛对应 PushError。"""
    if 200 <= status < 300:
        return PushResult(status, body)
    if status == 429:
        raise PushRateLimitedError(status, body=body, retry_after=_parse_retry_after(headers))
    if 500 <= status < 600:
        raise PushServerError(status, body=body)
    raise PushRejectedError(status, body=body)


def _is_timeout(error):
    """判断错误是否为超时（TimeoutError 直抛，或 URLError 包裹的 TimeoutError）。"""
    if isinstance(error, TimeoutError):
        return True
    if isinstance(error, urllib.error.URLError):
        return isinstance(getattr(error, "reason", None), TimeoutError)
    return False


def _network_message(error):
    if isinstance(error, urllib.error.URLError):
        return f"推送网络失败: {getattr(error, 'reason', error)}"
    return f"推送网络失败: {error}"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def push_payload(server_url, token, body,
                 connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT):
    """POST 遥测 payload 到监控端 ingest（urllib，connect/read 超时分离）。

    参数:
        server_url:      ingest 完整地址（http/https，agent.json server_url）
        token:           Bearer token（agent.json token）
        body:            serialize_payload() 输出的 JSON 字符串
        connect_timeout: connect 阶段超时（秒，正数，默认 5）
        read_timeout:    read 阶段超时（秒，正数，默认 30；覆盖响应头与响应体读取）
    返回:
        PushResult(status, body) —— 2xx 成功
    异常:
        PushRejectedError    3xx/4xx 客户端错误（3xx 重定向不跟随；400/401/409/413
                            及其它 4xx），不可重试
        PushRateLimitedError 429，可重试（retry_after 属性为 Retry-After 秒数或 None）
        PushServerError      5xx，可重试
        PushNetworkError     网络层失败（连接被拒/超时/DNS），可重试（timeout 标记超时）
        PushError            参数非法（URL/token/body/超时）
    """
    _validate_args(server_url, token, body, connect_timeout, read_timeout)

    req = urllib.request.Request(server_url, data=body.encode("utf-8"), method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)

    opener = _build_opener(connect_timeout, read_timeout)
    try:
        with opener.open(req) as resp:
            resp_body = _read_body(resp)
        return PushResult(resp.status, resp_body)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            resp_body = _read_body(e)
        except (urllib.error.URLError, TimeoutError, OSError) as body_err:
            # FIND-002 回归：错误状态码下响应体读取阶段超时（服务端发完状态头后
            # stall 响应体）→ 映射为 PushNetworkError(timeout=True)，与 2xx 路径一致，
            # 避免裸 TimeoutError 逃逸破坏"超时归 PushNetworkError、可重试"契约。
            raise PushNetworkError(_network_message(body_err),
                                   timeout=_is_timeout(body_err)) from body_err
        finally:
            e.close()
        return _raise_for_status(status, resp_body, e.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PushNetworkError(_network_message(e), timeout=_is_timeout(e)) from e
