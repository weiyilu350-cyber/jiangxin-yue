"""载入链保住用户原文：真实解析器＋实际前端脚本，无新增依赖。"""
import copy
import json
import shutil
import struct
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import presets as PS
from app.triggers import COUNT_WORDS, QUALITY_WORDS, TriggerResolver

ROOT = Path(__file__).resolve().parent.parent
PARAMS = {"width": 768, "height": 1024, "steps": 21, "cfg": 3.5,
          "seed": 12345, "sampler_name": "heun", "scheduler": "karras"}


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = TemporaryDirectory(prefix="t013-", dir=ROOT / "tests")
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp = Path(self.tmpdir.name)
        self.table = self.tmp / "table.json"
        self.overrides = self.tmp / "overrides.json"
        self.table.write_text(json.dumps({"pen_a": "@kanzar1n", "pen_b": "blue_archive"}),
                              encoding="utf-8")
        self.resolver = TriggerResolver(self.table, self.tmp, self.overrides)
        self.recipe = {"id": "r_aabb0013", "name": "测试配方", "unet": "base.safetensors",
                       "loras": [{"name": "pen_a.safetensors", "strength": 0.7}],
                       "positive": "@kanzar1n, 1girl, cafe", "negative": "预设负词",
                       "params": dict(PARAMS)}

    def load(self, mode="params", overrides=None, resolver=None):
        return PS.build_load(self.recipe, overrides or {}, "anima_tri", mode, 3,
                             ["base.safetensors"],
                             resolve_triggers=resolver or self.resolver.for_loras)

    def test_params_returns_parameters_and_resolved_triggers_only(self):
        self.assertEqual(self.load(), {"mode": "params", "params": PARAMS,
                                      "triggers": ["@kanzar1n"]})

    def test_triggers_follow_recipe_slot_order_including_zero_strength(self):
        self.recipe["loras"] = [{"name": "pen_b.safetensors", "strength": 0}, {},
                                {"name": "pen_a.safetensors", "strength": 0.7}]
        self.assertEqual(self.load()["triggers"], ["blue_archive", "@kanzar1n"])

    def test_profile_override_selects_its_own_pens_and_parameters(self):
        over = {"anima_tri": {self.recipe["id"]: {
            "loras": [{"name": "pen_b.safetensors", "strength": 0.5}],
            "params": {"steps": 17}}}}
        got = self.load(overrides=over)
        self.assertEqual(got["triggers"], ["blue_archive"])
        self.assertEqual(got["params"], dict(PARAMS, steps=17))

    def test_manual_override_wins_over_table(self):
        self.overrides.write_text('{"pen_a": "manual_trigger"}', encoding="utf-8")
        self.assertEqual(self.load()["triggers"], ["manual_trigger"])

    def test_explicit_empty_override_does_not_fall_back(self):
        self.overrides.write_text('{"pen_a": ""}', encoding="utf-8")
        self.assertEqual(self.load()["triggers"], [])

    def test_metadata_fallback_is_used_for_new_pen(self):
        header = json.dumps({"__metadata__": {"trigger_words": "metadata_trigger"}}).encode()
        (self.tmp / "new_pen.safetensors").write_bytes(struct.pack("<Q", len(header)) + header)
        self.recipe["loras"] = [{"name": "new_pen.safetensors", "strength": 0.7}]
        self.assertEqual(self.load()["triggers"], ["metadata_trigger"])

    def test_no_pens_means_no_triggers_even_when_saved_prompt_has_them(self):
        self.recipe["loras"] = []
        self.assertEqual(self.load()["triggers"], [])

    def test_loading_does_not_mutate_recipe_or_parameters(self):
        before = copy.deepcopy(self.recipe)
        got = self.load()
        got["params"]["steps"] = 1
        got["triggers"].append("extra")
        self.assertEqual(self.recipe, before)

    def test_full_payload_matches_old_contract_without_resolving_triggers(self):
        def forbidden(_pens):
            self.fail("整套载入不该新增触发词处理")
        self.assertEqual(self.load("full", resolver=forbidden), {
            "mode": "full", "positive": self.recipe["positive"],
            "negative": self.recipe["negative"], "params": PARAMS,
            "unet": self.recipe["unet"], "loras": self.recipe["loras"],
        })

    def test_endpoint_uses_existing_resolver_and_keeps_storage_read_only(self):
        from app import runner, settings as ST
        # 导入 main 不得重生在用的 token，也不得对真实炉子做启动对账。
        real_token, real_reconcile = ST.load_token, runner.reconcile
        ST.load_token, runner.reconcile = lambda: "unit-test-only", lambda *_a, **_k: {}
        try:
            from app import main
        finally:
            ST.load_token, runner.reconcile = real_token, real_reconcile
        from fastapi.testclient import TestClient
        path = self.tmp / "presets.json"
        PS.save_presets(path, {"recipes": [self.recipe], "overrides": {}})
        before = path.read_bytes()
        saved = main.PRESETS_FILE, main.TRIGGERS, main.preset_ctx
        main.PRESETS_FILE, main.TRIGGERS = path, self.resolver
        main.preset_ctx = lambda _pid: ("anima_tri", 3, ["base.safetensors"])
        try:
            client = TestClient(main.app, base_url="http://127.0.0.1:8788",
                                headers={"Authorization": "Bearer " + main.TOKEN})
            response = client.post("/api/presets/r_aabb0013/load",
                                   json={"profile_id": "anima_tri", "mode": "params"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), self.load())
            self.assertEqual(path.read_bytes(), before)
        finally:
            main.PRESETS_FILE, main.TRIGGERS, main.preset_ctx = saved


JS_HARNESS = r"""
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const writes = [], elements = {};
const values = Object.assign({profile:'fixture', unet:'current_base', lora0:'current_pen',
  lorastr0:'0.35', positive:'1girl, cat_ears, rain', negative:' 用户负词\n  bad_hands  ',
  width:'832', height:'1216', steps:'30', cfg:'4', seed:'-1', sampler_name:'euler',
  scheduler:'simple', count:'3', 'size-preset':'自定义'}, input.fields);
for (const [id, initial] of Object.entries(values)) {
  let value = initial;
  elements[id] = {options:['current_base','preset_base','current_pen','preset_pen',''].map(value=>({value}))};
  Object.defineProperty(elements[id], 'value', {get:()=>value, set:v=>{
    writes.push(id); value=String(v);
  }});
}
const context = vm.createContext({window:{HUAFANG_TOKEN:'unit-test-only'},
  document:{getElementById:id=>elements[id]}, input});
vm.runInContext(fs.readFileSync(input.source, 'utf8'), context);
vm.runInContext("BOOT={profiles:[{id:'fixture',lora_slots:1}]};", context);
const misses = vm.runInContext('applyPayload(input.payload)', context);
if (input.repeat) vm.runInContext('applyPayload(input.payload)', context);
process.stdout.write(JSON.stringify({values:Object.fromEntries(Object.entries(elements).map(
  ([id,element])=>[id,element.value])),writes,misses}));
"""


@unittest.skipUnless(shutil.which("node"), "前端脚本测试需要现有 Node.js；不自动安装")
class FrontendTests(unittest.TestCase):
    def apply(self, positive="1girl, cat_ears, rain", triggers=None, **extra):
        payload = {"mode": "params", "params": PARAMS,
                   "triggers": ["@kanzar1n"] if triggers is None else triggers}
        payload.update(extra.pop("payload", {}))
        fields = {"positive": positive}
        fields.update(extra.pop("fields", {}))
        result = subprocess.run([shutil.which("node"), "-e", JS_HARNESS],
                                input=json.dumps({"source": str(ROOT / "static" / "app.js"),
                                                  "payload": payload, "fields": fields, **extra}),
                                text=True, encoding="utf-8", capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_user_prompt_survives_and_trigger_lands_after_count(self):
        self.assertEqual(self.apply()["values"]["positive"],
                         "1girl, @kanzar1n, cat_ears, rain")

    def test_case_underscore_and_internal_space_variants_are_deduplicated(self):
        for variant in ("@kanzar1n", "@KANZAR1N", "@kanzar 1n", "@kanzar_1n"):
            with self.subTest(variant=variant):
                got = self.apply(f"1girl, {variant}, cat_ears, @KANZAR1N, rain")
                self.assertEqual(got["values"]["positive"],
                                 "1girl, @kanzar1n, cat_ears, rain")

    def test_normal_tag_underscore_space_variants_match(self):
        got = self.apply("1girl, BLUE ARCHIVE, rain", ["blue_archive"])
        self.assertEqual(got["values"]["positive"], "1girl, blue_archive, rain")

    def test_user_spacing_newlines_and_weighted_words_are_preserved(self):
        text = "  1girl , \n(cat_ears:1.2)  , rain\t,\n用户原词 , \t "
        got = self.apply(text)["values"]["positive"]
        self.assertEqual(got, "  1girl , @kanzar1n, \n(cat_ears:1.2)  , rain\t,\n用户原词 , \t ")

    def test_negative_is_never_written_in_params_mode(self):
        got = self.apply(payload={"positive": "cafe", "negative": "预设负词"})
        self.assertEqual(got["values"]["negative"], " 用户负词\n  bad_hands  ")
        self.assertNotIn("negative", got["writes"])
        self.assertNotIn("cafe", got["values"]["positive"])

    def test_all_empty_triggers_leave_positive_byte_for_byte_without_writing(self):
        text = " \n 1girl, cat_ears ,雨  "
        got = self.apply(text, ["", "  ,  "])
        self.assertEqual(got["values"]["positive"], text)
        self.assertNotIn("positive", got["writes"])

    def test_parameters_change_but_base_pen_strength_and_count_do_not(self):
        got = self.apply(payload={"unet": "preset_base",
                                  "loras": [{"name": "preset_pen", "strength": 0.9}]})
        for key, expected in PARAMS.items():
            self.assertEqual(got["values"][key], str(expected))
        for key, expected in {"unet": "current_base", "lora0": "current_pen",
                              "lorastr0": "0.35", "count": "3"}.items():
            self.assertEqual(got["values"][key], expected)
            self.assertNotIn(key, got["writes"])

    def test_absent_seed_preserves_current_seed(self):
        params = {k: v for k, v in PARAMS.items() if k != "seed"}
        got = self.apply(payload={"params": params}, fields={"seed": "6789"})
        self.assertEqual(got["values"]["seed"], "6789")

    def test_slot_order_multiple_tokens_and_duplicates(self):
        got = self.apply("newest, 1girl, BLUE ARCHIVE, rain",
                         ["blue_archive, @kanzar1n", "BLUE ARCHIVE, third_trigger"])
        self.assertEqual(got["values"]["positive"],
                         "newest, 1girl, blue_archive, @kanzar1n, third_trigger, rain")

    def test_every_existing_quality_count_and_year_word_stays_before_B(self):
        for head in sorted(QUALITY_WORDS | COUNT_WORDS | {"year 2026"}):
            with self.subTest(head=head):
                got = self.apply(head + ", rain")
                self.assertEqual(got["values"]["positive"], head + ", @kanzar1n, rain")

    def test_nonmatching_triggers_substrings_and_weights_are_kept(self):
        text = "1girl, other_trigger, (@kanzar1n:1.2), @kanzar1n_extra, bluearchive"
        got = self.apply(text, ["@kanzar1n", "blue_archive"])
        self.assertEqual(got["values"]["positive"],
                         "1girl, @kanzar1n, blue_archive, other_trigger, (@kanzar1n:1.2), @kanzar1n_extra, bluearchive")

    def test_repeated_load_is_idempotent_with_and_without_head(self):
        for text in ("1girl, cat_ears, rain", "cat_ears, rain"):
            with self.subTest(text=text):
                once = self.apply(text)["values"]["positive"]
                self.assertEqual(self.apply(text, repeat=True)["values"]["positive"], once)

    def test_empty_positive_accepts_triggers_without_extra_comma(self):
        self.assertEqual(self.apply("")["values"]["positive"], "@kanzar1n")

    def test_full_mode_preserves_previous_replacement_behavior(self):
        payload = {"mode": "full", "unet": "preset_base",
                   "loras": [{"name": "preset_pen", "strength": 0.9}],
                   "positive": "@kanzar1n, 1girl, cafe", "negative": "预设负词"}
        got = self.apply(payload=payload)
        for key in ("positive", "negative", "unet"):
            self.assertEqual(got["values"][key], payload[key])
        self.assertEqual(got["values"]["lora0"], "preset_pen")
        self.assertEqual(got["values"]["lorastr0"], "0.9")


if __name__ == "__main__":
    unittest.main()
