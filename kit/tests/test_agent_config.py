"""TASK-022 — kit/tools/agent/ 配置加载单测。

覆盖：
- 合法配置 → 返回规范化配置（poll_interval_seconds 缺省默认 30）
- 缺失 server_url / token / projects → AgentConfigError（消息含字段名）
- projects 缺 id / path、空数组、非数组 → AgentConfigError
- 类型错误（server_url 数字、poll 为布尔/字符串/负数）→ AgentConfigError
- 空串 / <...> 占位符视为未填 → AgentConfigError
- load_config：文件缺失 / JSON 非法 → AgentConfigError
- CLI 入口：缺失字段 → stderr 报错并 exit 1（子进程验证）
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "agent"
)
sys.path.insert(0, AGENT_DIR)
import agent_config  # noqa: E402


def valid_cfg(**overrides):
    cfg = {
        "server_url": "https://aimonitor.example.com/api/ingest",
        "token": "secret-token",
        "projects": [{"id": "proj-1", "path": "/srv/proj-1"}],
        "poll_interval_seconds": 30,
    }
    cfg.update(overrides)
    return cfg


class AgentConfigTests(unittest.TestCase):
    def test_valid_full(self):
        out = agent_config.validate(valid_cfg())
        self.assertEqual(out["server_url"], "https://aimonitor.example.com/api/ingest")
        self.assertEqual(out["token"], "secret-token")
        self.assertEqual(out["projects"], [{"id": "proj-1", "path": "/srv/proj-1"}])
        self.assertEqual(out["poll_interval_seconds"], 30)

    def test_default_poll_interval(self):
        cfg = valid_cfg()
        del cfg["poll_interval_seconds"]
        self.assertEqual(agent_config.validate(cfg)["poll_interval_seconds"],
                         agent_config.DEFAULT_POLL_INTERVAL_SECONDS)
        self.assertEqual(agent_config.DEFAULT_POLL_INTERVAL_SECONDS, 30)

    def test_default_poll_interval_when_null(self):
        out = agent_config.validate(valid_cfg(poll_interval_seconds=None))
        self.assertEqual(out["poll_interval_seconds"], 30)

    def test_missing_server_url(self):
        cfg = valid_cfg()
        del cfg["server_url"]
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(cfg)
        self.assertIn("server_url", str(cm.exception))

    def test_missing_token(self):
        cfg = valid_cfg()
        del cfg["token"]
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(cfg)
        self.assertIn("token", str(cm.exception))

    def test_missing_projects(self):
        cfg = valid_cfg()
        del cfg["projects"]
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(cfg)
        self.assertIn("projects", str(cm.exception))

    def test_projects_empty_array(self):
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(valid_cfg(projects=[]))
        self.assertIn("空数组", str(cm.exception))

    def test_projects_not_array(self):
        with self.assertRaises(agent_config.AgentConfigError):
            agent_config.validate(valid_cfg(projects={"id": "x", "path": "/x"}))

    def test_project_missing_id(self):
        cfg = valid_cfg(projects=[{"path": "/srv/proj-1"}])
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(cfg)
        self.assertIn("projects[0]", str(cm.exception))
        self.assertIn("id", str(cm.exception))

    def test_project_missing_path(self):
        cfg = valid_cfg(projects=[{"id": "proj-1"}])
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(cfg)
        self.assertIn("projects[0]", str(cm.exception))
        self.assertIn("path", str(cm.exception))

    def test_project_entry_not_object(self):
        cfg = valid_cfg(projects=["proj-1"])
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(cfg)
        self.assertIn("projects[0]", str(cm.exception))

    def test_type_errors_aggregated(self):
        cfg = valid_cfg(server_url=123, token=["t"], projects=[{"id": 1, "path": None}],
                        poll_interval_seconds="30")
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(cfg)
        msg = str(cm.exception)
        for field in ("server_url", "token", "projects[0].id", "projects[0].path",
                      "poll_interval_seconds"):
            self.assertIn(field, msg)

    def test_poll_interval_boolean_rejected(self):
        with self.assertRaises(agent_config.AgentConfigError):
            agent_config.validate(valid_cfg(poll_interval_seconds=True))

    def test_poll_interval_non_positive_rejected(self):
        for bad in (0, -5):
            with self.assertRaises(agent_config.AgentConfigError):
                agent_config.validate(valid_cfg(poll_interval_seconds=bad))

    def test_empty_string_considered_unset(self):
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(valid_cfg(token="", projects=[{"id": "  ", "path": "/x"}]))
        self.assertIn("token", str(cm.exception))
        self.assertIn("projects[0]", str(cm.exception))

    def test_placeholder_considered_unset(self):
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(valid_cfg(token="<your-ingest-token>"))
        self.assertIn("token", str(cm.exception))

    def test_unknown_fields_ignored(self):
        out = agent_config.validate(valid_cfg(log_level="debug", max_retries=5))
        self.assertEqual(out["poll_interval_seconds"], 30)
        self.assertNotIn("log_level", out)

    def test_strips_whitespace(self):
        out = agent_config.validate(valid_cfg(server_url="  https://x.example.com  ",
                                              projects=[{"id": " a ", "path": " /p "}]))
        self.assertEqual(out["server_url"], "https://x.example.com")
        self.assertEqual(out["projects"], [{"id": "a", "path": "/p"}])

    def test_load_config_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "agent.json")
            with self.assertRaises(agent_config.AgentConfigError) as cm:
                agent_config.load_config(missing)
            self.assertIn("未找到", str(cm.exception))

    def test_load_config_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            with self.assertRaises(agent_config.AgentConfigError) as cm:
                agent_config.load_config(path)
            self.assertIn("不是合法 JSON", str(cm.exception))

    def test_load_config_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "agent.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(valid_cfg(), f)
            out = agent_config.load_config(path)
            self.assertEqual(out["server_url"], "https://aimonitor.example.com/api/ingest")

    # ---- TASK-042: state 字段校验 ----

    def test_state_defaults_to_active(self):
        """缺省 state 字段 → 默认 active。"""
        out = agent_config.validate(valid_cfg())
        self.assertEqual(out["state"], "active")

    def test_state_explicit_active(self):
        """state=active 显式设置。"""
        out = agent_config.validate(valid_cfg(state="active"))
        self.assertEqual(out["state"], "active")

    def test_state_unregistered_allows_empty_token(self):
        """state=unregistered 时 token 可为空。"""
        out = agent_config.validate(valid_cfg(state="unregistered", token=None))
        self.assertEqual(out["state"], "unregistered")
        self.assertIsNone(out["token"])

    def test_state_unregistered_with_token(self):
        """state=unregistered 时允许预填 token。"""
        out = agent_config.validate(valid_cfg(state="unregistered", token="pre-filled"))
        self.assertEqual(out["state"], "unregistered")
        self.assertEqual(out["token"], "pre-filled")

    def test_state_pending_allows_empty_token(self):
        """state=pending 时 token 可为空。"""
        out = agent_config.validate(valid_cfg(state="pending", token=None))
        self.assertEqual(out["state"], "pending")
        self.assertIsNone(out["token"])

    def test_state_pending_with_req_id(self):
        """state=pending 时 req_id/request_key 被保留。"""
        out = agent_config.validate(valid_cfg(
            state="pending", token=None, req_id="req-abc", request_key="key-xyz"
        ))
        self.assertEqual(out["state"], "pending")
        self.assertEqual(out["req_id"], "req-abc")
        self.assertEqual(out["request_key"], "key-xyz")

    def test_state_active_requires_token(self):
        """state=active 且 token 为空 → 校验报错。"""
        cfg = valid_cfg(state="active")
        del cfg["token"]
        with self.assertRaises(agent_config.AgentConfigError) as cm:
            agent_config.validate(cfg)
        self.assertIn("token", str(cm.exception))
        self.assertIn("active", str(cm.exception))

    def test_state_invalid_rejected(self):
        """state 非法值 → 校验报错。"""
        for bad in ("invalid", "registered", "", 123):
            with self.assertRaises(agent_config.AgentConfigError) as cm:
                agent_config.validate(valid_cfg(state=bad))
            self.assertIn("state", str(cm.exception).lower())

    def test_req_id_ignored_when_not_pending(self):
        """state=active/unregistered 时 req_id/request_key 被忽略。"""
        for s in ("active", "unregistered"):
            out = agent_config.validate(valid_cfg(state=s, req_id="keep", request_key="keep"))
            self.assertIsNone(out["req_id"], f"state={s}: req_id 应被忽略")
            self.assertIsNone(out["request_key"], f"state={s}: request_key 应被忽略")

    def test_state_in_output(self):
        """返回的规范化配置含 state/req_id/request_key 字段。"""
        out = agent_config.validate(valid_cfg())
        self.assertIn("state", out)
        self.assertIn("req_id", out)
        self.assertIn("request_key", out)


class AgentCliTests(unittest.TestCase):
    """子进程验证 CLI：缺失字段 → stderr 报错 + exit 1。"""

    AGENT = os.path.join(AGENT_DIR, "agent.py")

    def write(self, tmp, name, cfg):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return path

    def run_cli(self, path):
        return subprocess.run(
            [sys.executable, self.AGENT, "--check-config", "--config", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def test_missing_fields_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "bad.json", valid_cfg(token="<your-ingest-token>"))
            proc = self.run_cli(path)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("配置错误", proc.stderr)
            self.assertIn("token", proc.stderr)

    def test_missing_file_exit_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli(os.path.join(tmp, "nope.json"))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("配置错误", proc.stderr)

    def test_valid_config_exit_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "ok.json", valid_cfg())
            proc = self.run_cli(path)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("配置 OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
