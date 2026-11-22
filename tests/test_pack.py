# -*- coding: utf-8 -*-
"""N-4：pack_check --exe 在 build_info.json 缺失时必须 FAIL（退出码 1）。

以前缺了就"跳过"（PASS），等于自检白跑：拿一个旧 exe 来查也能过。
另锁一条：exe 修改时间早于源码时给出警告。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _ctx import PACKAGE_TREE, SKIP_NOT_PACKAGE, main as main_mod  # noqa: F401  （数据目录先指到临时目录）

PROG = Path(main_mod.__file__).resolve().parent
SCRIPTS = PROG / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pack_check  # noqa: E402
import package_release  # noqa: E402
import private_words  # noqa: E402


def _app_version() -> str:
    for line in (PROG / "main.py").read_text(encoding="utf-8").splitlines():
        m = re.match(r'^APP_VERSION\s*=\s*"([^"]+)"', line)
        if m:
            return m.group(1)
    return ""


@unittest.skipUnless(PACKAGE_TREE, SKIP_NOT_PACKAGE)
class PackCheckExeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="puppy_pack_"))
        self.exe = self.tmp / "PolitePuppy.exe"
        # 假 exe：MZ 头 + 凑过 5 MB 大小检查
        self.exe.write_bytes(b"MZ" + b"\0" * (6 * 1024 * 1024))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            return pack_check.main(argv), buf.getvalue()

    def test_missing_build_info_fails(self):
        """验收：删掉 build_info.json 后退出码为 1。"""
        rows = pack_check.check_exe(self.exe)
        bad = [label for ok, label in rows if not ok]
        self.assertTrue(any("build_info.json" in s for s in bad),
                        "缺 build_info.json 应 FAIL：%r" % rows)
        code, out = self._run(["--exe", str(self.exe)])
        self.assertEqual(code, 1)
        self.assertIn("build_info.json", out)

    def test_stale_exe_gets_warning(self):
        """exe 修改时间早于源码 → 出现警告行（不 FAIL，但要看得见）。"""
        (self.tmp / "build_info.json").write_text(json.dumps({
            "version": _app_version(),
            "source": {"main.py": "0" * 64, "index.html": "0" * 64},
            "exe_sha256": "0" * 64,
        }), encoding="utf-8")
        old = (PROG / "main.py").stat().st_mtime - 3600
        os.utime(self.exe, (old, old))
        rows = pack_check.check_exe(self.exe)
        warns = [label for ok, label in rows if "警告" in label]
        self.assertTrue(any("早于" in w for w in warns), "没有时间戳警告：%r" % rows)

    def test_matching_info_passes(self):
        """build_info.json + 表情清单齐全且哈希一致 → 退出码 0（N-22 口径）。"""
        import hashlib
        import time as _time

        def sha(p: Path) -> str:
            return hashlib.sha256(p.read_bytes()).hexdigest()

        (self.tmp / "build_info.json").write_text(json.dumps({
            "version": _app_version(),
            "built_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": {"main.py": sha(PROG / "main.py"),
                       "index.html": sha(PROG / "index.html")},
            "exe_sha256": sha(self.exe),
        }), encoding="utf-8")
        (self.tmp / "sticker_manifest.json").write_text("[]", encoding="utf-8")
        code, out = self._run(["--exe", str(self.exe)])
        self.assertEqual(code, 0, "齐全的构建产物应全过：\n%s" % out)

    def test_missing_sticker_manifest_fails(self):
        """N-22/N-23：exe 旁边缺 sticker_manifest.json → FAIL。"""
        import hashlib
        import time as _time

        def sha(p: Path) -> str:
            return hashlib.sha256(p.read_bytes()).hexdigest()

        (self.tmp / "build_info.json").write_text(json.dumps({
            "version": _app_version(),
            "built_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": {"main.py": sha(PROG / "main.py"),
                       "index.html": sha(PROG / "index.html")},
            "exe_sha256": sha(self.exe),
        }), encoding="utf-8")
        # 故意不写 sticker_manifest.json
        code, out = self._run(["--exe", str(self.exe)])
        self.assertEqual(code, 1)
        self.assertIn("sticker_manifest.json", out)

    def test_built_at_older_than_source_fails(self):
        """N-22：build_info.built_at 早于源码修改时间 → FAIL（旧构建信息配新源码）。"""
        import hashlib
        import time as _time

        def sha(p: Path) -> str:
            return hashlib.sha256(p.read_bytes()).hexdigest()

        (self.tmp / "build_info.json").write_text(json.dumps({
            "version": _app_version(),
            "built_at": "2020-01-01 00:00:00",
            "source": {"main.py": sha(PROG / "main.py"),
                       "index.html": sha(PROG / "index.html")},
            "exe_sha256": sha(self.exe),
        }), encoding="utf-8")
        (self.tmp / "sticker_manifest.json").write_text("[]", encoding="utf-8")
        code, out = self._run(["--exe", str(self.exe)])
        self.assertEqual(code, 1)
        self.assertIn("built_at", out)


class DistBindingTest(unittest.TestCase):
    """N-22：组装只从 dist 取构建产物，exe/build_info/源码三绑定验证。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="puppy_dist_"))
        self.dist = self.tmp / "dist"
        self.out = self.tmp / "out"
        self.dist.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk_dist(self, exe_sha: str, stale_manifest: bool = False) -> None:
        import hashlib
        import time as _time

        def sha(p: Path) -> str:
            return hashlib.sha256(p.read_bytes()).hexdigest()

        exe = self.dist / "PolitePuppy.exe"
        exe.write_bytes(b"MZ" + b"\0" * 1024)     # 内容随意，哈希按内容算
        (self.dist / "build_info.json").write_text(json.dumps({
            "version": _app_version(),
            "built_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": {"main.py": sha(PROG / "main.py"),
                       "index.html": sha(PROG / "index.html")},
            "exe_sha256": exe_sha,
        }), encoding="utf-8")
        (self.dist / "sticker_manifest.json").write_text("[]", encoding="utf-8")
        if stale_manifest:
            (self.dist / "sticker_manifest.json").unlink()

    def test_old_exe_aborts_assembly(self):
        """验收：故意把旧 exe 放进组装目录 → 组装中止。"""
        self._mk_dist("f" * 64)     # build_info 登记的哈希与实际 exe 不符
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            code = package_release.main(["--out", str(self.out),
                                         "--with-exe", "--dist", str(self.dist)])
        self.assertEqual(code, 1, "哈希不匹配必须中止组装")
        self.assertNotIn("PolitePuppy.exe", buf.getvalue())
        self.assertFalse((self.out / "01_程序" / "PolitePuppy.exe").exists())

    def test_wrong_source_hash_aborts_assembly(self):
        """build_info 登记的源码哈希与包内源码不一致 → 中止（旧源码打的 exe）。"""
        import hashlib
        import time as _time
        exe = self.dist / "PolitePuppy.exe"
        exe.write_bytes(b"MZ" + b"\0" * 1024)
        (self.dist / "build_info.json").write_text(json.dumps({
            "version": _app_version(),
            "built_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": {"main.py": "0" * 64, "index.html": "0" * 64},
            "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        }), encoding="utf-8")
        (self.dist / "sticker_manifest.json").write_text("[]", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            code = package_release.main(["--out", str(self.out),
                                         "--with-exe", "--dist", str(self.dist)])
        self.assertEqual(code, 1, "源码哈希不符必须中止")

    def test_missing_manifest_aborts_assembly(self):
        """验收：故意删掉清单 → 组装中止；pack_check --exe 也失败。"""
        import hashlib
        import time as _time

        def sha(p: Path) -> str:
            return hashlib.sha256(p.read_bytes()).hexdigest()

        exe = self.dist / "PolitePuppy.exe"
        exe.write_bytes(b"MZ" + b"\0" * 1024)
        (self.dist / "build_info.json").write_text(json.dumps({
            "version": _app_version(),
            "built_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": {"main.py": sha(PROG / "main.py"),
                       "index.html": sha(PROG / "index.html")},
            "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
        }), encoding="utf-8")
        # 不写 sticker_manifest.json
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            code = package_release.main(["--out", str(self.out),
                                         "--with-exe", "--dist", str(self.dist)])
        self.assertEqual(code, 1, "缺清单必须中止组装")

    def test_tree_mode_fails_on_forbidden_file(self):
        """N-22 --tree：发布树里出现黑名单文件 → FAIL。"""
        pkg = self.tmp / "pkg"
        (pkg / "01_程序").mkdir(parents=True)
        (pkg / "01_程序" / "main.py").write_text("# ok", encoding="utf-8")
        (pkg / "01_程序" / ".env").write_text("KEY=x", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = pack_check.main(["--tree", str(pkg)])
        self.assertEqual(code, 1, "发布树出现 .env 必须 FAIL")
        self.assertIn(".env", buf.getvalue())
        # 干净树 → PASS
        (pkg / "01_程序" / ".env").unlink()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = pack_check.main(["--tree", str(pkg)])
        self.assertEqual(code, 0)

    def test_dev_tree_hint_not_fail(self):
        """开发树（01_程序）里有 .env 是正常的——check() 只是提示，不 FAIL。"""
        rows = pack_check.check()
        # 本机开发树可能存在 .env：断言这些行不是 FAIL
        for ok, label in rows:
            if ".env" in label or "private_names" in label:
                self.assertTrue(ok, "开发树提示不应判 FAIL：%s" % label)


@unittest.skipUnless(PACKAGE_TREE, SKIP_NOT_PACKAGE)
class ReleaseGateTest(unittest.TestCase):
    """N-21：发布模式（--release）下私人词表为空必须 FAIL（退出码 1）。

    词表为空的机器上扫描器会安静地放过昵称——发布检查不允许这种
    「没人知道漏了」的状态；开发时（不带 --release）保持现状。
    """

    def test_release_gate_fails_without_names(self):
        # mock 掉词表来源，保证「空词表」场景可复现（不受本机词表影响）
        from unittest import mock
        with mock.patch.object(private_words, "private_names", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = pack_check.main(["--release"])
        self.assertEqual(code, 1)

    def test_release_gate_passes_with_names(self):
        old = os.environ.get("PUPPY_PRIVATE_NAMES")
        os.environ["PUPPY_PRIVATE_NAMES"] = "zzgateprobe"
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = pack_check.main(["--release"])
        finally:
            if old is None:
                os.environ.pop("PUPPY_PRIVATE_NAMES", None)
            else:
                os.environ["PUPPY_PRIVATE_NAMES"] = old
        self.assertEqual(code, 0)

    def test_dev_mode_not_affected(self):
        """不带 --release 的开发模式：词表为空也照常 PASS。"""
        from unittest import mock
        with mock.patch.object(private_words, "private_names", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = pack_check.main([])
        self.assertEqual(code, 0)


class PackageReleaseGateTest(unittest.TestCase):
    """N-21：package_release.py --release 在词表为空时退出码 1。"""

    def test_package_release_gate(self):
        from unittest import mock
        with mock.patch.object(private_words, "private_names", return_value=[]):
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                code = package_release.main(["--release"])
        self.assertEqual(code, 1)
        # 词表已配置时门放行（继续走到后续流程，这里用 --check + 假包路径
        # 会在「目录不存在」处退出 2——说明发布门已通过）
        with mock.patch.object(private_words, "private_names",
                               return_value=["zzgateprobe"]):
            buf = io.StringIO()
            err = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                code = package_release.main(["--release", "--check", "Z:/no_such_dir"])
        self.assertEqual(code, 2, "词表非空时发布门应放行（目录不存在才报 2）")


if __name__ == "__main__":
    unittest.main()
