"""T003 单测：只读扫描 ＋ 触发词人工覆盖。

覆盖卡片 8 条雷区与 6 条必写异常路径：
覆盖文件缺失/损坏/半截 JSON、非法笔名、空串覆盖、清除不存在的覆盖。
"""
import json
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import models as MS
from app.triggers import TriggerResolver, compose

PEN_TABLE_HIT = "sample_style.safetensors"
PEN_TABLE_EMPTY = "Empty Style.safetensors"
PEN_META_ONLY = "brand_new_pen.safetensors"


def fake_root(tmp: Path) -> Path:
    """造一个假的 ComfyUI models 根：四个子目录都在，但是空的。"""
    root = tmp / "models"
    for sub in MS.MODEL_DIRS.values():
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def fake_pen(root: Path, name: str, role: str = "lora", metadata: dict = None,
             pad: int = 0) -> Path:
    """造假模型文件：只有 safetensors 头部（元数据兜底只读头部，够用）。"""
    d = root / MS.MODEL_DIRS[role]
    d.mkdir(parents=True, exist_ok=True)
    header = json.dumps({"__metadata__": metadata or {}, "x.weight": {}},
                        ensure_ascii=False).encode()
    p = d / name
    p.write_bytes(struct.pack("<Q", len(header)) + header + b"\0" * pad)
    return p


def snapshot(root: Path) -> dict:
    """盘上现状快照：文件名 → (size, mtime)，用来证明扫描是只读的（雷区 1）。"""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.stat()
        out[str(p.relative_to(root))] = (st.st_size, st.st_mtime, p.is_dir())
    return out


class NormalizeTests(unittest.TestCase):
    """雷区 3：大小写、.safetensors 后缀、前后空格都算同一支笔。"""

    def test_same_pen_different_spelling(self):
        keys = {
            MS.normalize_name("Sample_Style.safetensors"),
            MS.normalize_name("sample_style"),
            MS.normalize_name("  Sample_Style.SAFETENSORS  "),
        }
        self.assertEqual(keys, {"sample_style"})

    def test_empty_and_non_string(self):
        self.assertEqual(MS.normalize_name(""), "")
        self.assertEqual(MS.normalize_name(None), "")
        self.assertEqual(MS.normalize_name(123), "")
        self.assertEqual(MS.normalize_name(["a"]), "")

    def test_suffix_in_middle_is_not_stripped(self):
        self.assertEqual(MS.normalize_name("a.safetensors.bak"), "a.safetensors.bak")


class OverrideFileTests(unittest.TestCase):
    """雷区 2 ＋ 必写异常路径：缺失/损坏/半截 JSON 当空表，且绝不改写用户文件。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "trigger_overrides.json"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def test_missing_file_is_empty_table(self):
        self.assertEqual(MS.load_overrides(self.path), {})
        self.assertFalse(self.path.exists(), "读一把不该把文件创建出来")

    def test_corrupt_file_is_empty_table_and_untouched(self):
        self.write("{ 这不是 JSON")
        before = self.path.read_bytes()
        self.assertEqual(MS.load_overrides(self.path), {})
        self.assertEqual(self.path.read_bytes(), before, "损坏文件被改写了")

    def test_half_json_is_empty_table_and_untouched(self):
        self.write('{"sample_style": "sample"')
        before = self.path.read_bytes()
        self.assertEqual(MS.load_overrides(self.path), {})
        self.assertEqual(self.path.read_bytes(), before)

    def test_not_a_dict_is_empty_table(self):
        self.write('["a", "b"]')
        self.assertEqual(MS.load_overrides(self.path), {})
        self.write('"just a string"')
        self.assertEqual(MS.load_overrides(self.path), {})

    def test_keys_normalized_and_bad_values_dropped(self):
        self.write(json.dumps({
            "Sample_Style.safetensors": "my_sample",
            "  empty style  ": "",
            "not_a_string": 123,
            "": "no key",
            "null_value": None,
        }, ensure_ascii=False))
        self.assertEqual(MS.load_overrides(self.path),
                         {"sample_style": "my_sample", "empty style": ""})

    def test_save_backs_up_previous_bytes(self):
        self.write('{"old": "值"}')
        MS.save_overrides(self.path, {"new": "值2"})
        self.assertEqual(MS.load_overrides(self.path), {"new": "值2"})
        bak = self.path.with_name(self.path.name + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(bak.read_text(encoding="utf-8"), '{"old": "值"}')
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists(),
                         "临时文件没清掉")

    def test_save_creates_parent_dir_and_no_bak_when_new(self):
        p = self.tmp / "sub" / "dir" / "trigger_overrides.json"
        MS.save_overrides(p, {"a": "b"})
        self.assertEqual(MS.load_overrides(p), {"a": "b"})
        self.assertFalse(p.with_name(p.name + ".bak").exists())

    def test_save_after_corrupt_keeps_corrupt_bytes_in_bak(self):
        self.write("{ 坏掉的")
        MS.save_overrides(self.path, {"a": "b"})
        self.assertEqual(MS.load_overrides(self.path), {"a": "b"})
        self.assertEqual(self.path.with_name(self.path.name + ".bak")
                         .read_text(encoding="utf-8"), "{ 坏掉的")

    def test_put_then_load(self):
        MS.put_override(self.path, PEN_TABLE_HIT, "my_sample")
        self.assertEqual(MS.load_overrides(self.path), {"sample_style": "my_sample"})

    def test_put_same_pen_twice_leaves_one_key(self):
        MS.put_override(self.path, "Sample_Style.safetensors", "甲")
        MS.put_override(self.path, "  sample_style ", "乙")
        self.assertEqual(MS.load_overrides(self.path), {"sample_style": "乙"})

    def test_put_empty_string_is_kept(self):
        """空串＝明确无触发词，不能当「没有覆盖」丢掉（雷区 4）。"""
        MS.put_override(self.path, PEN_TABLE_HIT, "")
        self.assertEqual(MS.load_overrides(self.path), {"sample_style": ""})

    def test_drop_existing(self):
        MS.put_override(self.path, PEN_TABLE_HIT, "my_sample")
        MS.drop_override(self.path, "Sample_Style.SAFETENSORS")
        self.assertEqual(MS.load_overrides(self.path), {})

    def test_drop_missing_override_is_idempotent_and_writes_nothing(self):
        """必写异常路径：清除不存在的覆盖——不报错、不动盘上文件。"""
        self.write('{"other_pen": "别的"}')
        before = self.path.read_bytes()
        MS.drop_override(self.path, PEN_TABLE_HIT)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(MS.load_overrides(self.path), {"other_pen": "别的"})

    def test_drop_when_file_missing_does_not_create_it(self):
        MS.drop_override(self.path, PEN_TABLE_HIT)
        self.assertFalse(self.path.exists())


class ScanTests(unittest.TestCase):
    """雷区 1：只读扫描——不写、不动 mtime/size。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = fake_root(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_shapes_and_roles(self):
        fake_pen(self.root, "base.safetensors", "unet", pad=10)
        fake_pen(self.root, "te.safetensors", "clip")
        fake_pen(self.root, "vae.safetensors", "vae")
        pen = fake_pen(self.root, PEN_TABLE_HIT, pad=7)
        got = MS.scan_models(self.root)
        self.assertEqual(sorted(got), ["clip", "lora", "unet", "vae"])
        self.assertEqual([i["name"] for i in got["unet"]], ["base.safetensors"])
        item = got["lora"][0]
        self.assertEqual(item["name"], PEN_TABLE_HIT)
        self.assertEqual(item["size"], pen.stat().st_size)
        self.assertEqual(item["mtime"], pen.stat().st_mtime)
        self.assertNotIn("trigger", item, "没给 resolver 就不该带触发词")

    def test_scan_is_read_only(self):
        fake_pen(self.root, PEN_TABLE_HIT, metadata={"modelspec.trigger": "sample"})
        fake_pen(self.root, "base.safetensors", "unet")
        before = snapshot(self.root)
        resolver = TriggerResolver(self.tmp / "笔触发词.json",
                                   self.root / MS.MODEL_DIRS["lora"])
        MS.scan_models(self.root, resolver)
        MS.scan_models(self.root, resolver)
        self.assertEqual(snapshot(self.root), before, "扫描改动了模型目录")

    def test_missing_subdir_is_empty_list(self):
        root = self.tmp / "只有loras"
        (root / MS.MODEL_DIRS["lora"]).mkdir(parents=True)
        fake_pen(root, PEN_TABLE_HIT)
        got = MS.scan_models(root)
        self.assertEqual(got["unet"], [])
        self.assertEqual(got["clip"], [])
        self.assertEqual(got["vae"], [])
        self.assertEqual(len(got["lora"]), 1)

    def test_only_safetensors_counted(self):
        fake_pen(self.root, PEN_TABLE_HIT)
        (self.root / MS.MODEL_DIRS["lora"] / "notes.txt").write_text("x", encoding="utf-8")
        (self.root / MS.MODEL_DIRS["lora"] / "old.ckpt").write_bytes(b"\0")
        self.assertEqual([i["name"] for i in MS.scan_models(self.root)["lora"]],
                         [PEN_TABLE_HIT])

    def test_names_helper_matches_scan(self):
        fake_pen(self.root, "b.safetensors")
        fake_pen(self.root, "a.safetensors")
        self.assertEqual(MS.model_names("lora", self.root),
                         [i["name"] for i in MS.scan_models(self.root)["lora"]])
        self.assertEqual(MS.model_names("lora", self.root),
                         ["a.safetensors", "b.safetensors"], "要按名字排序")

    def test_sort_is_by_name_string_not_path(self):
        """锁 A1 时的下拉框顺序：按文件名字符串排（大写在小写前），不是 Path 的不分大小写。"""
        fake_pen(self.root, "apple.safetensors")
        fake_pen(self.root, "Banana.safetensors")
        self.assertEqual(MS.model_names("lora", self.root),
                         ["Banana.safetensors", "apple.safetensors"])

    def test_resolver_attaches_triple_to_pens_only(self):
        fake_pen(self.root, PEN_TABLE_HIT, metadata={"modelspec.trigger": "meta_sample"})
        fake_pen(self.root, "base.safetensors", "unet")
        resolver = TriggerResolver(self.tmp / "笔触发词.json",
                                   self.root / MS.MODEL_DIRS["lora"])
        got = MS.scan_models(self.root, resolver)
        trig = got["lora"][0]["trigger"]
        self.assertEqual(sorted(trig), ["effective", "meta", "override", "table"])
        self.assertNotIn("trigger", got["unet"][0])

    def test_find_pen(self):
        fake_pen(self.root, PEN_TABLE_HIT)
        self.assertEqual(MS.find_pen(PEN_TABLE_HIT, self.root), PEN_TABLE_HIT)
        self.assertEqual(MS.find_pen("  sample_style ", self.root), PEN_TABLE_HIT)
        self.assertEqual(MS.find_pen("sample_style.safetensors", self.root),
                         PEN_TABLE_HIT)
        self.assertIsNone(MS.find_pen("没这支笔.safetensors", self.root))
        self.assertIsNone(MS.find_pen("", self.root))
        self.assertIsNone(MS.find_pen(None, self.root))


class ResolverLayerTests(unittest.TestCase):
    """雷区 4/5：override > 表 > 元数据，三层各命中一次；不传 override 时行为不变。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = fake_root(self.tmp)
        self.loras = self.root / MS.MODEL_DIRS["lora"]
        fake_pen(self.root, PEN_TABLE_HIT, metadata={"modelspec.trigger": "meta_sample"})
        fake_pen(self.root, PEN_TABLE_EMPTY, metadata={"modelspec.trigger": "meta_empty"})
        fake_pen(self.root, PEN_META_ONLY, metadata={"modelspec.trigger": "meta_new"})
        self.table_file = self.tmp / "笔触发词.json"
        self.table_file.write_text(json.dumps({
            "sample": "sample",
            "Empty Style": "",
        }, ensure_ascii=False), encoding="utf-8")
        self.override_file = self.tmp / "trigger_overrides.json"

    def tearDown(self):
        self._tmp.cleanup()

    def resolver(self, with_override=True) -> TriggerResolver:
        return TriggerResolver(self.table_file, self.loras,
                               self.override_file if with_override else None)

    def save_override(self, name: str, value: str) -> None:
        MS.put_override(self.override_file, name, value)

    def test_two_layer_baseline_without_override_path(self):
        """回归保护：不传 override_path 时与 T001 完全一致。"""
        r = self.resolver(with_override=False)
        self.assertEqual(r.lookup(PEN_TABLE_HIT), "sample")
        self.assertEqual(r.lookup(PEN_TABLE_EMPTY), "")
        self.assertEqual(r.lookup(PEN_META_ONLY), "meta_new")
        d = r.details(PEN_TABLE_HIT)
        self.assertIsNone(d["override"])
        self.assertEqual(d["effective"], "sample")

    def test_table_beats_metadata(self):
        r = self.resolver()
        self.assertEqual(r.lookup(PEN_TABLE_HIT), "sample")
        d = r.details(PEN_TABLE_HIT)
        self.assertEqual((d["table"], d["meta"], d["override"], d["effective"]),
                         ("sample", "meta_sample", None, "sample"))

    def test_table_empty_string_is_authoritative(self):
        r = self.resolver()
        self.assertEqual(r.lookup(PEN_TABLE_EMPTY), "")
        d = r.details(PEN_TABLE_EMPTY)
        self.assertEqual((d["table"], d["meta"], d["effective"]), ("", "meta_empty", ""))

    def test_metadata_fallback(self):
        r = self.resolver()
        self.assertEqual(r.lookup(PEN_META_ONLY), "meta_new")
        d = r.details(PEN_META_ONLY)
        self.assertEqual((d["table"], d["meta"], d["override"], d["effective"]),
                         (None, "meta_new", None, "meta_new"))

    def test_override_beats_table(self):
        self.save_override(PEN_TABLE_HIT, "my_sample")
        r = self.resolver()
        self.assertEqual(r.lookup(PEN_TABLE_HIT), "my_sample")
        d = r.details(PEN_TABLE_HIT)
        self.assertEqual((d["table"], d["override"], d["effective"]),
                         ("sample", "my_sample", "my_sample"))

    def test_override_beats_metadata(self):
        self.save_override(PEN_META_ONLY, "mine")
        r = self.resolver()
        self.assertEqual(r.lookup(PEN_META_ONLY), "mine")
        self.assertEqual(r.details(PEN_META_ONLY)["override"], "mine")

    def test_empty_override_blocks_metadata(self):
        """雷区 4：覆盖为空串＝明确无触发词，不兜元数据（D20）。"""
        self.save_override(PEN_META_ONLY, "")
        r = self.resolver()
        self.assertEqual(r.lookup(PEN_META_ONLY), "")
        d = r.details(PEN_META_ONLY)
        self.assertEqual(d["override"], "")
        self.assertIsNone(d["table"])
        self.assertEqual(d["meta"], "meta_new")
        self.assertEqual(d["effective"], "")

    def test_override_key_normalization(self):
        """雷区 3：覆盖存成别的写法也认得出来。"""
        MS.put_override(self.override_file, "Sample_Style", "my_sample")
        r = self.resolver()
        self.assertEqual(r.lookup(PEN_TABLE_HIT), "my_sample")
        self.assertEqual(r.lookup("sample_style"), "my_sample")

    def test_corrupt_override_file_falls_back(self):
        """必写异常路径：覆盖文件损坏 → 当没有覆盖，不崩。"""
        self.override_file.write_text('{"sample_style": "my_sample"', encoding="utf-8")
        r = self.resolver()
        self.assertEqual(r.lookup(PEN_TABLE_HIT), "sample")
        d = r.details(PEN_TABLE_HIT)
        self.assertIsNone(d["override"])
        self.assertEqual(d["effective"], "sample")

    def test_missing_override_file_falls_back(self):
        r = self.resolver()
        self.assertEqual(r.lookup(PEN_TABLE_HIT), "sample")
        self.assertIsNone(r.details(PEN_TABLE_HIT)["override"])

    def test_late_written_override_is_seen(self):
        """覆盖表不缓存：界面存完立刻生效，不用重启服务。"""
        r = self.resolver()
        self.assertEqual(r.lookup(PEN_TABLE_HIT), "sample")
        self.save_override(PEN_TABLE_HIT, "my_sample")
        self.assertEqual(r.lookup(PEN_TABLE_HIT), "my_sample")
        MS.drop_override(self.override_file, PEN_TABLE_HIT)
        self.assertEqual(r.lookup(PEN_TABLE_HIT), "sample")

    def test_lookup_equals_details_effective(self):
        """lookup 走短路、details 走全量，两条路口径必须一致。"""
        cases = [
            (PEN_TABLE_HIT, None), (PEN_TABLE_HIT, "my_sample"), (PEN_TABLE_HIT, ""),
            (PEN_TABLE_EMPTY, None), (PEN_TABLE_EMPTY, "empty_mine"), (PEN_TABLE_EMPTY, ""),
            (PEN_META_ONLY, None), (PEN_META_ONLY, "mine"), (PEN_META_ONLY, ""),
            ("没这支笔.safetensors", None), ("", None),
        ]
        for name, override in cases:
            if self.override_file.exists():
                self.override_file.unlink()
            if override is not None:
                self.save_override(name, override)
            for r in (self.resolver(), self.resolver(with_override=False)):
                self.assertEqual(r.lookup(name), r.details(name)["effective"] or "",
                                 f"{name!r} override={override!r} 两条路不一致")

    def test_for_loras_and_compose_use_override(self):
        """验收 C/E 的单元版：出图链路（for_loras → compose）吃到覆盖。"""
        pos = "newest, safe, 1girl, solo, long hair"
        pens = [{"name": PEN_TABLE_HIT, "strength": 0.65}]
        r = self.resolver()
        self.assertEqual(compose(pos, r.for_loras(pens)),
                         "newest, safe, 1girl, solo, sample, long hair")
        self.save_override(PEN_TABLE_HIT, "my_sample")
        self.assertEqual(compose(pos, r.for_loras(pens)),
                         "newest, safe, 1girl, solo, my_sample, long hair")
        self.assertNotIn("sample,", compose(pos, r.for_loras(pens)).replace("my_sample,", ""))
        self.save_override(PEN_TABLE_HIT, "")
        self.assertEqual(compose(pos, r.for_loras(pens)), pos, "空串覆盖要逐字不变")

    def test_compose_is_untouched_by_override_wiring(self):
        """不改 compose：清理口径仍是「本次所选笔」的 token（T001 已验语义）。"""
        self.assertEqual(compose("newest, 1girl, solo, other_trigger, dress", ["sample"]),
                         "newest, 1girl, solo, sample, other_trigger, dress")


class ModelApiTests(unittest.TestCase):
    """三个模型 API ＋ 必写异常路径：非法笔名 404、超长 400、损坏不崩。"""

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
        # 导入 app.main 会重生 token.txt；还原，别跟跑着的服务对不上
        if cls._token_before is not None:
            cls._token_file.write_text(cls._token_before, encoding="utf-8")

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = fake_root(self.tmp)
        fake_pen(self.root, PEN_TABLE_HIT, metadata={"modelspec.trigger": "meta_sample"})
        fake_pen(self.root, PEN_META_ONLY, metadata={"modelspec.trigger": "meta_new"})
        fake_pen(self.root, "anima-base-v1.0.safetensors", "unet")
        self.table_file = self.tmp / "笔触发词.json"
        self.table_file.write_text(json.dumps({"sample": "sample"}, ensure_ascii=False),
                                   encoding="utf-8")
        self.override_file = self.tmp / "trigger_overrides.json"
        self._saved = (self.main.MODELS_ROOT, self.main.TRIGGERS,
                       self.main.TRIGGER_OVERRIDES_FILE)
        self.main.MODELS_ROOT = self.root
        self.main.TRIGGER_OVERRIDES_FILE = self.override_file
        self.main.TRIGGERS = TriggerResolver(
            self.table_file, self.root / MS.MODEL_DIRS["lora"], self.override_file)

    def tearDown(self):
        (self.main.MODELS_ROOT, self.main.TRIGGERS,
         self.main.TRIGGER_OVERRIDES_FILE) = self._saved
        self._tmp.cleanup()

    def get_models(self):
        return self.client.get("/api/models")

    def save(self, name, trigger):
        return self.client.post("/api/models/trigger", json={"name": name, "trigger": trigger})

    def clear(self, name):
        return self.client.delete("/api/models/trigger?name=" + name)

    def test_needs_token(self):
        r = self.client.get("/api/models", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_shape(self):
        body = self.get_models().json()
        self.assertEqual(sorted(body), ["clip", "lora", "unet", "vae"])
        self.assertEqual([i["name"] for i in body["unet"]], ["anima-base-v1.0.safetensors"])
        pen = {i["name"]: i for i in body["lora"]}[PEN_TABLE_HIT]
        self.assertEqual(sorted(pen), ["mtime", "name", "size", "trigger"])
        self.assertEqual(pen["trigger"]["effective"], "sample")
        self.assertNotIn("trigger", body["unet"][0])

    def test_bootstrap_and_models_agree_on_unet(self):
        """验收 A：底模列表与 /api/bootstrap 的 models.unet 一致。"""
        boot = self.client.get("/api/bootstrap").json()["models"]["unet"]
        scan = [i["name"] for i in self.get_models().json()["unet"]]
        self.assertEqual(boot, scan)

    def test_scan_does_not_touch_files(self):
        before = snapshot(self.root)
        self.get_models()
        self.assertEqual(snapshot(self.root), before)

    def test_save_override(self):
        r = self.save(PEN_TABLE_HIT, "my_sample")
        self.assertEqual(r.status_code, 200)
        trig = r.json()
        self.assertEqual((trig["table"], trig["override"], trig["effective"]),
                         ("sample", "my_sample", "my_sample"))
        self.assertEqual(MS.load_overrides(self.override_file),
                         {"sample_style": "my_sample"})
        listed = {i["name"]: i for i in self.get_models().json()["lora"]}[PEN_TABLE_HIT]
        self.assertEqual(listed["trigger"]["effective"], "my_sample")

    def test_save_accepts_normalized_pen_name(self):
        r = self.save("  Sample_Style ", "my_sample")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["override"], "my_sample")

    def test_save_unknown_pen_is_404(self):
        r = self.save("没这支笔.safetensors", "x")
        self.assertEqual(r.status_code, 404)
        self.assertIsInstance(r.json()["detail"]["human"], str)
        self.assertTrue(r.json()["detail"]["human"])

    def test_save_missing_name_is_404(self):
        self.assertEqual(self.client.post("/api/models/trigger", json={"trigger": "x"}
                                          ).status_code, 404)

    def test_save_too_long_is_400(self):
        r = self.save(PEN_TABLE_HIT, "x" * (MS.MAX_TRIGGER + 1))
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()["detail"]["human"])
        self.assertFalse(self.override_file.exists(), "超长就不该落盘")

    def test_save_max_length_is_ok(self):
        r = self.save(PEN_TABLE_HIT, "x" * MS.MAX_TRIGGER)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["override"]), MS.MAX_TRIGGER)

    def test_save_non_string_is_400(self):
        for bad in (123, None, ["a"], {"a": 1}):
            r = self.save(PEN_TABLE_HIT, bad)
            self.assertEqual(r.status_code, 400, f"{bad!r} 该 400")
            self.assertTrue(r.json()["detail"]["human"])

    def test_save_empty_string_means_no_trigger(self):
        """验收 E：空串覆盖 → 这支笔不产生触发词。"""
        r = self.save(PEN_TABLE_HIT, "")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["effective"], "")
        self.assertEqual(self.main.TRIGGERS.lookup(PEN_TABLE_HIT), "")
        pos = "newest, 1girl, sample, solo, dress"
        self.assertEqual(compose(pos, self.main.TRIGGERS.for_loras(
            [{"name": PEN_TABLE_HIT, "strength": 1.0}])), pos)

    def test_clear_override(self):
        self.save(PEN_TABLE_HIT, "my_sample")
        r = self.clear(PEN_TABLE_HIT)
        self.assertEqual(r.status_code, 200)
        trig = r.json()
        self.assertIsNone(trig["override"])
        self.assertEqual(trig["effective"], "sample")
        self.assertEqual(MS.load_overrides(self.override_file), {})

    def test_clear_unknown_pen_is_404(self):
        self.assertEqual(self.clear("没这支笔.safetensors").status_code, 404)

    def test_clear_without_override_is_idempotent(self):
        """必写异常路径：清除不存在的覆盖 → 200，回到表/元数据。"""
        r = self.clear(PEN_TABLE_HIT)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["override"])
        self.assertEqual(r.json()["effective"], "sample")
        self.assertFalse(self.override_file.exists())

    def test_corrupt_override_file_does_not_break_api(self):
        """验收 F：覆盖文件损坏不崩。"""
        self.override_file.write_text("{ 半截", encoding="utf-8")
        r = self.get_models()
        self.assertEqual(r.status_code, 200)
        pen = {i["name"]: i for i in r.json()["lora"]}[PEN_TABLE_HIT]
        self.assertIsNone(pen["trigger"]["override"])
        self.assertEqual(pen["trigger"]["effective"], "sample")
        self.assertEqual(self.clear(PEN_TABLE_HIT).status_code, 200)

    def test_override_reaches_generation_path(self):
        """验收 C 的单元版：覆盖存完，出图链路的 positive_final 用它。"""
        self.save(PEN_TABLE_HIT, "my_sample")
        pos = "newest, safe, 1girl, solo, long hair"
        got = compose(pos, self.main.TRIGGERS.for_loras(
            [{"name": PEN_TABLE_HIT, "strength": 0.65}]))
        self.assertEqual(got, "newest, safe, 1girl, solo, my_sample, long hair")
        self.clear(PEN_TABLE_HIT)
        got = compose(pos, self.main.TRIGGERS.for_loras(
            [{"name": PEN_TABLE_HIT, "strength": 0.65}]))
        self.assertEqual(got, "newest, safe, 1girl, solo, sample, long hair")

    def test_token_never_in_query(self):
        """雷区 6：清除接口只认 name，不带 token（token 只走 Authorization 头）。"""
        r = self.client.delete("/api/models/trigger?name=" + PEN_TABLE_HIT)
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("token", str(r.request.url).lower())


if __name__ == "__main__":
    unittest.main()
