# -*- coding: utf-8 -*-
"""冒烟测试 4：本地 HTTP 服务——敏感路径 404、跨站 403、接口可用（P1-5）。"""

from __future__ import annotations

import http.client
import json
import unittest
from pathlib import Path

from _ctx import api_headers, main, PROG


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # preferred_ports=[0] 让系统分配空闲端口：测试不会撞上正在跑的那只桌宠
        cls.srv = main.LocalServer(PROG, preferred_ports=[0])
        cls.port = cls.srv.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    # ------------------------------------------------------------ 工具
    def _req(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None, host: str = ""):
        hdrs = {"Host": host or ("127.0.0.1:%d" % self.port)}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json; charset=utf-8"
            hdrs["Content-Length"] = str(len(payload))
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=payload, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
            return resp.status, raw
        finally:
            conn.close()

    def _json(self, method: str, path: str, body: dict | None = None,
              headers: dict | None = None):
        code, raw = self._req(method, path, body, headers)
        try:
            return code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return code, {}

    # ------------------------------------------------------------ 正常接口
    def test_health(self):
        code, data = self._json("GET", "/api/health", headers=api_headers())
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"))
        self.assertIn("layers", data)

    def test_index_served(self):
        code, raw = self._req("GET", "/")
        self.assertEqual(code, 200)
        self.assertGreater(len(raw), 1000)

    def test_memory_write_and_search(self):
        text = "冒烟测试：今天风很大"
        code, data = self._json("POST", "/api/memory",
                                {"type": "episode", "text": text},
                                headers=api_headers())
        self.assertEqual(code, 200)
        self.assertTrue(data.get("ok"), data)
        code, found = self._json("GET", "/api/memory?q=%E6%9C%89%E9%A3%8E&type=episode",
                                 headers=api_headers())
        self.assertEqual(code, 200)
        self.assertTrue(found.get("ok"))

    # ------------------------------------------------------------ 敏感路径 404
    def test_sensitive_paths_are_404(self):
        for path in ("/main.py", "/.env", "/memory.db", "/%E8%AE%B0%E5%BF%86/memory.db",
                     "/puppy_agent/core/safety.py"):
            code, _ = self._req("GET", path, headers=api_headers())
            self.assertEqual(code, 404, "%s 应该是 404，实际 %s" % (path, code))

    # ------------------------------------------------------------ 跨站 403
    def test_cross_site_forbidden(self):
        code, _ = self._req("GET", "/api/health",
                            headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(code, 403)

    def test_foreign_origin_forbidden(self):
        code, _ = self._req("GET", "/api/health",
                            headers={"Origin": "http://evil.example.com"})
        self.assertEqual(code, 403)

    def test_dns_rebinding_host_forbidden(self):
        code, _ = self._req("GET", "/api/health", host="evil.example.com:%d" % self.port)
        self.assertEqual(code, 403)

    def test_options(self):
        code, _ = self._req("OPTIONS", "/api/chat", headers=api_headers())
        self.assertEqual(code, 204)

    # ------------------------------------------------------------ 令牌（P1-5）
    def test_token_required_when_enabled(self):
        token = getattr(main, "API_TOKEN", "") or ""
        if not token:
            self.skipTest("还没有启用令牌机制")
        code, _ = self._req("GET", "/api/health")
        self.assertEqual(code, 401)
        code, _ = self._req("GET", "/api/health", headers={"X-Puppy-Token": token})
        self.assertEqual(code, 200)

    # ------------------------------------------------------------ N-1 白名单
    def test_non_public_paths_require_token(self):
        """白名单之外，没有令牌一律 401——不再是"只有 /api/ 才查"。"""
        for path in ("/memory_diary.txt", "/voice/chat", "/deck", "/shot",
                     "/uploads/x.png", "/img/x.png", "/ppt/x.pptx",
                     "/memory.db", "/main.py"):
            code, _ = self._req("GET", path)
            self.assertEqual(code, 401, "%s 无令牌应该 401，实际 %s" % (path, code))

    def test_public_paths_stay_open(self):
        """页面骨架和内置表情仍然免令牌，否则窗口根本加载不出来。"""
        for path in ("/", "/pet", "/dog_girl.png"):
            code, _ = self._req("GET", path)
            self.assertEqual(code, 200, "%s 应该免令牌可访问，实际 %s" % (path, code))

    def test_diary_route_is_gone(self):
        """日记全文只走 /api/diary；/memory_diary.txt 这条后门已删除。"""
        code, _ = self._req("GET", "/memory_diary.txt", headers=api_headers())
        self.assertEqual(code, 404)

    def test_show_leaks_no_path_or_pid(self):
        """单实例唤回免令牌，所以只能回版本号；测试进程里没有 PetApp 时是 503。"""
        code, data = self._json("GET", "/show")
        self.assertIn(code, (200, 503), "实际 %s" % code)
        self.assertNotIn("exePath", data)
        self.assertNotIn("pid", data)

    def test_signed_media_url_is_accepted(self):
        """媒体链接带 k/e 时不该再要请求头（<img>/<audio> 带不了）。"""
        url = main.signed_url("/uploads/not-there.png")
        code, _ = self._req("GET", url)
        self.assertEqual(code, 404, "签名应被接受，只是文件不存在")

    def test_tampered_media_signature_is_rejected(self):
        url = main.signed_url("/uploads/not-there.png")
        bad = url.replace("k=", "k=0")
        code, _ = self._req("GET", bad)
        self.assertEqual(code, 401)

    def test_media_sign_endpoint_rejects_other_paths(self):
        code, data = self._json("GET", "/api/media/sign?path=/main.py",
                                headers=api_headers())
        self.assertEqual(code, 400)
        self.assertFalse(data.get("ok"))

    # ------------------------------------------------------------ N-13 表情白名单
    def test_builtin_sticker_is_public(self):
        """N-13：随包内置表情（清单内）免令牌可访问。"""
        import urllib.parse
        names = sorted(main._STICKER_NAMES)
        if not names:
            self.skipTest("表情包目录为空")
        code, _ = self._req("GET", "/stickers/" + urllib.parse.quote(names[0]))
        self.assertEqual(code, 200, "内置表情 %r 应免令牌" % names[0])

    def test_foreign_sticker_requires_token_or_sig(self):
        """N-13：不在清单里的表情文件（用户丢的）无令牌 401，带签名可过。"""
        import urllib.parse
        foreign = "/stickers/" + urllib.parse.quote("zz_not_builtin_999.png")
        code, _ = self._req("GET", foreign)
        self.assertEqual(code, 401, "清单外的表情应要令牌")
        url = main.signed_url(foreign)
        code, _ = self._req("GET", url)
        self.assertEqual(code, 404, "签名应被接受，只是文件不存在")
        code, _ = self._json("GET", "/api/media/sign?path=%s" % foreign,
                             headers=api_headers())
        self.assertEqual(code, 200, "/api/media/sign 应给 stickers 路径签名")

    # ------------------------------------------------------------ N-23 清单缺失即关闭
    def test_frozen_without_manifest_closes_stickers(self):
        """frozen 模式缺清单 → 内置集合为空（失败即关闭），不现场扫描。"""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="n23_") as td:
            td_path = Path(td)
            (td_path / "表情包").mkdir()
            (td_path / "表情包" / "user_drop.png").write_bytes(b"x")
            with mock.patch.object(main.sys, "frozen", True, create=True), \
                 mock.patch.object(main, "exe_dir", return_value=td_path):
                names = main.builtin_sticker_names()
            self.assertEqual(names, frozenset(),
                             "frozen 缺清单必须关闭，绝不能把用户图片当内置")

    def test_frozen_with_manifest_loads_list(self):
        """frozen 模式有清单 → 内置集合来自清单文件本身。"""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="n23b_") as td:
            td_path = Path(td)
            (td_path / "sticker_manifest.json").write_text(
                json.dumps(["a.png", "b.png"], ensure_ascii=False),
                encoding="utf-8")
            with mock.patch.object(main.sys, "frozen", True, create=True), \
                 mock.patch.object(main, "exe_dir", return_value=td_path):
                names = main.builtin_sticker_names()
            self.assertEqual(names, frozenset({"a.png", "b.png"}))

    def test_source_mode_still_scans_when_no_manifest(self):
        """源码模式没清单 → 现场扫描（目录内容即随包内容）。"""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory(prefix="n23c_") as td:
            td_path = Path(td)
            (td_path / "表情包").mkdir()
            (td_path / "表情包" / "bundled.png").write_bytes(b"x")
            with mock.patch.object(main.sys, "frozen", False, create=True), \
                 mock.patch.object(main, "exe_dir", return_value=td_path):
                names = main.builtin_sticker_names()
            self.assertEqual(names, frozenset({"bundled.png"}))

    # ------------------------------------------------------------ N-16 签名绑参数
    def test_signed_url_binds_query_params(self):
        """N-16：签名覆盖查询参数——拿着同一签名换参数复用应 401。"""
        url = main.signed_url("/deck", {"topic": "测试"})
        code, _ = self._req("GET", url)
        self.assertEqual(code, 200, "参数未变时应通过")
        # 同签名换 topic → 401（签名只覆盖了原参数串）
        bad = url.replace("topic=", "topic2=")
        code, _ = self._req("GET", bad)
        self.assertEqual(code, 401, "换参数复用签名应 401")

    def test_sign_local_url_binds_query(self):
        """N-16：sign_local_url（/deck、/shot 内部窗口）也绑参数。"""
        url = main.sign_local_url("/shot?path=zz.png&w=1")
        # 签名应通过：/shot 是动态页面，文件不存在也照常渲染（200）
        code, _ = self._req("GET", url)
        self.assertEqual(code, 200)
        # 换参数 → 401
        bad = url.replace("w=1", "w=2")
        code, _ = self._req("GET", bad)
        self.assertEqual(code, 401)

    # ------------------------------------------------------------ N-3 墓碑
    def test_blocked_fact_list_and_restore(self):
        """删掉的事实能在「已屏蔽」里看到，只有手动恢复才回到在用。"""
        code, made = self._json("POST", "/api/memory",
                                {"type": "fact", "key": "接口墓碑", "value": "临时值"},
                                headers=api_headers())
        self.assertEqual(code, 200)
        fid = int(made.get("id") or 0)
        self.assertGreater(fid, 0)

        code, _ = self._req("DELETE", "/api/memory/%d?type=fact" % fid,
                            headers=api_headers())
        self.assertEqual(code, 200)

        code, blocked = self._json("GET", "/api/memory?include_deleted=1&type=fact",
                                   headers=api_headers())
        self.assertTrue(blocked.get("ok"))
        self.assertTrue(any(int(it["id"]) == fid for it in blocked.get("items") or []))

        code, live = self._json("GET", "/api/memory?type=fact&limit=100",
                                headers=api_headers())
        self.assertFalse(any(int(it["id"]) == fid for it in live.get("items") or []))

        code, restored = self._json("POST", "/api/memory/restore", {"id": fid},
                                    headers=api_headers())
        self.assertTrue(restored.get("ok"), restored)

        code, live2 = self._json("GET", "/api/memory?type=fact&limit=100",
                                 headers=api_headers())
        self.assertTrue(any(int(it["id"]) == fid for it in live2.get("items") or []))


if __name__ == "__main__":
    unittest.main()
