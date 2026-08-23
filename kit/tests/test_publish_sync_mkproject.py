# -*- coding: utf-8 -*-
"""TASK-006 单元测试：cli/publish、cli/sync、cli/mkproject。

- publish/sync 走同一临时目录做往返验证（发布 → 拉取 → 校验文件与 manifest/SYNC-RECORD）。
- mkproject 用最小 kit 源生成 kit/ 子目录布局项目，校验结构与入口文件。
- 三个脚本通过 importlib 加载（与 tests/test_task_evidence.py 同约定），
  ROOT 用模块属性 monkeypatch 到临时目录，避免污染真实仓库。
"""
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


def load_script(name):
    import os
    path = f"kit/cli/{name}" if os.path.isfile(f"kit/cli/{name}") else f"cli/{name}"
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class PublishSyncRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self._argv = sys.argv[:]

    def tearDown(self):
        sys.argv = self._argv
        self.tmp.cleanup()

    def _make_project_root(self):
        root = self.base / "project"
        (root / "docs").mkdir(parents=True)
        (root / "knowledge" / "modules").mkdir(parents=True)
        (root / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")
        (root / "knowledge" / "modules" / "a.md").write_text("a\n", encoding="utf-8")
        return root

    def test_publish_creates_versioned_package_with_manifest(self):
        publish = load_script("publish")
        publish.ROOT = str(self._make_project_root())
        target = self.base / "shared"
        sys.argv = ["publish", str(target), "--package-name", "planning", "--version", "sync-test-1"]
        publish.main()

        pkg = target / "planning" / "sync-test-1"
        self.assertTrue((pkg / "docs" / "guide.md").is_file(), "docs 应被复制进发布包")
        self.assertTrue((pkg / "knowledge" / "modules" / "a.md").is_file(), "knowledge 应被复制进发布包")

        manifest = json.loads((pkg / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["package_id"], "planning-sync-test-1")
        self.assertEqual(manifest["version"], "sync-test-1")
        self.assertIn("docs", manifest["directories"])
        self.assertIn("knowledge", manifest["directories"])
        # 文件清单必须是相对路径（docs/guide.md），不允许 ../ 越界路径
        self.assertIn("docs/guide.md", manifest["files"])
        self.assertIn("knowledge/modules/a.md", manifest["files"])
        for f in manifest["files"]:
            self.assertNotIn("..", f, f"manifest 文件路径越界: {f}")

    def test_publish_auto_version_when_omitted(self):
        publish = load_script("publish")
        publish.ROOT = str(self._make_project_root())
        target = self.base / "shared"
        sys.argv = ["publish", str(target), "--package-name", "planning"]
        publish.main()
        versions = sorted(p.name for p in (target / "planning").iterdir())
        self.assertEqual(len(versions), 1)
        self.assertTrue(versions[0].startswith("sync-"), versions)

    def test_sync_pulls_published_version_with_record(self):
        publish = load_script("publish")
        publish.ROOT = str(self._make_project_root())
        target = self.base / "shared"
        sys.argv = ["publish", str(target), "--package-name", "planning", "--version", "sync-test-1"]
        publish.main()

        # 另一台“机器”：ROOT 指向不同目录
        sync = load_script("sync")
        machine2 = self.base / "machine2"
        machine2.mkdir()
        sync.ROOT = str(machine2)
        sys.argv = ["sync", str(target), "--package-name", "planning", "--version", "sync-test-1"]
        sync.main()

        dest = machine2 / "planning-versions" / "sync-test-1"
        self.assertTrue((dest / "docs" / "guide.md").is_file(), "拉取后 docs 应存在")
        self.assertTrue((dest / "knowledge" / "modules" / "a.md").is_file(), "拉取后 knowledge 应存在")
        self.assertTrue((dest / "SYNC-RECORD.json").is_file(), "应生成 SYNC-RECORD.json")
        record = json.loads((dest / "SYNC-RECORD.json").read_text(encoding="utf-8"))
        self.assertEqual(record["version"], "sync-test-1")
        self.assertEqual(record["package"], "planning")

    def test_sync_latest_picks_last_sorted_version(self):
        publish = load_script("publish")
        publish.ROOT = str(self._make_project_root())
        target = self.base / "shared"
        for v in ("sync-20260703-01", "sync-20260703-02"):
            sys.argv = ["publish", str(target), "--package-name", "planning", "--version", v]
            publish.main()

        sync = load_script("sync")
        machine2 = self.base / "machine2"
        machine2.mkdir()
        sync.ROOT = str(machine2)
        sys.argv = ["sync", str(target), "--package-name", "planning"]  # latest
        sync.main()
        self.assertTrue((machine2 / "planning-versions" / "sync-20260703-02" / "SYNC-RECORD.json").is_file())


class MkprojectKitLayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self._argv = sys.argv[:]

    def tearDown(self):
        sys.argv = self._argv
        self.tmp.cleanup()

    def _make_kit(self):
        """构造标准 kit 源仓库（模拟 aibase 布局）：
        src-root/
          AGENTS.md      # 源仓库根完整导航
          kit/           # 框架内容根（aios/ agents/ cli/ ...）
        --from 传 kit/（框架内容根）；AGENTS.md 从源仓库根复制。
        """
        src_root = self.base / "src-root"
        kit = src_root / "kit"
        (kit / "aios" / "governance").mkdir(parents=True)
        (kit / "agents" / "coder").mkdir(parents=True)
        (kit / "cli").mkdir()
        (kit / "profiles").mkdir()
        (kit / "tools" / "agent").mkdir(parents=True)
        (kit / "runtime" / "tasks").mkdir(parents=True)
        # 源仓库根：AGENTS.md（完整导航，含 kit/ 前缀路径）+ aios.config.yaml（项目配置在根）
        (src_root / "AGENTS.md").write_text(
            "# AGENTS.md\n\n## 快速导航\n\n1. [`kit/aios/governance/`](kit/aios/governance/)\n",
            encoding="utf-8")
        (src_root / "aios.config.yaml").write_text("version: 1\nprofile: backend\n", encoding="utf-8")
        (src_root / "opencode.md").write_text("# OpenCode 入口\n", encoding="utf-8")
        (kit / "runtime" / "tasks" / "TASK.template.md").write_text("---\nname: TASK.template\n---\n", encoding="utf-8")
        (kit / "agents" / "coder" / "role.md").write_text("# coder role\n", encoding="utf-8")
        (kit / "cli" / "task").write_text("#!/usr/bin/env python3\nprint('kit task')\n", encoding="utf-8")
        (kit / "tools" / "agent" / "agent.py").write_text(
            "#!/usr/bin/env python3\n# AIOS telemetry agent (TASK-029: mkproject 集成)", encoding="utf-8")
        return kit

    def test_mkproject_creates_kit_subdir_layout(self):
        mkproject = load_script("mkproject")
        kit = self._make_kit()
        project = self.base / "myproject"
        sys.argv = ["mkproject", str(project), "--from", str(kit)]
        mkproject.main()

        # 框架进 kit/（只读区；AGENTS.md 不在 kit/，项目根 AGENTS.md 是完整导航）
        self.assertFalse((project / "kit" / "AGENTS.md").exists(), "kit/ 不应再有 AGENTS.md")
        self.assertFalse((project / "kit" / "aios.config.yaml").exists())
        self.assertTrue((project / "kit" / "aios" / "governance").is_dir())
        self.assertTrue((project / "kit" / "agents" / "coder" / "role.md").is_file())
        # TASK-029 集成：mkproject 生成项目自动携带 kit/tools/agent/（遥测 agent 整目录）
        self.assertTrue((project / "kit" / "tools" / "agent" / "agent.py").is_file(),
                        "生成项目应携带 kit/tools/agent/agent.py")
        # 项目内容留根
        self.assertTrue((project / "aios.config.yaml").is_file())
        self.assertTrue((project / "AGENTS.md").is_file())
        self.assertTrue((project / "knowledge").is_dir())
        self.assertTrue((project / "docs").is_dir())
        self.assertTrue((project / "runtime" / "tasks").is_dir())
        self.assertTrue((project / "runtime" / "states").is_dir())
        self.assertTrue((project / ".gitignore").is_file())
        # 项目根 AGENTS.md 是完整框架导航（含 kit/ 前缀路径），不是薄指针
        agents = (project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("kit/aios/governance/", agents)
        self.assertIn("AGENTS.md", agents)
        # runtime 模板只复制模板/说明，不复制 kit 自己的任务实例
        self.assertTrue((project / "runtime" / "tasks" / "TASK.template.md").is_file())
        # 入口文件与 init 安装产物一致：opencode.md + CLAUDE.md symlink
        self.assertTrue((project / "opencode.md").is_file())
        self.assertTrue((project / "CLAUDE.md").is_symlink())
        self.assertEqual((project / "CLAUDE.md").readlink(), Path("AGENTS.md"))

    def test_mkproject_refuses_nonempty_target(self):
        mkproject = load_script("mkproject")
        kit = self._make_kit()
        project = self.base / "occupied"
        project.mkdir()
        (project / "existing.txt").write_text("x", encoding="utf-8")
        sys.argv = ["mkproject", str(project), "--from", str(kit)]
        with self.assertRaises(SystemExit):
            mkproject.main()


if __name__ == "__main__":
    unittest.main()
