"""C 卡 §6：图库索引／收藏评分标签／回填／筛选矩阵／对比板／守卫。

盯的是四件在别的层测不到的事：
1. **登记与回填的字段是真的**：从（工作流, profile）抽出的字面值与 PNG 里内嵌的那份逐项一致，
   认不出的图**跳过并计数**，绝不编一个假 seed 出来（卡面铁律 2）。
2. **幂等**：重复回填只 `INSERT OR IGNORE`，主人打过的星星／评分／标签一个字都不许被抹掉。
3. **不碰图**：这里没有任何写图片字节的代码路径；「文件未动」的 sha256 铁证在验收 C 里跑。
4. **对比模式 A 复用 generate**：N 个变体＝N 次同 seed 提交，回执各自完整，不另开出图路。
"""
import copy
import json
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from app import gallery
from app import runner
from app import settings as ST
from app import workflow_profile as wp

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"

T2I_WF, T2I_PROF = wp.load_pair(WORKFLOWS / "anima_t2i.json")
IRISES_WF, IRISES_PROF = wp.load_pair(WORKFLOWS / "chosen_irises.json")
HIRES_WF, HIRES_PROF = wp.load_pair(WORKFLOWS / "anima_hires.json")

# anima_t2i 与 chosen_irises 的 `{节点号: class_type}` 完全一样（实测），所以「认哪份 profile」
# 必须靠 PNG 里那串 `huafang/<工作流号>/<时刻>` 前缀——这正是本文件要钉住的两条路。
PROFILES = {
    "anima_t2i": {"workflow": T2I_WF, "profile": T2I_PROF},
    "chosen_irises": {"workflow": IRISES_WF, "profile": IRISES_PROF},
}


def save_nodes(prof, final=None):
    """按 profile 取保存口节点号：测试也只在 profile 里读节点号，绝不写死（D83）。"""
    return [ss["node"] for ss in prof["save_slots"]
            if final is None or bool(ss.get("final")) is final]


def disk_names(wf, prof):
    """按 profile 的槽位取在盘模型名：exist_fn 要查真文件，编名字会 409。"""
    out = {}
    for slot in prof["model_slots"]:
        out[slot["role"]] = wf[slot["node"]]["inputs"][slot["input"]]
    pens = [{"name": wf[ls["node"]]["inputs"][ls["name_input"]], "strength": 0.8}
            for ls in prof["lora_slots"]]
    return out, pens


T2I_MODELS, T2I_PENS = disk_names(T2I_WF, T2I_PROF)


def t2i_req(**over):
    req = dict(T2I_MODELS)
    req.update({"profile_id": "anima_t2i", "loras": copy.deepcopy(T2I_PENS),
                "positive": "1girl", "negative": "", "width": 832, "height": 1216,
                "count": 1, "seed": 777, "steps": 30, "cfg": 4.0,
                "sampler_name": "euler", "scheduler": "simple"})
    req.update(over)
    return req


def filled(prof=None, wf=None, prefix="huafang/anima_t2i/20261028_000001", **vals):
    """造一份「已经填过值」的工作流副本：节点号只从 profile 读，值按槽位写进去。"""
    prof = prof or T2I_PROF
    wf = copy.deepcopy(wf or T2I_WF)
    st = prof["stages"][0]
    for key in ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"):
        ref = st["params"].get(key)
        if ref and key in vals:
            wf[ref["node"]]["inputs"][ref["input"]] = vals[key]
    for which in ("positive", "negative"):
        src = (st.get("prompt") or {}).get(which + "_source") or []
        if src and which in vals:
            wf[src[0]["node"]]["inputs"][src[0]["input"]] = vals[which]
    lz = st.get("latent_size")
    if lz and "width" in vals:
        scale = lz.get("size_scale") or [1.0, 1.0]
        wf[lz["node"]]["inputs"][lz["width_input"]] = int(vals["width"] * scale[0])
        wf[lz["node"]]["inputs"][lz["height_input"]] = int(vals["height"] * scale[1])
    for ss in prof["save_slots"]:
        wf[ss["node"]]["inputs"][ss["prefix_input"]] = prefix
    return wf


def write_png(path: Path, prompt: dict) -> Path:
    """造一张带 ComfyUI `prompt` tEXt 块的最小 PNG（块结构与真图一致，像素无所谓）。"""
    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))
    body = (chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"prompt\x00" + json.dumps(prompt, ensure_ascii=False).encode("utf-8"))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + body)
    return path


def hist_entry(pid, workflow, outputs, create_time=1761600000000):
    """/history 一条记录的真形状：prompt 是 list，client_id 与 create_time 在 prompt[3]（雷区 1）。"""
    return {"prompt": [1, pid, workflow,
                       {"client_id": "huafang", "create_time": create_time}, []],
            "outputs": outputs,
            "status": {"status_str": "success", "completed": True, "messages": []},
            "extra_data": None}


class FakeHistory:
    """只实现回填会调的 history_recent；炉子没开时它就是抛异常那一路。"""

    def __init__(self, entries=None, boom=False):
        self.entries = entries or {}
        self.boom = boom

    def history_recent(self, max_items=50):
        if self.boom:
            raise RuntimeError("连不上炉子")
        return self.entries


class MultiComfy(FakeHistory):
    """按 prompt_id 各记各的提交：compare 一次提 N 个，history 必须各回各的那份工作流。"""

    def __init__(self, prof=None):
        super().__init__()
        self.prof = prof or T2I_PROF
        self.submitted = {}
        self.n = 0

    def submit(self, workflow, client_id):
        self.n += 1
        pid = f"pid_{self.n}"
        self.submitted[pid] = workflow
        return pid

    def queue(self):
        return {"queue_running": [], "queue_pending": []}

    def history(self, prompt_id):
        wf = self.submitted.get(prompt_id)
        if wf is None:
            return None
        outputs = {nid: {"images": [{"filename": f"cmp_{prompt_id}_{nid}.png",
                                     "subfolder": "huafang/anima_t2i", "type": "output"}]}
                   for nid in save_nodes(self.prof, final=True)}
        return hist_entry(prompt_id, wf, outputs)

    def view_bytes(self, filename, subfolder="", type="output"):
        return b"\x89PNG\r\n\x1a\n fake bytes"


def row_of(**over):
    """一行索引的最小合法形状（NOT NULL 列都有值），测试改哪项就写哪项。"""
    row = {"filename": "x_00001_.png", "subfolder": "huafang/anima_t2i", "type": "output",
           "kind": "final", "source": "job", "servable": 1, "path": "",
           "workflow_id": "anima_t2i", "seed": 777, "steps": 30, "cfg": 4.0,
           "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
           "width": 832, "height": 1216, "count": 1,
           "positive": "1girl, blue sky", "negative": "bad hands",
           "unet": "anima-base-v1.0.safetensors", "clip": "qwen_3_06b_base.safetensors",
           "vae": "qwen_image_vae.safetensors", "pens": "[]",
           "created": "2026-10-27T15:12:03"}
    row.update(over)
    return row


class TempGallery(unittest.TestCase):
    """每个用例一个临时库；跑完整个临时目录删掉（真机验收用临时目录，跑完清理）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hf_c_"))
        self.gal = gallery.Gallery(self.tmp / "gallery.sqlite")

    def tearDown(self):
        self.gal.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------- 登记／幂等
class RegisterJobTests(TempGallery):
    def test_done_job_lands_with_request_values_winning(self):
        """卡 §1 登记时机：job 到 done → 每张成品一行，界面要的值盖过工作流字面值。"""
        job = runner.Job("j1", {"seed": 4242, "width": 1024, "height": 1024,
                                "positive": "1girl, night", "negative": "lowres"})
        job.profile = T2I_PROF
        job.sent = filled(seed=1, positive="", negative="", width=832, height=1216)
        job.positive_final = "1girl, night, trigger_word"
        job.created = "2026-10-29T01:02:03"
        job.images = [{"filename": "a_00001_.png", "subfolder": "huafang/anima_t2i"},
                      {"filename": "a_00002_.png", "subfolder": "huafang/anima_t2i"}]
        self.assertEqual(self.gal.register_job(job), 2)
        items = self.gal.query({})["items"]
        self.assertEqual(len(items), 2)
        got = {it["filename"]: it for it in items}["a_00001_.png"]
        self.assertEqual(got["seed"], 4242)
        self.assertEqual((got["width"], got["height"]), (1024, 1024))
        self.assertEqual(got["positive"], "1girl, night, trigger_word")   # 触发词入链后的那一份
        self.assertEqual(got["negative"], "lowres")
        self.assertEqual(got["kind"], "final")
        self.assertEqual(got["source"], "job")
        self.assertEqual(got["created"], "2026-10-29T01:02:03")
        self.assertEqual(got["workflow_id"], "anima_t2i")
        self.assertEqual(got["unet"], T2I_MODELS["unet"])
        self.assertEqual(got["pens"][0]["name"], T2I_PENS[0]["name"])
        self.assertEqual(got["steps"], 30)          # 界面没管的项＝工作流字面值
        self.assertEqual(got["fav"], False)
        self.assertEqual(got["rating"], 0)
        self.assertEqual(got["tags"], [])

    def test_keys_the_ui_never_sends_keep_workflow_literals(self):
        """界面根本没发 clip/vae：不许填 None 冒充「主人没填」（回执同一口径）。"""
        job = runner.Job("j2", {"seed": 5})
        job.profile = T2I_PROF
        job.sent = filled(seed=99)
        job.images = [{"filename": "b_00001_.png", "subfolder": ""}]
        self.gal.register_job(job)
        got = self.gal.query({})["items"][0]
        self.assertEqual(got["clip"], T2I_MODELS["clip"])
        self.assertEqual(got["vae"], T2I_MODELS["vae"])
        self.assertEqual(got["width"], 832)          # 请求没给尺寸 → 用工作流字面值
        self.assertEqual(got["seed"], 5)

    def test_same_job_twice_is_idempotent(self):
        job = runner.Job("j3", {"seed": 1})
        job.profile = T2I_PROF
        job.sent = filled()
        job.images = [{"filename": "c_00001_.png", "subfolder": "huafang/anima_t2i"}]
        self.assertEqual(self.gal.register_job(job), 1)
        self.assertEqual(self.gal.register_job(job), 0)
        self.assertEqual(self.gal.stats()["total"], 1)

    def test_backslash_and_slash_subfolders_are_the_same_file(self):
        """炉子回话里的 subfolder 是照抄画舫提交的 filename_prefix（Windows 上反斜杠），
        扫盘那一路用 as_posix() 给正斜杠。不折成一种写法，同一张图就登两行——
        主人在其中一行上打的收藏／评分／标签，另一行压根不知道。"""
        job = runner.Job("j5", {"seed": 7})
        job.profile = T2I_PROF
        job.sent = filled()
        job.images = [{"filename": "e_00001_.png", "subfolder": "huafang\\anima_t2i"}]
        self.assertEqual(self.gal.register_job(job), 1)
        job.images = [{"filename": "e_00001_.png", "subfolder": "huafang/anima_t2i"}]
        self.assertEqual(self.gal.register_job(job), 0)
        self.assertEqual(self.gal.stats()["total"], 1)
        self.assertEqual(self.gal.query({})["items"][0]["subfolder"], "huafang/anima_t2i")

    def test_readonly_job_without_profile_registers_nothing(self):
        """只读记录没有 profile（雷区 4）：不登记，也不许崩。"""
        job = runner.Job("j4", {})
        job.images = [{"filename": "d_00001_.png", "subfolder": ""}]
        self.assertEqual(self.gal.register_job(job), 0)
        self.assertEqual(self.gal.stats()["total"], 0)

    def test_broken_db_does_not_turn_generation_into_failure(self):
        """索引库写不进去：register_job 回 -1，绝不把已经成功的出图弄成失败。"""
        gal = gallery.Gallery(self.tmp / "gallery.sqlite")
        gal._connect().execute("DROP TABLE images")     # 造一个写不进去的库
        gal._conn.commit()
        job = runner.Job("j5", {"seed": 1})
        job.profile = T2I_PROF
        job.sent = filled()
        job.images = [{"filename": "e_00001_.png", "subfolder": ""}]
        self.assertEqual(gal.register_job(job), -1)
        gal.close()


# ---------------------------------------------------------------------- 收藏／评分／标签
class PatchTests(TempGallery):
    def setUp(self):
        super().setUp()
        self.gal.insert(row_of(filename="f_00001_.png"))
        self.id = self.gal.query({})["items"][0]["id"]

    def test_each_field_updates_alone_and_does_not_bleed(self):
        self.gal.patch(self.id, {"fav": True})
        got = self.gal.get(self.id)
        self.assertTrue(got["fav"])
        self.assertEqual(got["rating"], 0)
        self.assertEqual(got["tags"], [])

        self.gal.patch(self.id, {"rating": 3})
        got = self.gal.get(self.id)
        self.assertEqual(got["rating"], 3)
        self.assertTrue(got["fav"], "改评分不许把收藏抹掉")

        self.gal.patch(self.id, {"tags": ["夜景", " Anima "]})
        got = self.gal.get(self.id)
        self.assertEqual(got["tags"], ["夜景", "Anima"])      # 去空格、去重
        self.assertEqual(got["rating"], 3)
        self.assertTrue(got["fav"])

    def test_unfav_and_zero_rating_are_legit_values(self):
        self.gal.patch(self.id, {"fav": True, "rating": 5})
        got = self.gal.patch(self.id, {"fav": False, "rating": 0})
        self.assertFalse(got["fav"])
        self.assertEqual(got["rating"], 0)

    def test_tags_accept_a_comma_sentence(self):
        got = self.gal.patch(self.id, {"tags": "白天，海边, 海边"})
        self.assertEqual(got["tags"], ["白天", "海边"])

    def test_bad_values_are_400_in_plain_chinese(self):
        cases = [({"rating": 6}, "bad_rating"), ({"rating": -1}, "bad_rating"),
                 ({"rating": "五"}, "bad_rating"), ({"rating": True}, "bad_rating"),
                 ({"fav": "也许"}, "bad_fav"), ({"tags": {"a": 1}}, "bad_tags"),
                 ({"tags": [1, 2]}, "bad_tags"),
                 ({"tags": ["x" * (gallery.MAX_TAG_LEN + 1)]}, "bad_tags"),
                 ({"tags": [str(i) for i in range(gallery.MAX_TAGS + 1)]}, "bad_tags"),
                 ({}, "empty_patch"), ({"workflow_id": "别的"}, "empty_patch")]
        for patch, code in cases:
            with self.subTest(patch=patch):
                with self.assertRaises(gallery.GalleryError) as ctx:
                    self.gal.patch(self.id, patch)
                self.assertEqual(ctx.exception.code, code)
                self.assertTrue(ctx.exception.human)          # 必须带人话

    def test_patch_refuses_to_touch_other_columns(self):
        """收藏／评分／标签之外一个字段都不许改：连 seed 都改不了（卡面「只进索引侧」的边界）。"""
        before = self.gal.get(self.id)
        self.gal.patch(self.id, {"fav": True, "seed": 1, "filename": "改名.png",
                                 "kind": "intermediate"})
        after = self.gal.get(self.id)
        self.assertEqual(after["seed"], before["seed"])
        self.assertEqual(after["filename"], before["filename"])
        self.assertEqual(after["kind"], before["kind"])
        self.assertTrue(after["fav"])

    def test_missing_image_is_a_plain_error_not_a_silent_success(self):
        with self.assertRaises(gallery.GalleryError) as ctx:
            self.gal.patch(999999, {"fav": True})
        self.assertEqual(ctx.exception.code, "no_such_image")
        self.assertEqual(self.gal.stats()["fav"], 0)

    def test_tags_listing_counts_and_sorts(self):
        self.gal.patch(self.id, {"tags": ["夜景"]})
        self.gal.insert(row_of(filename="g_00001_.png"))
        other = self.gal.query({"q": "g_00001"})["items"][0]["id"]
        self.gal.patch(other, {"tags": ["夜景", "海边"]})
        tags = {t["tag"]: t["count"] for t in self.gal.tags()}
        self.assertEqual(tags, {"夜景": 2, "海边": 1})
        self.assertEqual(self.gal.tags()[0]["tag"], "夜景")     # 用量多的在前


# ---------------------------------------------------------------------- 坏库重建
class CorruptDbTests(TempGallery):
    def test_corrupt_db_is_moved_aside_and_rebuilt(self):
        """卡 §6：sqlite 损坏 → 重建不崩（与 settings `_read_dict` 同哲学：坏文件不删，只让路）。"""
        bad = self.tmp / "corrupt.sqlite"
        bad.write_bytes("根本不是 sqlite 文件".encode("utf-8") * 40)
        gal = gallery.Gallery(bad)
        self.assertEqual(gal.stats()["total"], 0)               # 不崩，回一个空库
        self.assertTrue(gal.rebuilt["moved"])
        self.assertTrue(Path(gal.rebuilt["bad"]).exists())       # 坏库挪开留着，主人查得到为什么
        self.assertTrue(bad.exists())                            # 新库仍建在规范路径上
        self.assertEqual(gal.insert(row_of()), True)             # 重建后照样能写
        gal.close()

    def test_locked_corrupt_db_switches_to_a_new_name_instead_of_crashing(self):
        """坏文件被别的进程攥着挪不动（Windows 常事）：换新库名接着干活，坏文件原地留着。"""
        bad = self.tmp / "locked.sqlite"
        bad.write_bytes("也不是 sqlite".encode("utf-8") * 40)
        gal = gallery.Gallery(bad)
        holder = open(bad, "rb")                                 # 攥住句柄，replace 就会 OSError
        try:
            self.assertEqual(gal.stats()["total"], 0)
        finally:
            holder.close()
        self.assertFalse(gal.rebuilt["moved"])
        self.assertTrue(bad.exists())
        self.assertNotEqual(Path(gal.rebuilt["now"]), bad)
        self.assertEqual(gal.insert(row_of()), True)
        gal.close()


# ---------------------------------------------------------------------- 筛选矩阵
class FilterMatrixTests(TempGallery):
    def setUp(self):
        super().setUp()
        rows = [
            row_of(filename="m1_00001_.png", workflow_id="anima_t2i", seed=11,
                   width=832, height=1216, created="2026-10-20T10:00:00",
                   positive="1girl, blue sky", negative="bad hands", source="job",
                   pens=json.dumps([{"role": "lora1", "name": "style_a.safetensors",
                                     "strength": 0.8, "stages": ["stage1"]}])),
            row_of(filename="m2_00001_.png", workflow_id="anima_tri", seed=22,
                   width=1024, height=1024, created="2026-10-25T10:00:00",
                   positive="2girls, night", negative="", source="disk", kind="intermediate",
                   unet="anima-aesthetic-v1.1.safetensors"),
            row_of(filename="m3_00001_.png", workflow_id="anima_t2i", seed=33,
                   width=512, height=512, created="2026-10-28T10:00:00",
                   positive="1boy, school", negative="lowres", source="history"),
        ]
        for r in rows:
            self.assertTrue(self.gal.insert(r), r["filename"])
        ids = {it["filename"]: it["id"] for it in self.gal.query({"show_intermediate": "1"})["items"]}
        self.gal.patch(ids["m1_00001_.png"], {"fav": True, "rating": 5, "tags": ["收藏级"]})
        self.gal.patch(ids["m3_00001_.png"], {"rating": 2, "tags": ["收藏级", "练习"]})
        self.ids = ids

    def names(self, f):
        return sorted(i["filename"] for i in self.gal.query(f)["items"])

    def test_default_list_hides_intermediate_but_kind_filter_can_ask_for_it(self):
        self.assertEqual(self.names({}), ["m1_00001_.png", "m3_00001_.png"])
        self.assertEqual(self.names({"show_intermediate": "1"}),
                         ["m1_00001_.png", "m2_00001_.png", "m3_00001_.png"])
        self.assertEqual(self.names({"kind": "intermediate"}), ["m2_00001_.png"])
        self.assertEqual(self.names({"kind": "unknown"}), [])

    def test_workflow_filter(self):
        self.assertEqual(self.names({"workflow": "anima_tri"}), [])       # tri 那张是中间图，默认藏
        self.assertEqual(self.names({"workflow": "anima_tri", "show_intermediate": "1"}),
                         ["m2_00001_.png"])
        self.assertEqual(self.names({"workflow": "anima_t2i"}),
                         ["m1_00001_.png", "m3_00001_.png"])
        self.assertEqual(self.names({"workflow": "没这个工作流"}), [])

    def test_model_filters(self):
        self.assertEqual(self.names({"unet": "anima-aesthetic-v1.1.safetensors",
                                     "show_intermediate": "1"}), ["m2_00001_.png"])
        self.assertEqual(self.names({"unet": "anima-base-v1.0.safetensors"}),
                         ["m1_00001_.png", "m3_00001_.png"])
        self.assertEqual(self.names({"clip": T2I_MODELS["clip"]}),
                         ["m1_00001_.png", "m3_00001_.png"])
        self.assertEqual(self.names({"clip": "不存在的clip"}), [])
        self.assertEqual(self.names({"vae": T2I_MODELS["vae"]}),
                         ["m1_00001_.png", "m3_00001_.png"])
        self.assertEqual(self.names({"vae": "不存在的vae"}), [])

    def test_source_filter(self):
        self.assertEqual(self.names({"source": "history"}), ["m3_00001_.png"])
        self.assertEqual(self.names({"source": "job"}), ["m1_00001_.png"])
        self.assertEqual(self.names({"source": "disk"}), [])

    def test_fav_only(self):
        self.assertEqual(self.names({"fav_only": "1"}), ["m1_00001_.png"])
        self.assertEqual(self.names({"fav_only": "true"}), ["m1_00001_.png"])
        self.assertEqual(self.names({"fav_only": "0"}), ["m1_00001_.png", "m3_00001_.png"])
        self.assertEqual(self.names({"fav_only": "坏值"}),
                         ["m1_00001_.png", "m3_00001_.png"])   # 筛选里的坏值只忽略，不 400

    def test_rating_min(self):
        self.assertEqual(self.names({"rating_min": "2"}), ["m1_00001_.png", "m3_00001_.png"])
        self.assertEqual(self.names({"rating_min": "5"}), ["m1_00001_.png"])
        self.assertEqual(self.names({"rating_min": "6"}), ["m1_00001_.png"])   # 夹到 5＝只看五星
        self.assertEqual(self.names({"rating_min": "0"}), ["m1_00001_.png", "m3_00001_.png"])

    def test_size_range(self):
        self.assertEqual(self.names({"w_min": "1024", "show_intermediate": "1"}), ["m2_00001_.png"])
        self.assertEqual(self.names({"w_max": "512"}), ["m3_00001_.png"])
        self.assertEqual(self.names({"h_min": "1216"}), ["m1_00001_.png"])
        self.assertEqual(self.names({"h_max": "1024"}), ["m3_00001_.png"])
        self.assertEqual(self.names({"w_min": "512", "w_max": "832"}),
                         ["m1_00001_.png", "m3_00001_.png"])
        self.assertEqual(self.names({"w_min": "2000"}), [])

    def test_date_range(self):
        self.assertEqual(self.names({"date_from": "2026-10-25"}), ["m3_00001_.png"])
        self.assertEqual(self.names({"date_to": "2026-10-25", "show_intermediate": "1"}),
                         ["m1_00001_.png", "m2_00001_.png"])
        self.assertEqual(self.names({"date_from": "2026-10-28", "date_to": "2026-10-28"}),
                         ["m3_00001_.png"])                    # 「到某天」含那天整天
        self.assertEqual(self.names({"date_from": "2026-11-01"}), [])

    def test_tag_filter(self):
        self.assertEqual(self.names({"tag": "练习"}), ["m3_00001_.png"])
        self.assertEqual(self.names({"tag": "收藏级"}), ["m1_00001_.png", "m3_00001_.png"])
        self.assertEqual(self.names({"tag": "没有这个标签"}), [])
        self.assertEqual(self.names({"tag": '收藏级", "path": "--'}), [])   # 引号被吃掉，不拼出坏 SQL

    def test_pen_filter(self):
        self.assertEqual(self.names({"pen": "style_a"}), ["m1_00001_.png"])
        self.assertEqual(self.names({"pen": "style_zzz"}), [])

    def test_keyword_search_hits_positive_negative_and_filename(self):
        self.assertEqual(self.names({"q": "blue sky"}), ["m1_00001_.png"])
        self.assertEqual(self.names({"q": "lowres"}), ["m3_00001_.png"])   # 负词也搜得到
        self.assertEqual(self.names({"q": "m2_00001"}), [])                # 中间图默认藏
        self.assertEqual(self.names({"q": "m2_00001", "show_intermediate": "1"}),
                         ["m2_00001_.png"])
        self.assertEqual(self.names({"q": "绝无此词"}), [])

    def test_combined_filters_intersect(self):
        self.assertEqual(self.names({"workflow": "anima_t2i", "fav_only": "1"}), ["m1_00001_.png"])
        self.assertEqual(self.names({"workflow": "anima_t2i", "fav_only": "1", "rating_min": "5"}),
                         ["m1_00001_.png"])
        self.assertEqual(self.names({"workflow": "anima_t2i", "fav_only": "1", "q": "school"}), [])
        self.assertEqual(self.names({"tag": "收藏级", "q": "school"}), ["m3_00001_.png"])
        self.assertEqual(self.names({"w_min": "512", "h_max": "512", "source": "history"}),
                         ["m3_00001_.png"])

    def test_pagination(self):
        out = self.gal.query({"per_page": "1", "page": "1", "show_intermediate": "1"})
        self.assertEqual((out["total"], out["pages"], out["per_page"]), (3, 3, 1))
        self.assertEqual([i["filename"] for i in out["items"]], ["m3_00001_.png"])   # created 倒序
        self.assertEqual([i["filename"] for i in
                          self.gal.query({"per_page": "1", "page": "3",
                                          "show_intermediate": "1"})["items"]],
                         ["m1_00001_.png"])
        self.assertEqual(self.gal.query({"per_page": "1", "page": "99"})["items"], [])
        self.assertEqual(self.gal.query({"per_page": "99999"})["per_page"], gallery.PER_PAGE_MAX)
        self.assertEqual(self.gal.query({"per_page": "abc"})["per_page"], 60)        # 坏值兜默认

    def test_stats(self):
        self.assertEqual(self.gal.stats(), {"total": 3, "fav": 1, "intermediate": 1})


# ---------------------------------------------------------------------- 回填（磁盘）
class DiskBackfillTests(TempGallery):
    def setUp(self):
        super().setUp()
        self.furnace = self.tmp / "furnace"
        self.extra = self.tmp / "extra"
        # 炉子里两张成品＋一张中间图（hires 的中间口），前缀照 runner 的写法带工作流号
        write_png(self.furnace / "huafang/anima_t2i/20261028_000001_00001_.png",
                  filled(prefix="huafang/anima_t2i/20261028_000001", seed=101,
                         positive="1girl, spring", negative="worst quality",
                         width=832, height=1216))
        write_png(self.furnace / "huafang/anima_t2i/20261028_000002_00001_.png",
                  filled(prefix="huafang/anima_t2i/20261028_000002", seed=202,
                         positive="1girl, winter"))
        write_png(self.furnace / "huafang/chosen_irises/20261028_000003_00001_.png",
                  filled(prof=IRISES_PROF, wf=IRISES_WF,
                         prefix="huafang/chosen_irises/20261028_000003", seed=303))
        # 附加目录：一张 T010 副本（与炉子原件同名）、一张主人自己挪过位置的、一张认不出的、一个非图片文件
        write_png(self.extra / "20261028_000001_00001_.png",
                  filled(prefix="huafang/anima_t2i/20261028_000001", seed=101))
        write_png(self.extra / "主人挑出来的_00001_.png",
                  filled(prefix="huafang/anima_t2i/20261001_000000", seed=404))
        write_png(self.extra / "ba-library_00001_.png", filled(prefix="ba-library", seed=505))
        (self.extra / "说明.txt").write_text("不是图", encoding="utf-8")

    def backfill(self, dirs=None, furnace=None, client=None):
        return self.gal.backfill(client=client, profiles=PROFILES,
                                 dirs=[str(self.extra)] if dirs is None else dirs,
                                 furnace_root=str(self.furnace) if furnace is None else furnace)

    def test_furnace_originals_are_attributed_from_their_own_embedded_workflow(self):
        rep = self.backfill(dirs=[])
        self.assertEqual((rep["scanned"], rep["added"], rep["skipped"]), (3, 3, 0))
        self.assertEqual(rep["reasons"], {})
        got = {i["filename"]: i for i in self.gal.query({"per_page": 50})["items"]}
        self.assertEqual(sorted(got), ["20261028_000001_00001_.png", "20261028_000002_00001_.png",
                                       "20261028_000003_00001_.png"])
        one = got["20261028_000001_00001_.png"]
        self.assertEqual(one["seed"], 101)
        self.assertEqual(one["positive"], "1girl, spring")
        self.assertEqual(one["negative"], "worst quality")
        self.assertEqual((one["width"], one["height"]), (832, 1216))
        self.assertEqual(one["workflow_id"], "anima_t2i")
        self.assertEqual(one["subfolder"], "huafang/anima_t2i")
        self.assertEqual(one["source"], "disk")
        self.assertEqual(one["servable"], 1)
        self.assertEqual(one["kind"], "final")
        self.assertEqual(got["20261028_000003_00001_.png"]["workflow_id"], "chosen_irises")

    def test_structurally_identical_profiles_are_told_apart_by_the_prefix(self):
        """anima_t2i 与 chosen_irises 结构全等：靠前缀里的工作流号认，认不出就跳过（铁律 2）。"""
        rep = self.backfill(dirs=[])
        self.assertEqual(rep["added"], 3)
        ambiguous = self.tmp / "amb"
        write_png(ambiguous / "ba-rooftop_00001_.png", filled(prefix="ba-rooftop", seed=9))
        rep2 = self.gal.backfill(client=None, profiles=PROFILES, dirs=[str(ambiguous)],
                                 furnace_root=None)
        self.assertEqual((rep2["added"], rep2["skipped"]), (0, 1))
        self.assertIn("认不出这份图是哪个工作流出的", list(rep2["reasons"])[0])

    def test_extra_dir_rows_are_marked_unservable_and_copies_are_not_duplicated(self):
        rep = self.backfill()
        self.assertEqual(rep["dirs"], [str(self.furnace), str(self.extra)])   # 炉子先扫
        got = {i["filename"]: i for i in self.gal.query({"per_page": 50})["items"]}
        # T010 副本与炉子原件同名 → 不再登一行，否则同一张图在图库里出现两次
        self.assertEqual(got["20261028_000001_00001_.png"]["servable"], 1)
        self.assertEqual(got["20261028_000001_00001_.png"]["subfolder"], "huafang/anima_t2i")
        self.assertEqual(rep["already"], 1)
        # 附加目录里独有的图：登进来，但取不到字节（/api/view 是炉子的代理）
        own = got["主人挑出来的_00001_.png"]
        self.assertEqual(own["servable"], 0)
        self.assertEqual(own["seed"], 404)
        self.assertEqual(own["path"], str(self.extra / "主人挑出来的_00001_.png"))
        self.assertEqual(own["workflow_id"], "anima_t2i")   # 认工作流靠图里内嵌的前缀，不靠它躺在哪
        self.assertEqual(own["kind"], "unknown")     # 文件名对不上任何保存口 → 不猜是不是成品

    def test_backfill_is_idempotent_and_never_touches_favourites(self):
        target = self.tmp / "furnace/huafang/anima_t2i/20261028_000001_00001_.png"
        first = self.backfill()
        self.assertEqual((first["scanned"], first["added"], first["already"], first["skipped"]),
                         (7, 4, 1, 2))
        self.gal.patch(self.gal.query({"q": "spring"})["items"][0]["id"],
                       {"fav": True, "rating": 5, "tags": ["主人旧图"]})
        before = target.read_bytes()
        second = self.backfill()
        third = self.backfill()
        self.assertEqual((second["added"], third["added"]), (0, 0))
        self.assertEqual(second["already"], 5)              # 3 张原件＋1 张副本＋1 张附加目录独有图
        self.assertEqual(self.gal.stats()["total"], first["added"])
        fav = self.gal.query({"fav_only": "1"})["items"][0]
        self.assertEqual((fav["rating"], fav["tags"]), (5, ["主人旧图"]))
        self.assertEqual(before, target.read_bytes(), "回填只读：图片字节一个都不许变")

    def test_overlapping_dirs_are_not_scanned_twice(self):
        rep = self.gal.backfill(client=None, profiles=PROFILES,
                                dirs=[str(self.furnace), str(self.furnace / "huafang"),
                                      str(self.extra)],
                                furnace_root=str(self.furnace))
        self.assertEqual(rep["dirs"], [str(self.furnace), str(self.extra)])
        self.assertEqual(rep["scanned"], 7)                 # 4（炉子）＋3（附加目录里的图）
        self.assertIn("目录与已扫目录重叠，跳过", "".join(rep["reasons"]))

    def test_missing_dir_and_dead_furnace_only_cost_a_skip(self):
        rep = self.gal.backfill(client=FakeHistory(boom=True), profiles=PROFILES,
                                dirs=[str(self.tmp / "根本没有这个目录")],
                                furnace_root=str(self.furnace))
        self.assertEqual(rep["added"], 3)                   # 少两个来源也照样把能登的登进来
        reasons = "".join(rep["reasons"])
        self.assertIn("炉子历史读不出来", reasons)
        self.assertIn("目录不存在", reasons)

    def test_intermediate_images_are_indexed_but_hidden_by_default(self):
        """D92 同一口径：中间图入库（可筛），默认列表不显示。"""
        mid = self.tmp / "mid"
        prefix = "huafang/hires_mid"
        wf = copy.deepcopy(HIRES_WF)
        for ss in HIRES_PROF["save_slots"]:
            wf[ss["node"]]["inputs"][ss["prefix_input"]] = (
                prefix if not ss.get("final") else f"huafang/anima_hires/{prefix}")
        write_png(mid / "huafang" / "hires_mid_00001_.png", wf)
        rep = self.gal.backfill(client=None, profiles={"anima_hires": {
            "workflow": HIRES_WF, "profile": HIRES_PROF}}, dirs=[str(mid)], furnace_root=None)
        self.assertEqual(rep["added"], 1)
        self.assertEqual(self.gal.query({})["total"], 0)
        self.assertEqual(self.gal.query({"show_intermediate": "1"})["items"][0]["kind"],
                         "intermediate")

    def test_jpeg_without_prompt_chunk_is_skipped_not_guessed(self):
        d = self.tmp / "jpgs"
        d.mkdir()
        (d / "no_meta.jpg").write_bytes(b"\xff\xd8\xff\xe0" + "这不是 PNG".encode("utf-8"))
        rep = self.gal.backfill(client=None, profiles=PROFILES, dirs=[str(d)], furnace_root=None)
        self.assertEqual((rep["scanned"], rep["added"], rep["skipped"]), (1, 0, 1))
        self.assertIn("读不出图里内嵌的工作流", list(rep["reasons"])[0])


# ---------------------------------------------------------------------- 回填（炉子历史）
class HistoryBackfillTests(TempGallery):
    def entries(self):
        wf = filled(prefix="huafang/anima_t2i/20261028_000001", seed=101, positive="1girl, spring")
        outs = {nid: {"images": [{"filename": f"h_{nid}_00001_.png",
                                  "subfolder": "huafang/anima_t2i", "type": "output"}]}
                for nid in save_nodes(T2I_PROF, final=True)}
        return {"pid1": hist_entry("pid1", wf, outs)}

    def test_history_entries_are_registered_with_their_own_values(self):
        rep = self.gal.backfill(client=FakeHistory(self.entries()), profiles=PROFILES,
                                dirs=[], furnace_root=None)
        self.assertEqual((rep["history_entries"], rep["added"], rep["scanned"]), (1, 1, 1))
        got = self.gal.query({})["items"][0]
        self.assertEqual(got["source"], "history")
        self.assertEqual(got["seed"], 101)
        self.assertEqual(got["positive"], "1girl, spring")
        self.assertEqual(got["kind"], "final")
        self.assertEqual(got["servable"], 1)
        self.assertTrue(got["created"].startswith("2025-"))     # create_time 换出来的时刻

    def test_history_backfill_is_idempotent(self):
        self.gal.backfill(client=FakeHistory(self.entries()), profiles=PROFILES,
                          dirs=[], furnace_root=None)
        rep = self.gal.backfill(client=FakeHistory(self.entries()), profiles=PROFILES,
                                dirs=[], furnace_root=None)
        self.assertEqual((rep["added"], rep["already"]), (0, 1))
        self.assertEqual(self.gal.stats()["total"], 1)

    def test_history_of_an_unknown_workflow_is_skipped_and_counted(self):
        alien = {"1": {"class_type": "完全没有这种节点", "inputs": {}}}
        rep = self.gal.backfill(client=FakeHistory({"pid9": hist_entry("pid9", alien, {})}),
                                profiles=PROFILES, dirs=[], furnace_root=None)
        self.assertEqual((rep["scanned"], rep["added"], rep["skipped"]), (1, 0, 1))
        self.assertIn("history 工作流对不上任何 profile", "".join(rep["reasons"]))

    def test_malformed_history_entry_is_skipped_not_crashed(self):
        rep = self.gal.backfill(client=FakeHistory({"pid": "不是字典", "pid2": {"outputs": "也不是字典"}}),
                                profiles=PROFILES, dirs=[], furnace_root=None)
        self.assertEqual(rep["added"], 0)
        self.assertIn("history 条目结构不对", "".join(rep["reasons"]))


# ---------------------------------------------------------------------- PNG 解析
class PngPromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hf_c_png_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_the_prompt_chunk_and_nothing_else(self):
        p = write_png(self.tmp / "a_00001_.png", filled(seed=7))
        emb = gallery.png_prompt(p)
        self.assertEqual(gallery._shape(emb), gallery._shape(T2I_WF))
        self.assertEqual(gallery.Gallery.attrs_of(emb, T2I_PROF)["seed"], 7)

    def test_non_png_and_truncated_png_return_none(self):
        (self.tmp / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0 nope")
        self.assertIsNone(gallery.png_prompt(self.tmp / "b.jpg"))
        good = write_png(self.tmp / "c_00001_.png", filled())
        good.write_bytes(good.read_bytes()[:60])            # 半截文件
        self.assertIsNone(gallery.png_prompt(good))
        self.assertIsNone(gallery.png_prompt(self.tmp / "根本不存在.png"))

    def test_counter_suffix_strips_to_the_submitted_prefix(self):
        self.assertEqual(gallery._COUNTER.sub("", "huafang/anima_t2i/20261028_000001_00001_.png"),
                         "huafang/anima_t2i/20261028_000001")
        self.assertEqual(gallery._COUNTER.sub("", "hires_mid_00012_.png"), "hires_mid")

    def test_prefix_node_resolves_the_exact_save_slot(self):
        wf = filled(prefix="huafang/anima_t2i/x")
        nid = save_nodes(T2I_PROF, final=True)[0]
        self.assertEqual(gallery.prefix_node(wf, "huafang/anima_t2i/x"), nid)
        self.assertIsNone(gallery.prefix_node(wf, "别的前缀"))


class EndpointBase(unittest.TestCase):
    """端点层：TestClient＋真中间件＋假 Comfy（不引 mock 框架），图库一律用临时库。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.settings import TOKEN_FILE
        cls._token_file = TOKEN_FILE
        cls._token_before = (TOKEN_FILE.read_text(encoding="utf-8")
                             if TOKEN_FILE.exists() else None)
        from app import main
        cls.main = main
        cls.http = TestClient(main.app, base_url="http://127.0.0.1:8788",
                              headers={"Authorization": "Bearer " + main.TOKEN})

    @classmethod
    def tearDownClass(cls):
        # 导入 app.main 会重生 token.txt；还原，别跟跑着的服务对不上（B 卡裁定第 5 款）
        if cls._token_before is not None:
            cls._token_file.write_text(cls._token_before, encoding="utf-8")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hf_c_ep_"))
        self._gal = self.main.GAL
        self.main.GAL = gallery.Gallery(self.tmp / "gallery.sqlite")
        self._client = self.main.CLIENT
        self._jobs = dict(runner.JOBS)
        runner.JOBS.clear()
        self._settings = copy.deepcopy(self.main.SETTINGS)
        self.main.SETTINGS["output"] = {"dir": ""}          # 端点测试不往主人目录里倒图（T010）
        self.main.SETTINGS["output_dir"] = str(self.tmp / "furnace")

    def tearDown(self):
        self.main.GAL.close()
        self.main.GAL = self._gal
        self.main.SETTINGS.clear()
        self.main.SETTINGS.update(self._settings)
        runner.JOBS.clear()
        runner.JOBS.update(self._jobs)
        self.main.CLIENT = self._client
        shutil.rmtree(self.tmp, ignore_errors=True)

    def use(self, fake):
        self.main.CLIENT = fake
        return fake

    def seed_rows(self, n=3):
        """往临时库里塞 n 行，返回它们的编号（对比模式 B 用）。"""
        ids = []
        for i in range(n):
            self.main.GAL.insert(row_of(filename=f"pick{i}_00001_.png", seed=1000 + i))
        out = self.main.GAL.query({"per_page": 50})
        return [it["id"] for it in sorted(out["items"], key=lambda x: x["filename"])]


class GuardTests(EndpointBase):
    def test_gallery_endpoints_reject_a_wrong_token(self):
        bad = {"Authorization": "Bearer not-the-token-huafang-never-issued"}   # 头值只能 ASCII
        for path in ("/api/gallery", "/api/gallery/tags", "/api/gallery/1", "/api/view?filename=x"):
            with self.subTest(path=path):
                self.assertEqual(self.http.get(path, headers=bad).status_code, 401)
        self.assertEqual(self.http.patch("/api/gallery/1", json={"fav": True},
                                         headers=bad).status_code, 401)
        self.assertEqual(self.http.post("/api/gallery/backfill", json={}, headers=bad).status_code, 401)
        self.assertEqual(self.http.post("/api/compare", json={}, headers=bad).status_code, 401)

    def test_no_token_anywhere_in_urls_or_payloads(self):
        """D135：token 只走 Authorization 头，绝不进 URL/query，也不进任何回给前端的字段。"""
        self.main.GAL.insert(row_of())
        for r in (self.http.get("/api/gallery"), self.http.get("/api/gallery/tags"),
                  self.http.get("/api/gallery/1"),
                  self.http.post("/api/compare", json={"mode": "pick", "ids": [1, 2]})):
            with self.subTest(url=str(r.request.url)):
                self.assertNotIn(self.main.TOKEN, str(r.request.url))
                self.assertNotIn(self.main.TOKEN, r.text)
        for r in (self.http.get("/"), self.http.get("/gallery"), self.http.get("/compare")):
            with self.subTest(page=str(r.request.url)):
                self.assertEqual(r.status_code, 200)
                self.assertNotIn("?", str(r.request.url))          # 页面上不带 query
                self.assertIn(self.main.TOKEN, r.text)             # 只在页面里注一次，给 fetch 用

    def test_view_endpoint_still_proxies_the_furnace(self):
        """/api/view 的既有守卫与语义一个字不动（C 卡禁止栏）。"""
        fake = self.use(MultiComfy())
        r = self.http.get("/api/view", params={"filename": "a.png", "subfolder": "huafang/anima_t2i"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, fake.view_bytes("a.png", "huafang/anima_t2i"))
        self.assertEqual(r.headers["content-type"], "image/png")

    def test_pages_are_served_without_touching_the_api_guard(self):
        self.assertIn("图库", self.http.get("/gallery").text)
        self.assertIn("对比", self.http.get("/compare").text)


class GalleryEndpointTests(EndpointBase):
    def test_tags_route_is_declared_before_the_id_route(self):
        """/api/gallery/tags 必须抢在 /api/gallery/{id} 前面，否则 tags 会被当成编号去查。"""
        self.main.GAL.insert(row_of())
        self.main.GAL.patch(1, {"tags": ["夜景"]})
        r = self.http.get("/api/gallery/tags")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"tags": [{"tag": "夜景", "count": 1}]})

    def test_list_carries_filters_paging_and_stats(self):
        for i in range(3):
            self.main.GAL.insert(row_of(filename=f"p{i}_00001_.png",
                                        created=f"2026-10-2{i + 1}T10:00:00"))
        self.main.GAL.patch(1, {"fav": True})
        r = self.http.get("/api/gallery", params={"per_page": 2, "page": 1, "fav_only": "1"})
        body = r.json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual((body["total"], body["pages"], body["per_page"]), (1, 1, 2))
        self.assertEqual(body["stats"], {"total": 3, "fav": 1, "intermediate": 0})
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(self.http.get("/api/gallery", params={"q": "p2"}).json()["total"], 1)
        self.assertEqual(self.http.get("/api/gallery", params={"q": "没有"}).json()["total"], 0)

    def test_get_and_patch_speak_human_on_bad_input(self):
        self.main.GAL.insert(row_of())
        r = self.http.get("/api/gallery/1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["filename"], "x_00001_.png")

        r = self.http.patch("/api/gallery/1", json={"fav": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["fav"])
        self.assertEqual(r.json()["rating"], 0)          # 局部改：没给的字段不动

        r = self.http.patch("/api/gallery/1", json={"rating": 9})
        self.assertEqual(r.status_code, 400)
        self.assertIn("0–5", r.json()["detail"]["human"])
        self.assertEqual(r.json()["detail"]["code"], "bad_rating")

        r = self.http.patch("/api/gallery/1", json={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "empty_patch")

        for path in ("/api/gallery/999999", "/api/gallery/abc"):
            with self.subTest(path=path):
                r = self.http.get(path)
                self.assertEqual(r.status_code, 404)      # 坏编号也是人话 404，不是 422
                self.assertIn("图库里没有这张图", r.json()["detail"]["human"])
        self.assertEqual(self.http.patch("/api/gallery/999999", json={"fav": True}).status_code, 404)

    def test_backfill_scans_only_settings_dirs_never_the_request_body(self):
        """回填扫哪儿只由 settings 说了算：请求体里塞目录不生效（那等于开一个任意盘扫描口）。"""
        # 必须顶掉真 CLIENT：回填除了扫目录还要问炉子 /history，炉子正开着的话
        # 它内存里那几条真记录会混进 added，这条用例就成了「今天有没有出过图」的占卜。
        self.use(FakeHistory())
        smuggled = self.tmp / "smuggled"
        write_png(smuggled / "偷渡_00001_.png",
                  filled(prefix="huafang/anima_t2i/20261028_000001", seed=1))
        r = self.http.post("/api/gallery/backfill", json={"dirs": [str(smuggled)]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["added"], 0)
        self.assertNotIn(str(smuggled), r.json()["dirs"])

        allowed = self.tmp / "allowed"
        write_png(allowed / "正经_00001_.png",
                  filled(prefix="huafang/anima_t2i/20261028_000002", seed=2))
        old = ST.gallery_dirs
        ST.gallery_dirs = lambda: [str(allowed)]
        try:
            r = self.http.post("/api/gallery/backfill", json={})
        finally:
            ST.gallery_dirs = old
        body = r.json()
        self.assertEqual(body["added"], 1)
        self.assertEqual(body["dirs"], [str(self.tmp / "furnace"), str(allowed)])
        self.assertEqual(body["stats"]["total"], 1)
        self.assertEqual(self.http.get("/api/gallery").json()["items"][0]["seed"], 2)

    def test_backfill_survives_a_dead_furnace_and_reports_it(self):
        self.use(FakeHistory(boom=True))
        r = self.http.post("/api/gallery/backfill", json={})
        self.assertEqual(r.status_code, 200)               # 炉子没开不是 500，是一句人话计数
        self.assertIn("炉子历史读不出来", "".join(r.json()["reasons"]))


class DoneRegistrationTests(EndpointBase):
    def test_a_finished_job_lands_in_the_gallery_with_receipt_matching_values(self):
        """卡 §1 登记时机：真出图 → done → 图库立见，字段与回执逐项一致（验收 A 的单测口径）。"""
        fake = self.use(MultiComfy())
        req = t2i_req(seed=4242, width=1024, height=1024, positive="1girl, night")
        r1 = self.http.post("/api/generate", json=req)
        self.assertEqual(r1.status_code, 200, r1.text)
        r2 = self.http.get(f"/api/job/{r1.json()['job_id']}")
        body = r2.json()
        self.assertEqual(body["status"], "done")
        self.assertEqual([it["ok"] for it in body["receipt"]], [True] * 13)   # A 阶段基线不许破

        items = self.http.get("/api/gallery").json()["items"]
        self.assertEqual(len(items), 1)
        got = items[0]
        self.assertEqual(got["filename"], body["images"][0]["filename"])
        self.assertEqual(got["seed"], 4242)
        self.assertEqual((got["width"], got["height"]), (1024, 1024))
        self.assertEqual(got["steps"], 30)
        self.assertEqual(got["source"], "job")
        self.assertEqual(got["kind"], "final")
        self.assertEqual(got["servable"], 1)
        # 登记的正词＝触发词入链后真正提交的那份（compose 会把笔触发词插进去，所以不等于界面原文）
        self.assertEqual(got["positive"], body["positive_final"])
        src = T2I_PROF["stages"][0]["prompt"]["positive_source"][0]     # 节点号只从 profile 读（D83）
        sent = next(iter(fake.submitted.values()))
        self.assertEqual(got["positive"], sent[src["node"]]["inputs"][src["input"]])
        self.assertEqual(len(fake.submitted), 1)

    def test_index_failure_never_fails_the_generation(self):
        """索引库写坏了：出图照样 done，回执照样全 ✓（卡面「索引不许拦出图主链路」）。"""
        self.main.GAL._connect().execute("DROP TABLE images")
        self.main.GAL._conn.commit()
        self.use(MultiComfy())
        r1 = self.http.post("/api/generate", json=t2i_req())
        body = self.http.get(f"/api/job/{r1.json()['job_id']}").json()
        self.assertEqual(body["status"], "done")
        self.assertEqual([it["ok"] for it in body["receipt"]], [True] * 13)


class CompareVariantsTests(EndpointBase):
    def base(self, **over):
        return t2i_req(**over)

    def test_n_variants_share_one_seed_and_each_gets_a_full_receipt(self):
        """卡 §4 模式 A：同 seed 出 N 张＝复用 generate N 次，回执各自完整（验收 E 的单测口径）。"""
        fake = self.use(MultiComfy())
        variants = [{"steps": 20}, {"steps": 30}, {"steps": 40}, {"cfg": 6.0}]
        r = self.http.post("/api/compare", json={"mode": "variants",
                                                "base": self.base(seed=-1),
                                                "variants": variants})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["mode"], "variants")
        self.assertIsInstance(body["seed"], int)
        self.assertNotIn(body["seed"], (-1, None))
        self.assertEqual(len(body["jobs"]), 4)
        self.assertEqual(len({j["job_id"] for j in body["jobs"]}), 4)
        self.assertEqual(len(fake.submitted), 4)            # 真提了 4 次，不是一次出 4 张

        seeds, steps = set(), []
        for job, variant in zip(body["jobs"], variants):
            self.assertEqual(job["variant"], variant)
            self.assertEqual(job["status"], "running")
            done = self.http.get(f"/api/job/{job['job_id']}").json()
            self.assertEqual(done["status"], "done")
            self.assertEqual([it["ok"] for it in done["receipt"]], [True] * 13)
            rows = {it["key"]: it["actual"] for it in done["receipt"]}
            seeds.add(rows["seed"])
            steps.append(rows["steps"])
            self.assertEqual(rows["batch_size"], 1)          # 每个变体出 1 张，拼板才是 N 格
        self.assertEqual(len(seeds), 1)                      # 同 seed 是模式 A 的命根子
        self.assertEqual(steps, [20, 30, 40, 30])            # cfg 那个变体沿用基准 steps
        slot = T2I_PROF["stages"][0]["params"]["seed"]       # 节点号只从 profile 读（D83）
        self.assertEqual({wf[slot["node"]]["inputs"][slot["input"]]
                          for wf in fake.submitted.values()}, {body["seed"]})

    def test_a_given_seed_is_kept_verbatim_for_every_variant(self):
        fake = self.use(MultiComfy())
        r = self.http.post("/api/compare", json={"mode": "variants", "base": self.base(seed=31337),
                                                "variants": [{"steps": 20}, {"steps": 40}]})
        self.assertEqual(r.json()["seed"], 31337)
        for jid in [j["job_id"] for j in r.json()["jobs"]]:
            rows = {it["key"]: it["actual"]
                    for it in self.http.get(f"/api/job/{jid}").json()["receipt"]}
            self.assertEqual(rows["seed"], 31337)

    def test_variants_may_not_touch_seed_count_or_profile(self):
        for variant in ({"seed": 1}, {"count": 4}, {"profile_id": "anima_hires"}):
            with self.subTest(variant=variant):
                r = self.http.post("/api/compare", json={
                    "mode": "variants", "base": self.base(), "variants": [variant, {"steps": 20}]})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["detail"]["code"], "locked_field")
                self.assertIn("不许改", r.json()["detail"]["human"])

    def test_variant_count_bounds(self):
        one = [{"steps": 20}]
        nine = [{"steps": 20 + i} for i in range(9)]
        for variants, code in ((one, "bad_variants"), (nine, "bad_variants")):
            with self.subTest(n=len(variants)):
                r = self.http.post("/api/compare", json={
                    "mode": "variants", "base": self.base(), "variants": variants})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["detail"]["code"], code)
                self.assertIn("2–8", r.json()["detail"]["human"])
        self.assertEqual(runner.JOBS, {})                    # 拒了就是一个任务也不许提
        for n in (2, 8):
            with self.subTest(n=n):
                self.use(MultiComfy())
                r = self.http.post("/api/compare", json={
                    "mode": "variants", "base": self.base(),
                    "variants": [{"steps": 20 + i} for i in range(n)]})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(len(r.json()["jobs"]), n)

    def test_bad_shapes_speak_human(self):
        cases = [
            ({"mode": "变体"}, 400, "bad_mode"),
            ({"mode": "variants", "variants": [{"steps": 20}, {"steps": 30}]}, 400, "bad_base"),
            ({"mode": "variants", "base": {"steps": 30}, "variants": [{"steps": 20}, {"steps": 30}]},
             400, "bad_base"),                                  # base 里没有 profile_id
            ({"mode": "variants", "base": self.base(), "variants": "不是数组"}, 400, "bad_variants"),
            ({"mode": "variants", "base": self.base(), "variants": [{"steps": 20}, 5]},
             400, "bad_variants"),
        ]
        for payload, status, code in cases:
            with self.subTest(code=code):
                self.use(MultiComfy())
                r = self.http.post("/api/compare", json=payload)
                self.assertEqual(r.status_code, status)
                self.assertEqual(r.json()["detail"]["code"], code)
                self.assertTrue(r.json()["detail"]["human"])

    def test_unknown_profile_propagates_the_same_404_as_generate(self):
        """模式 A 复用 generate，所以工作流不存在／契约不过的报错口径与出图台一字不差。"""
        self.use(MultiComfy())
        r = self.http.post("/api/compare", json={
            "mode": "variants", "base": self.base(profile_id="没这个工作流"),
            "variants": [{"steps": 20}, {"steps": 30}]})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["detail"], "没找到这个工作流")


class ComparePickTests(EndpointBase):
    def test_pick_returns_the_records_it_was_given(self):
        ids = self.seed_rows(3)
        r = self.http.post("/api/compare", json={"mode": "pick", "ids": ids[:3]})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "pick")
        self.assertEqual([i["id"] for i in body["items"]], ids[:3])
        self.assertEqual({i["seed"] for i in body["items"]}, {1000, 1001, 1002})

    def test_pick_bounds_two_to_eight_and_nine_is_refused(self):
        ids = self.seed_rows(8)
        self.assertEqual(self.http.post("/api/compare", json={"mode": "pick",
                                                             "ids": ids[:2]}).status_code, 200)
        self.assertEqual(self.http.post("/api/compare", json={"mode": "pick",
                                                             "ids": ids}).status_code, 200)
        for bad in (ids[:1], ids + [ids[0] + 1000]):
            with self.subTest(n=len(bad)):
                r = self.http.post("/api/compare", json={"mode": "pick", "ids": bad})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()["detail"]["code"], "bad_pick_count")
                self.assertIn("2–8", r.json()["detail"]["human"])

    def test_picking_the_same_image_twice_counts_once(self):
        ids = self.seed_rows(3)
        r = self.http.post("/api/compare", json={"mode": "pick", "ids": [ids[0], ids[0], ids[1]]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["items"]), 2)
        r = self.http.post("/api/compare", json={"mode": "pick", "ids": [ids[0]] * 9})
        self.assertEqual(r.status_code, 400)                  # 去重后只剩 1 张＝不够比
        self.assertEqual(r.json()["detail"]["code"], "bad_pick_count")

    def test_pick_bad_ids_speak_human(self):
        self.seed_rows(3)
        for payload, code, status in (({"mode": "pick"}, "bad_ids", 400),
                                      ({"mode": "pick", "ids": "1,2"}, "bad_ids", 400),
                                      ({"mode": "pick", "ids": [1, "两张"]}, "bad_ids", 400),
                                      ({"mode": "pick", "ids": [1, 999999]}, "no_such_image", 404)):
            with self.subTest(code=code):
                r = self.http.post("/api/compare", json=payload)
                self.assertEqual(r.status_code, status)
                self.assertEqual(r.json()["detail"]["code"], code)
                self.assertTrue(r.json()["detail"]["human"])


# ---------------------------------------------------------------------- settings 读侧
class GalleryDirsTests(unittest.TestCase):
    """`settings.gallery_dirs()`：回填扫哪儿的唯一凭据（卡面「默认含 T010 复制目录」）。

    端点那侧的用例是把这个函数整个换成 lambda 的，所以它自己的口径得单独钉一遍。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hf_c_dirs_"))
        self._real = ST.SETTINGS_FILE
        ST.SETTINGS_FILE = self.tmp / "settings.json"
        ST.SETTINGS_FILE.write_text("{}", encoding="utf-8")

    def tearDown(self):
        ST.SETTINGS_FILE = self._real
        shutil.rmtree(self.tmp, ignore_errors=True)

    def save(self, obj):
        ST.SETTINGS_FILE.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def test_extra_dirs_and_the_t010_copy_dir_both_land(self):
        a, b = str(self.tmp / "画集"), str(self.tmp / "复制")
        self.save({"gallery": {"extra_dirs": [a, b]}, "output": {"dir": str(self.tmp / "t010")}})
        self.assertEqual(ST.gallery_dirs(), [a, b, str(self.tmp / "t010")])

    def test_only_absolute_paths_are_taken(self):
        """相对路径不收：那是拿主人的配置文件当目录浏览器，扫到哪儿全看 cwd 脸色。"""
        self.save({"gallery": {"extra_dirs": ["相对/路径", "", "   ", str(self.tmp / "正经")],
                               }, "output": {"dir": "../溜出去"}})
        self.assertEqual(ST.gallery_dirs(), [str(self.tmp / "正经")])

    def test_same_dir_written_twice_counts_once(self):
        d = str(self.tmp / "画集")
        self.save({"gallery": {"extra_dirs": [d, d.upper(), d + "\\"]}, "output": {"dir": d}})
        self.assertEqual(ST.gallery_dirs(), [d])      # Windows 上大小写与末尾斜杠都是同一个目录

    def test_junk_shapes_are_ignored_not_crashed_on(self):
        self.save({"gallery": {"extra_dirs": [123, None, {"a": 1}, [str(self.tmp / "套娃")]]},
                   "output": {"dir": 456}})
        self.assertEqual(ST.gallery_dirs(), [])
        for bad in ({"gallery": "不是字典"}, {"gallery": {"extra_dirs": "不是列表"}},
                    {"output": "不是字典"}, {"output": {"dir": None}}):
            with self.subTest(bad=bad):
                self.save(bad)
                self.assertEqual(ST.gallery_dirs(), [])

    def test_a_broken_or_missing_settings_file_yields_an_empty_list(self):
        self.assertEqual(ST.gallery_dirs(), [])                   # 只有 {} 的空表
        ST.SETTINGS_FILE.write_text("{半截", encoding="utf-8")
        self.assertEqual(ST.gallery_dirs(), [])
        ST.SETTINGS_FILE.unlink()
        self.assertEqual(ST.gallery_dirs(), [])

    def test_it_reads_the_disk_every_call_and_never_writes(self):
        """改完 settings.json 点一下回填就生效，不用重启；读侧一个字都不许往回写。"""
        d = str(self.tmp / "画集")
        self.save({"gallery": {"extra_dirs": [d]}})
        self.assertEqual(ST.gallery_dirs(), [d])
        self.save({"gallery": {"extra_dirs": []}})
        self.assertEqual(ST.gallery_dirs(), [])
        self.assertEqual(json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8")),
                         {"gallery": {"extra_dirs": []}})
        self.assertFalse(list(self.tmp.glob("*.tmp")))            # 没有半截的原子写残留
        self.assertFalse(list(self.tmp.glob("*.bak")))            # 也没有偷偷备份


if __name__ == "__main__":
    unittest.main()
