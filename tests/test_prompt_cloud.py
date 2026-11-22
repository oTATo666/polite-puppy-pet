# -*- coding: utf-8 -*-
"""N-2：云端模式下，工具结果能不能拼进 prompt。

这段逻辑在前端（index.html）。为了测的是**真正会上线的代码**而不是复制品，
这里把 agToolPromptBlock() 的函数体从 index.html 里抽出来，用 node 跑一遍，
外面套一层最小桩（agLast / state / modeKind / memOutboundBlocked）。
没装 node 就跳过——CI 上缺 node 不该让整包测试变红。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROG = Path(__file__).resolve().parent.parent
INDEX = PROG / "index.html"


def extract_function(src: str, name: str) -> str:
    """从源码里切出 `function name(...) { ... }`，按花括号配对。"""
    key = "function " + name
    start = src.find(key)
    if start < 0:
        raise AssertionError("index.html 里找不到 %s" % name)
    brace = src.find("{", start)
    if brace < 0:
        raise AssertionError("%s 没有函数体" % name)
    depth = 0
    for i in range(brace, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError("%s 的花括号没配平" % name)


HARNESS = r"""
var fs = require("fs");
var fnSrc = fs.readFileSync(process.argv[2], "utf8");
var caseJson = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

var state = { cloudNoMem: true };
var MODE = "cloud";
var agLast = null;
var AG_MAX_HINT = 180;

function modeKind() { return MODE; }
function memOutboundBlocked() {
  return modeKind() === "cloud" && state.cloudNoMem !== false;
}
var agToolPromptBlock;
eval("agToolPromptBlock = (" + fnSrc + ")");

var out = {};
caseJson.forEach(function (c) {
  MODE = c.mode;
  state.cloudNoMem = c.cloudNoMem;
  agLast = c.agLast;
  out[c.name] = agToolPromptBlock();
});
process.stdout.write(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "没装 node，跳过前端逻辑回归")
class PromptCloudTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX.read_text(encoding="utf-8")
        cls.fn = extract_function(cls.src, "agToolPromptBlock")

    def _run(self, cases: list[dict]) -> dict:
        with tempfile.TemporaryDirectory(prefix="puppy_prompt_") as tmp:
            tmp = Path(tmp)
            (tmp / "fn.js").write_text(self.fn, encoding="utf-8")
            (tmp / "cases.json").write_text(
                json.dumps(cases, ensure_ascii=False), encoding="utf-8")
            (tmp / "harness.js").write_text(HARNESS, encoding="utf-8")
            proc = subprocess.run(
                ["node", str(tmp / "harness.js"), str(tmp / "fn.js"),
                 str(tmp / "cases.json")],
                capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_mem_result_blocked_in_cloud(self):
        """云端 + 默认设置：mem_search 的摘要一个字都不许进 prompt。"""
        out = self._run([{
            "name": "cloud_mem",
            "mode": "cloud", "cloudNoMem": True,
            "agLast": {"tool": "mem_search", "ok": True,
                       "summary": "他讨厌香菜，住在杭州"},
        }])
        text = out["cloud_mem"]
        self.assertNotIn("香菜", text)
        self.assertNotIn("杭州", text)
        self.assertIn("云端模式", text)

    def test_mem_result_allowed_when_switch_off(self):
        out = self._run([{
            "name": "cloud_mem_open",
            "mode": "cloud", "cloudNoMem": False,
            "agLast": {"tool": "mem_search", "ok": True,
                       "summary": "他讨厌香菜，住在杭州"},
        }])
        self.assertIn("香菜", out["cloud_mem_open"])

    def test_mem_result_allowed_locally(self):
        out = self._run([{
            "name": "local_mem",
            "mode": "local", "cloudNoMem": True,
            "agLast": {"tool": "mem_search", "ok": True,
                       "summary": "他讨厌香菜，住在杭州"},
        }])
        self.assertIn("香菜", out["local_mem"])

    def test_file_content_needs_cloud_confirmation(self):
        """云端模式没确认 → 不拼；确认过 → 拼，而且仍标成不可信数据。"""
        out = self._run([
            {"name": "fs_unconfirmed", "mode": "cloud", "cloudNoMem": True,
             "agLast": {"tool": "fs_read", "ok": True,
                        "summary": "note.txt：明天交作业", "cloudOk": False}},
            {"name": "fs_confirmed", "mode": "cloud", "cloudNoMem": True,
             "agLast": {"tool": "fs_read", "ok": True,
                        "summary": "note.txt：明天交作业", "cloudOk": True}},
            {"name": "fs_local", "mode": "local", "cloudNoMem": True,
             "agLast": {"tool": "fs_read", "ok": True,
                        "summary": "note.txt：明天交作业"}},
        ])
        self.assertEqual(out["fs_unconfirmed"], "")
        self.assertIn("明天交作业", out["fs_confirmed"])
        self.assertIn("不可信", out["fs_confirmed"])
        self.assertIn("明天交作业", out["fs_local"])


if __name__ == "__main__":
    unittest.main()
