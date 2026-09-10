"""TASK-069 — kit/tools/dispatcher/registry.py 单测。

覆盖：
- 10 条样例注册（6 本地 + 4 agent 传输）→ 条目模型 id/name/path/transport
- transport 缺失 → 默认 local（本地）
- is_local/is_agent 判定正确
- 注册表缺失 / JSON 格式错误 / 缺 projects 数组 / 条目缺 id/path → RegistryError
- CLI list 标注（local / agent）与退出码
- 注册表路径不存在 → list 退出非 0 + stderr 报错
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

DISPATCHER_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "dispatcher"
)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISPATCHER_PY = os.path.join(DISPATCHER_DIR, "dispatcher.py")

sys.path.insert(0, DISPATCHER_DIR)
import registry  # noqa: E402


def sample_registry(root_dir, agent_path_prefix="D:/share/the5"):
    """10 条样例：6 本地（无 transport）+ 4 agent 传输（D:/share/*）。"""
    projects = [
        {"id": "aimonitor", "name": "aimonitor", "path": os.path.join(root_dir, "aimonitor")},
        {"id": "aibase", "name": "aibase", "path": os.path.join(root_dir, "aibase")},
        {"id": "westhill", "name": "westhill", "path": os.path.join(root_dir, "story", "westhill")},
        {"id": "x1design", "name": "x1design", "path": os.path.join(root_dir, "x1design")},
        {"id": "account-1", "name": "account-1", "path": os.path.join(root_dir, "account")},
        {"id": "baseline", "name": "baseline", "path": os.path.join(root_dir, "baseline")},
        {"id": "hb-share-aibase", "name": "hb-share-aibase",
         "path": f"{agent_path_prefix}/aibase", "transport": "agent"},
        {"id": "hb-share-ue-learning", "name": "hb-share-ue-learning",
         "path": f"{agent_path_prefix}/ue-learning", "transport": "agent"},
        {"id": "hb-share-baseline", "name": "hb-share-baseline",
         "path": f"{agent_path_prefix}/baseline", "transport": "agent"},
        {"id": "x1prototype", "name": "x1prototype",
         "path": f"{agent_path_prefix}/x1prototype", "transport": "agent"},
    ]
    return {"poll_interval_seconds": 30, "projects": projects}


def write_registry(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class RegistryLoadTests(unittest.TestCase):
    def test_load_ten_entries_transport_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "projects.json")
            write_registry(cfg, sample_registry(tmp))
            entries = registry.load_registry(cfg)

            self.assertEqual(len(entries), 10)
            local_ids = [e.id for e in entries[:6]]
            agent_ids = [e.id for e in entries[6:]]

            self.assertEqual(local_ids, ["aimonitor", "aibase", "westhill",
                                         "x1design", "account-1", "baseline"])
            self.assertEqual(agent_ids, ["hb-share-aibase", "hb-share-ue-learning",
                                         "hb-share-baseline", "x1prototype"])
            # transport：本地默认 local；agent 条目显式 agent
            self.assertTrue(all(e.transport == "local" for e in entries[:6]))
            self.assertTrue(all(e.transport == "agent" for e in entries[6:]))
            # 条目模型字段齐全
            for e in entries:
                self.assertTrue(e.id and e.name and e.path and e.transport)

    def test_is_local_and_is_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "projects.json")
            write_registry(cfg, sample_registry(tmp))
            entries = registry.load_registry(cfg)
            self.assertTrue(all(registry.is_local(e) for e in entries[:6]))
            self.assertTrue(all(registry.is_agent(e) for e in entries[6:]))
            self.assertFalse(any(registry.is_agent(e) for e in entries[:6]))
            self.assertFalse(any(registry.is_local(e) for e in entries[6:]))

    def test_transport_missing_defaults_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "projects.json")
            write_registry(cfg, {"projects": [
                {"id": "p1", "path": "/x/p1"},
                {"id": "p2", "path": "/x/p2", "transport": "agent"},
            ]})
            entries = registry.load_registry(cfg)
            self.assertEqual(entries[0].transport, "local")
            self.assertEqual(entries[1].transport, "agent")
            self.assertTrue(registry.is_local(entries[0]))
            self.assertTrue(registry.is_agent(entries[1]))

    def test_missing_registry_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(registry.RegistryError) as ctx:
                registry.load_registry(os.path.join(tmp, "no-such.json"))
            self.assertIn("不存在", str(ctx.exception))

    def test_invalid_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "bad.json")
            with open(cfg, "w", encoding="utf-8") as f:
                f.write("{ not json")
            with self.assertRaises(registry.RegistryError) as ctx:
                registry.load_registry(cfg)
            self.assertIn("读取失败", str(ctx.exception))

    def test_missing_projects_key_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "no-projects.json")
            write_registry(cfg, {"poll_interval_seconds": 30})
            with self.assertRaises(registry.RegistryError) as ctx:
                registry.load_registry(cfg)
            self.assertIn("projects", str(ctx.exception))

    def test_entry_missing_id_or_path_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "bad-entry.json")
            write_registry(cfg, {"projects": [
                {"id": "", "path": "/x"},
                {"id": "ok", "path": ""},
            ]})
            with self.assertRaises(registry.RegistryError):
                registry.load_registry(cfg)


class RegistryCliTests(unittest.TestCase):
    def _run(self, cfg, *extra):
        return subprocess.run(
            [sys.executable, DISPATCHER_PY, *extra, "--config", cfg],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
        )

    def test_list_labels_local_and_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "projects.json")
            write_registry(cfg, sample_registry(tmp))
            proc = self._run(cfg, "list")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = proc.stdout
            # 10 条注册都在输出中
            for pid in ("aimonitor", "aibase", "westhill", "x1design",
                        "account-1", "baseline",
                        "hb-share-aibase", "hb-share-ue-learning",
                        "hb-share-baseline", "x1prototype"):
                self.assertIn(pid, out)
            # 本地条目标 local；D:/share/* 标 agent
            self.assertIn("local", out)
            self.assertIn("agent", out)
            self.assertIn("D:/share/the5/aibase", out)

    def test_list_missing_config_exit_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(os.path.join(tmp, "no.json"), "list")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("注册表错误", proc.stderr)
            self.assertIn("不存在", proc.stderr)


if __name__ == "__main__":
    unittest.main()
