# -*- coding: utf-8 -*-
"""AG-1：插件信任清单 + plugin lint + SDK v2 兼容（测试）。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from _ctx import PROG, main

SCRIPTS = PROG / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from puppy_agent.core.plugin import CORE_VERSION, RiskLevel, ToolSpec  # noqa: E402
from puppy_agent.core.runtime import AgentRuntime  # noqa: E402
from puppy_agent.core.trust import TrustList  # noqa: E402
import plugin_lint  # noqa: E402


class SdkV2Test(unittest.TestCase):
    """SDK v2：核心版本 1.1.0、permissions 字段、旧插件零改动可加载。"""

    def test_core_version_is_1_1_0(self):
        self.assertEqual(CORE_VERSION, "1.1.0")

    def test_permissions_default_empty(self):
        spec = ToolSpec(name="t", description="d")
        self.assertEqual(spec.permissions, [])
        self.assertIn("permissions", spec.schema())

    def test_min_core_1_0_still_loads(self):
        """声明 min_core_version=1.0.0 的旧插件在核心 1.1.0 下仍可加载。"""
        from puppy_agent.core.plugin import core_version_ok
        self.assertTrue(core_version_ok("1.0.0"))


class TrustListTest(unittest.TestCase):
    """信任清单：内置默认信任；用户目录默认禁用；启用绑定哈希；改动自动禁用。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="puppy_trust_"))
        self.builtin = self.tmp / "builtin_plugins"
        self.user = self.tmp / "user_plugins"
        self.builtin.mkdir()
        self.user.mkdir()
        (self.builtin / "fs.py").write_text("# builtin", encoding="utf-8")
        self.user_plugin = self.user / "custom.py"
        self.user_plugin.write_text("# custom v1", encoding="utf-8")
        self.trust = TrustList(self.tmp / "data", builtin_dir=self.builtin)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_builtin_allowed(self):
        v = self.trust.decide(self.builtin / "fs.py")
        self.assertTrue(v["allow"])
        self.assertEqual(v["reason"], "builtin")

    def test_user_plugin_denied_by_default(self):
        v = self.trust.decide(self.user_plugin)
        self.assertFalse(v["allow"])
        self.assertIn("默认禁用", v["reason"])

    def test_enable_then_load(self):
        self.assertTrue(self.trust.enable(self.user_plugin)["ok"])
        v = self.trust.decide(self.user_plugin)
        self.assertTrue(v["allow"])

    def test_hash_change_auto_disables(self):
        self.trust.enable(self.user_plugin)
        self.user_plugin.write_text("# custom v2 (changed)", encoding="utf-8")
        v = self.trust.decide(self.user_plugin)
        self.assertFalse(v["allow"])
        self.assertTrue(v["changed"])
        # 记录里也应已翻成禁用
        v2 = self.trust.decide(self.user_plugin)
        self.assertFalse(v2["allow"])

    def test_disable(self):
        self.trust.enable(self.user_plugin)
        self.trust.disable("custom.py")
        v = self.trust.decide(self.user_plugin)
        self.assertFalse(v["allow"])
        self.assertIn("禁用", v["reason"])

    def test_runtime_skips_untrusted_user_plugin(self):
        """AgentRuntime：用户目录里的插件默认不加载（注册表里没有它的工具）。"""
        plugin_src = (
            "from puppy_agent.core.plugin import Plugin, PluginManifest, ToolSpec, RiskLevel\n"
            "class P(Plugin):\n"
            "    manifest = PluginManifest(id='zz_custom', name='custom')\n"
            "    def tools(self):\n"
            "        return [ToolSpec(name='zz_custom_tool', description='d',\n"
            "                        risk=RiskLevel.LOW,\n"
            "                        invoke=lambda a: {'ok': True})]\n"
        )
        (self.user / "zz_custom.py").write_text(plugin_src, encoding="utf-8")
        rt = AgentRuntime(self.builtin, self.tmp / "data",
                          extra_dirs=[self.user])
        rt.load_all()
        self.assertIsNone(rt.registry.get("zz_custom_tool"))
        # 用户在设置页启用后 → 加载成功
        self.assertTrue(rt.trust.enable(self.user / "zz_custom.py")["ok"])
        rt.load_all()
        self.assertIsNotNone(rt.registry.get("zz_custom_tool"))

    def test_runtime_hash_change_drops_tools(self):
        plugin_src = (
            "from puppy_agent.core.plugin import Plugin, PluginManifest, ToolSpec, RiskLevel\n"
            "class P(Plugin):\n"
            "    manifest = PluginManifest(id='zz_hash', name='hash')\n"
            "    def tools(self):\n"
            "        return [ToolSpec(name='zz_hash_tool', description='d',\n"
            "                        risk=RiskLevel.LOW,\n"
            "                        invoke=lambda a: {'ok': True})]\n"
        )
        p = self.user / "zz_hash.py"
        p.write_text(plugin_src, encoding="utf-8")
        rt = AgentRuntime(self.builtin, self.tmp / "data",
                          extra_dirs=[self.user])
        rt.trust.enable(p)
        rt.load_all()
        self.assertIsNotNone(rt.registry.get("zz_hash_tool"))
        # 文件被改 → 重载后工具消失
        p.write_text(plugin_src.replace("zz_hash_tool", "zz_hash_tool2"),
                     encoding="utf-8")
        rt.load_all()
        self.assertIsNone(rt.registry.get("zz_hash_tool"))
        self.assertIsNone(rt.registry.get("zz_hash_tool2"))


class PluginLintTest(unittest.TestCase):
    """plugin_lint：好插件全过；坏插件抓出问题。"""

    def test_builtin_plugins_all_pass(self):
        """包内自带插件必须全过 lint（发布门）。"""
        rows_all = []
        plugins_dir = PROG / "puppy_agent" / "plugins"
        for p in sorted(plugins_dir.glob("*.py")):
            if p.name.startswith("_"):
                continue
            for ok, label in plugin_lint.lint_file(p):
                if not ok:
                    rows_all.append("%s: %s" % (p.name, label))
        self.assertEqual(rows_all, [], "包内插件 lint 不过：%r" % rows_all)

    def test_bad_plugin_caught(self):
        tmp = Path(tempfile.mkdtemp(prefix="puppy_lint_"))
        try:
            bad = tmp / "bad.py"
            bad.write_text(
                "from puppy_agent.core.plugin import Plugin, PluginManifest, ToolSpec\n"
                "class P(Plugin):\n"
                "    manifest = PluginManifest(id='', name='')\n"
                "    def tools(self):\n"
                "        return [ToolSpec(name='', description='',\n"
                "                        parameters={'type': 'bogus'})]\n",
                encoding="utf-8")
            rows = plugin_lint.lint_file(bad)
            fails = [label for ok, label in rows if not ok]
            self.assertGreater(len(fails), 0, "坏插件必须被抓出问题")
            self.assertTrue(any("manifest.id" in x for x in fails))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
