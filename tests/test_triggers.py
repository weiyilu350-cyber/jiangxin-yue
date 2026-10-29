import json
import struct
import tempfile
import unittest
from pathlib import Path

from app.triggers import TriggerResolver, compose


def fake_lora(tmp: Path, name: str, metadata: dict) -> Path:
    """造一个只有头部的假 safetensors：_metadata_trigger 只读头部，够用。"""
    header = json.dumps({"__metadata__": metadata, "x.weight": {}}, ensure_ascii=False).encode()
    p = tmp / name
    p.write_bytes(struct.pack("<Q", len(header)) + header)
    return p


def resolver(table: dict, tmp: Path) -> TriggerResolver:
    tp = tmp / "笔触发词.json"
    tp.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    return TriggerResolver(tp, tmp)


class LookupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_exact_filename_wins(self):
        r = resolver({"Sample painting style": "Sample painting style"}, self.tmp)
        self.assertEqual(r.lookup("Sample painting style.safetensors"), "Sample painting style")

    def test_bare_name(self):
        r = resolver({"sample_trigger": "@example_author"}, self.tmp)
        self.assertEqual(r.lookup("sample_trigger"), "@example_author")

    def test_substring_fallback(self):
        r = resolver({"sample_style": "sample", "sample": "short", "pattern_": "pattern_tag"}, self.tmp)
        self.assertEqual(r.lookup("sample_style.safetensors"), "sample")
        self.assertEqual(r.lookup("sample_other.safetensors"), "short")
        self.assertEqual(r.lookup("pattern_v16_final.safetensors"), "pattern_tag")

    def test_longest_key_wins(self):
        r = resolver({"example-pen": "SHORT", "example-pen_v4.0": "LONG"}, self.tmp)
        self.assertEqual(r.lookup("example-pen_v4.0.safetensors"), "LONG")
        self.assertEqual(r.lookup("example-pen_v9.safetensors"), "SHORT")

    def test_explicit_empty_is_authoritative(self):
        """表里写空串＝这支笔明确无触发词，不许再往元数据兜。"""
        fake_lora(self.tmp, "sample_style_v2.safetensors", {"modelspec.trigger": "should_not_win"})
        r = resolver({"sample_style_v2": ""}, self.tmp)
        self.assertEqual(r.lookup("sample_style_v2.safetensors"), "")

    def test_metadata_explicit_trigger(self):
        fake_lora(self.tmp, "brand_new.safetensors", {"modelspec.trigger": "nb_style"})
        r = resolver({}, self.tmp)
        self.assertEqual(r.lookup("brand_new.safetensors"), "nb_style")

    def test_metadata_tag_frequency(self):
        fake_lora(self.tmp, "Sample metadata style.safetensors", {
            "ss_tag_frequency": json.dumps({"images": {
                "1girl": 999, "solo": 999, "sample_meta": 27, "long hair": 25,
            }}),
        })
        r = resolver({}, self.tmp)
        self.assertEqual(r.lookup("Sample metadata style.safetensors"), "sample_meta")

    def test_metadata_tie_prefers_filename_kin(self):
        fake_lora(self.tmp, "Sample painting style.safetensors", {
            "ss_tag_frequency": json.dumps({"d": {
                "sample painting style": 65, "breasts": 65, "1girl": 65,
            }}),
        })
        r = resolver({}, self.tmp)
        self.assertEqual(r.lookup("Sample painting style.safetensors"), "sample painting style")

    def test_unknown_and_unreadable_returns_empty(self):
        r = resolver({}, self.tmp)
        self.assertEqual(r.lookup("no_such_pen.safetensors"), "")
        (self.tmp / "broken.safetensors").write_bytes(b"\x01\x02")
        self.assertEqual(r.lookup("broken.safetensors"), "")
        self.assertEqual(r.lookup(""), "")

    def test_missing_table_file_does_not_raise(self):
        r = TriggerResolver(self.tmp / "gone.json", self.tmp)
        fake_lora(self.tmp, "pen.safetensors", {"modelspec.trigger": "t"})
        self.assertEqual(r.lookup("pen.safetensors"), "t")

    def test_for_loras_keeps_slot_order_and_skips_empty(self):
        r = resolver({"a": "ta", "b": "", "c": "tc"}, self.tmp)
        got = r.for_loras([
            {"name": "a.safetensors", "strength": 0.8},
            None,
            {"name": "b.safetensors", "strength": 0.5},
            {"name": "c.safetensors", "strength": 0.3},
        ])
        self.assertEqual(got, ["ta", "tc"])
        self.assertEqual(r.for_loras(None), [])


class ComposeTests(unittest.TestCase):
    def test_b_position_after_quality_and_count(self):
        got = compose("newest, safe, 1girl, solo, long hair", ["sample"])
        self.assertEqual(got, "newest, safe, 1girl, solo, sample, long hair")

    def test_no_head_words_inserts_at_front(self):
        self.assertEqual(compose("long hair, blue eyes", ["x"]), "x, long hair, blue eyes")

    def test_year_prefix_counts_as_head(self):
        self.assertEqual(compose("year 2024, newest, 1girl, dress", ["x"]),
                         "year 2024, newest, 1girl, x, dress")

    def test_slot_order_is_preserved(self):
        got = compose("newest, safe, 1girl, solo, dress", ["other_trigger", "sample", "third trigger"])
        self.assertEqual(got, "newest, safe, 1girl, solo, other_trigger, sample, third trigger, dress")

    def test_dedupe_against_user_text(self):
        """用户自己在正词里写过触发词，只留 B 位一份。"""
        got = compose("newest, 1girl, sample, solo, long hair", ["sample"])
        self.assertEqual(got, "newest, 1girl, solo, sample, long hair")

    def test_dedupe_across_slots(self):
        got = compose("1girl", ["@4x0style", "@4x0style", "k4s322"])
        self.assertEqual(got, "1girl, @4x0style, k4s322")

    def test_multi_token_trigger_keeps_inner_order(self):
        got = compose("1girl, solo, dress", ["blue_archive, game_cg, halo"])
        self.assertEqual(got, "1girl, solo, blue_archive, game_cg, halo, dress")

    def test_empty_positive(self):
        self.assertEqual(compose("", ["a", "b"]), "a, b")

    def test_no_triggers_leaves_text_alone(self):
        self.assertEqual(compose("newest, 1girl, dress", []), "newest, 1girl, dress")
        self.assertEqual(compose("newest, 1girl, dress", None), "newest, 1girl, dress")

    def test_dedupe_is_case_insensitive(self):
        self.assertEqual(compose("1girl, THIRD TRIGGER, dress", ["third trigger"]), "1girl, third trigger, dress")


class ComposeCleanupSpecTests(unittest.TestCase):
    """锁定 2026-10-27 更正后的 compose 清理口径（6 条）：
    只清「本次所选笔」触发词 token（归一比对），未选笔/普通词/笔名都不动；空触发词逐字不变。
    """

    def test_selected_trigger_deduped_to_b_position(self):
        # 点1：正词已含本次所选笔触发词 → 摘掉原位、只留 B 位一份
        got = compose("newest, 1girl, sample, solo, long hair", ["sample"])
        self.assertEqual(got, "newest, 1girl, solo, sample, long hair")
        self.assertEqual(got.lower().count("sample"), 1)

    def test_unselected_trigger_left_alone(self):
        # 点1：other_trigger 不是本次所选笔的触发词 → 原位保留，不清
        got = compose("newest, 1girl, solo, other_trigger, dress", ["sample"])
        self.assertEqual(got, "newest, 1girl, solo, sample, other_trigger, dress")

    def test_pen_name_is_not_a_trigger_left_alone(self):
        # 点3：笔名（lora 文件裸名 sample_style）不是触发词 → 不清
        got = compose(
            "masterpiece, best quality, 1girl, solo, looking at viewer, sample_style",
            ["sample"],
        )
        self.assertEqual(
            got,
            "masterpiece, best quality, 1girl, solo, sample, looking at viewer, sample_style",
        )

    def test_multi_token_trigger_cleaned_token_by_token_with_space_variant(self):
        # 点4：多 token 触发词逐 token 清；正词里的空格变体 'blue archive' 也算命中 'blue_archive'
        got = compose("1girl, blue archive, solo, dress", ["blue_archive, game_cg"])
        self.assertEqual(got, "1girl, solo, blue_archive, game_cg, dress")

    def test_case_and_underscore_variant_deduped(self):
        # 点4：大小写 + 下划线变体归一，跨写法只留 B 位一份（插入用触发词原写法）
        got = compose("1girl, Blue_Archive, dress", ["blue archive"])
        self.assertEqual(got, "1girl, blue archive, dress")

    def test_empty_trigger_pen_leaves_positive_verbatim(self):
        # 点5：空触发词的笔（D20）→ 正词逐字不变，连已存在的旧触发词也不动
        pos = "newest, 1girl, sample, solo, white dress"
        self.assertEqual(compose(pos, [""]), pos)
        self.assertEqual(compose(pos, []), pos)
        self.assertEqual(compose(pos, None), pos)


class SampleTableTests(unittest.TestCase):
    """合成笔柜覆盖有表项、明确空词、元数据兜底，不依赖私人触发词表。"""

    def test_every_sample_pen_resolves_without_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            fake_lora(root, "sample_style.safetensors", {})
            fake_lora(root, "empty.safetensors", {"modelspec.trigger": "ignored"})
            fake_lora(root, "metadata.safetensors", {"modelspec.trigger": "meta_tag"})
            r = resolver({"sample_style": "sample", "empty": ""}, root)
            names = sorted(p.name for p in root.glob("*.safetensors"))
            self.assertEqual(len(names), 3)
            for name in names:
                self.assertIsInstance(r.lookup(name), str, f"{name} 查触发词抛了")
            self.assertEqual(r.lookup("empty.safetensors"), "")
            self.assertEqual(r.lookup("metadata.safetensors"), "meta_tag")


if __name__ == "__main__":
    unittest.main()
