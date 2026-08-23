"""TASK-026 — kit/tools/agent/ 重试退避状态机单测。

覆盖：
- 指数退避序列：1→2→4→8→16→32→60→60（cap 后恒定，不再增长）
- 连续失败计数：每次 record_failure 计数 +1，next_retry_at = now + 本次退避
- 恢复后重置：record_success 计数清零、解除退避立即 can_push；下一次失败重新从 1s 起步
- 不阻塞下一轮：未到 next_retry_at 时 can_push=False、seconds_until_retry 报剩余秒；
  主循环 fail→skip→fail→…→success 全序列模拟（FakeClock 推进，无真实 sleep）
- Retry-After 优先级：正整数优先于指数退避；缺失/非法/0 回退指数退避
- 溢出回归（REVIEW FIND-001）：failures >= 1025 / 超大 Retry-After 不再 OverflowError，
  返回 cap / 回退指数退避；非有限 base/max 拒绝；base>max 钳到 cap
- 时间注入：FakeClock 确定性验证退避时序
- 入参校验：failures/base_delay/max_delay/time_fn 非法 → ValueError/TypeError
"""
import os
import sys
import time
import unittest

AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "agent"
)
sys.path.insert(0, AGENT_DIR)
import agent_retry  # noqa: E402
import agent_http  # noqa: E402


class FakeClock:
    """可手动推进的假时钟（time_fn 注入用），替代真实 time.sleep。"""

    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make_state(clock, **kwargs):
    return agent_retry.PushRetryState(time_fn=clock, **kwargs)


class BackoffDelayTests(unittest.TestCase):
    def test_default_sequence_exponential_then_cap(self):
        seq = [agent_retry.backoff_delay(i) for i in range(1, 10)]
        self.assertEqual(seq, [1, 2, 4, 8, 16, 32, 60, 60, 60])

    def test_cap_constant_after_reaching_max(self):
        for i in (7, 8, 20, 100):
            self.assertEqual(agent_retry.backoff_delay(i), 60)

    def test_custom_base_and_cap(self):
        self.assertEqual(agent_retry.backoff_delay(1, base_delay=2.0, max_delay=10.0), 2.0)
        self.assertEqual(agent_retry.backoff_delay(3, base_delay=2.0, max_delay=10.0), 8.0)
        self.assertEqual(agent_retry.backoff_delay(4, base_delay=2.0, max_delay=10.0), 10.0)
        self.assertEqual(agent_retry.backoff_delay(9, base_delay=2.0, max_delay=10.0), 10.0)

    def test_invalid_failures(self):
        for bad in (0, -1, 1.5, "2", True, None):
            with self.assertRaises(ValueError, msg=f"failures={bad!r}"):
                agent_retry.backoff_delay(bad)

    def test_invalid_base_and_cap(self):
        for name in ("base_delay", "max_delay"):
            for bad in (0, -1, "5", True, None):
                with self.assertRaises(ValueError, msg=f"{name}={bad!r}"):
                    agent_retry.backoff_delay(1, **{name: bad})


class RetryStateBasicTests(unittest.TestCase):
    def test_initial_state_allows_push(self):
        clock = FakeClock(1000.0)
        state = make_state(clock)
        self.assertTrue(state.can_push())
        self.assertEqual(state.consecutive_failures, 0)
        self.assertIsNone(state.next_retry_at)
        self.assertEqual(state.seconds_until_retry(), 0.0)

    def test_first_failure_schedules_base_delay(self):
        clock = FakeClock(1000.0)
        state = make_state(clock)
        delay = state.record_failure()
        self.assertEqual(delay, 1.0)
        self.assertEqual(state.consecutive_failures, 1)
        self.assertEqual(state.next_retry_at, 1001.0)
        self.assertFalse(state.can_push())
        self.assertEqual(state.seconds_until_retry(), 1.0)

    def test_can_push_boundary_after_delay(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        state.record_failure()  # 退避 1s，next_retry_at=1.0
        clock.advance(0.999)
        self.assertFalse(state.can_push())
        self.assertAlmostEqual(state.seconds_until_retry(), 0.001, places=3)
        clock.advance(0.001)  # 恰好到 next_retry_at
        self.assertTrue(state.can_push())
        self.assertEqual(state.seconds_until_retry(), 0.0)

    def test_seconds_until_retry_reports_remaining(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        state.record_failure()  # 第 1 次失败：delay 1 → next_retry_at = 1.0
        state.record_failure()  # 第 2 次失败：delay 2 → next_retry_at = 当前(0) + 2 = 2.0
        self.assertEqual(state.seconds_until_retry(), 2.0)
        clock.advance(1.0)
        self.assertEqual(state.seconds_until_retry(), 1.0)
        clock.advance(1.0)
        self.assertEqual(state.seconds_until_retry(), 0.0)
        self.assertTrue(state.can_push())

    def test_record_failure_returns_used_delay_matches_backoff(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        for i in range(1, 5):
            delay = state.record_failure()
            self.assertEqual(delay, agent_retry.backoff_delay(i))
            self.assertEqual(state.next_retry_at, clock.t + delay)


class ExponentialGrowthTests(unittest.TestCase):
    def test_consecutive_failures_grow_exponentially(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        expected = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]
        for i, exp in enumerate(expected, start=1):
            delay = state.record_failure()
            self.assertEqual(delay, exp, msg=f"第 {i} 次连续失败")
            self.assertEqual(state.consecutive_failures, i)
            # 未到退避时间必须跳过
            self.assertFalse(state.can_push())
            # 推到恰好下一次允许时间
            clock.advance(delay)
            self.assertTrue(state.can_push())

    def test_cap_hit_after_repeated_failures(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        delays = [state.record_failure() for _ in range(10)]
        self.assertEqual(delays, [1, 2, 4, 8, 16, 32, 60, 60, 60, 60])
        self.assertEqual(state.consecutive_failures, 10)
        clock.advance(60.0)
        self.assertTrue(state.can_push())


class RecoveryResetTests(unittest.TestCase):
    def test_success_resets_counter_and_backoff(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        state.record_failure()
        state.record_failure()
        state.record_failure()  # 连续 3 次，退避已到 4s
        self.assertEqual(state.consecutive_failures, 3)
        state.record_success()
        self.assertEqual(state.consecutive_failures, 0)
        self.assertIsNone(state.next_retry_at)
        self.assertTrue(state.can_push())
        # 下一次失败重新从 1s 起步（而不是延续 4s/8s）
        self.assertEqual(state.record_failure(), 1.0)
        self.assertEqual(state.consecutive_failures, 1)

    def test_success_after_long_backoff_immediately_unblocks(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        for _ in range(8):
            state.record_failure()  # 退避已到 60s cap
        self.assertFalse(state.can_push())
        state.record_success()
        self.assertTrue(state.can_push())  # 恢复后立即可以推送，无需等满 60s


class RetryAfterTests(unittest.TestCase):
    def test_retry_after_positive_int_priority(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        delay = state.record_failure(retry_after=42)
        self.assertEqual(delay, 42.0)
        self.assertEqual(state.next_retry_at, 42.0)
        self.assertEqual(state.consecutive_failures, 1)

    def test_retry_after_missing_falls_back_to_exponential(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        state.record_failure()          # delay 1
        self.assertEqual(state.record_failure(retry_after=None), 2.0)  # 指数退避
        self.assertEqual(state.record_failure(retry_after=0), 4.0)     # 0 视为无效 → 指数
        self.assertEqual(state.record_failure(retry_after=-5), 8.0)    # 负数视为无效 → 指数
        self.assertEqual(state.record_failure(retry_after="42"), 16.0)  # 非数字 → 指数
        self.assertEqual(state.record_failure(retry_after=True), 32.0)  # bool 不是有效秒数

    def test_retry_after_used_on_rate_limited_error(self):
        """与 TASK-025 契约集成：PushRateLimitedError.retry_after 直接作为退避时长。"""
        clock = FakeClock(0.0)
        state = make_state(clock)
        try:
            raise agent_http.PushRateLimitedError(429, body="slow",
                                                  retry_after=42)
        except agent_http.PushError as e:
            delay = state.record_failure(retry_after=getattr(e, "retry_after", None))
        self.assertEqual(delay, 42.0)
        self.assertEqual(state.next_retry_at, 42.0)


class LoopPatternTests(unittest.TestCase):
    def test_main_loop_skip_until_allowed_then_recover(self):
        """主循环模式全序列：fail→skip→fail→…→success→reset（不阻塞、无 sleep）。

        outcomes 只预置实际推送轮次的结果（跳过轮不消费）；用 FakeClock 按轮询
        节奏推进，退避中（can_push=False）的轮次直接跳过。
        """
        clock = FakeClock(start=0.0)
        state = make_state(clock)
        # 预置每轮推送结果：None=成功，否则抛该异常
        outcomes = [agent_http.PushServerError(500),  # t=0  推送 → 失败 → 退避 1s
                    agent_http.PushServerError(503),  # t=1  推送 → 失败 → 退避 2s
                    agent_http.PushServerError(500),  # t=3  推送 → 失败 → 退避 4s
                    None]                             # t=7  推送 → 成功 → 恢复重置
        pushed_at = []

        def do_round():
            if not state.can_push():
                return  # 不阻塞：跳过本轮
            pushed_at.append(clock.t)
            outcome = outcomes.pop(0)
            if outcome is None:
                state.record_success()
            else:
                state.record_failure(retry_after=getattr(outcome, "retry_after", None))

        do_round()                       # t=0  失败 → next_retry_at=1
        clock.advance(0.5); do_round()   # t=0.5 未到 1s → 跳过
        clock.advance(0.5); do_round()   # t=1  失败 → next_retry_at=3
        clock.advance(1.0); do_round()   # t=2  未到 3s → 跳过
        clock.advance(1.0); do_round()   # t=3  失败 → next_retry_at=7
        clock.advance(1.0); do_round()   # t=4  跳过
        clock.advance(1.0); do_round()   # t=5  跳过
        clock.advance(1.0); do_round()   # t=6  跳过
        clock.advance(1.0); do_round()   # t=7  成功 → 恢复重置
        self.assertEqual(pushed_at, [0.0, 1.0, 3.0, 7.0])
        self.assertEqual(state.consecutive_failures, 0)
        self.assertIsNone(state.next_retry_at)
        self.assertTrue(state.can_push())

    def test_backoff_not_affected_by_skipped_rounds(self):
        """跳过的轮次不参与退避计时状态（计数只在真实失败时 +1）。"""
        clock = FakeClock(0.0)
        state = make_state(clock)
        state.record_failure()  # 退避 1s → next_retry_at=1.0
        clock.advance(0.2)      # t=0.2
        skipped = 0
        while clock.t < state.next_retry_at - 1e-9:
            self.assertFalse(state.can_push())
            self.assertEqual(state.consecutive_failures, 1)  # 跳过不计数
            skipped += 1
            clock.advance(0.1)
        self.assertGreaterEqual(skipped, 7)
        self.assertEqual(state.consecutive_failures, 1)  # 跳过轮次不影响计数
        clock.advance(0.01)  # 浮点累加可能停在 next_retry_at 前一个 epsilon，再推进一步
        self.assertTrue(state.can_push())  # 时间推进超过退避 → 允许推送


class OverflowRegressionTests(unittest.TestCase):
    """REVIEW FIND-001 回归：长时间持续失败与超大 Retry-After 不再 OverflowError。

    旧实现 `base * 2**(failures-1)` 在连续失败 >= 1025 次（约 17h）时先把大整数转
    float 溢出崩溃；本类锁定修复后的行为（返回 cap / 回退指数退避）。
    """

    def test_huge_failure_count_returns_cap(self):
        for n in (1024, 1025, 1100, 10000, 2 ** 20):
            self.assertEqual(agent_retry.backoff_delay(n), 60.0, msg=f"failures={n}")

    def test_huge_failure_count_through_state_machine(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        for _ in range(1100):
            state.record_failure()
        self.assertEqual(state.consecutive_failures, 1100)
        self.assertEqual(state.next_retry_at, 60.0)  # 封顶后恒定 60s，不崩溃

    def test_huge_retry_after_falls_back_to_exponential(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        # 无法表示为 float 的 Retry-After → 视为无效，回退指数退避（首次 1s）
        delay = state.record_failure(retry_after=10 ** 400)
        self.assertEqual(delay, 1.0)
        self.assertEqual(state.consecutive_failures, 1)
        self.assertEqual(state.next_retry_at, 1.0)

    def test_infinite_retry_after_falls_back_to_exponential(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        state.record_failure()
        self.assertEqual(state.record_failure(retry_after=float("inf")), 2.0)

    def test_large_finite_retry_after_still_honored(self):
        clock = FakeClock(0.0)
        state = make_state(clock)
        delay = state.record_failure(retry_after=10 ** 300)  # 可表示有限大 → 服务器要求优先
        self.assertEqual(delay, float(10 ** 300))

    def test_non_finite_base_or_cap_rejected(self):
        for name in ("base_delay", "max_delay"):
            for bad in (float("inf"), float("nan"), 10 ** 400):
                with self.assertRaises(ValueError, msg=f"backoff_delay {name}={bad!r}"):
                    agent_retry.backoff_delay(1, **{name: bad})
                with self.assertRaises(ValueError, msg=f"PushRetryState {name}={bad!r}"):
                    agent_retry.PushRetryState(**{name: bad})

    def test_base_above_cap_clamps_to_cap(self):
        self.assertEqual(agent_retry.backoff_delay(1, base_delay=10.0, max_delay=5.0), 5.0)
        self.assertEqual(agent_retry.backoff_delay(3, base_delay=10.0, max_delay=5.0), 5.0)
        clock = FakeClock(0.0)
        state = make_state(clock, base_delay=10.0, max_delay=5.0)
        self.assertEqual(state.record_failure(), 5.0)  # 首项即钳到 cap


class ConstructorValidationTests(unittest.TestCase):
    def test_invalid_base_or_cap(self):
        for name in ("base_delay", "max_delay"):
            for bad in (0, -1, "5", True, None):
                with self.assertRaises(ValueError, msg=f"{name}={bad!r}"):
                    agent_retry.PushRetryState(**{name: bad})

    def test_invalid_time_fn(self):
        for bad in (None, "time.time", 42):
            with self.assertRaises(TypeError, msg=f"time_fn={bad!r}"):
                agent_retry.PushRetryState(time_fn=bad)

    def test_default_time_fn_is_time_dot_time(self):
        state = agent_retry.PushRetryState()
        self.assertIs(state.time_fn, time.time)


if __name__ == "__main__":
    unittest.main()
