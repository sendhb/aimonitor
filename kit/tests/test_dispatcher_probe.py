"""TASK-069 — kit/tools/dispatcher/probe.py 单测。

覆盖：
- scan_project：本地条目统计六种状态计数 + 最近事件；agent 条目标记 skipped
- 无 runtime/tasks/ 的本地项目 → 全 0（有数据但为空）
- CLI scan：只统计 6 个本地项目、4 个 agent 条目输出 skipped(agent-transport)
  + stderr 告警、整体 exit 0
- 注册表缺失 → scan exit 非 0 + stderr 报错
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
import probe  # noqa: E402
import registry  # noqa: E402


def sample_registry(root_dir, agent_path_prefix="D:/share/the5"):
    """10 条样例：6 本地（无 transport）+ 4 agent 传输（D:/share/*）。

    与 test_dispatcher_registry 保持同一形状（独立定义，避免跨测试模块导入
    在 package-style 运行方式下解析失败）。
    """
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

TASK_TPL = """---
name: {name}
description: probe fixture
metadata:
  type: task
  status: {status}
  created: 2026-08-28
  updated: 2026-08-28
  priority: P2
  risk: P2
  approval-ref: none
---
# {name}
"""


def make_task(project_root, name, status):
    os.makedirs(os.path.join(project_root, "runtime", "tasks"), exist_ok=True)
    path = os.path.join(project_root, "runtime", "tasks", f"{name}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(TASK_TPL.format(name=name, status=status))
    return path


def make_projects(root):
    """在 temp root 下建 6 个本地项目 runtime/，agent 4 条路径不存在。"""
    fixture = sample_registry(root, agent_path_prefix=os.path.join(root, "__nonexistent_agents__"))
    local_statuses = {
        "aimonitor": ["open", "open", "in-progress", "done"],          # open=2, in-progress=1, done=1
        "aibase": ["in-review", "blocked", "cancelled"],               # in-review=1, blocked=1, cancelled=1
        "westhill": ["open", "done", "done", "done"],                  # open=1, done=3
        "x1design": [],                                                 # 全 0
        "account-1": ["done", "done"],                                  # done=2
        "baseline": ["in-progress"],                                    # in-progress=1
    }
    for entry in fixture["projects"]:
        pid = entry["id"]
        if pid in local_statuses:
            root_path = os.path.join(root, pid) if pid != "westhill" else os.path.join(root, "story", "westhill")
            # sample_registry 的 westhill path 是 root/story/westhill
            for i, status in enumerate(local_statuses[pid]):
                make_task(root_path, f"TASK-{i + 1:03d}-{pid}", status)
            if pid == "aimonitor":
                os.makedirs(os.path.join(root_path, "runtime", "logs"), exist_ok=True)
                with open(os.path.join(root_path, "runtime", "logs", "task-events.jsonl"),
                          "w", encoding="utf-8") as f:
                    f.write('{"seq": 1, "ev": "task.created", "task": "TASK-001-aimonitor"}\n')
                    f.write('{"seq": 2, "ev": "task.verified", "task": "TASK-001-aimonitor"}\n')
    return fixture


class ProbeUnitTests(unittest.TestCase):
    def _loaded_entries(self, tmp):
        cfg = os.path.join(tmp, "projects.json")
        write_registry(cfg, make_projects(tmp))
        return registry.load_registry(cfg)

    def test_scan_project_counts_statuses_and_latest_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = probe.scan_projects(self._loaded_entries(tmp))

            self.assertEqual(len(entries), 10)
            # 前 6 本地：全部未跳过
            self.assertTrue(all(not e["skipped"] for e in entries[:6]))
            # 后 4 agent：全部跳过 + 原因
            self.assertTrue(all(e["skipped"] for e in entries[6:]))
            self.assertTrue(all(e["reason"] == "agent-transport" for e in entries[6:]))
            self.assertTrue(all(e["counts"] is None for e in entries[6:]))

            by_id = {e["entry"].id: e for e in entries[:6]}
            self.assertEqual(by_id["aimonitor"]["counts"],
                             {"open": 2, "in-progress": 1, "in-review": 0,
                              "blocked": 0, "done": 1, "cancelled": 0})
            self.assertEqual(by_id["aibase"]["counts"],
                             {"open": 0, "in-progress": 0, "in-review": 1,
                              "blocked": 1, "done": 0, "cancelled": 1})
            self.assertEqual(by_id["westhill"]["counts"],
                             {"open": 1, "in-progress": 0, "in-review": 0,
                              "blocked": 0, "done": 3, "cancelled": 0})
            self.assertEqual(by_id["x1design"]["counts"],
                             {"open": 0, "in-progress": 0, "in-review": 0,
                              "blocked": 0, "done": 0, "cancelled": 0})
            self.assertEqual(by_id["baseline"]["counts"],
                             {"open": 0, "in-progress": 1, "in-review": 0,
                              "blocked": 0, "done": 0, "cancelled": 0})

            # 最近事件取 task-events.jsonl 最后一条
            self.assertEqual(by_id["aimonitor"]["latest_event"]["seq"], 2)
            self.assertIsNone(by_id["aibase"]["latest_event"])

    def test_scan_agent_entries_do_not_touch_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = probe.scan_projects(self._loaded_entries(tmp))
            for res in results[6:]:
                self.assertTrue(res["skipped"])
                self.assertFalse(os.path.exists(res["entry"].path))


class ProbeCliTests(unittest.TestCase):
    def _run(self, cfg, *extra):
        return subprocess.run(
            [sys.executable, DISPATCHER_PY, *extra, "--config", cfg],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
        )

    def test_scan_counts_only_local_and_skips_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "projects.json")
            write_registry(cfg, make_projects(tmp))
            proc = self._run(cfg, "scan")
            self.assertEqual(proc.returncode, 0, proc.stderr)

            # 6 个本地项目都在输出中（含计数）
            for pid in ("aimonitor", "aibase", "westhill", "x1design",
                        "account-1", "baseline"):
                self.assertIn(pid, proc.stdout)
            self.assertIn("open=2", proc.stdout)          # aimonitor
            self.assertIn("in-review=1", proc.stdout)     # aibase
            self.assertIn("blocked=1", proc.stdout)       # aibase
            self.assertIn("done=3", proc.stdout)          # westhill
            self.assertIn("cancelled=1", proc.stdout)     # aibase

            # 4 个 agent 条目：skipped(agent-transport) + stderr 告警
            for pid in ("hb-share-aibase", "hb-share-ue-learning",
                        "hb-share-baseline", "x1prototype"):
                self.assertIn(pid, proc.stdout)
            self.assertEqual(proc.stdout.count("skipped(agent-transport)"), 4)
            self.assertIn("WARN", proc.stderr)
            self.assertIn("agent-transport", proc.stderr)
            self.assertIn("WARN", proc.stderr)
            self.assertIn("hb-share-baseline", proc.stderr)

    def test_scan_missing_config_exit_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run(os.path.join(tmp, "no.json"), "scan")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("注册表错误", proc.stderr)


if __name__ == "__main__":
    unittest.main()
