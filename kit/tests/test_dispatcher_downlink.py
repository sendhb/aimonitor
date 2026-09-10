"""TASK-073 — kit/tools/dispatcher/downlink.py 单测 + CLI 接线测试。

覆盖：
- find_entry：注册表精确匹配 / 非注册表路径拒绝 / agent 条目拒绝
- run：在项目目录执行并收集 exit code + stdout/stderr；非 0 退出回报；
  超时回报 timed_out；目录缺失拒绝
- CLI：allocate 打印候选；run 用 fake 工具链端到端跑通；
  downlink --path 伪造路径 → 报错退出不执行
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
import downlink  # noqa: E402
import policy  # noqa: E402
import registry  # noqa: E402

TASK_TPL = """---
name: {name}
description: downlink fixture
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

FAKE_TASK_PY = """#!/usr/bin/env python3
import sys
print("fake-task", *sys.argv[1:])
sys.exit(0)
"""
# autoloop-coder 入口自 TASK-026 起是 Python 薄 shim，dispatcher 以
# sys.executable 调用（不再经 bash），假脚本同步为 Python。
FAKE_CODER_PY = """#!/usr/bin/env python3
import sys
print("fake-autoloop-coder", *sys.argv[1:])
sys.exit(0)
"""


def write_file(path, content, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)


def make_local_project(root, project):
    """建一个带 open 任务 + fake 工具链的本地项目，返回路径。"""
    proj = os.path.join(root, project)
    name = f"TASK-001-{project}"
    write_file(os.path.join(proj, "runtime", "tasks", f"{name}.md"),
               TASK_TPL.format(name=name, status="open"))
    write_file(os.path.join(proj, "kit", "cli", "task"), FAKE_TASK_PY)
    write_file(os.path.join(proj, "kit", "cli", "autoloop-coder"), FAKE_CODER_PY,
               mode=0o755)
    return proj


def write_registry(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def local_registry(root, projects):
    return {
        "poll_interval_seconds": 30,
        "projects": [
            {"id": p, "name": p, "path": os.path.join(root, p)} for p in projects
        ],
    }


class DownlinkUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.p1 = make_local_project(self._tmp.name, "proj-a")
        self.entries = registry.load_registry(self._write_cfg(
            local_registry(self._tmp.name, ["proj-a"])
        ))
        self.entry = self.entries[0]

    def tearDown(self):
        self._tmp.cleanup()

    def _write_cfg(self, data):
        cfg = os.path.join(self._tmp.name, "projects.json")
        write_registry(cfg, data)
        return cfg

    def test_find_entry_matches_registry_path(self):
        e = downlink.find_entry(self.entries, self.p1)
        self.assertEqual(e.id, "proj-a")

    def test_find_entry_rejects_non_registry_path(self):
        fake = os.path.join(self._tmp.name, "not-in-registry")
        with self.assertRaises(downlink.DownlinkError) as ctx:
            downlink.find_entry(self.entries, fake)
        self.assertIn("不在注册表", str(ctx.exception))
        self.assertIn("拒绝", str(ctx.exception))

    def test_find_entry_rejects_agent_entry(self):
        cfg = os.path.join(self._tmp.name, "agent.json")
        write_registry(cfg, {"projects": [
            {"id": "far", "name": "far", "path": self.p1, "transport": "agent"},
        ]})
        entries = registry.load_registry(cfg)
        with self.assertRaises(downlink.DownlinkError) as ctx:
            downlink.find_entry(entries, self.p1)
        self.assertIn("agent 传输条目", str(ctx.exception))

    def test_run_executes_in_project_dir_captures_output(self):
        res = downlink.run(
            self.entry, sys.executable,
            ["-c", "import os; print(os.getcwd())"],
        )
        self.assertEqual(res.exit_code, 0)
        self.assertIn(self.p1, res.stdout)

    def test_run_reports_nonzero_exit(self):
        res = downlink.run(
            self.entry, sys.executable, ["-c", "import sys; sys.exit(3)"]
        )
        self.assertEqual(res.exit_code, 3)
        self.assertFalse(res.timed_out)

    def test_run_timeout_reports_timed_out(self):
        res = downlink.run(
            self.entry, sys.executable,
            ["-c", "import time; time.sleep(3)"], timeout=1,
        )
        self.assertTrue(res.timed_out)
        self.assertEqual(res.exit_code, -1)

    def test_run_rejects_missing_dir(self):
        bad = registry.RegistryEntry(
            id="ghost", name="ghost",
            path=os.path.join(self._tmp.name, "ghost"), transport="local",
        )
        with self.assertRaises(downlink.DownlinkError) as ctx:
            downlink.run(bad, sys.executable, ["-c", "pass"])
        self.assertIn("目录不存在", str(ctx.exception))

    def test_run_reports_stderr(self):
        res = downlink.run(
            self.entry, sys.executable,
            ["-c", "import sys; print('oops', file=sys.stderr); sys.exit(1)"],
        )
        self.assertEqual(res.exit_code, 1)
        self.assertIn("oops", res.stderr)


class DispatcherCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj_a = make_local_project(self._tmp.name, "proj-a")
        self.proj_b = make_local_project(self._tmp.name, "proj-b")
        # run/dispatch 默认 state-dir 是宿主机 runtime 全局状态；隔离到临时目录，
        # 避免宿主机活跃分配占用 max-workers 额度使本测试误报「无候选」
        self.state_dir = os.path.join(self._tmp.name, "state")
        self.cfg = os.path.join(self._tmp.name, "projects.json")
        write_registry(self.cfg, local_registry(self._tmp.name, ["proj-a", "proj-b"]))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, DISPATCHER_PY, *extra, "--config", self.cfg],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
        )

    def test_allocate_prints_candidates_only_one_with_max_workers_1(self):
        proc = self._run("allocate", "--max-workers", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 并发上限：只选 proj-a（注册表顺序第一个有候选的项目）
        self.assertIn("proj-a", proc.stdout)
        self.assertIn("TASK-001", proc.stdout)
        self.assertNotIn("proj-b", proc.stdout)

    def test_run_end_to_end_with_fake_toolchain(self):
        proc = self._run("run", "--max-workers", "1", "--state-dir", self.state_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fake-task", proc.stdout)
        self.assertIn("TASK-001", proc.stdout)
        self.assertIn("fake-autoloop-coder", proc.stdout)

    def test_dispatch_once_end_to_end_with_fake_toolchain(self):
        proc = self._run("dispatch", "--once", "--max-workers", "1",
                         "--state-dir", self.state_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fake-task", proc.stdout)
        self.assertIn("fake-autoloop-coder", proc.stdout)

    def test_downlink_rejects_forged_path(self):
        fake = os.path.join(self._tmp.name, "forged")
        proc = subprocess.run(
            [sys.executable, DISPATCHER_PY, "downlink",
             "--config", self.cfg, "--path", fake,
             "--command", "echo", "--arg", "forged"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("拒绝", proc.stderr)
        self.assertIn("不在注册表", proc.stderr)


class DispatcherDownlinkErrorPathTests(unittest.TestCase):
    """cmd_run 对 DownlinkError 的干净报错路径（SMELL-002 回归）。

    选中候选后目录被删/命令二进制缺失等场景下，`downlink.run` 抛
    DownlinkError；cmd_run 应记失败、继续、最终 exit 1，而不是抛 traceback。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = os.path.join(self._tmp.name, "proj-a")
        os.makedirs(self.proj, exist_ok=True)
        self.entry = registry.RegistryEntry(
            id="proj-a", name="proj-a", path=self.proj, transport="local",
        )
        self.cand = policy.Candidate(
            entry=self.entry, task_id="TASK-001",
            status="open", priority="P1", updated="2026-08-28",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_cmd_run_catches_downlink_error_returns_1_no_traceback(self):
        import io
        import types
        from unittest import mock

        import dispatcher as dispatcher_cli

        args = types.SimpleNamespace(max_workers=1)
        stderr = io.StringIO()
        with (
            mock.patch.object(
                dispatcher_cli.policy_lib, "evaluate_candidates", return_value=[self.cand]
            ),
            mock.patch.object(
                dispatcher_cli.downlink_lib, "run",
                side_effect=downlink.DownlinkError("目录不存在: /x"),
            ),
            mock.patch.object(sys, "stderr", stderr),
        ):
            rc = dispatcher_cli.cmd_run(args, [self.entry])

        self.assertEqual(rc, 1)
        self.assertIn("下行错误", stderr.getvalue())
        self.assertIn("目录不存在", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
