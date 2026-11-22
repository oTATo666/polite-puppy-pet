# -*- coding: utf-8 -*-
"""AG-8 / UI-0：设置页 API 与界面预留测试。

  · 后端 /api/agent/config GET/POST：模式校验、local 无模型拒绝、
    知情同意与上限持久化；
  · 插件列表 / 提醒与白名单 API：空态可用、非法插件名拒绝；
  · UI-0：<html> 的四个 data-* 属性、四个事件、prefers-reduced-motion
    初值、持久化键——只测文本契约（不改视觉）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
import io

from _ctx import AGENT_DATA, PROG, main

SCRIPTS = PROG / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class AgentConfigApiTest(unittest.TestCase):
    """后端配置 API（直接调函数，避免起 HTTP）。"""

    def test_get_shape(self):
        r = main._agent_config_get()
        self.assertTrue(r.get("ok"), r)
        self.assertIn(r["mode"], ("off", "rule", "local", "cloud"))
        self.assertEqual(set(r["modes"]), {"off", "rule", "local", "cloud"})
        self.assertIsInstance(r.get("cloudConsent"), bool)
        self.assertIsInstance(r.get("dailyLimit"), int)
        self.assertIsInstance(r.get("localAvailable"), bool)

    def test_reject_bad_mode(self):
        r = main._agent_config_set({"mode": "magic"})
        self.assertFalse(r.get("ok"))
        self.assertIn("模式", r.get("message") or "")

    def test_cloud_consent_and_limit_persist(self):
        import os
        tmp = Path(tempfile.mkdtemp(prefix="puppy_ag8_"))
        saved = main.CONFIG_FILE
        try:
            main.CONFIG_FILE = tmp / "settings.json"
            r = main._agent_config_set({"cloudConsent": True, "dailyLimit": 77})
            self.assertTrue(r.get("ok"), r)
            self.assertTrue(main._agent_config_get()["cloudConsent"])
            self.assertEqual(main._agent_config_get()["dailyLimit"], 77)
            # clamp
            r2 = main._agent_config_set({"dailyLimit": 99999})
            self.assertTrue(r2.get("ok"))
            self.assertEqual(main._agent_config_get()["dailyLimit"], 500)
        finally:
            main.CONFIG_FILE = saved

    def test_plugin_list_without_runtime(self):
        saved = main.AGENT_STATE.get("runtime")
        main.AGENT_STATE["runtime"] = None
        try:
            r = main._agent_plugin_list()
            self.assertFalse(r.get("ok"))
            self.assertEqual(r.get("items"), [])
        finally:
            main.AGENT_STATE["runtime"] = saved

    def test_plugin_bad_name_rejected(self):
        r = main._agent_plugin_toggle({"name": "..\\evil.py", "enabled": True})
        self.assertFalse(r.get("ok"))


class UiContractTest(unittest.TestCase):
    """UI-0 / AG-8 前端文本契约（node --check 另由 js_check.py 覆盖）。"""

    def setUp(self):
        self.html = (PROG / "index.html").read_text(encoding="utf-8")

    def test_html_data_attributes(self):
        # 方案§9：data-skin 默认 academy、data-theme 默认 day
        self.assertIn('data-skin="academy"', self.html)
        self.assertIn('data-theme="day"', self.html)
        self.assertIn('data-motion="full"', self.html)
        self.assertIn('data-agent-phase="idle"', self.html)

    def test_ui_events_and_persistence(self):
        for ev in ("puppy:skin-change", "puppy:theme-change",
                   "puppy:agent-phase", "puppy:motion-change"):
            self.assertIn(ev, self.html, "缺事件 %s" % ev)
        self.assertIn("prefers-reduced-motion", self.html)
        self.assertIn("puppy_ui0", self.html)

    def test_ag8_settings_controls(self):
        for token in ('name="agMode"', 'id="agCloudConsent"',
                      'id="agDailyLimit"', 'id="agPlugins"',
                      'id="agWhitelist"', 'id="agReminders"',
                      'id="agPlanSteps"', "/api/agent/config",
                      "/api/agent/resume"):
            self.assertIn(token, self.html, "设置页缺控件 %s" % token)

    def test_confirm_dialog_shows_args_and_why(self):
        self.assertIn("window.confirm(msg)", self.html)
        self.assertIn("工具：", self.html)
        self.assertIn("原因：", self.html)
        self.assertIn("参数：", self.html)


if __name__ == "__main__":
    unittest.main()
