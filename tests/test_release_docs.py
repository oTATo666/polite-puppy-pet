# -*- coding: utf-8 -*-
"""N-8：外发文档与数字契约。

  · README 占位符在源码里存在，组装时会被回填；
  · 回填后的数字（测试数/文件数/exe SHA16）与 MANIFEST / 证据一致；
  · 外发包排除内部过程文档（状态更新_*/任务完成列表*/STATUS*）；
  · README 如实写明：默认全本地、云端发什么、云端历史限制句、词表、备份。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _ctx import DOC_PLACEHOLDERS, PROG, SKIP_NO_DOC_PLACEHOLDERS

SCRIPTS = PROG / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import package_release  # noqa: E402


@unittest.skipUnless(DOC_PLACEHOLDERS, SKIP_NO_DOC_PLACEHOLDERS)
class DocPlaceholdersTest(unittest.TestCase):
    def test_source_readme_has_placeholders(self):
        """源码里的 README 必须是占位符（不是手抄死数）。"""
        txt = (PROG / "README.md").read_text(encoding="utf-8")
        for token in ("{{TESTS}}", "{{FILES}}", "{{EXE_SHA16}}"):
            self.assertIn(token, txt, "源码 README 缺占位符 %s" % token)

    def test_build_fills_placeholders_and_excludes_internal_docs(self):
        """在临时目录跑一次最小组装：占位符回填 + 内部文档排除。"""
        tmp = Path(tempfile.mkdtemp(prefix="puppy_n8_"))
        out = tmp / "pkg"
        # 直接调 build，不打包 exe（--with-exe 关），只验回填与排除
        res = package_release.build(out, with_exe=False, tests_count=218)
        readme = out / "01_程序" / "README.md"
        txt = readme.read_text(encoding="utf-8")
        for token in ("{{TESTS}}", "{{FILES}}", "{{EXE_SHA16}}"):
            self.assertNotIn(token, txt, "回填后仍有占位符 %s" % token)
        self.assertIn("测试 `218` 条全过", txt)
        # MANIFEST 里的文件数必须与回填数一致
        manifest = json.loads((out / "03_校验" / "MANIFEST.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(manifest["file_count"], res["files"])
        n_files_claim = int(
            [ln for ln in txt.splitlines() if "本包自检基线" in ln][0]
            .split("外发包 `")[1].split("`")[0])
        self.assertEqual(n_files_claim, manifest["file_count"])
        # 内部过程文档不进包
        docs = out / "02_文档"
        if docs.exists():
            for p in docs.rglob("*"):
                if p.is_file():
                    self.assertFalse(p.name.startswith(("状态更新_",
                                                         "任务完成列表", "STATUS")),
                                     "内部文档进了包：%s" % p.name)

    def test_readme_privacy_wording(self):
        txt = (PROG / "README.md").read_text(encoding="utf-8")
        for phrase in ("默认", "不联网", "云端", "会发", "聊天窗口里已经存在",
                       "私人词表", "保留最近 3 份"):
            self.assertIn(phrase, txt, "README 缺如实说明：%s" % phrase)


if __name__ == "__main__":
    unittest.main()
