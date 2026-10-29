"""T007 界面设置（背景／密度）：读侧兜底、写侧校验、原子写、端点守卫。

红线：settings.json 损坏／字段缺／值非法都不许崩，一律兜回 {"bg":"off","density":"loose"}；
bg 只认 static/bg 里真有的文件名——路径穿越与任意路径都挡在门外（不当目录浏览器）。
"""
import json
import tempfile
import unittest
from pathlib import Path

from app import settings as ST

DEFAULT_UI = {"bg": "off", "density": "loose", "blur": 10}


class TempCase(unittest.TestCase):
    """把 settings.json 与 static/bg 一起挪进临时目录；跑完一个字节都不留在仓库里。"""

    def setUp(self):
        self._real_settings = ST.SETTINGS_FILE
        self._real_bg = ST.BG_DIR
        self._tmp = Path(tempfile.mkdtemp(prefix="huafang-ui-"))
        ST.SETTINGS_FILE = self._tmp / "settings.json"
        ST.BG_DIR = self._tmp / "bg"

    def tearDown(self):
        ST.SETTINGS_FILE = self._real_settings
        ST.BG_DIR = self._real_bg
        for p in sorted(self._tmp.rglob("*"), key=lambda x: -len(str(x))):
            if p.is_file():
                p.unlink()
            else:
                p.rmdir()
        self._tmp.rmdir()

    def write_settings(self, text):
        ST.SETTINGS_FILE.write_text(text, encoding="utf-8")

    def write_json(self, obj):
        self.write_settings(json.dumps(obj, ensure_ascii=False))

    def make_bg(self, *names):
        ST.BG_DIR.mkdir(parents=True, exist_ok=True)
        for n in names:
            p = ST.BG_DIR / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
        return list(names)


class DefaultsTests(unittest.TestCase):
    def test_defaults_shape_is_exactly_the_card(self):
        """v2 增加默认模糊 10px，旧配置缺键照常读。"""
        self.assertEqual(ST.DEFAULTS["ui"], DEFAULT_UI)

    def test_density_whitelist(self):
        self.assertEqual(ST.UI_DENSITY, ("loose", "compact"))


class ReadSideTests(TempCase):
    """ui_settings()：怎么坏都兜默认，绝不上抛。"""

    def test_missing_file(self):
        self.assertEqual(ST.ui_settings(), DEFAULT_UI)

    def test_empty_object(self):
        self.write_json({})
        self.assertEqual(ST.ui_settings(), DEFAULT_UI)

    def test_corrupt_json(self):
        self.write_settings('{"ui": {"bg": "x", ')
        self.assertEqual(ST.ui_settings(), DEFAULT_UI)

    def test_top_level_not_a_dict(self):
        self.write_settings("[1, 2, 3]")
        self.assertEqual(ST.ui_settings(), DEFAULT_UI)

    def test_ui_block_not_a_dict(self):
        self.write_json({"ui": "loose"})
        self.assertEqual(ST.ui_settings(), DEFAULT_UI)

    def test_missing_fields_fall_back_each_on_its_own(self):
        self.write_json({"ui": {"density": "compact", "blur": 10}})
        self.assertEqual(ST.ui_settings(), {"bg": "off", "density": "compact", "blur": 10})
        self.write_json({"ui": {"bg": "off"}})
        self.assertEqual(ST.ui_settings(), DEFAULT_UI)

    def test_bad_values_fall_back(self):
        for ui in ({"bg": 7, "density": 7}, {"bg": None, "density": None},
                   {"bg": "medium", "density": "tight"}, {"bg": ["a"], "density": {"x": 1}}):
            self.write_json({"ui": ui})
            self.assertEqual(ST.ui_settings(), DEFAULT_UI, str(ui))

    def test_bg_file_deleted_falls_back_to_off(self):
        """主人自己把图删了：悄悄回 off，不为一张背景图报错。"""
        self.make_bg("gone.svg")
        self.write_json({"ui": {"bg": "gone.svg", "density": "compact", "blur": 10}})
        self.assertEqual(ST.ui_settings(), {"bg": "gone.svg", "density": "compact", "blur": 10})
        (ST.BG_DIR / "gone.svg").unlink()
        self.assertEqual(ST.ui_settings(), {"bg": "off", "density": "compact", "blur": 10})

    def test_good_values_pass_through(self):
        self.make_bg("day.svg")
        self.write_json({"ui": {"bg": "day.svg", "density": "compact", "blur": 10}})
        self.assertEqual(ST.ui_settings(), {"bg": "day.svg", "density": "compact", "blur": 10})


class BgFilesTests(TempCase):
    def test_missing_dir_is_empty_list(self):
        self.assertEqual(ST.bg_files(), [])

    def test_only_image_exts_sorted_and_no_hidden(self):
        self.make_bg("z.svg", "a.png", "b.JPG", "c.webp", ".hidden.png", "readme.txt")
        self.assertEqual(ST.bg_files(), ["a.png", "b.JPG", "c.webp", "z.svg"])

    def test_subdirs_are_not_listed(self):
        self.make_bg("a.png", "sub/inner.png")
        self.assertEqual(ST.bg_files(), ["a.png"])


class NormUiTests(TempCase):
    """norm_ui()：写侧校验；缺键＝沿用现值，非法＝(None, 错误dict)。"""

    def test_full_legal(self):
        self.make_bg("day.svg")
        ui, err = ST.norm_ui({"bg": "day.svg", "density": "compact", "blur": 10}, ["day.svg"])
        self.assertEqual((ui, err), ({"bg": "day.svg", "density": "compact", "blur": 10}, None))

    def test_off_is_always_legal(self):
        ui, err = ST.norm_ui({"bg": "off", "density": "loose", "blur": 10}, [])
        self.assertEqual((ui, err), (DEFAULT_UI, None))

    def test_missing_key_keeps_current(self):
        """只改密度不许顺手把背景关掉（前端一次只发一个键也算合法）。"""
        cur = {"bg": "day.svg", "density": "loose", "blur": 10}
        ui, err = ST.norm_ui({"density": "compact", "blur": 10}, ["day.svg"], cur)
        self.assertEqual((ui, err), ({"bg": "day.svg", "density": "compact", "blur": 10}, None))

    def test_no_current_means_defaults(self):
        ui, err = ST.norm_ui({}, None, None)
        self.assertEqual((ui, err), (DEFAULT_UI, None))

    def test_unknown_keys_ignored(self):
        ui, err = ST.norm_ui({"bg": "off", "density": "loose", "theme": "dark", "x": 1}, [])
        self.assertEqual((ui, err), (DEFAULT_UI, None))

    def test_not_a_dict(self):
        for bad in ("loose", 7, None, ["bg"]):
            ui, err = ST.norm_ui(bad, [])
            self.assertIsNone(ui, str(bad))
            self.assertEqual(err["code"], "bad_ui")
            self.assertTrue(err["human"])

    def test_bg_must_exist_in_whitelist(self):
        for bad in ("nope.svg", "day.PNG", "", " ", 7, None, ["day.svg"]):
            ui, err = ST.norm_ui({"bg": bad}, ["day.svg"])
            self.assertIsNone(ui, str(bad))
            self.assertEqual(err["code"], "bad_bg", str(bad))
            self.assertTrue(err["human"])

    def test_bg_path_traversal_blocked(self):
        """白名单比对天然挡住分隔符与 ..：static/bg 外一个文件都读不到。"""
        self.make_bg("day.svg")
        (self._tmp / "secret.png").write_text("x", encoding="utf-8")
        for bad in ("../secret.png", "..\\secret.png", "/etc/passwd",
                    "sub/day.svg", "day.svg/../../secret.png"):
            ui, err = ST.norm_ui({"bg": bad}, ST.bg_files())
            self.assertIsNone(ui, bad)
            self.assertEqual(err["code"], "bad_bg", bad)

    def test_bg_surrounding_space_tolerated(self):
        self.make_bg("day.svg")
        ui, err = ST.norm_ui({"bg": "  day.svg  "}, ST.bg_files())
        self.assertEqual((ui, err), ({"bg": "day.svg", "density": "loose", "blur": 10}, None))

    def test_bad_density(self):
        for bad in ("tight", "COMPACT", "", 7, None, True):
            ui, err = ST.norm_ui({"density": bad}, [])
            self.assertIsNone(ui, str(bad))
            self.assertEqual(err["code"], "bad_density", str(bad))
            self.assertTrue(err["human"])

    def test_available_defaults_to_disk_scan(self):
        """不给 available 就现扫 static/bg（端点就是这么调的）。"""
        self.make_bg("day.svg")
        ui, err = ST.norm_ui({"bg": "day.svg"})
        self.assertEqual((ui, err), ({"bg": "day.svg", "density": "loose", "blur": 10}, None))
        ui, err = ST.norm_ui({"bg": "night.svg"})
        self.assertIsNone(ui)
        self.assertEqual(err["code"], "bad_bg")


class SaveUiTests(TempCase):
    def test_atomic_write_and_bak_and_keeps_other_keys(self):
        """旧文转 .bak、临时文件原子替换；output/gpu 那些别的键一个字不动。"""
        self.write_json({"output": {"dir": r"C:\我的作品"},
                         "gpu": {"warn_free_mb": 900},
                         "ui": {"bg": "off", "density": "loose", "blur": 10}})
        ST.save_ui({"bg": "off", "density": "compact", "blur": 10})
        saved = json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["ui"], {"bg": "off", "density": "compact", "blur": 10})
        self.assertEqual(saved["output"], {"dir": r"C:\我的作品"})
        self.assertEqual(saved["gpu"], {"warn_free_mb": 900})
        bak = ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8"))["ui"]["density"], "loose")
        self.assertFalse(ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".tmp").exists())

    def test_save_on_missing_file_creates_it(self):
        ST.save_ui(DEFAULT_UI)
        self.assertEqual(json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8"))["ui"], DEFAULT_UI)

    def test_save_does_not_bake_defaults_into_file(self):
        """只写 ui 块：不许把 DEFAULTS 整个钉进用户文件（与 save_output_dir 同纪律）。"""
        self.write_json({})
        ST.save_ui({"bg": "off", "density": "compact", "blur": 10})
        self.assertEqual(set(json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8"))), {"ui"})


class LoadSettingsTests(TempCase):
    def test_ui_merged_with_defaults(self):
        self.write_json({"ui": {"density": "compact", "blur": 10}})
        self.assertEqual(ST.load_settings()["ui"], {"bg": "off", "density": "compact", "blur": 10})

    def test_corrupt_settings_still_loads(self):
        self.write_settings("{{{ not json")
        s = ST.load_settings()
        self.assertEqual(s["ui"], DEFAULT_UI)
        self.assertEqual(s["output"], {"dir": ""})


class EndpointTests(TempCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app import main
        cls.main = main
        cls.http = TestClient(main.app, base_url="http://127.0.0.1:8788",
                              headers={"Authorization": "Bearer " + main.TOKEN})

    def post(self, payload, **kw):
        return self.http.post("/api/ui", json=payload, **kw)

    def test_bootstrap_carries_ui_and_bg_files(self):
        self.make_bg("day.svg", "night.svg")
        self.write_json({"ui": {"bg": "night.svg", "density": "compact", "blur": 10}})
        b = self.http.get("/api/bootstrap").json()
        self.assertEqual(b["ui"], {"bg": "night.svg", "density": "compact", "blur": 10})
        self.assertEqual(b["bg_files"], ["day.svg", "night.svg"])

    def test_bootstrap_defaults_when_settings_broken(self):
        self.write_settings("not json at all")
        b = self.http.get("/api/bootstrap").json()
        self.assertEqual(b["ui"], DEFAULT_UI)

    def test_bootstrap_sees_new_file_without_restart(self):
        """主人往 static/bg 丢图 → 刷新页面就能选（bg_files 每次现扫）。"""
        self.assertEqual(self.http.get("/api/bootstrap").json()["bg_files"], [])
        self.make_bg("day.svg")
        self.assertEqual(self.http.get("/api/bootstrap").json()["bg_files"], ["day.svg"])

    def test_post_valid_writes_file(self):
        self.make_bg("day.svg")
        self.write_json({})
        r = self.post({"bg": "day.svg", "density": "compact", "blur": 10})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual(b["ui"], {"bg": "day.svg", "density": "compact", "blur": 10})
        self.assertTrue(b["human"])
        self.assertEqual(json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8"))["ui"],
                         {"bg": "day.svg", "density": "compact", "blur": 10})

    def test_post_off_and_loose(self):
        self.write_json({"ui": {"bg": "off", "density": "compact", "blur": 10}})
        r = self.post({"bg": "off", "density": "loose", "blur": 10})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ui"], DEFAULT_UI)

    def test_post_illegal_bg_is_400_and_writes_nothing(self):
        self.make_bg("day.svg")
        self.write_json({"ui": DEFAULT_UI})
        before = ST.SETTINGS_FILE.read_bytes()
        for bad in ("night.svg", "../day.svg", 7, ""):
            r = self.post({"bg": bad})
            self.assertEqual(r.status_code, 400, str(bad))
            d = r.json()["detail"]
            self.assertEqual(d["code"], "bad_bg", str(bad))
            self.assertTrue(d["human"])
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)
        self.assertFalse(ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".bak").exists())

    def test_post_illegal_density_is_400(self):
        self.write_json({"ui": DEFAULT_UI})
        r = self.post({"density": "tight"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "bad_density")

    def test_post_body_not_a_dict_is_rejected(self):
        """body 根本不是对象：框架层就挡下来（422），norm_ui 的 bad_ui 分支是第二道保险。"""
        self.write_json({"ui": DEFAULT_UI})
        r = self.http.post("/api/ui", json="loose")
        self.assertIn(r.status_code, (400, 422))
        self.assertEqual(json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8"))["ui"], DEFAULT_UI)

    def test_post_patch_semantics(self):
        """只发一个键＝只改那一个；另一个沿用盘上现值。"""
        self.make_bg("day.svg")
        self.write_json({"ui": {"bg": "day.svg", "density": "loose", "blur": 10}})
        r = self.post({"density": "compact", "blur": 10})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["ui"], {"bg": "day.svg", "density": "compact", "blur": 10})

    def test_post_creates_bak_on_second_write(self):
        self.write_json({"ui": DEFAULT_UI})
        self.post({"density": "compact", "blur": 10})
        bak = ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8"))["ui"]["density"], "loose")
        self.assertFalse(ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".tmp").exists())

    def test_wrong_token_is_401(self):
        r = self.post(DEFAULT_UI, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_bad_origin_is_403(self):
        r = self.post(DEFAULT_UI, headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_token_in_url_is_not_honored(self):
        """token 只走 Authorization 头（D135）：query 里带对了也不算数。
        （TestClient 的 per-request headers 是「合并」不是「覆盖」，所以要拿一把错 token 来证。）"""
        r = self.http.post("/api/ui?token=" + self.main.TOKEN, json=DEFAULT_UI,
                           headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
