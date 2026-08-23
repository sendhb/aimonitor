"""agent_retry.py — AIOS 遥测推送重试退避状态机（kit/tools/agent/ 退避层）。

消费 TASK-025 `agent_http.push_payload()` 抛出的推送失败，提供"指数退避 +
连续失败计数 + 恢复重置"的时间状态机，供 TASK-027 主循环使用：

    1s → 2s → 4s → 8s → 16s → 32s → 60s → 60s → …（cap = MAX_DELAY）

**关键设计：不阻塞下一轮。** 本模块不 sleep、不做同步重试循环；它只维护状态并给出
"下一次允许推送的时间"（`next_retry_at`）。主循环每轮先问 `can_push()`：
未到时间则跳过本轮推送（退避等待不塞进轮询间隔），到时间才尝试推送。

主循环用法（TASK-027 模式）：

    state = PushRetryState()
    ...
    if not state.can_push():          # 退避中 → 跳过本轮，不阻塞
        continue
    try:
        push_payload(server_url, token, body)
    except agent_http.PushError as e:
        state.record_failure(retry_after=getattr(e, "retry_after", None))
        # 按 agent_http.is_retryable(e) 决定日志级别/告警（本模块不重复判定）
    else:
        state.record_success()        # 恢复 → 计数清零、解除退避

时间注入：`time_fn` 默认为 `time.time`；单测传 FakeClock 手动推进，
退避时序（指数增长 / cap / 恢复重置 / 不阻塞）全部确定性验证。

**溢出安全（REVIEW FIND-001 回归）**：指数退避不直接求 `2**(failures-1)` 巨大整数——
长时间持续失败（默认 60s cap 下约 17h 即 failures ≥ 1025）或超大 Retry-After 不再
`OverflowError` 崩溃，而是返回 cap / 回退指数退避。
"""
import math
import time

BASE_DELAY = 1.0   # 首次失败后的退避基数（秒）
MAX_DELAY = 60.0   # 退避上限（秒）——指数增长超过后恒定不再涨


def _finite_positive_seconds(name, value):
    """校验秒数参数为有限正数并返回 float；否则抛 ValueError。

    拒绝 bool、非数字、<= 0、inf/nan，以及超出 float 表示范围的大整数
    （如 10**400）——这类值无法参与退避计时，视为配置错误。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是有限正数（秒）")
    try:
        fv = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} 必须是有限正数（秒）：{value!r} 超出 float 范围")
    if not math.isfinite(fv) or fv <= 0:
        raise ValueError(f"{name} 必须是有限正数（秒）：{value!r} 不是有限正数")
    return fv


def backoff_delay(failures, base_delay=BASE_DELAY, max_delay=MAX_DELAY):
    """第 failures 次连续失败后的退避时长：base * 2^(failures-1)，封顶 max_delay。

    参数:
        failures:   连续失败次数（>= 1 的整数，调用方在失败计数后传入）
        base_delay: 退避基数（有限正数秒，默认 1.0）
        max_delay:  退避上限（有限正数秒，默认 60.0）
    返回:
        float 秒数；序列 1,2,4,8,16,32,60,60,…（cap 后恒定）
    异常:
        ValueError: failures 非 >= 1 整数 / base_delay、max_delay 非有限正数

    溢出安全：采用逐次翻倍 + 封顶算法，不构造 `2**(failures-1)` 巨大整数——
    failures 再大（长时间持续失败）也只翻倍到 cap 即返回，绝不 OverflowError。
    """
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 1:
        raise ValueError("failures 必须是 >= 1 的整数（连续失败次数）")
    base_f = _finite_positive_seconds("base_delay", base_delay)
    max_f = _finite_positive_seconds("max_delay", max_delay)
    delay = min(base_f, max_f)  # base > max 时首项即钳到 cap
    remaining = failures - 1
    while remaining > 0 and delay < max_f:
        delay = min(delay * 2.0, max_f)
        remaining -= 1
    return delay


class PushRetryState:
    """推送重试退避状态机（指数退避 + 连续失败计数 + 恢复重置，不阻塞）。

    状态:
        consecutive_failures: 连续失败次数（成功后清零）
        next_retry_at:        下一次允许推送的 epoch 秒；None = 未在退避（立即可推）
        base_delay / max_delay: 退避基数与上限（构造后只读）
        time_fn:              时间来源（默认 time.time；单测注入 FakeClock）

    方法:
        record_failure(retry_after=None)  记录失败：计数 +1，计算退避并设置 next_retry_at
        record_success()                  记录成功：计数清零、解除退避（恢复后重置）
        can_push()                        当前是否允许推送（未在退避 / 已到 next_retry_at）
        seconds_until_retry()             距离下次允许推送的剩余秒数（<= 0 表示已可推送）
    """

    def __init__(self, base_delay=BASE_DELAY, max_delay=MAX_DELAY, time_fn=time.time):
        """构造状态机；base_delay/max_delay 非法抛 ValueError，time_fn 非法抛 TypeError。"""
        self.base_delay = _finite_positive_seconds("base_delay", base_delay)
        self.max_delay = _finite_positive_seconds("max_delay", max_delay)
        if not callable(time_fn):
            raise TypeError("time_fn 必须是可调用对象（返回 epoch 秒）")
        self.time_fn = time_fn
        self.consecutive_failures = 0
        self.next_retry_at = None

    def _now(self):
        return float(self.time_fn())

    def record_failure(self, retry_after=None):
        """记录一次推送失败：连续失败计数 +1，计算退避并设置下次允许推送时间。

        参数:
            retry_after: 服务器 Retry-After 秒数（来自 PushRateLimitedError，整数秒）——
                         提供且为正数时优先使用（服务器明确要求），否则走指数退避兜底。
        返回:
            float 本次使用的退避秒数（主循环可据此日志"下次重试约 N 秒后"）。
        """
        self.consecutive_failures += 1
        delay = self._delay_for_failure(retry_after)
        self.next_retry_at = self._now() + delay
        return delay

    def _delay_for_failure(self, retry_after):
        """本次失败使用的退避时长：Retry-After 正数（可表示且有限）优先，否则指数退避。

        溢出兜底：Retry-After 是正数但 `float()` 溢出（如 10**400）或非有限
        （inf/nan）时视为无效，回退指数退避——恶意/故障 ingest 服务器不能借此
        让 agent 在首次失败即崩溃（REVIEW FIND-001 回归）。
        """
        if (isinstance(retry_after, (int, float))
                and not isinstance(retry_after, bool) and retry_after > 0):
            try:
                retry_f = float(retry_after)
            except (OverflowError, ValueError):
                retry_f = None
            if retry_f is not None and math.isfinite(retry_f):
                return retry_f
        return backoff_delay(self.consecutive_failures, self.base_delay, self.max_delay)

    def record_success(self):
        """记录一次推送成功：连续失败计数清零、解除退避（恢复后重置）。

        下一次失败将重新从 base_delay（1s）起步，而不是延续旧退避。
        """
        self.consecutive_failures = 0
        self.next_retry_at = None

    def can_push(self):
        """当前是否允许推送。未在退避中（next_retry_at 为 None）或已到时间 → True。

        主循环每轮据此跳过退避中的轮次，实现"不阻塞下一轮"。
        """
        if self.next_retry_at is None:
            return True
        return self._now() >= self.next_retry_at

    def seconds_until_retry(self):
        """距离下次允许推送还有多少秒；未在退避或已到时间 → 0.0。"""
        if self.next_retry_at is None:
            return 0.0
        return max(0.0, self.next_retry_at - self._now())
