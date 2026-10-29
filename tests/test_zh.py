"""T004 单测：中文补全（本地词表）＋整句翻译（本机 Ollama，CPU）。
先写测试再写实现（卡片雷区）。Ollama 一律用假的出网函数顶掉，不引 mock 框架、不真联网。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from app import settings as ST
from app import zh as ZH

# 真表是 7 列：[en, 分类, 热度, 中文名, 中文别名(|), 英文别名(|), 中文说明]
ROWS = [
    ["grey_hair", 0, 8489348, "灰发", "灰色头发|银发|银灰色头发", "gray_hair", "灰色头发"],
    ["silver_hairband", 4, 971, "银色发箍", "银发箍|银色的发箍", "", "戴在头上的银色发箍"],
    ["1girl", 0, 8489348, "一个女孩", "单女|女性单人", "1girls", "画面中只有一位女性"],
    ["old", 0, 5000, "老人", "银发老人|上了年纪的人", "", "老年人"],
    ["sakata_gintoki_(cosplay)", 0, 10, "银时银发扮演", "", "", "坂田银时的银发 cos"],
    ["bad_row"],                                   # 形状不对，得跳过而不是崩
    ["empty_zh", 0, 1, "", "", "", ""],            # 中文名空，不进候选
    [None, 0, 1, "空英文", "", "", ""],            # 英文列不是字符串，跳过
]


def write_table(root: Path, rows=None, raw: str = None) -> Path:
    p = root / "zh_tags.json"
    if raw is not None:
        p.write_text(raw, encoding="utf-8")
    else:
        p.write_text(json.dumps(ROWS if rows is None else rows, ensure_ascii=False),
                     encoding="utf-8")
    return p


class FakeResponse:
    """假装是 httpx.Response：只给 zh.py 用到的三样。"""

    def __init__(self, status_code=200, payload=None, text=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json
        self.text = text if text is not None else json.dumps(payload or {}, ensure_ascii=False)

    def json(self):
        if self._bad_json:
            raise ValueError("不是 JSON")
        if self._payload is None:
            raise ValueError("没有 JSON 体")
        return self._payload


def fake_post(*replies):
    """按序回话；元素是 Exception 就当场抛（用来演连接失败/超时）。"""
    calls = []
    it = iter(replies)

    def poster(url, payload, timeout):
        calls.append({"url": url, "payload": payload, "timeout": timeout})
        item = next(it)
        if isinstance(item, BaseException):
            raise item
        return item

    return poster, calls


def fake_probe(answer):
    def prober(url, timeout):
        return answer
    return prober


def service(table_path, cfg=None, *replies, **kw):
    base = {"enabled": True, "ollama_url": "http://127.0.0.1:11434",
            "model": "qwen3.5:9b", "timeout_sec": 20, "table": str(table_path)}
    base.update(cfg or {})
    poster, calls = fake_post(*(replies or [FakeResponse(200, {"response": "silver_hair"})]))
    svc = ZH.ZhService(base, poster=poster, prober=kw.get("prober", fake_probe(True)))
    return svc, calls


def resp(text):
    return FakeResponse(200, {"response": text})


class TableTests(unittest.TestCase):
    """雷区 1：18 MB 词表必须懒加载＋进程内缓存；读不到当空表，不崩。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_table_is_empty(self):
        t = ZH.ZhTable(self.root / "没有这个表.json")
        self.assertEqual(t.complete("银发"), [])
        self.assertEqual(t.complete("银发"), [])   # 再查一次也不崩

    def test_corrupt_table_is_empty(self):
        t = ZH.ZhTable(write_table(self.root, raw='[["grey_hair", 0, 1, "灰发", "银'))
        self.assertEqual(t.complete("银发"), [])

    def test_garbage_table_is_empty(self):
        t = ZH.ZhTable(write_table(self.root, raw="这不是 JSON"))
        self.assertEqual(t.complete("银发"), [])

    def test_non_list_table_is_empty(self):
        t = ZH.ZhTable(write_table(self.root, raw='{"a": 1}'))
        self.assertEqual(t.complete("灰发"), [])

    def test_bad_rows_skipped_not_fatal(self):
        t = ZH.ZhTable(write_table(self.root))
        got = t.complete("灰发")
        self.assertEqual(got, [{"zh": "灰发", "en": "grey_hair"}])
        self.assertEqual(t.complete("空英文"), [])   # 英文列不是字符串 → 不进候选
        self.assertEqual(t.complete("bad_row"), [])

    def test_lazy_load_then_cached(self):
        p = write_table(self.root)
        t = ZH.ZhTable(p)
        self.assertEqual(t.size(), 0)                # 还没读盘
        first = t.complete("灰发")
        self.assertTrue(first)
        self.assertGreater(t.size(), 0)
        # 换掉盘上的表：缓存住了就不该看见新内容（雷区 1 的「每次请求重读会卡死服务」）
        write_table(self.root, rows=[["white_hair", 0, 1, "白发", "灰发", "", ""]])
        self.assertEqual(t.complete("灰发"), first)

    def test_prefix_hits_before_substring_hits(self):
        t = ZH.ZhTable(write_table(self.root))
        ens = [x["en"] for x in t.complete("银发", limit=20)]
        self.assertEqual(ens[0], "grey_hair")        # 「银发」是它的别名，前缀命中
        self.assertIn("silver_hairband", ens)        # 「银发箍」前缀命中
        self.assertIn("old", ens)                    # 「银发老人」前缀命中
        self.assertEqual(ens[-1], "sakata_gintoki_(cosplay)")  # 只子串命中的排最后

    def test_empty_query_returns_empty(self):
        t = ZH.ZhTable(write_table(self.root))
        self.assertEqual(t.complete(""), [])
        self.assertEqual(t.complete("   "), [])
        self.assertEqual(t.complete(None), [])

    def test_dedup_pairs(self):
        rows = [["a_tag", 0, 9, "同一个中文", "", "", ""],
                ["a_tag", 0, 8, "同一个中文", "", "", ""],
                ["b_tag", 0, 7, "同一个中文", "同一个中文", "", ""]]
        t = ZH.ZhTable(write_table(self.root, rows=rows))
        got = t.complete("同一个中文")
        self.assertEqual(got, [{"zh": "同一个中文", "en": "a_tag"},
                               {"zh": "同一个中文", "en": "b_tag"}])  # 别名与主名重复只算一条

    def test_limit_respected(self):
        t = ZH.ZhTable(write_table(self.root))
        self.assertEqual(len(t.complete("银发", limit=1)), 1)
        self.assertEqual(len(t.complete("银发", limit=2)), 2)
        self.assertLessEqual(len(t.complete("银发", limit=9999)), 100)
        self.assertEqual(len(t.complete("银发", limit=0)), 1)   # 非法 limit 兜到 1

    def test_ascii_query_still_matches(self):
        t = ZH.ZhTable(write_table(self.root))
        self.assertEqual(t.complete("gray_hair"), [])   # 只按中文查，英文串不匹配中文列


class CleanOutputTests(unittest.TestCase):
    """雷区 2／6：解释、markdown、引号、中文残留都得剥掉。"""

    def assertNoCjk(self, s):
        self.assertFalse(ZH.has_cjk(s), f"结果里还有中文：{s!r}")

    def test_strips_markdown_and_explanation(self):
        out = ZH.clean_output("**silver_hair**, 1girl\n解释：这是银发的意思")
        self.assertEqual(out, "silver_hair, 1girl")
        self.assertNoCjk(out)

    def test_strips_code_fence(self):
        self.assertEqual(ZH.clean_output("```\nsilver_hair, rain\n```"), "silver_hair, rain")

    def test_strips_label_and_quotes(self):
        self.assertEqual(ZH.clean_output('英文："silver hair"'), "silver hair")
        self.assertEqual(ZH.clean_output("English: 'standing in rain'"), "standing in rain")

    def test_strips_list_markers(self):
        self.assertEqual(ZH.clean_output("- silver_hair\n- 1girl\n* rain"), "silver_hair, 1girl, rain")

    def test_drops_inline_cjk_residue(self):
        out = ZH.clean_output("silver_hair（银发）, 1girl")
        self.assertNoCjk(out)
        self.assertIn("silver_hair", out)
        self.assertIn("1girl", out)

    def test_pure_chinese_explanation_becomes_empty(self):
        self.assertEqual(ZH.clean_output("这段中文的意思是银发的少女站在雨里"), "")

    def test_tidy_commas_and_spaces(self):
        self.assertEqual(ZH.clean_output("silver hair , ,  standing"), "silver hair, standing")
        self.assertEqual(ZH.clean_output(",silver hair,"), "silver hair")

    def test_json_blob_stripped(self):
        out = ZH.clean_output('{"tag": "silver_hair"}')
        self.assertNoCjk(out)
        self.assertNotIn("{", out)
        self.assertNotIn("}", out)


class SegmentTests(unittest.TestCase):
    """雷区 3：只翻中文片段，英文 tag／@画师／触发词一律不进模型。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.svc, _ = service(write_table(Path(self._tmp.name)))

    def tearDown(self):
        self._tmp.cleanup()

    def test_only_chinese_segments(self):
        self.assertEqual(self.svc.segments("sample 与 @example_artist 一起喝茶"), ["与", "一起喝茶"])

    def test_english_only_has_no_segments(self):
        self.assertEqual(self.svc.segments("1girl, solo, silver hair"), [])

    def test_segments_dedup_keep_order(self):
        self.assertEqual(self.svc.segments("银发, sample, 站在雨里, 银发"), ["银发", "站在雨里"])

    def test_chinese_punctuation_stays_inside_segment(self):
        # 与早期版本同口径：整句一起送模型，上下文不丢，翻出来才像话
        self.assertEqual(self.svc.segments("银发，站在雨里"), ["银发，站在雨里"])

    def test_latin_never_swallowed_by_segment(self):
        self.assertEqual(self.svc.segments("银发 sample 站在雨里"), ["银发", "站在雨里"])


class TranslateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_payload_shape(self):
        svc, calls = service(write_table(self.root), None, resp("silver_hair"))
        svc.translate("银发")
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c["url"], "http://127.0.0.1:11434/api/generate")
        self.assertEqual(c["timeout"], 20)
        p = c["payload"]
        self.assertEqual(p["model"], "qwen3.5:9b")
        self.assertFalse(p["stream"])
        self.assertFalse(p["think"])
        self.assertEqual(p["options"], {"temperature": 0, "num_predict": 256, "num_gpu": 0})
        self.assertIn("银发", p["prompt"])

    def test_no_chinese_means_no_call(self):
        svc, calls = service(write_table(self.root))
        out = svc.translate("1girl, solo, sample")
        self.assertEqual(out, {"text": "1girl, solo, sample", "changed": False})
        self.assertEqual(calls, [])

    def test_trigger_words_preserved_verbatim(self):
        svc, calls = service(write_table(self.root), None, resp("silver_hair young_girl"))
        out = svc.translate("1girl, sample, @example_artist, 一个银发少女")
        self.assertIn("sample", out["text"])
        self.assertIn("@example_artist", out["text"])       # @ 一个字符都不能少
        self.assertIn("1girl", out["text"])
        self.assertIn("silver_hair young_girl", out["text"])
        self.assertFalse(ZH.has_cjk(out["text"]))
        self.assertTrue(out["changed"])
        # 模型只看见中文片段，触发词根本没送去
        self.assertEqual(len(calls), 1)
        self.assertNotIn("sample", calls[0]["payload"]["prompt"])
        self.assertNotIn("example_artist", calls[0]["payload"]["prompt"])

    def test_each_segment_translated_once(self):
        svc, calls = service(write_table(self.root), None, resp("silver_hair"), resp("standing in rain"))
        out = svc.translate("银发, 站在雨里, 银发")
        self.assertEqual(len(calls), 2)              # 重复片段只翻一次
        self.assertEqual(out["text"].count("silver_hair"), 2)   # 两处都换掉了
        self.assertIn("standing in rain", out["text"])
        self.assertFalse(ZH.has_cjk(out["text"]))

    def test_model_echoed_chinese_is_stripped(self):
        svc, _ = service(write_table(self.root), None, resp("silver_hair 银发"))
        out = svc.translate("银发")
        self.assertFalse(ZH.has_cjk(out["text"]))    # 雷区 2：结果不许有中文
        self.assertIn("silver_hair", out["text"])

    def test_fullwidth_comma_outside_segment_becomes_ascii(self):
        svc, calls = service(write_table(self.root), None, resp("silver_hair"))
        out = svc.translate("1girl，银发")
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["text"], "1girl, silver_hair")   # 全角逗号不会把两个 tag 粘住
        self.assertFalse(ZH.has_cjk(out["text"]))

    def test_timeout_override(self):
        svc, calls = service(write_table(self.root), None, resp("silver_hair"))
        svc.translate("银发", timeout=3)
        self.assertEqual(calls[0]["timeout"], 3)

    def test_connect_error_is_zherror(self):
        svc, _ = service(write_table(self.root), None,
                         httpx.ConnectError("Connection refused"))
        with self.assertRaises(ZH.ZhError) as cm:
            svc.translate("银发")
        self.assertEqual(cm.exception.code, "ollama_down")
        self.assertTrue(cm.exception.human)

    def test_timeout_is_zherror(self):
        svc, _ = service(write_table(self.root), None, httpx.ReadTimeout("timed out"))
        with self.assertRaises(ZH.ZhError) as cm:
            svc.translate("银发")
        self.assertEqual(cm.exception.code, "ollama_timeout")
        self.assertIn("20", cm.exception.human)      # 人话里带上超时秒数

    def test_empty_response_is_zherror(self):
        svc, _ = service(write_table(self.root), None, resp("   "))
        with self.assertRaises(ZH.ZhError) as cm:
            svc.translate("银发")
        self.assertEqual(cm.exception.code, "empty_result")

    def test_explanation_only_response_is_zherror(self):
        svc, _ = service(write_table(self.root), None, resp("这个词的意思是银发"))
        with self.assertRaises(ZH.ZhError) as cm:
            svc.translate("银发")
        self.assertEqual(cm.exception.code, "empty_result")

    def test_missing_model_is_human(self):
        svc, _ = service(write_table(self.root), None,
                         FakeResponse(404, text='{"error":"model \'qwen3.5:9b\' not found"}'))
        with self.assertRaises(ZH.ZhError) as cm:
            svc.translate("银发")
        self.assertEqual(cm.exception.code, "model_missing")
        self.assertIn("qwen3.5:9b", cm.exception.human)

    def test_http_500_is_human(self):
        svc, _ = service(write_table(self.root), None, FakeResponse(500, text="炸了"))
        with self.assertRaises(ZH.ZhError) as cm:
            svc.translate("银发")
        self.assertEqual(cm.exception.code, "ollama_http")
        self.assertIn("500", cm.exception.human)

    def test_bad_json_body_is_human(self):
        svc, _ = service(write_table(self.root), None, FakeResponse(200, bad_json=True))
        with self.assertRaises(ZH.ZhError) as cm:
            svc.translate("银发")
        self.assertEqual(cm.exception.code, "ollama_bad_json")

    def test_disabled_does_not_call_out(self):
        svc, calls = service(write_table(self.root), {"enabled": False})
        with self.assertRaises(ZH.ZhError) as cm:
            svc.translate("银发")
        self.assertEqual(cm.exception.code, "zh_disabled")
        self.assertEqual(calls, [])


class ConfigTests(unittest.TestCase):
    def test_missing_fields_use_defaults(self):
        svc = ZH.ZhService({"table": "x.json"})
        self.assertEqual(svc.model, ZH.DEFAULTS["model"])
        self.assertEqual(svc.timeout, ZH.DEFAULTS["timeout_sec"])
        self.assertEqual(svc.url, ZH.DEFAULTS["ollama_url"])
        self.assertTrue(svc.enabled)

    def test_junk_config_does_not_crash(self):
        svc = ZH.ZhService("不是字典")
        self.assertEqual(svc.model, ZH.DEFAULTS["model"])
        svc2 = ZH.ZhService({"timeout_sec": "二十秒"})
        self.assertEqual(svc2.timeout, ZH.DEFAULTS["timeout_sec"])

    def test_status_shape(self):
        svc, _ = service(Path("x.json"), None, prober=fake_probe(True))
        self.assertEqual(svc.status(), {"ollama": True, "model": "qwen3.5:9b", "timeout": 20})

    def test_status_false_when_down(self):
        svc, _ = service(Path("x.json"), None, prober=fake_probe(False))
        self.assertFalse(svc.status()["ollama"])

    def test_status_false_when_disabled(self):
        svc, _ = service(Path("x.json"), {"enabled": False}, prober=fake_probe(True))
        self.assertFalse(svc.status()["ollama"])

    def test_status_never_raises(self):
        def boom(url, timeout):
            raise RuntimeError("探针自己炸了")
        svc = ZH.ZhService({"table": "x.json"}, prober=boom)
        self.assertEqual(svc.status()["ollama"], False)


class SettingsToleranceTests(unittest.TestCase):
    """卡 §3：settings.json 缺字段用默认；配置损坏不崩。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._real = ST.SETTINGS_FILE
        ST.SETTINGS_FILE = Path(self._tmp.name) / "settings.json"

    def tearDown(self):
        ST.SETTINGS_FILE = self._real
        self._tmp.cleanup()

    def test_missing_file_gets_defaults(self):
        s = ST.load_settings()
        self.assertEqual(s["zh"]["model"], ST.DEFAULTS["zh"]["model"])
        self.assertTrue(s["zh"]["enabled"])

    def test_partial_zh_block_filled(self):
        ST.SETTINGS_FILE.write_text(json.dumps({"zh": {"model": "别的模型"}}), encoding="utf-8")
        s = ST.load_settings()
        self.assertEqual(s["zh"]["model"], "别的模型")
        self.assertEqual(s["zh"]["timeout_sec"], ST.DEFAULTS["zh"]["timeout_sec"])
        self.assertEqual(s["zh"]["table"], ST.DEFAULTS["zh"]["table"])

    def test_corrupt_settings_does_not_crash_or_rewrite(self):
        ST.SETTINGS_FILE.write_text('{"zh": {"model": "x"', encoding="utf-8")
        before = ST.SETTINGS_FILE.read_bytes()
        s = ST.load_settings()
        self.assertEqual(s["comfy_url"], ST.DEFAULTS["comfy_url"])
        self.assertEqual(s["zh"]["model"], ST.DEFAULTS["zh"]["model"])
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)   # 绝不改写用户的坏文件

    def test_non_dict_settings_is_ignored(self):
        ST.SETTINGS_FILE.write_text("[1, 2, 3]", encoding="utf-8")
        s = ST.load_settings()
        self.assertEqual(s["port"], ST.DEFAULTS["port"])

    def test_zh_block_not_a_dict_is_ignored(self):
        ST.SETTINGS_FILE.write_text(json.dumps({"zh": "开着"}), encoding="utf-8")
        s = ST.load_settings()
        self.assertEqual(s["zh"], ST.DEFAULTS["zh"])


class ZhApiTests(unittest.TestCase):
    """端点层：失败也回 200 ＋ ok=false ＋ 人话，绝不吐 500 拦住出图（雷区 4）。"""

    @classmethod
    def setUpClass(cls):
        from app.settings import TOKEN_FILE
        cls._token_file = TOKEN_FILE
        cls._token_before = (TOKEN_FILE.read_text(encoding="utf-8")
                             if TOKEN_FILE.exists() else None)
        from fastapi.testclient import TestClient
        from app import main
        cls.main = main
        cls.client = TestClient(main.app, base_url="http://127.0.0.1:8788",
                                headers={"Authorization": "Bearer " + main.TOKEN})

    @classmethod
    def tearDownClass(cls):
        if cls._token_before is not None:
            cls._token_file.write_text(cls._token_before, encoding="utf-8")

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._real_zh = self.main.ZH

    def tearDown(self):
        self.main.ZH = self._real_zh
        self._tmp.cleanup()

    def wire(self, *replies, **kw):
        svc, calls = service(write_table(self.root), kw.get("cfg"),
                             *(replies or [resp("silver_hair")]),
                             prober=kw.get("prober", fake_probe(True)))
        self.main.ZH = svc
        return svc, calls

    def test_needs_token(self):
        r = self.client.get("/api/zh/status", headers={"Authorization": "Bearer wrong-token"})
        self.assertEqual(r.status_code, 401)

    def test_status(self):
        self.wire()
        body = self.client.get("/api/zh/status").json()
        self.assertEqual(body, {"ollama": True, "model": "qwen3.5:9b", "timeout": 20})

    def test_complete(self):
        self.wire()
        items = self.client.get("/api/zh/complete", params={"q": "银发"}).json()["items"]
        self.assertTrue(items)
        self.assertEqual(items[0]["en"], "grey_hair")
        self.assertEqual(set(items[0]), {"zh", "en"})

    def test_complete_empty_query(self):
        self.wire()
        self.assertEqual(self.client.get("/api/zh/complete").json(), {"items": []})

    def test_complete_limit_param(self):
        self.wire()
        items = self.client.get("/api/zh/complete", params={"q": "银发", "limit": "1"}).json()["items"]
        self.assertEqual(len(items), 1)
        bad = self.client.get("/api/zh/complete", params={"q": "银发", "limit": "很多"}).json()["items"]
        self.assertTrue(bad)   # 非法 limit 不 422、不 500，兜到默认

    def test_translate_ok(self):
        self.wire(resp("silver_hair young_girl"))
        r = self.client.post("/api/zh/translate", json={"text": "一个银发少女"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["changed"])
        self.assertFalse(ZH.has_cjk(body["text"]))

    def test_translate_no_chinese(self):
        self.wire()
        body = self.client.post("/api/zh/translate", json={"text": "1girl, solo"}).json()
        self.assertEqual(body, {"ok": True, "text": "1girl, solo", "changed": False})

    def test_translate_failure_is_200_not_500(self):
        self.wire(httpx.ConnectError("Connection refused"))
        r = self.client.post("/api/zh/translate", json={"text": "一个银发少女"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertFalse(body["changed"])
        self.assertEqual(body["text"], "一个银发少女")     # 原文原样退回，前端好回滚
        self.assertEqual(body["error"]["code"], "ollama_down")
        self.assertTrue(body["error"]["human"])

    def test_translate_missing_text_is_400(self):
        self.wire()
        r = self.client.post("/api/zh/translate", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("human", r.json()["detail"])

    def test_translate_text_too_long_is_400(self):
        self.wire()
        r = self.client.post("/api/zh/translate", json={"text": "银" * 5001})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "text_too_long")


if __name__ == "__main__":
    unittest.main()
