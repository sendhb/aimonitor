# -*- coding: utf-8 -*-
"""TASK-030 单元测试：cli/init 跨平台化 + 依赖自举（--install-deps）。

覆盖：
- install_commands 平台分派（Linux/Darwin/Windows/未知平台）
- missing_tools 依赖检测（mock shutil.which）
- bootstrap_deps 全就绪直接通过；未知平台安全失败
- copy_kit 排除规则（__pycache__/install.sh/工具配置不进入目标 kit/）
- copy_runtime_templates 只复制模板，不复制任务实例数据
- guard_target 防呆（目标在源仓库内 → SystemExit）
- setup_config 非交互模式原样复制 profile 模板
"""
import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def load_script(name):
    path = f"kit/cli/{name}" if os.path.isfile(f"kit/cli/{name}") else f"cli/{name}"
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class InstallCommandsTest(unittest.TestCase):
    def test_linux_has_pkg_managers(self):
        init = load_script("init")
        cmds = init.install_commands("Linux")
        names = [n for n, _ in cmds]
        self.assertIn("apt-get", names)
        self.assertIn("pacman", names)
        for _, cmd in cmds:
            self.assertIn("git", cmd)

    def test_darwin_uses_brew(self):
        init = load_script("init")
        cmds = init.install_commands("Darwin")
        self.assertEqual(cmds[0][0], "brew")

    def test_windows_uses_winget_then_choco(self):
        init = load_script("init")
        cmds = init.install_commands("Windows")
        names = [n for n, _ in cmds]
        self.assertEqual(names[0], "winget")
        self.assertIn("choco", names)
        joined = " ".join(" ".join(cmd) for _, cmd in cmds)
        self.assertIn("Git.Git", joined)
        self.assertIn("Python.Python.3.12", joined)  # NOTE-001: Windows 也要自举 python
        self.assertIn("choco install python", joined)

    def test_unknown_platform_no_commands(self):
        init = load_script("init")
        self.assertEqual(init.install_commands("Haiku"), [])


class MissingToolsTest(unittest.TestCase):
    def test_git_only_missing(self):
        # NOTE-005：外层 which mock 与 missing_tools 包装行是 no-op，已删除
        init = load_script("init")
        with mock.patch("shutil.which", side_effect=lambda c: "/usr/bin/git" if c == "git" else None):
            self.assertEqual(init.missing_tools(), ["python"])

    @mock.patch("shutil.which", return_value=None)
    def test_python_alias_detected(self, _which):
        init = load_script("init")
        # python3 缺失但 python 存在 → 不算缺
        with mock.patch("shutil.which", side_effect=lambda c: "C:/Python/python.exe" if c == "python" else None):
            self.assertEqual(init.missing_tools(), ["git"])

    @mock.patch("shutil.which", return_value=None)
    def test_all_missing(self, _which):
        init = load_script("init")
        self.assertEqual(sorted(init.missing_tools()), ["git", "python"])


class BootstrapDepsTest(unittest.TestCase):
    def test_all_ready_returns_true(self):
        init = load_script("init")
        with mock.patch.object(init, "missing_tools", return_value=[]):
            self.assertTrue(init.bootstrap_deps())

    @mock.patch("platform.system", return_value="Haiku")
    def test_unknown_platform_returns_false(self, _sys):
        init = load_script("init")
        with mock.patch.object(init, "missing_tools", return_value=["git"]):
            self.assertFalse(init.bootstrap_deps())

    @mock.patch("platform.system", return_value="Linux")
    def test_no_pkg_manager_returns_false(self, _sys):
        init = load_script("init")
        with mock.patch.object(init, "missing_tools", return_value=["git"]):
            with mock.patch("shutil.which", return_value=None):
                self.assertFalse(init.bootstrap_deps())

    @mock.patch("platform.system", return_value="Linux")
    def test_install_succeeds_returns_true(self, _sys):
        init = load_script("init")
        with mock.patch.object(init, "missing_tools", side_effect=[["git"], []]):
            with mock.patch("shutil.which", side_effect=lambda c: "/usr/bin/apt-get" if c == "apt-get" else "/usr/bin/git"):
                with mock.patch("subprocess.run", return_value=None) as run:
                    self.assertTrue(init.bootstrap_deps(install_yes=True))
                    run.assert_called_once()


class CopyKitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.init = load_script("init")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_kit(self):
        kit = self.base / "kit"
        (kit / "aios" / "governance").mkdir(parents=True)
        (kit / "agents").mkdir(parents=True)
        (kit / "cli").mkdir(parents=True)
        (kit / "profiles" / "backend").mkdir(parents=True)
        (kit / "aios" / "governance" / "task-policy.md").write_text("# t\n", encoding="utf-8")
        (kit / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (kit / "__pycache__").mkdir(parents=True)
        (kit / "cli" / "__pycache__").mkdir(parents=True)
        (kit / "__pycache__" / "x.pyc").write_bytes(b"x")
        (kit / "cli" / "__pycache__" / "y.pyc").write_bytes(b"y")
        return kit

    def test_copy_kit_excludes_cache_and_installers(self):
        kit = self._fake_kit()
        self.init.KIT = kit
        target = self.base / "proj"
        self.init.copy_kit(target)
        kit_dst = target / "kit"
        self.assertTrue((kit_dst / "aios" / "governance" / "task-policy.md").is_file())
        self.assertTrue((kit_dst / "profiles" / "backend").is_dir())
        self.assertFalse((kit_dst / "install.sh").exists())
        self.assertFalse((kit_dst / "__pycache__").exists())
        self.assertFalse((kit_dst / "cli" / "__pycache__").exists())
        self.assertEqual(list(kit_dst.rglob("*.pyc")), [])


class CopyRuntimeTemplatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.init = load_script("init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_templates_copied_not_task_instances(self):
        runtime = self.base / "kit" / "runtime"
        (runtime / "tasks").mkdir(parents=True)
        (runtime / "logs").mkdir(parents=True)
        (runtime / "tasks" / "README.md").write_text("task dir\n", encoding="utf-8")
        (runtime / "tasks" / "TASK.template.md").write_text("# template\n", encoding="utf-8")
        (runtime / "tasks" / "TASK-001-fake.md").write_text("# instance\n", encoding="utf-8")
        (runtime / "logs" / "fail.log").write_text("x\n", encoding="utf-8")
        (runtime / "_WORKFLOW.md").write_text("wf\n", encoding="utf-8")
        self.init.KIT = self.base / "kit"
        target = self.base / "proj"
        self.init.copy_runtime_templates(target)
        dst = target / "runtime"
        self.assertTrue((dst / "tasks" / "README.md").is_file())
        self.assertTrue((dst / "tasks" / "TASK.template.md").is_file())
        self.assertTrue((dst / "_WORKFLOW.md").is_file())
        self.assertFalse((dst / "tasks" / "TASK-001-fake.md").exists())
        self.assertFalse((dst / "logs" / "fail.log").exists())


class GuardTargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.init = load_script("init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_target_inside_src_root_exits(self):
        src = self.base / "kit"
        src.mkdir()
        with self.assertRaises(SystemExit):
            self.init.guard_target(src, src / "runtime" / "tasks")

    def test_target_outside_src_root_ok(self):
        src = self.base / "kit"
        src.mkdir()
        outside = self.base / "other-project"
        self.init.guard_target(src, outside)  # 不应抛异常

    def test_target_is_src_root_itself_exits(self):
        src = self.base / "kit"
        src.mkdir()
        with self.assertRaises(SystemExit):
            self.init.guard_target(src, src)

    def test_module_src_root_resolves_symlinked_kit(self):
        # FIND-002 回归：经符号链接路径加载 init 时，SRC_ROOT 必须解析到物理路径
        # （与 main() 里 target.resolve() 口径一致，guard 才不会被 ValueError 绕过）。
        # 修复前 SCRIPT=abspath（不解析符号链接）→ SRC_ROOT 是逻辑路径 → 本测试失败。
        import shutil as _shutil
        real = self.base / "real"
        (real / "kit" / "cli").mkdir(parents=True)
        link = self.base / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest("symlink 不可用（如 Windows 无开发者模式/管理员）")
        _shutil.copy2(Path("kit/cli/init").resolve(), real / "kit" / "cli" / "init")
        # 从逻辑路径（含符号链接）加载：__file__ 走 link，模块内 resolve() 必须解析到 real
        script_path = str(link / "kit" / "cli" / "init")
        loader = importlib.machinery.SourceFileLoader("init_link_test", script_path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        self.assertEqual(mod.SRC_ROOT, Path(real).resolve())

    def test_guard_fires_when_target_reaches_src_via_symlink(self):
        # FIND-002 回归：src_root（物理路径）与经符号链接的 target（resolve 后也到物理路径）
        # 口径一致 → 防呆必须触发，不能被 ValueError 静默绕过
        real = self.base / "real-kit"
        real.mkdir()
        link = self.base / "link-kit"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest("symlink 不可用（如 Windows 无开发者模式/管理员）")
        src_root = Path(real).resolve()
        target = Path(link / "runtime" / "tasks").resolve()
        with self.assertRaises(SystemExit):
            self.init.guard_target(src_root, target)


class NoClobberCopyTest(unittest.TestCase):
    """FIND-001 回归：重跑 init 不得覆盖已存在文件（bash cp -rn 语义）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.init = load_script("init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_copy_dir_does_not_overwrite_nested_files(self):
        src = self.base / "src"
        (src / "nested").mkdir(parents=True)
        (src / "nested" / "file.txt").write_text("FROM_SOURCE", encoding="utf-8")
        dst = self.base / "dst"
        (dst / "nested").mkdir(parents=True)
        (dst / "nested" / "file.txt").write_text("USER_CUSTOM", encoding="utf-8")
        self.init.copy_dir(src, dst)
        self.assertEqual((dst / "nested" / "file.txt").read_text(encoding="utf-8"), "USER_CUSTOM")

    def test_copy_kit_does_not_overwrite_nested_files(self):
        kit = self.base / "kit"
        (kit / "aios" / "governance").mkdir(parents=True)
        (kit / "aios" / "governance" / "task-policy.md").write_text(
            "FRAMEWORK_VERSION", encoding="utf-8")
        self.init.KIT = kit
        target = self.base / "proj"
        (target / "kit" / "aios" / "governance").mkdir(parents=True)
        (target / "kit" / "aios" / "governance" / "task-policy.md").write_text(
            "USER_MODIFIED", encoding="utf-8")
        self.init.copy_kit(target)
        self.assertEqual(
            (target / "kit" / "aios" / "governance" / "task-policy.md").read_text(encoding="utf-8"),
            "USER_MODIFIED")


class SetupConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.init = load_script("init")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_profile(self):
        kit = self.base / "kit"
        (kit / "profiles" / "backend").mkdir(parents=True)
        (kit / "profiles" / "backend" / "config.template.yaml").write_text(
            "version: 1\nprofile: backend\ncommands:\n  build: <build>\n", encoding="utf-8")
        return kit

    def test_non_interactive_copies_template(self):
        self.init.KIT = self._fake_profile()
        target = self.base / "proj"
        target.mkdir()
        ok = self.init.setup_config(target, "backend", non_interactive=True)
        self.assertTrue(ok)
        cfg = target / "aios.config.yaml"
        self.assertTrue(cfg.is_file())
        self.assertIn("profile: backend", cfg.read_text(encoding="utf-8"))

    def test_existing_config_not_overwritten(self):
        self.init.KIT = self._fake_profile()
        target = self.base / "proj"
        target.mkdir()
        cfg = target / "aios.config.yaml"
        cfg.write_text("version: 1\nprofile: custom\n", encoding="utf-8")
        self.assertTrue(self.init.setup_config(target, "backend", non_interactive=True))
        self.assertIn("profile: custom", cfg.read_text(encoding="utf-8"))

    def test_unknown_profile_exits(self):
        self.init.KIT = self._fake_profile()
        target = self.base / "proj"
        target.mkdir()
        with self.assertRaises(SystemExit):
            self.init.setup_config(target, "nosuchprofile", non_interactive=True)


if __name__ == "__main__":
    unittest.main()
