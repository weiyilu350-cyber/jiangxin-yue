"""T002 预设单测：先按雷区写测试，再写实现（任务卡 §已知雷区）。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import presets as PS

SETTINGS = {"default_unet": "anima-base-v1.0.safetensors"}
UNETS = ["anima-base-v1.0.safetensors", "anima-hires.safetensors"]
PEN1 = {"name": "sample_style.safetensors", "strength": 0.9}
PEN2 = {"name": "sample_style.safetensors", "strength": 0.8}
PARAMS = {
    "width": 832, "height": 1216, "steps": 30, "cfg": 4.0,
    "seed": -1, "sampler_name": "euler", "scheduler": "simple",
}


def recipe(name="甲", loras=None, unet=UNETS[0], params=None, rid=None):
    return {
        "id": rid or PS.gen_recipe_id(),
        "name": name,
        "unet": unet,
        "loras": list(loras or []),
        "positive": "1girl, solo",
        "negative": "worst quality",
        "params": dict(params or PARAMS),
        "created_at": "2026-10-27T19:00:00",
    }


class DiskTests(unittest.TestCase):
    """雷区 1：文件缺失/损坏/半截 JSON → 空表，不崩、不覆盖；写入原子。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "presets.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_gives_empty_table(self):
        self.assertEqual(PS.load_presets(self.path), {"recipes": [], "overrides": {}})
        self.assertFalse(self.path.exists())

    def test_corrupt_json_gives_empty_table(self):
        self.path.write_text("{{{ 不是 JSON", encoding="utf-8")
        self.assertEqual(PS.load_presets(self.path), {"recipes": [], "overrides": {}})

    def test_half_written_json_gives_empty_table(self):
        self.path.write_text('{"recipes": [{"id": "r_1"', encoding="utf-8")
        self.assertEqual(PS.load_presets(self.path), {"recipes": [], "overrides": {}})

    def test_load_never_touches_the_broken_file(self):
        broken = '{"recipes": [{"id": "r_1"'
        self.path.write_text(broken, encoding="utf-8")
        PS.load_presets(self.path)
        self.assertEqual(self.path.read_text(encoding="utf-8"), broken)

    def test_json_that_is_not_a_dict_gives_empty_table(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(PS.load_presets(self.path), {"recipes": [], "overrides": {}})

    def test_wrong_typed_sections_are_dropped(self):
        self.path.write_text(
            json.dumps({"recipes": "not-a-list", "overrides": [1]}), encoding="utf-8"
        )
        self.assertEqual(PS.load_presets(self.path), {"recipes": [], "overrides": {}})

    def test_missing_overrides_section_defaults_to_empty(self):
        r = recipe()
        self.path.write_text(json.dumps({"recipes": [r]}), encoding="utf-8")
        got = PS.load_presets(self.path)
        self.assertEqual(got["overrides"], {})
        self.assertEqual(got["recipes"][0]["id"], r["id"])

    def test_save_load_roundtrip(self):
        data = {"recipes": [recipe()], "overrides": {"anima_dual": {}}}
        PS.save_presets(self.path, data)
        self.assertEqual(PS.load_presets(self.path), data)

    def test_save_keeps_backup_of_previous_content(self):
        first = {"recipes": [recipe("旧")], "overrides": {}}
        PS.save_presets(self.path, first)
        PS.save_presets(self.path, {"recipes": [], "overrides": {}})
        bak = self.path.with_name("presets.json.bak")
        self.assertTrue(bak.exists())
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8")), first)

    def test_save_leaves_no_temp_file_behind(self):
        PS.save_presets(self.path, {"recipes": [], "overrides": {}})
        self.assertEqual(
            sorted(p.name for p in self.dir.iterdir()), ["presets.json"]
        )

    def test_save_creates_missing_parent_dir(self):
        deep = self.dir / "data" / "presets.json"
        PS.save_presets(deep, {"recipes": [recipe()], "overrides": {}})
        self.assertEqual(len(PS.load_presets(deep)["recipes"]), 1)

    def test_saved_text_keeps_chinese_readable(self):
        PS.save_presets(self.path, {"recipes": [recipe("双笔")], "overrides": {}})
        self.assertIn("双笔", self.path.read_text(encoding="utf-8"))


class NameConflictTests(unittest.TestCase):
    """雷区 2：同名不覆盖，去空格＋大小写归一后判重。"""

    def setUp(self):
        self.data = {"recipes": [], "overrides": {}}

    def add(self, name, **kw):
        return PS.add_recipe(self.data, name=name, unet=UNETS[0], loras=[PEN1],
                             positive="1girl", negative="", params=PARAMS, **kw)

    def test_first_save_goes_through(self):
        r = self.add("甲")
        self.assertEqual(r["name"], "甲")
        self.assertEqual(len(self.data["recipes"]), 1)

    def test_empty_name_rejected(self):
        with self.assertRaises(PS.PresetError) as cm:
            self.add("")
        self.assertEqual(cm.exception.code, "empty_name")
        self.assertTrue(cm.exception.human)
        self.assertEqual(self.data["recipes"], [])

    def test_whitespace_only_name_rejected(self):
        with self.assertRaises(PS.PresetError) as cm:
            self.add("   ")
        self.assertEqual(cm.exception.code, "empty_name")

    def test_name_is_stripped(self):
        self.assertEqual(self.add("  乙  ")["name"], "乙")

    def test_same_name_rejected_not_overwritten(self):
        self.add("甲")
        with self.assertRaises(PS.PresetError) as cm:
            self.add("甲")
        self.assertEqual(cm.exception.code, "dup_name")
        self.assertIn("甲", cm.exception.human)
        self.assertEqual(len(self.data["recipes"]), 1)

    def test_dupe_check_ignores_case_and_spaces(self):
        self.add("Blue")
        with self.assertRaises(PS.PresetError) as cm:
            self.add("  bLuE ")
        self.assertEqual(cm.exception.code, "dup_name")

    def test_builtin_default_name_is_reserved(self):
        with self.assertRaises(PS.PresetError) as cm:
            self.add("默认", reserved_names=("默认",))
        self.assertEqual(cm.exception.code, "dup_name")

    def test_different_names_both_kept(self):
        self.add("甲")
        self.add("乙")
        self.assertEqual([r["name"] for r in self.data["recipes"]], ["甲", "乙"])


class SaveShapeTests(unittest.TestCase):
    """存档口径：只存真笔、参数白名单、强度转数字、id 后端生成。"""

    def setUp(self):
        self.data = {"recipes": [], "overrides": {}}

    def test_null_slots_are_dropped_order_kept(self):
        r = PS.add_recipe(self.data, name="甲", unet=UNETS[0],
                          loras=[PEN1, None, PEN2, {"name": "", "strength": 0.5}],
                          positive="", negative="", params=PARAMS)
        self.assertEqual([p["name"] for p in r["loras"]], [PEN1["name"], PEN2["name"]])
        self.assertEqual([p["strength"] for p in r["loras"]], [0.9, 0.8])

    def test_strength_string_is_coerced_to_float(self):
        r = PS.add_recipe(self.data, name="甲", unet=UNETS[0],
                          loras=[{"name": PEN1["name"], "strength": "0.65"}],
                          positive="", negative="", params=PARAMS)
        self.assertEqual(r["loras"][0]["strength"], 0.65)
        self.assertIsInstance(r["loras"][0]["strength"], float)

    def test_params_keep_only_known_keys(self):
        junk = dict(PARAMS, node="12", class_type="KSampler", hack="<script>")
        r = PS.add_recipe(self.data, name="甲", unet=UNETS[0], loras=[],
                          positive="", negative="", params=junk)
        self.assertEqual(set(r["params"]), set(PS.PARAM_KEYS))

    def test_created_at_is_recorded(self):
        r = PS.add_recipe(self.data, name="甲", unet=UNETS[0], loras=[],
                          positive="", negative="", params=PARAMS)
        self.assertTrue(r["created_at"])

    def test_recipe_id_is_backend_generated(self):
        r = PS.add_recipe(self.data, name="甲", unet=UNETS[0], loras=[],
                          positive="", negative="", params=PARAMS)
        self.assertTrue(PS.valid_id(r["id"]))

    def test_two_recipes_get_different_ids(self):
        a = PS.add_recipe(self.data, name="甲", unet=UNETS[0], loras=[],
                          positive="", negative="", params=PARAMS)
        b = PS.add_recipe(self.data, name="乙", unet=UNETS[0], loras=[],
                          positive="", negative="", params=PARAMS)
        self.assertNotEqual(a["id"], b["id"])

    def test_disk_roundtrip_keeps_every_field(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "presets.json"
            r = PS.add_recipe(self.data, name="甲", unet=UNETS[0], loras=[PEN1],
                              positive="1girl", negative="bad", params=PARAMS)
            PS.save_presets(path, self.data)
            back = PS.load_presets(path)
            self.assertEqual(PS.find_by_id(back["recipes"], r["id"]), r)


class IdSafetyTests(unittest.TestCase):
    """雷区 9：id 只由后端生成，不接受前端传路径，文件名不参与 id。"""

    def test_generated_id_shape(self):
        rid = PS.gen_recipe_id()
        self.assertTrue(rid.startswith("r_"))
        self.assertEqual(len(rid), 10)
        int(rid[2:], 16)

    def test_generated_ids_are_unique(self):
        self.assertEqual(len({PS.gen_recipe_id() for _ in range(200)}), 200)

    def test_valid_id_rejects_paths_and_traversal(self):
        for bad in ("", None, "r_default/../secret", "..\\..\\x", "r_ZZZZZZZZ",
                    " presets", "r_1234567", "r_123456789", "甲"):
            self.assertFalse(PS.valid_id(bad), bad)

    def test_valid_id_accepts_backend_shapes(self):
        self.assertTrue(PS.valid_id(PS.gen_recipe_id()))
        self.assertTrue(PS.valid_id(PS.DEFAULT_ID))

    def test_default_id_is_not_a_path(self):
        self.assertNotIn("/", PS.DEFAULT_ID)
        self.assertNotIn("\\", PS.DEFAULT_ID)


class OverrideMergeTests(unittest.TestCase):
    """雷区 3：没 override 用配方原值；有 override 只覆盖出现的键。"""

    def setUp(self):
        self.r = recipe("双", loras=[PEN1])
        self.overrides = {
            "anima_dual": {self.r["id"]: {"params": {"width": 1024},
                                          "loras": [PEN1, PEN2]}},
            "anima_t2i": {self.r["id"]: {"positive": "solo only"}},
        }

    def test_no_override_keeps_recipe_values(self):
        got = PS.merge_override(self.r, {}, "anima_tri")
        self.assertEqual(got, self.r)

    def test_other_profile_override_is_ignored(self):
        got = PS.merge_override(self.r, self.overrides, "anima_tri")
        self.assertEqual(got["params"]["width"], 832)
        self.assertEqual(got["positive"], "1girl, solo")

    def test_only_present_param_keys_are_replaced(self):
        got = PS.merge_override(self.r, self.overrides, "anima_dual")
        self.assertEqual(got["params"]["width"], 1024)
        self.assertEqual(got["params"]["height"], 1216)
        self.assertEqual(got["params"]["steps"], 30)

    def test_list_override_replaces_whole_list(self):
        """D98：双笔差异靠 override 补，不静默丢槽。"""
        got = PS.merge_override(self.r, self.overrides, "anima_dual")
        self.assertEqual([p["name"] for p in got["loras"]], [PEN1["name"], PEN2["name"]])

    def test_scalar_override_replaces(self):
        got = PS.merge_override(self.r, self.overrides, "anima_t2i")
        self.assertEqual(got["positive"], "solo only")

    def test_merge_does_not_mutate_recipe(self):
        before = json.dumps(self.r, ensure_ascii=False, sort_keys=True)
        PS.merge_override(self.r, self.overrides, "anima_dual")
        self.assertEqual(json.dumps(self.r, ensure_ascii=False, sort_keys=True), before)

    def test_override_for_unknown_recipe_is_ignored(self):
        got = PS.merge_override(recipe("别的"), self.overrides, "anima_dual")
        self.assertEqual(got["params"]["width"], 832)


class CompatibilityTests(unittest.TestCase):
    """雷区 5：usable 与 warnings 必须同时给。"""

    def test_fits_is_usable_with_no_warnings(self):
        usable, warnings = PS.compatibility(recipe(loras=[PEN1]), 1, UNETS)
        self.assertTrue(usable)
        self.assertEqual(warnings, [])

    def test_returns_both_usable_and_warnings(self):
        got = PS.compatibility(recipe(loras=[PEN1]), 1, UNETS)
        self.assertIsInstance(got, tuple)
        self.assertEqual(len(got), 2)
        self.assertIsInstance(got[1], list)

    def test_too_many_pens_is_not_usable(self):
        usable, warnings = PS.compatibility(recipe(loras=[PEN1, PEN2]), 1, UNETS)
        self.assertFalse(usable)
        self.assertEqual(len(warnings), 1)
        self.assertIn("2 支笔", warnings[0])
        self.assertIn("1 槽", warnings[0])

    def test_exact_fit_is_usable(self):
        usable, warnings = PS.compatibility(recipe(loras=[PEN1, PEN2]), 2, UNETS)
        self.assertTrue(usable)
        self.assertEqual(warnings, [])

    def test_unknown_unet_is_not_usable(self):
        usable, warnings = PS.compatibility(recipe(unet="不存在的模型.safetensors"), 3, UNETS)
        self.assertFalse(usable)
        self.assertIn("不存在的模型.safetensors", warnings[0])

    def test_pen_and_unet_warnings_both_listed(self):
        usable, warnings = PS.compatibility(
            recipe(loras=[PEN1, PEN2], unet="gone.safetensors"), 1, UNETS)
        self.assertFalse(usable)
        self.assertEqual(len(warnings), 2)

    def test_no_pen_recipe_is_usable(self):
        usable, warnings = PS.compatibility(recipe(loras=[]), 1, UNETS)
        self.assertTrue(usable)
        self.assertEqual(warnings, [])


class NoSilentTruncationTests(unittest.TestCase):
    """雷区 7：不兼容载入必须报错说人话，不许「载入了但少了笔」。"""

    def setUp(self):
        self.r = recipe("双", loras=[PEN1, PEN2])

    def test_incompatible_full_load_raises_with_human(self):
        with self.assertRaises(PS.PresetError) as cm:
            PS.build_load(self.r, {}, "anima_t2i", "full", 1, UNETS)
        self.assertEqual(cm.exception.code, "incompatible")
        self.assertIn("2 支笔", cm.exception.human)

    def test_incompatible_params_load_also_refuses(self):
        with self.assertRaises(PS.PresetError) as cm:
            PS.build_load(self.r, {}, "anima_t2i", "params", 1, UNETS)
        self.assertEqual(cm.exception.code, "incompatible")

    def test_unknown_unet_load_refuses(self):
        with self.assertRaises(PS.PresetError):
            PS.build_load(recipe(unet="gone.safetensors"), {}, "anima_tri",
                          "full", 3, UNETS)

    def test_compatible_load_keeps_every_pen(self):
        got = PS.build_load(self.r, {}, "anima_dual", "full", 2, UNETS)
        self.assertEqual(len(got["loras"]), 2)
        self.assertEqual([p["name"] for p in got["loras"]], [PEN1["name"], PEN2["name"]])

    def test_override_can_make_a_recipe_fit(self):
        """t2i 上给这支双笔配方一条单笔 override，就该能整套载入。"""
        overrides = {"anima_t2i": {self.r["id"]: {"loras": [PEN1]}}}
        got = PS.build_load(self.r, overrides, "anima_t2i", "full", 1, UNETS)
        self.assertEqual([p["name"] for p in got["loras"]], [PEN1["name"]])


class LoadModeTests(unittest.TestCase):
    """雷区 4：params 模式返回体里不能带 loras。"""

    def setUp(self):
        self.r = recipe("甲", loras=[PEN1])

    def test_full_mode_carries_everything(self):
        got = PS.build_load(self.r, {}, "anima_t2i", "full", 1, UNETS)
        self.assertEqual(got["mode"], "full")
        self.assertEqual(got["unet"], UNETS[0])
        self.assertEqual(got["loras"], [PEN1])
        self.assertEqual(got["positive"], "1girl, solo")
        self.assertEqual(got["negative"], "worst quality")
        self.assertEqual(got["params"], PARAMS)

    def test_params_mode_has_no_loras_key(self):
        got = PS.build_load(self.r, {}, "anima_t2i", "params", 1, UNETS)
        self.assertNotIn("loras", got)

    def test_params_mode_has_no_unet_key(self):
        got = PS.build_load(self.r, {}, "anima_t2i", "params", 1, UNETS)
        self.assertNotIn("unet", got)

    def test_params_mode_carries_triggers_and_params(self):
        got = PS.build_load(self.r, {}, "anima_t2i", "params", 1, UNETS)
        self.assertEqual(got["mode"], "params")
        self.assertNotIn("positive", got)
        self.assertNotIn("negative", got)
        self.assertEqual(got["triggers"], [])
        self.assertEqual(got["params"], PARAMS)

    def test_unknown_mode_refused(self):
        with self.assertRaises(PS.PresetError) as cm:
            PS.build_load(self.r, {}, "anima_t2i", "half", 1, UNETS)
        self.assertEqual(cm.exception.code, "bad_mode")

    def test_payload_is_a_copy_not_the_recipe(self):
        got = PS.build_load(self.r, {}, "anima_t2i", "full", 1, UNETS)
        got["loras"][0]["strength"] = 0.1
        got["params"]["width"] = 64
        self.assertEqual(self.r["loras"][0]["strength"], 0.9)
        self.assertEqual(self.r["params"]["width"], 832)


class DeleteTests(unittest.TestCase):
    """雷区 6：删配方连带删所有 profile 下的 overrides，不留孤儿。"""

    def setUp(self):
        self.a = recipe("甲", rid="r_aaaaaaaa")
        self.b = recipe("乙", rid="r_bbbbbbbb")
        self.data = {
            "recipes": [self.a, self.b],
            "overrides": {
                "anima_dual": {"r_aaaaaaaa": {"params": {"width": 1024}},
                               "r_bbbbbbbb": {"params": {"width": 768}}},
                "anima_t2i": {"r_aaaaaaaa": {"positive": "x"}},
            },
        }

    def orphans(self):
        return [rid for prof in self.data["overrides"].values() for rid in prof]

    def test_delete_removes_the_recipe(self):
        self.assertTrue(PS.delete_recipe(self.data, "r_aaaaaaaa"))
        self.assertEqual([r["id"] for r in self.data["recipes"]], ["r_bbbbbbbb"])

    def test_delete_removes_overrides_in_every_profile(self):
        PS.delete_recipe(self.data, "r_aaaaaaaa")
        self.assertNotIn("r_aaaaaaaa", self.orphans())

    def test_delete_drops_empty_profile_buckets(self):
        PS.delete_recipe(self.data, "r_aaaaaaaa")
        self.assertNotIn("anima_t2i", self.data["overrides"])
        self.assertIn("anima_dual", self.data["overrides"])

    def test_delete_keeps_other_recipe_overrides(self):
        PS.delete_recipe(self.data, "r_aaaaaaaa")
        self.assertEqual(self.data["overrides"]["anima_dual"],
                         {"r_bbbbbbbb": {"params": {"width": 768}}})

    def test_delete_missing_returns_false(self):
        self.assertFalse(PS.delete_recipe(self.data, "r_cccccccc"))
        self.assertEqual(len(self.data["recipes"]), 2)

    def test_delete_builtin_default_is_refused(self):
        with self.assertRaises(PS.PresetError) as cm:
            PS.delete_recipe(self.data, PS.DEFAULT_ID, protected_ids=(PS.DEFAULT_ID,))
        self.assertEqual(cm.exception.code, "protected")
        self.assertTrue(cm.exception.human)


class ListPresetsTests(unittest.TestCase):
    """雷区 8（后端半边）：默认只列可用；显示全部才带不兼容项与 warning。"""

    def setUp(self):
        self.default = PS.default_recipe(SETTINGS)
        self.fit = recipe("单", loras=[PEN1], rid="r_11111111")
        self.over = recipe("双", loras=[PEN1, PEN2], rid="r_22222222")
        self.data = {"recipes": [self.fit, self.over], "overrides": {}}

    def names(self, **kw):
        return [p["name"] for p in PS.list_presets(
            self.data, self.default, "anima_t2i", 1, UNETS, **kw)]

    def test_default_listed_first_and_flagged_builtin(self):
        items = PS.list_presets(self.data, self.default, "anima_tri", 3, UNETS)
        self.assertEqual(items[0]["id"], PS.DEFAULT_ID)
        self.assertTrue(items[0]["builtin"])
        self.assertTrue(items[0]["usable"])

    def test_default_view_hides_unusable(self):
        self.assertEqual(self.names(), ["默认", "单"])

    def test_show_all_lists_unusable_too(self):
        self.assertEqual(self.names(show_all=True), ["默认", "单", "双"])

    def test_every_item_carries_usable_and_warnings(self):
        for item in PS.list_presets(self.data, self.default, "anima_t2i", 1, UNETS,
                                    show_all=True):
            self.assertIn("usable", item)
            self.assertIsInstance(item["warnings"], list)

    def test_unusable_item_explains_why(self):
        items = {p["name"]: p for p in PS.list_presets(
            self.data, self.default, "anima_t2i", 1, UNETS, show_all=True)}
        self.assertFalse(items["双"]["usable"])
        self.assertIn("2 支笔", items["双"]["warnings"][0])
        self.assertTrue(items["单"]["usable"])
        self.assertEqual(items["单"]["warnings"], [])

    def test_usability_follows_the_current_profile(self):
        self.assertEqual(self.names(), ["默认", "单"])
        items = PS.list_presets(self.data, self.default, "anima_dual", 2, UNETS)
        self.assertEqual([p["name"] for p in items], ["默认", "单", "双"])

    def test_saved_recipe_appears_immediately(self):
        PS.add_recipe(self.data, name="新", unet=UNETS[0], loras=[PEN1],
                      positive="", negative="", params=PARAMS)
        self.assertIn("新", self.names())

    def test_list_does_not_mutate_the_table(self):
        before = json.dumps(self.data, ensure_ascii=False, sort_keys=True)
        PS.list_presets(self.data, self.default, "anima_t2i", 1, UNETS, show_all=True)
        self.assertEqual(json.dumps(self.data, ensure_ascii=False, sort_keys=True), before)


class PresetApiTests(unittest.TestCase):
    """异常路径单测（卡片必写）：profile 不存在、同名、不兼容载入、删除后无孤儿。"""

    @classmethod
    def setUpClass(cls):
        from app.settings import TOKEN_FILE
        cls._token_file = TOKEN_FILE
        cls._token_before = (
            TOKEN_FILE.read_text(encoding="utf-8") if TOKEN_FILE.exists() else None
        )
        from fastapi.testclient import TestClient
        from app import main
        cls.main = main
        cls.client = TestClient(main.app, base_url="http://127.0.0.1:8788",
                                headers={"Authorization": "Bearer " + main.TOKEN})

    @classmethod
    def tearDownClass(cls):
        # 导入 app.main 会重生 token.txt；把它还原，别跟跑着的服务对不上
        if cls._token_before is not None:
            cls._token_file.write_text(cls._token_before, encoding="utf-8")

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "presets.json"
        self._real_path = self.main.PRESETS_FILE
        self.main.PRESETS_FILE = self.path

    def tearDown(self):
        self.main.PRESETS_FILE = self._real_path
        self._tmp.cleanup()

    def get(self, pid="anima_t2i", show_all=None):
        url = f"/api/presets?profile_id={pid}"
        if show_all:
            url += "&all=1"
        return self.client.get(url)

    def save(self, name="甲", loras=None, unet=UNETS[0], params=None,
             positive="1girl, solo", negative="worst quality", pid="anima_t2i"):
        return self.client.post("/api/presets", json={
            "profile_id": pid, "name": name, "unet": unet,
            "loras": loras if loras is not None else [PEN1],
            "positive": positive, "negative": negative,
            "params": dict(params or PARAMS),
        })

    def load(self, rid, pid="anima_t2i", mode="full"):
        return self.client.post(f"/api/presets/{rid}/load",
                                json={"profile_id": pid, "mode": mode})

    def test_api_needs_token(self):
        r = self.client.get("/api/presets?profile_id=anima_t2i",
                            headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_unknown_profile_is_404(self):
        self.assertEqual(self.get("没这个工作流").status_code, 404)

    def test_default_preset_is_always_listed(self):
        body = self.get().json()
        self.assertEqual(body["presets"][0]["id"], PS.DEFAULT_ID)
        self.assertTrue(body["presets"][0]["builtin"])
        self.assertTrue(body["presets"][0]["usable"])

    def test_saved_preset_shows_up_at_once(self):
        rid = self.save("甲").json()["id"]
        names = {p["name"]: p["id"] for p in self.get().json()["presets"]}
        self.assertEqual(names.get("甲"), rid)

    def test_empty_name_is_400(self):
        r = self.save("   ")
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()["detail"]["human"])

    def test_dup_name_is_409_and_says_human(self):
        self.save("甲")
        r = self.save(" 甲 ")
        self.assertEqual(r.status_code, 409)
        self.assertIn("甲", r.json()["detail"]["human"])

    def test_full_load_returns_what_was_saved(self):
        rid = self.save("甲", loras=[PEN1], params=dict(PARAMS, steps=21)).json()["id"]
        body = self.load(rid).json()
        self.assertEqual(body["mode"], "full")
        self.assertEqual(body["unet"], UNETS[0])
        self.assertEqual(body["loras"], [PEN1])
        self.assertEqual(body["positive"], "1girl, solo")
        self.assertEqual(body["negative"], "worst quality")
        self.assertEqual(body["params"]["steps"], 21)

    def test_params_load_carries_no_loras(self):
        rid = self.save("甲").json()["id"]
        body = self.load(rid, mode="params").json()
        self.assertNotIn("loras", body)
        self.assertNotIn("unet", body)
        self.assertNotIn("positive", body)
        self.assertNotIn("negative", body)
        self.assertIn("triggers", body)

    def test_incompatible_load_is_409_not_silent_truncation(self):
        rid = self.save("双", loras=[PEN1, PEN2], pid="anima_dual").json()["id"]
        r = self.load(rid, pid="anima_t2i")
        self.assertEqual(r.status_code, 409)
        self.assertIn("2 支笔", r.json()["detail"]["human"])

    def test_incompatible_preset_hidden_until_show_all(self):
        self.save("双", loras=[PEN1, PEN2], pid="anima_dual")
        self.assertNotIn("双", [p["name"] for p in self.get().json()["presets"]])
        shown = {p["name"]: p for p in self.get(show_all=True).json()["presets"]}
        self.assertIn("双", shown)
        self.assertFalse(shown["双"]["usable"])
        self.assertTrue(shown["双"]["warnings"])

    def test_same_preset_is_usable_on_its_own_workflow(self):
        self.save("双", loras=[PEN1, PEN2], pid="anima_dual")
        shown = {p["name"]: p for p in self.get("anima_dual").json()["presets"]}
        self.assertTrue(shown["双"]["usable"])

    def test_load_unknown_recipe_is_404(self):
        self.assertEqual(self.load("r_12345678").status_code, 404)

    def test_load_non_backend_id_is_404(self):
        self.assertEqual(self.load("r_zzzzzzzz").status_code, 404)

    def test_load_builtin_default_works(self):
        r = self.load(PS.DEFAULT_ID)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["loras"], [])

    def test_load_bad_mode_is_400(self):
        rid = self.save("甲").json()["id"]
        self.assertEqual(self.load(rid, mode="half").status_code, 400)

    def test_load_unknown_profile_is_404(self):
        rid = self.save("甲").json()["id"]
        self.assertEqual(self.load(rid, pid="nope").status_code, 404)

    def test_delete_removes_recipe_and_every_override(self):
        rid = self.save("甲").json()["id"]
        table = PS.load_presets(self.path)
        table["overrides"] = {"anima_t2i": {rid: {"params": {"width": 1024}}},
                              "anima_dual": {rid: {"params": {"width": 768}}}}
        PS.save_presets(self.path, table)
        self.assertEqual(self.client.delete(f"/api/presets/{rid}").status_code, 200)
        after = PS.load_presets(self.path)
        self.assertEqual(after["recipes"], [])
        self.assertEqual(
            [k for prof in after["overrides"].values() for k in prof], [])

    def test_delete_unknown_is_404(self):
        self.assertEqual(self.client.delete("/api/presets/r_12345678").status_code, 404)

    def test_delete_builtin_default_is_409(self):
        r = self.client.delete(f"/api/presets/{PS.DEFAULT_ID}")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["detail"]["human"])

    def test_table_survives_a_restart_of_the_reader(self):
        rid = self.save("甲").json()["id"]
        self.assertIn(rid, [p["id"] for p in self.get().json()["presets"]])


class DefaultRecipeTests(unittest.TestCase):
    def test_default_recipe_shape(self):
        r = PS.default_recipe(SETTINGS)
        self.assertEqual(r["id"], PS.DEFAULT_ID)
        self.assertEqual(r["name"], "默认")
        self.assertEqual(r["unet"], SETTINGS["default_unet"])
        self.assertEqual(r["loras"], [])
        self.assertEqual(r["positive"], "")

    def test_default_params_cover_every_key(self):
        self.assertEqual(set(PS.default_recipe(SETTINGS)["params"]), set(PS.PARAM_KEYS))

    def test_profile_defaults_win(self):
        r = PS.default_recipe(SETTINGS, {"steps": 20, "width": 768, "junk": 1})
        self.assertEqual(r["params"]["steps"], 20)
        self.assertEqual(r["params"]["width"], 768)
        self.assertNotIn("junk", r["params"])

    def test_default_is_usable_on_a_one_slot_profile(self):
        usable, warnings = PS.compatibility(PS.default_recipe(SETTINGS), 1, UNETS)
        self.assertTrue(usable)
        self.assertEqual(warnings, [])

    def test_default_full_load_clears_pens_without_error(self):
        got = PS.build_load(PS.default_recipe(SETTINGS), {}, "anima_tri", "full", 3, UNETS)
        self.assertEqual(got["loras"], [])
        self.assertEqual(got["params"]["seed"], -1)


if __name__ == "__main__":
    unittest.main()
