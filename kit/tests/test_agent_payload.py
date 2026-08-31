"""TASK-024 — kit/tools/agent/ payload 构造与序列化单测。

覆盖：
- 字段完整：完整快照 → project_id/ts/files 六字段齐全；全 None 快照 → 字段仍齐全；快照缺键 → 字段为 None
- 容量上限：content/focus 超 MAX_CONTENT_CHARS 截尾+标记、条目超上限截断、不修改入参、
  多字节 UTF-8 内容触发整体 PayloadTooLargeError
- 空项目：目录存在但为空（空列表/0）与目录缺失（None）语义原样保留
- 序列化：往返一致、UTF-8 不转义、sort_keys 确定性、validate_capacity 返回字节数/超限抛错
- 入参校验：project_id 非法、snapshot 非 dict、ts 非法 → PayloadError
"""
import json
import os
import sys
import time
import unittest

AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "agent"
)
sys.path.insert(0, AGENT_DIR)
import agent_payload  # noqa: E402


def full_snapshot():
    return {
        "tasks": [
            {"name": "TASK-001-a.md", "content": "# 任务一\n正文"},
            {"name": "TASK-002-b.md", "content": "# 任务二\n正文"},
        ],
        "focus": "# Current Focus\n\nTASK-024",
        "heartbeats": [
            {"file": "autoloop-coder.heartbeat", "mtime": 1786892280.0},
        ],
        "events": [
            {"name": "autoloop-coder-events.jsonl", "content": '{"ts":1,"outcome":"ok"}\n'},
        ],
        "verification_count": 2,
        "review_count": 1,
    }


def none_snapshot():
    return {
        "tasks": None,
        "focus": None,
        "heartbeats": None,
        "events": None,
        "verification_count": None,
        "review_count": None,
    }


class BuildPayloadTests(unittest.TestCase):
    FILES_KEYS = ("tasks", "focus", "heartbeats", "events",
                  "verification_count", "review_count")

    def test_full_fields(self):
        payload = agent_payload.build_payload("proj-1", full_snapshot(), ts=1786892400.0)
        self.assertEqual(payload["project_id"], "proj-1")
        self.assertEqual(payload["ts"], 1786892400.0)
        self.assertEqual(
            sorted(payload["files"].keys()), sorted(self.FILES_KEYS)
        )
        self.assertEqual(payload["files"]["tasks"],
                         [{"name": "TASK-001-a.md", "content": "# 任务一\n正文"},
                          {"name": "TASK-002-b.md", "content": "# 任务二\n正文"}])
        self.assertEqual(payload["files"]["focus"], "# Current Focus\n\nTASK-024")
        self.assertEqual(payload["files"]["heartbeats"],
                         [{"file": "autoloop-coder.heartbeat", "mtime": 1786892280.0}])
        self.assertEqual(payload["files"]["events"][0]["name"], "autoloop-coder-events.jsonl")
        self.assertEqual(payload["files"]["verification_count"], 2)
        self.assertEqual(payload["files"]["review_count"], 1)

    def test_all_none_snapshot_fields_still_complete(self):
        """空项目（快照全 None）→ 六字段仍齐全，值保持 None，不抛异常。"""
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0)
        self.assertEqual(sorted(payload["files"].keys()), sorted(self.FILES_KEYS))
        for key in self.FILES_KEYS:
            self.assertIsNone(payload["files"][key])

    def test_missing_snapshot_keys_default_to_none(self):
        """快照缺键 → 对应 files 字段为 None（字段完整性与读取层契约解耦）。"""
        payload = agent_payload.build_payload("proj-1", {}, ts=1.0)
        self.assertEqual(sorted(payload["files"].keys()), sorted(self.FILES_KEYS))
        for key in self.FILES_KEYS:
            self.assertIsNone(payload["files"][key])

    def test_empty_lists_and_zero_preserved(self):
        """目录存在但为空（空列表/0）与 None 区分：原样保留，不吞成 None。"""
        snap = {
            "tasks": [], "focus": None, "heartbeats": [],
            "events": [], "verification_count": 0, "review_count": 0,
        }
        payload = agent_payload.build_payload("proj-1", snap, ts=1.0)
        self.assertEqual(payload["files"]["tasks"], [])
        self.assertEqual(payload["files"]["heartbeats"], [])
        self.assertEqual(payload["files"]["events"], [])
        self.assertEqual(payload["files"]["verification_count"], 0)
        self.assertEqual(payload["files"]["review_count"], 0)
        self.assertIsNone(payload["files"]["focus"])

    def test_project_id_validation(self):
        for bad in (None, "", "   ", 123, ["proj"]):
            with self.assertRaises(agent_payload.PayloadError, msg=f"bad={bad!r}"):
                agent_payload.build_payload(bad, none_snapshot(), ts=1.0)

    def test_project_id_stripped(self):
        payload = agent_payload.build_payload("  proj-1  ", none_snapshot(), ts=1.0)
        self.assertEqual(payload["project_id"], "proj-1")

    def test_snapshot_must_be_dict(self):
        for bad in (None, [], "snapshot", 42):
            with self.assertRaises(agent_payload.PayloadError, msg=f"bad={bad!r}"):
                agent_payload.build_payload("proj-1", bad, ts=1.0)

    def test_ts_validation(self):
        with self.assertRaises(agent_payload.PayloadError):
            agent_payload.build_payload("proj-1", none_snapshot(), ts="now")
        with self.assertRaises(agent_payload.PayloadError):
            agent_payload.build_payload("proj-1", none_snapshot(), ts=True)

    def test_ts_default_to_now(self):
        before = time.time()
        payload = agent_payload.build_payload("proj-1", none_snapshot())
        after = time.time()
        self.assertGreaterEqual(payload["ts"], before)
        self.assertLessEqual(payload["ts"], after)
        self.assertIsInstance(payload["ts"], float)

    def test_ts_int_coerced_to_float(self):
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1786892400)
        self.assertEqual(payload["ts"], 1786892400.0)


class CapacityTests(unittest.TestCase):
    def test_content_truncated_with_suffix(self):
        snap = full_snapshot()
        snap["tasks"] = [{"name": "TASK-big.md",
                          "content": "x" * (agent_payload.MAX_CONTENT_CHARS + 100)}]
        payload = agent_payload.build_payload("proj-1", snap, ts=1.0)
        content = payload["files"]["tasks"][0]["content"]
        self.assertEqual(len(content), agent_payload.MAX_CONTENT_CHARS + len(agent_payload.TRUNCATION_SUFFIX))
        self.assertTrue(content.endswith(agent_payload.TRUNCATION_SUFFIX))
        self.assertFalse(content.startswith("x" * (agent_payload.MAX_CONTENT_CHARS + 1) + "…"))
        self.assertEqual(content[:10], "x" * 10)

    def test_content_at_limit_not_truncated(self):
        snap = full_snapshot()
        snap["tasks"] = [{"name": "T.md", "content": "y" * agent_payload.MAX_CONTENT_CHARS}]
        payload = agent_payload.build_payload("proj-1", snap, ts=1.0)
        self.assertEqual(payload["files"]["tasks"][0]["content"], "y" * agent_payload.MAX_CONTENT_CHARS)

    def test_focus_truncated(self):
        snap = full_snapshot()
        snap["focus"] = "z" * (agent_payload.MAX_CONTENT_CHARS + 50)
        payload = agent_payload.build_payload("proj-1", snap, ts=1.0)
        self.assertEqual(len(payload["files"]["focus"]),
                         agent_payload.MAX_CONTENT_CHARS + len(agent_payload.TRUNCATION_SUFFIX))
        self.assertTrue(payload["files"]["focus"].endswith(agent_payload.TRUNCATION_SUFFIX))

    def test_none_content_not_truncated(self):
        snap = full_snapshot()
        snap["tasks"] = [{"name": "T.md", "content": None}]
        payload = agent_payload.build_payload("proj-1", snap, ts=1.0)
        self.assertIsNone(payload["files"]["tasks"][0]["content"])

    def test_entry_count_capped(self):
        snap = full_snapshot()
        snap["tasks"] = [{"name": f"TASK-{i:03d}.md", "content": "c"} for i in range(agent_payload.MAX_TASKS + 20)]
        snap["events"] = [{"name": f"e-{i}.jsonl", "content": "x"} for i in range(agent_payload.MAX_EVENTS + 5)]
        snap["heartbeats"] = [{"file": f"h-{i}.heartbeat", "mtime": float(i)} for i in range(agent_payload.MAX_HEARTBEATS + 3)]
        payload = agent_payload.build_payload("proj-1", snap, ts=1.0)
        self.assertEqual(len(payload["files"]["tasks"]), agent_payload.MAX_TASKS)
        self.assertEqual(len(payload["files"]["events"]), agent_payload.MAX_EVENTS)
        self.assertEqual(len(payload["files"]["heartbeats"]), agent_payload.MAX_HEARTBEATS)
        # 保留的是前 N 条（有序丢弃尾部）
        self.assertEqual(payload["files"]["tasks"][0]["name"], "TASK-000.md")
        self.assertEqual(payload["files"]["tasks"][-1]["name"], f"TASK-{agent_payload.MAX_TASKS - 1:03d}.md")

    def test_input_snapshot_not_mutated(self):
        snap = full_snapshot()
        snap["tasks"] = [{"name": "T-big.md", "content": "x" * (agent_payload.MAX_CONTENT_CHARS + 10)}]
        original_content = snap["tasks"][0]["content"]
        agent_payload.build_payload("proj-1", snap, ts=1.0)
        # 入参快照未被截断/修改
        self.assertEqual(snap["tasks"][0]["content"], original_content)
        self.assertEqual(len(snap["tasks"][0]["content"]), agent_payload.MAX_CONTENT_CHARS + 10)
        self.assertNotIn(agent_payload.TRUNCATION_SUFFIX, snap["tasks"][0]["content"])

    def test_multibyte_content_triggers_payload_too_large(self):
        """逐字段截断后仍超整体上限（4 字节 UTF-8 内容）→ 序列化抛 PayloadTooLargeError。"""
        snap = full_snapshot()
        snap["tasks"] = [{"name": f"T-{i:03d}.md",
                          "content": "\U0001D538" * agent_payload.MAX_CONTENT_CHARS}
                         for i in range(agent_payload.MAX_TASKS)]
        payload = agent_payload.build_payload("proj-1", snap, ts=1.0)
        with self.assertRaises(agent_payload.PayloadTooLargeError):
            agent_payload.serialize_payload(payload)
        with self.assertRaises(agent_payload.PayloadTooLargeError):
            agent_payload.validate_capacity(payload)


class SerializationTests(unittest.TestCase):
    def test_roundtrip(self):
        payload = agent_payload.build_payload("proj-1", full_snapshot(), ts=1786892400.0)
        text = agent_payload.serialize_payload(payload)
        self.assertEqual(json.loads(text), payload)

    def test_utf8_not_escaped(self):
        payload = agent_payload.build_payload("proj-1", full_snapshot(), ts=1.0)
        text = agent_payload.serialize_payload(payload)
        self.assertIn("# 任务一", text)  # ensure_ascii=False
        self.assertNotIn("\\u", text)

    def test_deterministic_sorted_keys(self):
        p1 = agent_payload.build_payload("proj-1", full_snapshot(), ts=1.0)
        p2 = agent_payload.build_payload("proj-1", full_snapshot(), ts=1.0)
        self.assertEqual(agent_payload.serialize_payload(p1),
                         agent_payload.serialize_payload(p2))
        # 顶层键字典序：files, project_id, ts
        text = agent_payload.serialize_payload(p1)
        self.assertLess(text.index('"files"'), text.index('"project_id"'))
        self.assertLess(text.index('"project_id"'), text.index('"ts"'))

    def test_validate_capacity_returns_byte_size(self):
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0)
        size = agent_payload.validate_capacity(payload)
        self.assertIsInstance(size, int)
        self.assertGreater(size, 0)
        self.assertLessEqual(size, agent_payload.MAX_PAYLOAD_BYTES)

    def test_validate_capacity_raises_on_oversized(self):
        payload = {
            "project_id": "p",
            "ts": 1.0,
            "files": {
                "tasks": [{"name": "T.md", "content": "x" * (agent_payload.MAX_PAYLOAD_BYTES + 100)}],
                "focus": None, "heartbeats": None, "events": None,
                "verification_count": None, "review_count": None,
            },
        }
        with self.assertRaises(agent_payload.PayloadTooLargeError):
            agent_payload.validate_capacity(payload)
        with self.assertRaises(agent_payload.PayloadTooLargeError):
            agent_payload.serialize_payload(payload)

    def test_too_large_error_is_payload_error(self):
        payload = {
            "project_id": "p", "ts": 1.0,
            "files": {"tasks": [{"name": "T.md", "content": "x" * 10**6}],
                      "focus": None, "heartbeats": None, "events": None,
                      "verification_count": None, "review_count": None},
        }
        try:
            agent_payload.validate_capacity(payload)
            self.fail("应抛 PayloadTooLargeError")
        except agent_payload.PayloadError:
            pass


class TaskEventPayloadTests(unittest.TestCase):
    """TASK-066 — payload 事件流字段：向后兼容 + 事件/游标携带 + 校验。"""

    def test_without_task_events_no_new_keys(self):
        """未启用事件流 → payload 不含 events/cursor（向后兼容）。"""
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0)
        self.assertNotIn("events", payload)
        self.assertNotIn("cursor", payload)

    def test_with_task_events_and_cursor(self):
        events = [{"seq": 1, "ev": "task.created", "task": "TASK-001"},
                  {"seq": 2, "ev": "task.started", "task": "TASK-001"}]
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0,
                                              task_events=events, cursor=2)
        self.assertEqual(payload["events"], events)
        self.assertEqual(payload["cursor"], 2)
        self.assertEqual(payload["files"]["tasks"], None)  # 快照字段不受影响

    def test_empty_events_with_cursor(self):
        """已启用但本轮无新事件 → events=[]、cursor 保留（服务端可确认无缺口）。"""
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0,
                                              task_events=[], cursor=5)
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["cursor"], 5)

    def test_task_events_truncated_to_max(self):
        events = [{"seq": i} for i in range(1, agent_payload.MAX_TASK_EVENTS + 10)]
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0,
                                              task_events=events, cursor=events[-1]["seq"])
        self.assertEqual(len(payload["events"]), agent_payload.MAX_TASK_EVENTS)
        # SMELL-001（TASK-067）：截断后 cursor 钳制到实际放入的最大 seq
        self.assertEqual(payload["cursor"], agent_payload.MAX_TASK_EVENTS)

    def test_task_events_must_be_list(self):
        for bad in ("events", {"seq": 1}, 42):
            with self.assertRaises(agent_payload.PayloadError, msg=f"bad={bad!r}"):
                agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0, task_events=bad)

    def test_task_events_none_means_not_enabled(self):
        # None 是“未启用事件流”哨兵，payload 不含 events/cursor 键
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0, task_events=None)
        self.assertNotIn("events", payload)
        self.assertNotIn("cursor", payload)

    def test_cursor_must_be_int(self):
        for bad in ("3", True, 1.5, []):
            with self.assertRaises(agent_payload.PayloadError, msg=f"cursor={bad!r}"):
                agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0,
                                            task_events=[], cursor=bad)

    def test_serialize_includes_events_deterministically(self):
        events = [{"seq": 1, "ev": "task.created", "task": "TASK-001"}]
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0,
                                              task_events=events, cursor=1)
        body = agent_payload.serialize_payload(payload)
        parsed = json.loads(body)
        self.assertEqual(parsed["cursor"], 1)
        self.assertEqual(parsed["events"][0]["seq"], 1)


class TaskEventCursorClampTests(unittest.TestCase):
    """TASK-067 — SMELL-001：截断后 cursor 钳制到实际放入事件的最大 seq。

    生产路径 `_incremental_events` 已先截批，截断通常为 no-op；本组用例把
    「payload 自身 cursor 不变量」收敛到 build_payload 层，防未来调用方回归。
    """

    def test_truncated_cursor_clamped_to_max_seq(self):
        """直接传 250 条 + cursor=250 → payload 200 条且 cursor=200。"""
        events = [{"seq": i, "ev": "task.created", "task": f"TASK-{i:03d}"}
                  for i in range(1, agent_payload.MAX_TASK_EVENTS + 51)]
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0,
                                              task_events=events, cursor=250)
        self.assertEqual(len(payload["events"]), agent_payload.MAX_TASK_EVENTS)
        self.assertEqual(payload["cursor"], agent_payload.MAX_TASK_EVENTS)

    def test_truncated_cursor_kept_when_below_max_seq(self):
        """截断发生但 cursor 未越过实际放入的最大 seq → 原样保留（不回推）。"""
        events = [{"seq": i} for i in range(1, agent_payload.MAX_TASK_EVENTS + 10)]
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0,
                                              task_events=events, cursor=50)
        self.assertEqual(payload["cursor"], 50)

    def test_within_limit_cursor_unchanged(self):
        """events 条数 ≤ MAX → 即使 cursor 大于最大 seq 也原样保留（无截断即无钳制）。"""
        events = [{"seq": i} for i in range(1, 11)]
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0,
                                              task_events=events, cursor=99)
        self.assertEqual(len(payload["events"]), 10)
        self.assertEqual(payload["cursor"], 99)

    def test_empty_events_cursor_unchanged(self):
        """events=[] → cursor 原样保留（无事件可钳，服务端仅作心跳确认）。"""
        payload = agent_payload.build_payload("proj-1", none_snapshot(), ts=1.0,
                                              task_events=[], cursor=5)
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["cursor"], 5)


if __name__ == "__main__":
    unittest.main()
