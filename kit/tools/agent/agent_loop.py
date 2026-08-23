"""agent_loop.py — 主循环与常驻（kit/tools/agent/ 循环层）。

汇聚 TASK-022 配置 / TASK-023 runtime 读取 / TASK-024 payload 构造 /
TASK-025 HTTP 推送 / TASK-026 重试退避，组成常驻轮询主循环：

    python3 agent.py                     # 常驻：按 poll_interval_seconds（默认 30s）轮询
    python3 agent.py --once              # 单轮：推送一轮后退出（cron / systemd timer）
    python3 agent.py --interval 10       # 覆盖轮询间隔（秒）
    python3 agent.py --quiet             # 安静模式：抑制常规日志，错误始终输出 stderr

设计要点：
- 每项目独立退避状态（PushRetryState per project）：A 项目失败退避不阻塞 B 项目推送；
  退避中的项目本轮跳过（can_push 语义，TASK-026），轮询间隔恒定、不 sleep 累积。
- 信号：SIGINT/SIGTERM 注册处理器 → 置位 StopFlag（处理器内不抛异常，在途 HTTP 请求
  自然完成）；常驻睡眠按 POLL_SLEEP_STEP 分片——PEP 475 下 signal 不中断 time.sleep，
  分片保证信号响应延迟上限 ≈ 0.5s + 在途 read_timeout（≤30s），随后干净退出。
- 可测试：时钟/睡眠/读取/构造/序列化/推送函数全部可注入，单测零真实网络与 sleep。
"""
import math
import signal
import sys
import time

import agent_http
import agent_payload
import agent_retry
import agent_runtime

POLL_SLEEP_STEP = 0.5  # 常驻睡眠分片（秒）：信号响应延迟上限（不含在途 HTTP 请求）


class AgentLog:
    """agent 日志器：quiet=True 时常规日志（info）静默，错误（error）始终输出。

    info → stdout，error → stderr：故障/退避告警必须可见，不随 --quiet 隐藏。
    """

    def __init__(self, quiet=False, stream=None, err_stream=None):
        self.quiet = bool(quiet)
        self.stream = stream if stream is not None else sys.stdout
        self.err_stream = err_stream if err_stream is not None else sys.stderr

    def info(self, message):
        if not self.quiet:
            print(f"agent: {message}", file=self.stream)

    def error(self, message):
        print(f"agent: {message}", file=self.err_stream)


class StopFlag:
    """可调用停止标志：信号处理器置位，主循环每轮/每个睡眠分片后检查。

    作为 stop_fn 传入 run_forever（callable）；signum 记录首个触发信号，
    供退出日志显示 SIGINT/SIGTERM。
    """

    def __init__(self):
        self.stopped = False
        self.signum = None

    def __call__(self):
        return self.stopped

    def request_stop(self, signum=None):
        if signum is not None and self.signum is None:
            self.signum = signum
        self.stopped = True


def _make_signal_handler(flag, sig):
    def handler(signum, frame):
        flag.request_stop(signum)
    return handler


def install_signal_handlers(flag):
    """注册 SIGINT/SIGTERM → 置位 stop 标志；返回成功注册的信号列表。

    非主线程（ValueError）或环境不允许（OSError）时跳过该信号而不抛异常——
    单测可在任意线程安全调用。处理器只置位标志不抛异常，保证"干净退出"。
    """
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _make_signal_handler(flag, sig))
            installed.append(sig)
        except (ValueError, OSError):
            continue
    return installed


def poll_once(cfg, states=None, log=None, clock=time.time,
              read_fn=agent_runtime.read_project_runtime,
              payload_fn=agent_payload.build_payload,
              serialize_fn=agent_payload.serialize_payload,
              push_fn=agent_http.push_payload,
              on_auth_rejected=None):
    """执行一轮轮询：遍历 cfg["projects"] 逐项目 读取→构造→序列化→推送。

    参数:
        cfg:      agent_config.validate 规范化配置（server_url/token/projects）
        states:   {project_id: PushRetryState} 跨轮共享状态表；缺省新建空表
        log:      AgentLog；缺省新建（quiet=False）
        clock:    时间来源（time_fn 注入，FakeClock 供单测）
        其余 *fn: 读取/构造/序列化/推送函数，单测注入替身（默认接真实模块）
        on_auth_rejected: 可选回调 on_auth_rejected(pid, error)——推送收到 HTTP 401
            （token 失效/被吊销）时调用；由注册流程注入，用于重新轮询注册状态（INT-003）
    返回:
        (pushed, skipped, failed)：成功推送 / 退避跳过 / 失败的项目数
    """
    if states is None:
        states = {}
    if log is None:
        log = AgentLog()

    pushed = skipped = failed = 0
    for project in cfg["projects"]:
        pid = project["id"]
        state = states.get(pid)
        if state is None:
            state = states[pid] = agent_retry.PushRetryState(time_fn=clock)

        if not state.can_push():
            skipped += 1
            log.info(f"{pid}: 退避中（约 {state.seconds_until_retry():.0f}s 后重试），本轮跳过")
            continue

        try:
            snapshot = read_fn(project["path"])
            payload = payload_fn(pid, snapshot)
            body = serialize_fn(payload)
        except agent_payload.PayloadError as e:
            failed += 1
            state.record_failure()
            log.error(f"{pid}: payload 构造失败（已记录退避）: {e}")
            continue

        try:
            push_fn(cfg["server_url"], cfg["token"], body)
        except agent_http.PushError as e:
            failed += 1
            retry_after = getattr(e, "retry_after", None)
            delay = state.record_failure(retry_after=retry_after)
            if agent_http.is_retryable(e):
                log.error(f"{pid}: 推送失败（可重试），约 {delay:.0f}s 后重试: {e}")
            else:
                log.error(f"{pid}: 推送失败（不可重试，已记录退避）: {e}")
            # INT-003：401 = token 失效/被吊销 → 通知注册流程重新轮询状态
            if on_auth_rejected is not None and getattr(e, "status", None) == 401:
                on_auth_rejected(pid, e)
            continue

        state.record_success()
        pushed += 1
        log.info(f"{pid}: 推送成功")
    return pushed, skipped, failed


def _positive_seconds(name, value):
    """校验正有限秒数并返回 float；非法抛 ValueError（run_forever 防御性入参校验）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是正数（秒）")
    fv = float(value)
    if not math.isfinite(fv) or fv <= 0:
        raise ValueError(f"{name} 必须是正数（秒）")
    return fv


def run_forever(cfg, interval, states=None, log=None, stop_fn=None,
                clock=time.time, sleeper=time.sleep, poller=poll_once):
    """常驻循环：每轮 poll_once 后按 interval 睡眠（分片），stop_fn 置位时干净退出。

    参数:
        cfg:      规范化配置（agent_config.validate 输出）
        interval: 轮询间隔（秒，正数；config 或 --interval，CLI 层保证已校验）
        states:   跨轮共享退避状态表（缺省新建）
        log:      AgentLog（缺省新建）
        stop_fn:  停止判定 callable（缺省永不停止）；信号处理器置位的 StopFlag
        clock:    时间来源（单测注入 FakeClock）
        sleeper:  睡眠函数（单测注入手动推进时钟的替身）
        poller:   单轮执行函数（缺省 poll_once；单测注入计数替身）
    返回:
        "stopped"（退出原因；供测试/日志断言）
    """
    interval = _positive_seconds("interval", interval)
    if stop_fn is None:
        stop_fn = lambda: False
    if states is None:
        states = {}
    if log is None:
        log = AgentLog()

    log.info(f"常驻启动：{len(cfg['projects'])} 个项目，轮询间隔 {interval:g}s"
             f"（SIGINT/SIGTERM 干净退出）")
    while not stop_fn():
        poller(cfg, states=states, log=log, clock=clock)
        deadline = clock() + interval
        while not stop_fn():
            remaining = deadline - clock()
            if remaining <= 0.0:
                break
            sleeper(min(remaining, POLL_SLEEP_STEP))

    signum = getattr(stop_fn, "signum", None)
    if signum is None:
        log.info("停止标志置位，干净退出")
    else:
        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = str(signum)
        log.info(f"收到信号 {sig_name}，干净退出")
    return "stopped"


# ---------------------------------------------------------------------------
# 注册轮询循环
# ---------------------------------------------------------------------------


REGISTER_POLL_INTERVAL = 30      # 轮询间隔（秒）
REGISTER_POLL_MAX_SECONDS = 7 * 24 * 3600  # pending TTL（7 天）
REGISTER_POLL_MAX_RETRIES = 3   # 连续错误最大重试次数


def run_registration_polling(cfg, path, log=None, clock=time.time,
                             sleeper=time.sleep, stop_fn=None):
    """注册轮询循环：轮询审批结果，领取 token 后写入 agent.json 并启动推送。

    参数:
        cfg:      规范化配置（agent_config.validate 输出，state=pending）
        path:     agent.json 文件路径
        log:      AgentLog（缺省新建）
        clock:    时间来源（单测注入 FakeClock）
        sleeper:  睡眠函数（单测注入）
        stop_fn:  停止判定 callable（缺省永不停止）
    返回:
        "approved"  — 注册成功，已切换为推送
        "rejected"  — 审批被拒绝
        "expired"   — 注册请求已过期
        "revoked"   — 注册请求已被撤销
        "timeout"   — 超过 7 天未审批
        "error"     — 不可恢复的错误
    异常:
        不抛异常（所有错误 stderr 输出，无 traceback）
    """
    import agent_register as reg_lib  # noqa: E402

    if log is None:
        log = AgentLog()
    if stop_fn is None:
        stop_fn = lambda: False

    server_url = cfg["server_url"]
    req_id = cfg.get("req_id")
    request_key = cfg.get("request_key")

    if not req_id or not request_key:
        log.error("注册状态异常：缺少 req_id 或 request_key")
        return "error"

    poller = reg_lib.RegistrationPoller(log=log, time_fn=clock)
    retry_state = poller.retry_state
    start_time = clock()
    consecutive_errors = 0

    log.info(f"注册轮询启动：请求 {req_id}，{REGISTER_POLL_INTERVAL}s 间隔")

    while not stop_fn():
        # 检查 TTL
        elapsed = clock() - start_time
        if elapsed > REGISTER_POLL_MAX_SECONDS:
            log.error(f"注册等待超时（{REGISTER_POLL_MAX_SECONDS / 3600:.0f}h），请重新注册")
            return "timeout"

        # 检查退避
        if not retry_state.can_push():
            remaining = retry_state.seconds_until_retry()
            deadline = clock() + remaining
            while not stop_fn() and clock() < deadline:
                sleeper(min(deadline - clock(), POLL_SLEEP_STEP))
            if stop_fn():
                break
            continue

        # 执行一次轮询
        try:
            result = poller.poll(server_url, req_id, request_key)
        except reg_lib.PollRejectedError as e:
            log.error(str(e))
            return "error"
        except reg_lib.PollRetryableError as e:
            consecutive_errors += 1
            if consecutive_errors >= REGISTER_POLL_MAX_RETRIES:
                log.error(f"无法连接服务端（连续 {consecutive_errors} 次失败），请稍后重试")
                return "error"
            retry_state.record_failure()
            log.error(f"{e}（第 {consecutive_errors} 次）")
            continue
        except reg_lib.PollError as e:
            log.error(str(e))
            return "error"

        # 处理状态
        status = result["status"]
        consecutive_errors = 0
        retry_state.record_success()

        if status == "pending":
            # 继续轮询
            deadline = clock() + REGISTER_POLL_INTERVAL
            while not stop_fn() and clock() < deadline:
                sleeper(min(deadline - clock(), POLL_SLEEP_STEP))
            if stop_fn():
                break
            continue

        elif status == "approved":
            token = result["token"]
            # 保存 token 到 agent.json
            reg = reg_lib.RegistrationState(cfg)
            reg.transition_to("active", token=token)
            reg.save(path, token=token)
            # 更新 cfg 中的 token 和 state，确保后续推送使用正确 token
            cfg["token"] = token
            cfg["state"] = "active"
            cfg.pop("req_id", None)
            cfg.pop("request_key", None)
            log.info("✓ 注册成功，已获取 token，开始推送监控数据")
            interval = cfg.get("poll_interval_seconds", 30)

            # INT-003 吊销流程：推送期收到 401（token 失效/吊销）→ 重新轮询
            # 注册状态；收到 revoked/rejected → 停止推送，run_registration_polling
            # 返回 "revoked"（CLI 以非 0 退出，提示重新注册）。
            # 注意：req_id/request_key 已从 cfg 清除，但函数局部变量仍在闭包内可用。
            revoked_flag = []

            def _on_auth_rejected(pid, error):
                try:
                    auth_result = poller.poll(server_url, req_id, request_key)
                except reg_lib.PollError as e:
                    log.error(f"{pid}: token 失效后注册状态查询失败: {e}")
                    return
                if auth_result.get("status") in ("revoked", "rejected"):
                    revoked_flag.append(auth_result["status"])
                    log.error(f"{pid}: 注册状态 {auth_result['status']}，token 已失效，停止推送")
                else:
                    log.error(f"{pid}: 推送认证失败（HTTP 401），但注册状态仍为 "
                              f"{auth_result.get('status')}，继续按退避重试")

            def _auth_poller(cfg, states=None, log=None, clock=time.time):
                return poll_once(cfg, states=states, log=log, clock=clock,
                                 on_auth_rejected=_on_auth_rejected)

            def _combined_stop():
                return bool(revoked_flag) or stop_fn()

            run_forever(cfg, interval, log=log, stop_fn=_combined_stop,
                        clock=clock, sleeper=sleeper, poller=_auth_poller)
            if revoked_flag:
                return "revoked"
            return "approved"

        elif status == "rejected":
            log.error("注册请求被拒绝")
            return "rejected"

        elif status == "expired":
            log.error("注册请求已过期，请重新注册")
            return "expired"

        elif status == "revoked":
            log.error("注册请求已被撤销")
            return "revoked"

    return "stopped"
