# -*- coding: utf-8 -*-
"""冒烟测试 1：/api/health 分层自检（P1-2）。"""

from __future__ import annotations

import unittest

from _ctx import main


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.h = main._health()

    def test_ok_means_selfcheck_ran(self):
        """ok 只表示自检跑完了，不再代表"Ollama 通不通"。"""
        self.assertTrue(self.h.get("ok"))

    def test_four_layers(self):
        layers = self.h.get("layers") or {}
        for name in ("brain", "ollama", "cloud", "tts"):
            self.assertIn(name, layers, "少了 %s 这一层" % name)
            self.assertIn("ok", layers[name])

    def test_brain_layer_reports_corpus(self):
        brain = self.h["layers"]["brain"]
        self.assertIsInstance(brain.get("sentences"), int)
        self.assertIsInstance(brain.get("keywords"), int)

    def test_haskey_is_real_not_hardcoded(self):
        """老实现写死 True；现在必须跟着 .env 的真实配置走。"""
        self.assertIsInstance(self.h.get("hasKey"), bool)
        self.assertEqual(self.h["hasKey"], bool(main.env_key()))

    def test_version_and_pid_present(self):
        self.assertEqual(self.h.get("version"), main.APP_VERSION)
        self.assertGreater(self.h.get("pid") or 0, 0)


if __name__ == "__main__":
    unittest.main()
