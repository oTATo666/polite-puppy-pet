# -*- coding: utf-8 -*-
"""N-24/N-25/N-26：身份名参数化、外发语料脱敏、外发声音策略。

隐私约束：本文件**不出现任何真实称呼**——所有期望值都从 persona.json /
settings 合成数据动态构造；测试语料一律合成。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from _ctx import HAS_RELEASE_PERSONA, SKIP_NO_RELEASE_PERSONA, main, PROG

SCRIPTS = PROG / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import identity  # noqa: E402
import package_release  # noqa: E402
import scrub_brain  # noqa: E402


def _persona_ident() -> dict:
    """个人版 persona.json 的 identity（运行时读，词面不进本文件）。"""
    return (json.loads((PROG / "persona.json").read_text(encoding="utf-8"))
            .get("identity") or {})


class IdentityTest(unittest.TestCase):
    """identity.py：settings > persona > 中性默认，bind 后立即生效。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="puppy_ident_"))
        self.cfg = self.tmp / "settings.json"
        self.per = self.tmp / "persona.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        identity.bind(None, None)    # 还原，防止污染其他测试

    def _bind(self):
        identity.bind(self.cfg, self.per)

    def test_defaults_when_no_files(self):
        self._bind()
        self.assertEqual(identity.pet_name(), "小狗")
        self.assertEqual(identity.user_nick(), "你")

    def test_persona_fallback(self):
        self.per.write_text(json.dumps({
            "identity": {"name": "阿汪", "owner": "小测"}}, ensure_ascii=False),
            encoding="utf-8")
        self._bind()
        self.assertEqual(identity.pet_name(), "阿汪")
        self.assertEqual(identity.user_nick(), "小测")

    def test_settings_overrides_persona(self):
        self.per.write_text(json.dumps({
            "identity": {"name": "阿汪", "owner": "小测"}}, ensure_ascii=False),
            encoding="utf-8")
        self.cfg.write_text(json.dumps({
            "identity": {"user_nick": "定制称呼"}}, ensure_ascii=False),
            encoding="utf-8")
        self._bind()
        self.assertEqual(identity.user_nick(), "定制称呼")
        self.assertEqual(identity.pet_name(), "阿汪")

    def test_edition_field(self):
        self.per.write_text(json.dumps({"edition": "release"}), encoding="utf-8")
        self._bind()
        self.assertEqual(identity.edition(), "release")
        self.per.write_text(json.dumps({}), encoding="utf-8")
        self._bind()
        self.assertEqual(identity.edition(), "")

    @unittest.skipUnless(HAS_RELEASE_PERSONA, SKIP_NO_RELEASE_PERSONA)
    def test_release_edition_json_is_neutral(self):
        """release/persona.json 必须满足 D3 的中性约束。"""
        data = json.loads((PROG / "release" / "persona.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(data.get("edition"), "release")
        ident = data.get("identity") or {}
        self.assertEqual(ident.get("name"), "小狗")
        self.assertEqual(ident.get("owner"), "你")
        # banned 词只查**正向设定**：禁令列表（identity.not / personality.never）
        # 与 `_说明` 是约束描述（"不要自称人类"），不算设定，不查。
        def _strip_bans(o):
            if isinstance(o, dict):
                return {k: _strip_bans(v) for k, v in o.items()
                        if k not in ("not", "never") and not k.startswith("_")}
            if isinstance(o, list):
                return [_strip_bans(x) for x in o]
            return o
        blob = json.dumps(_strip_bans(data), ensure_ascii=False)
        for banned in ("高三", "河南", "十八岁", "人类", "学生"):
            self.assertNotIn(banned, blob, "中性人设不应含 %r" % banned)
        self.assertIn("如实回答", blob)
        self.assertIn("电子小狗", blob)


class FallbackPromptTest(unittest.TestCase):
    """N-24：个人版提示词逐字一致；外发版中性。"""

    def setUp(self):
        # 模拟个人版：persona.json 是真身，settings 为空
        identity.bind(None, PROG / "persona.json")

    def tearDown(self):
        identity.bind(None, None)

    def test_personal_prompt_identical(self):
        """个人版 = 原文模板填回 persona.json 里的名字，逐字一致。"""
        ident = _persona_ident()
        expect_first = ("你是%s，网名“礼貌小狗”。十八岁，河南的高三生，女的。"
                        % (ident.get("name") or ""))
        got = main._fallback_system()
        self.assertTrue(got.startswith(expect_first),
                        "个人版首句必须与历史版本逐字一致")
        self.assertIn("你不是AI，不是助手", got)
        self.assertIn("河南味儿", got)
        self.assertNotIn("%s", got)      # 占位符必须已填充

    @unittest.skipUnless(HAS_RELEASE_PERSONA, SKIP_NO_RELEASE_PERSONA)
    def test_release_prompt_neutral(self):
        """外发版（edition=release）：中性、如实承认 AI、无年龄地域学生。"""
        tmp = Path(tempfile.mkdtemp(prefix="puppy_rel_"))
        try:
            per = tmp / "persona.json"
            per.write_text((PROG / "release" / "persona.json")
                           .read_text(encoding="utf-8"), encoding="utf-8")
            identity.bind(None, per)
            got = main._fallback_system()
            self.assertIn("如实回答", got)
            self.assertIn("AI 小狗", got)
            for banned in ("高三", "河南", "十八岁", "学生"):
                self.assertNotIn(banned, got, "外发版提示词不应含 %r" % banned)
            self.assertIn("排他的恋爱或占有式语气", got)   # 显式禁令
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_draft_prompt_uses_identity(self):
        """小模型草稿提示词的名字也走 identity（合成身份验证）。"""
        tmp = Path(tempfile.mkdtemp(prefix="puppy_draft_"))
        try:
            per = tmp / "persona.json"
            per.write_text(json.dumps(
                {"identity": {"name": "阿汪", "owner": "小测"}},
                ensure_ascii=False), encoding="utf-8")
            identity.bind(None, per)
            # _small_model_draft 的提示词主体不可直接取，这里验证 identity 链路
            self.assertEqual(identity.pet_name(), "阿汪")
            self.assertEqual(identity.user_nick(), "小测")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ScrubTest(unittest.TestCase):
    """N-25：脱敏纯函数——合成语料上验证丢行/替换/计数。"""

    @classmethod
    def setUpClass(cls):
        cls.rules = scrub_brain._Rules()
        # 合成身份名（绝不借用真实称呼）
        cls.id_names = ["合成甲", "合成甲~1"]
        cls.other_names = ["合成乙"]

    def _scrub(self, data):
        return scrub_brain.scrub_brain_data(
            data, self.rules, self.id_names, self.other_names)

    def test_id_names_replaced_with_placeholders(self):
        data = {"persona": {"name": "合成甲", "user": "合成甲"},
                "kw": {"你好": [["合成甲在吗", 3]]}}
        out, rep = self._scrub(data)
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("合成甲", blob)
        self.assertIn(scrub_brain.PLACEHOLDER_PET, blob)
        self.assertEqual(out["persona"]["name"], scrub_brain.PLACEHOLDER_PET)
        self.assertEqual(out["kw"]["你好"][0][0],
                         "%s在吗" % scrub_brain.PLACEHOLDER_PET)
        self.assertGreater(rep["kept"], 0)

    def test_identity_word_drops_sentence(self):
        data = {"kw": {"学校": [["我在学校等你", 1]], "天气": [["今天好热", 2]]}}
        out, rep = self._scrub(data)
        self.assertNotIn("学校", out.get("kw") or {})
        self.assertIn("天气", out.get("kw") or {})

    def test_name_heuristic_drops_sentence(self):
        data = {"intents": {"greet": ["李晓明你好", "嗨嗨"]}}
        out, _rep = self._scrub(data)
        self.assertEqual(out["intents"]["greet"], ["嗨嗨"])

    def test_phone_email_url_dropped(self):
        data = {"openers": ["打我电话13812345678", "看我主页 https://x.y/z",
                            "发我邮箱 a@b.com", "正常一句"]}
        out, _rep = self._scrub(data)
        self.assertEqual(out["openers"], ["正常一句"])

    def test_built_from_removed(self):
        data = {"built_from": "某某.txt", "kw": {}}
        out, _rep = self._scrub(data)
        self.assertNotIn("built_from", out)

    def test_other_private_word_drops(self):
        data = {"kw": {"标注": [["合成乙的句子", 1]]}}
        out, _rep = self._scrub(data)
        self.assertNotIn("标注", out.get("kw") or {})

    def test_kw_trigger_with_private_word_dropped(self):
        """触发词命中**其他私人词** → 连键丢弃并计数；身份名触发词则被替换。"""
        data = {"kw": {"合成乙": [["随便一句", 1]],
                       "合成甲": [["陪你聊天", 1]],
                       "没事": [["嗯", 1]]}}
        out, rep = self._scrub(data)
        self.assertNotIn("合成乙", out.get("kw") or {})
        self.assertIn("没事", out.get("kw") or {})
        self.assertGreater(rep["dropped"].get("kw 触发词", 0), 0)
        # 身份名触发词 → 占位符键（合成甲不在 persona.json 里，按宠物占位）
        self.assertIn(scrub_brain.PLACEHOLDER_PET, out.get("kw") or {})

    def test_keep_ratio_threshold(self):
        """保留率 < 40% 时 scrub() 退出码 2（不硬放行）——纯函数路径验证。"""
        # 触发词与句子全部合成"姓 + 1 字"姓名形态 → 全部命中启发式被丢
        surnames = ["李", "王", "张", "刘", "陈"]
        given = ["甲", "乙", "丙", "丁"]
        data = {"kw": {surnames[i % 5] + given[(i + 1) % 4]:
                       [[surnames[i % 5] + given[i % 4], 1]]
                       for i in range(20)}}
        _out, rep = self._scrub(data)
        ratio = rep["kept"] / rep["total"] if rep["total"] else 1.0
        self.assertLess(ratio, scrub_brain.MIN_KEEP_RATIO,
                        "全敏感语料的保留率必须低于门槛")


class VoicePolicyTest(unittest.TestCase):
    """N-26：外发包不带参考音频与 voice_id.json；consent 闸。"""

    def test_copy_tree_skips_audio_and_id_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="puppy_voice_"))
        try:
            src = tmp / "voices" / "profiles" / "档案甲"
            src.mkdir(parents=True)
            (src / "ref.wav").write_bytes(b"RIFF")
            (src / "profile.json").write_text("{}", encoding="utf-8")
            (tmp / "voices" / "voice_id.json").write_text("{}", encoding="utf-8")
            (tmp / "voices" / "active.json").write_text("{}", encoding="utf-8")
            dst = tmp / "out" / "voices"
            package_release._copy_tree(
                tmp / "voices", dst, [],
                skip_names={"voice_id.json"},
                skip_suffixes={".wav", ".mp3", ".flac", ".pcm", ".ogg", ".m4a"})
            self.assertFalse((dst / "profiles" / "档案甲" / "ref.wav").exists())
            self.assertFalse((dst / "voice_id.json").exists())
            self.assertTrue((dst / "profiles" / "档案甲" / "profile.json").exists())
            self.assertTrue((dst / "active.json").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @unittest.skipUnless(HAS_RELEASE_PERSONA, SKIP_NO_RELEASE_PERSONA)
    def test_consent_gate(self):
        """外发版未确认 → clone_allows 必须为 False；同意后放行。"""
        from unittest import mock
        tmp = Path(tempfile.mkdtemp(prefix="puppy_consent_"))
        try:
            per = tmp / "persona.json"
            per.write_text((PROG / "release" / "persona.json")
                           .read_text(encoding="utf-8"), encoding="utf-8")
            identity.bind(None, per)
            try:
                # 未同意（settings 无 voiceConsent）→ 需要确认
                with mock.patch.object(main, "load_config", return_value={}):
                    self.assertTrue(main._voice_consent_required())
                # 同意 → 不再要求
                with mock.patch.object(main, "load_config",
                                       return_value={"voiceConsent": True}):
                    self.assertFalse(main._voice_consent_required())
            finally:
                identity.bind(None, None)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_personal_edition_needs_no_consent(self):
        """个人版（edition != release）默认视为已确认，行为不变。"""
        identity.bind(None, PROG / "persona.json")
        try:
            self.assertFalse(main._voice_consent_required())
        finally:
            identity.bind(None, None)


if __name__ == "__main__":
    unittest.main()
