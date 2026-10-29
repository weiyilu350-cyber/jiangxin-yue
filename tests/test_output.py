"""T010 输出目录：校验分支／重名不覆盖／复制失败不改 done／settings 原子写／端点人话。

最高红线（卡「禁止」栏）：目标目录里的既有文件一个字节都不许动——重名只能加后缀。
"""
import copy
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import output as OUT
from app import runner
from app import settings as ST
from app import workflow_profile as wp
from app.comfy import ComfyError

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"
COMFY_OUT = ST.DEFAULTS["output_dir"]        # 炉子自己的 output：拿它当「不许自循环」的对照


def free_drive() -> str:
    """挑一个本机真不存在的盘符，别把测试建到主人真盘上。"""
    for letter in "QRSTUVWXYZ":
        if not Path(letter + ":\\").exists():
            return letter
    raise unittest.SkipTest("找不到空闲盘符")


def stamp(path: Path) -> tuple:
    """既有文件的「指纹」：字节 ＋ mtime ＋ size，用来证明它一个字节没被动过。"""
    st = path.stat()
    return (path.read_bytes(), st.st_mtime_ns, st.st_size)


class FakeComfy:
    """只会 view_bytes 的假炉子；记调用，便于断言「空目录时一次都不下载」。"""

    def __init__(self, blobs=None, fail=(), down=False):
        self.blobs = blobs if blobs is not None else {}
        self.fail = set(fail)
        self.down = down
        self.calls = []

    def view_bytes(self, filename, subfolder="", type_="output"):
        self.calls.append((filename, subfolder))
        if self.down or filename in self.fail:
            raise ComfyError("view_failed", "取不到图片", "fake")
        return self.blobs.get(filename, b"PNG-" + filename.encode())


def mk_job(images, status="done", readonly=False):
    j = runner.Job("j_t010", {"seed": 7})
    j.status = status
    j.images = images
    j.readonly = readonly
    return j


IMG2 = [{"filename": "20261028_1.png", "subfolder": "huafang/anima_t2i"},
        {"filename": "20261028_2.png", "subfolder": "huafang/anima_t2i"}]


class TmpBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "画集"
        self.out.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def residue(self, directory: Path) -> list:
        return [p.name for p in directory.iterdir() if p.name.endswith(OUT.TMP_SUFFIX)]


# ------------------------------------------------------------------ 校验分支
class NormOutputDirTests(TmpBase):
    """卡 §3：空／相对／盘符不存在／是炉子 output／可创建，五个分支都得走到。"""

    def test_empty_means_no_copy(self):
        for raw in ("", "   ", None):
            self.assertEqual(ST.norm_output_dir(raw, COMFY_OUT), ("", None))

    def test_non_string_is_bad_path(self):
        for raw in (123, 1.5, True, [], {}, ("C:\\x",)):
            normed, err = ST.norm_output_dir(raw, COMFY_OUT)
            self.assertEqual(normed, "")
            self.assertEqual(err["code"], "bad_path")
            self.assertTrue(err["human"])

    def test_relative_path_is_bad_path_and_creates_nothing(self):
        before = sorted(p.name for p in self.tmp.iterdir())
        for raw in ("画集\\我的作品", "output", "/huafang/out", "..", "."):
            normed, err = ST.norm_output_dir(raw, COMFY_OUT)
            self.assertEqual(normed, "", raw)
            self.assertEqual(err["code"], "bad_path", raw)
            self.assertIn("完整路径", err["human"])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), before)

    def test_missing_drive_cannot_create(self):
        raw = free_drive() + ":\\画集\\我的作品"
        normed, err = ST.norm_output_dir(raw, COMFY_OUT)
        self.assertEqual(normed, "")
        self.assertEqual(err["code"], "cannot_create")
        self.assertIn("建不出来", err["human"])

    def test_file_in_place_of_dir_is_rejected_and_untouched(self):
        f = self.tmp / "notadir.png"
        f.write_bytes(b"1234567890")
        os.utime(f, (1000000, 1000000))
        before = stamp(f)
        normed, err = ST.norm_output_dir(str(f), COMFY_OUT)
        self.assertEqual(normed, "")
        self.assertEqual(err["code"], "cannot_create")
        self.assertEqual(stamp(f), before)     # 校验只读：既有文件一个字节都不动

    def test_comfy_output_itself_is_rejected(self):
        fake_furnace = self.tmp / "ComfyUI" / "output"
        fake_furnace.mkdir(parents=True)
        for raw in (str(fake_furnace), str(fake_furnace) + "\\", str(fake_furnace).upper()):
            normed, err = ST.norm_output_dir(raw, str(fake_furnace))
            self.assertEqual(normed, "")
            self.assertEqual(err["code"], "is_comfy_output", raw)
            self.assertIn("炉子", err["human"])

    def test_subdir_of_comfy_output_is_rejected_and_not_created(self):
        fake_furnace = self.tmp / "ComfyUI" / "output"
        fake_furnace.mkdir(parents=True)
        sub = fake_furnace / "huafang" / "anima_t2i"
        normed, err = ST.norm_output_dir(str(sub), str(fake_furnace))
        self.assertEqual(normed, "")
        self.assertEqual(err["code"], "is_comfy_output")
        self.assertFalse(sub.exists())         # 不许因为校验就在炉子里建目录

    def test_default_furnace_output_is_rejected_when_configured(self):
        configured = str(self.tmp / "ComfyUI" / "output")
        original = ST.DEFAULTS["output_dir"]
        ST.DEFAULTS["output_dir"] = configured
        try:
            normed, err = ST.norm_output_dir(configured)
        finally:
            ST.DEFAULTS["output_dir"] = original
        self.assertEqual(normed, "")
        self.assertEqual(err["code"], "is_comfy_output")

    def test_guard_off_lets_furnace_output_through(self):
        """reveal 走这条：comfy_output="" 时不设自循环拦阻（炉子 output 本来就该能打开）。"""
        fake_furnace = self.tmp / "ComfyUI" / "output"
        fake_furnace.mkdir(parents=True)
        normed, err = ST.norm_output_dir(str(fake_furnace), comfy_output="", create=False)
        self.assertEqual((normed, err), (str(fake_furnace), None))

    def test_creates_missing_dir_and_is_idempotent(self):
        target = self.tmp / "新目录" / "我的作品"
        normed, err = ST.norm_output_dir(str(target), COMFY_OUT)
        self.assertEqual(err, None)
        self.assertEqual(normed, str(target))
        self.assertTrue(target.is_dir())
        self.assertEqual(ST.norm_output_dir(str(target), COMFY_OUT), (str(target), None))

    def test_create_false_never_creates(self):
        target = self.tmp / "还没建"
        normed, err = ST.norm_output_dir(str(target), COMFY_OUT, create=False)
        self.assertEqual(normed, "")
        self.assertEqual(err["code"], "not_found")
        self.assertFalse(target.exists())

    def test_normalizes_separators_and_trailing_slash(self):
        target = self.tmp / "dir"
        target.mkdir()
        sloppy = str(target).replace("\\", "/") + "/"
        normed, err = ST.norm_output_dir(sloppy, COMFY_OUT)
        self.assertEqual((normed, err), (str(target), None))

    def test_surrounding_spaces_are_trimmed(self):
        target = self.tmp / "sp"
        target.mkdir()
        self.assertEqual(ST.norm_output_dir("  " + str(target) + "  ", COMFY_OUT),
                         (str(target), None))


# ------------------------------------------------------------------ 重名不覆盖
class UniquePathTests(TmpBase):
    def test_no_collision_keeps_name(self):
        self.assertEqual(OUT.unique_path(self.out, "a.png"), self.out / "a.png")

    def test_collision_gets_suffix(self):
        (self.out / "a.png").write_bytes(b"old")
        self.assertEqual(OUT.unique_path(self.out, "a.png"), self.out / "a_1.png")

    def test_suffix_climbs_over_existing_ones(self):
        for n in ("a.png", "a_1.png", "a_2.png"):
            (self.out / n).write_bytes(b"old")
        self.assertEqual(OUT.unique_path(self.out, "a.png"), self.out / "a_3.png")

    def test_furnace_style_name(self):
        (self.out / "20261028_120000_00001_.png").write_bytes(b"old")
        self.assertEqual(OUT.unique_path(self.out, "20261028_120000_00001_.png"),
                         self.out / "20261028_120000_00001__1.png")

    def test_only_last_segment_is_used(self):
        """炉子给的名字按不可信处理：`..\\` 与绝对路径都不许逃出目标目录。"""
        for raw in ("..\\..\\evil.png", "..//evil.png", str(self.tmp / "evil.png"), "sub\\x.png"):
            self.assertEqual(OUT.unique_path(self.out, raw).parent, self.out)
        self.assertEqual(OUT.unique_path(self.out, "..\\evil.png").name, "evil.png")


# ------------------------------------------------------------------ 复制成品
class CopyFinalsTests(TmpBase):
    def test_happy_path_copies_every_final(self):
        job = mk_job(IMG2)
        OUT.copy_finals(FakeComfy(), job, str(self.out))
        self.assertEqual(job.status, "done")
        self.assertIsNone(job.copy_error)
        self.assertTrue(job.copied)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["20261028_1.png", "20261028_2.png"])
        for im in IMG2:
            self.assertEqual((self.out / im["filename"]).read_bytes(),
                             b"PNG-" + im["filename"].encode())
        self.assertEqual(self.residue(self.out), [])

    def test_subfolder_is_passed_through(self):
        client = FakeComfy()
        OUT.copy_finals(client, mk_job(IMG2), str(self.out))
        self.assertEqual(client.calls,
                         [("20261028_1.png", "huafang/anima_t2i"),
                          ("20261028_2.png", "huafang/anima_t2i")])

    def test_existing_file_is_never_touched(self):
        """全卡最高红线：同名旧文件（内容不同）字节＋mtime 逐一未动，新图带 _1。"""
        old = self.out / "20261028_1.png"
        old.write_bytes("主人的真作品".encode("utf-8"))   # 内容与新图不同，才证明得了「没被覆盖」
        os.utime(old, (1000000, 1000000))
        before = stamp(old)
        job = mk_job([IMG2[0]])
        OUT.copy_finals(FakeComfy(), job, str(self.out))
        self.assertIsNone(job.copy_error)
        self.assertEqual(stamp(old), before)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["20261028_1.png", "20261028_1_1.png"])
        self.assertEqual((self.out / "20261028_1_1.png").read_bytes(),
                         b"PNG-20261028_1.png")

    def test_three_copies_of_same_name_climb(self):
        job = mk_job([{"filename": "a.png", "subfolder": ""}] * 3)
        OUT.copy_finals(FakeComfy(), job, str(self.out))
        self.assertIsNone(job.copy_error)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["a.png", "a_1.png", "a_2.png"])

    def test_view_bytes_error_keeps_done_and_says_human(self):
        job = mk_job(IMG2)
        job.receipt = [{"key": "seed", "ok": True}]
        OUT.copy_finals(FakeComfy(down=True), job, str(self.out))
        self.assertEqual(job.status, "done")          # 绝不因为复制把任务弄成失败
        self.assertEqual(job.receipt, [{"key": "seed", "ok": True}])
        self.assertTrue(job.copy_error.startswith("已存炉子输出，复制到你的目录失败："))
        self.assertIn("20261028_1.png", job.copy_error)
        self.assertIn("20261028_2.png", job.copy_error)
        self.assertIn("原件没丢", job.copy_error)
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertEqual(self.residue(self.out), [])

    def test_partial_failure_copies_the_rest(self):
        job = mk_job(IMG2)
        OUT.copy_finals(FakeComfy(fail={"20261028_2.png"}), job, str(self.out))
        self.assertEqual(job.status, "done")
        self.assertEqual([p.name for p in self.out.iterdir()], ["20261028_1.png"])
        self.assertIn("20261028_2.png", job.copy_error)
        self.assertNotIn("20261028_1.png", job.copy_error)

    def test_missing_target_dir_is_reported_not_raised(self):
        job = mk_job(IMG2)
        OUT.copy_finals(FakeComfy(), job, str(self.tmp / "被删了"))
        self.assertEqual(job.status, "done")
        self.assertTrue(job.copy_error.startswith("已存炉子输出，复制到你的目录失败："))
        self.assertIn("20261028_1.png", job.copy_error)
        self.assertIn("原件没丢", job.copy_error)

    def test_no_tmp_residue_when_target_name_is_taken(self):
        """唯一名被抢：宁可报错也不覆盖，且不留 .tmp 半截文件。"""
        (self.out / "a.png").write_bytes(b"old")
        real = OUT.unique_path
        OUT.unique_path = lambda d, n: d / "a.png"
        try:
            job = mk_job([{"filename": "a.png", "subfolder": ""}])
            OUT.copy_finals(FakeComfy(), job, str(self.out))
        finally:
            OUT.unique_path = real
        self.assertIn("a.png", job.copy_error)
        self.assertEqual((self.out / "a.png").read_bytes(), b"old")
        self.assertEqual([p.name for p in self.out.iterdir()], ["a.png"])
        self.assertEqual(self.residue(self.out), [])

    def test_empty_dir_means_no_copy_and_no_download(self):
        client = FakeComfy()
        for out in ("", None):
            job = mk_job(IMG2)
            OUT.copy_finals(client, job, out)
            self.assertIsNone(job.copy_error)
            self.assertFalse(job.copied)
        self.assertEqual(client.calls, [])
        self.assertEqual(list(self.out.iterdir()), [])

    def test_readonly_jobs_are_never_copied(self):
        """启动/刷新时从炉子历史重建的只读记录，不许往主人目录里倒。"""
        client = FakeComfy()
        job = mk_job(IMG2, readonly=True)
        OUT.copy_finals(client, job, str(self.out))
        self.assertEqual(client.calls, [])
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertFalse(job.copied)

    def test_not_done_and_no_images_are_skipped(self):
        client = FakeComfy()
        for status, images in (("running", IMG2), ("error", IMG2), ("done", []), ("pending", [])):
            job = mk_job(images, status=status)
            OUT.copy_finals(client, job, str(self.out))
            self.assertEqual(job.status, status)
            self.assertIsNone(job.copy_error)
        self.assertEqual(client.calls, [])

    def test_second_call_is_a_no_op(self):
        client = FakeComfy()
        job = mk_job(IMG2)
        OUT.copy_finals(client, job, str(self.out))
        OUT.copy_finals(client, job, str(self.out))
        self.assertEqual(len(client.calls), 2)          # 不重试、不复制第二遍
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["20261028_1.png", "20261028_2.png"])

    def test_image_without_filename_is_skipped(self):
        job = mk_job([{"filename": "", "subfolder": ""}, {"subfolder": ""}, IMG2[0]])
        OUT.copy_finals(FakeComfy(), job, str(self.out))
        self.assertIsNone(job.copy_error)
        self.assertEqual([p.name for p in self.out.iterdir()], ["20261028_1.png"])

    def test_path_traversal_name_stays_inside(self):
        job = mk_job([{"filename": "..\\..\\evil.png", "subfolder": ""}])
        OUT.copy_finals(FakeComfy(blobs={"evil.png": b"x"}), job, str(self.out))
        self.assertEqual([p.name for p in self.out.iterdir()], ["evil.png"])
        self.assertFalse((self.tmp / "evil.png").exists())


# ------------------------------------------------------------------ runner 钩子
WF, PROF = wp.load_pair(WORKFLOWS / "anima_t2i.json")
REQ = {"profile_id": "anima_t2i", "unet": "anima-base-v1.0.safetensors", "loras": [None],
       "positive": "1girl", "negative": "", "width": 832, "height": 1216, "steps": 30,
       "cfg": 4.0, "seed": 12345, "sampler_name": "euler", "scheduler": "simple", "count": 1}
SENT = runner.build_workflow(WF, PROF, REQ)


def entry(pid, images=IMG2, workflow=None, status_str="success", client_id="huafang"):
    outputs = {"9": {"images": images}} if images else {}
    return {"prompt": [1, pid, SENT if workflow is None else workflow,
                       {"client_id": client_id, "create_time": 1761600000000.0}, []],
            "outputs": outputs,
            "status": {"status_str": status_str, "messages": []},
            "extra_data": None}


class HistComfy:
    def __init__(self, one=None, history=None):
        self.one = one or {}
        self.history_data = history or {}

    def alive(self):
        return True

    def history(self, prompt_id):
        return self.one.get(prompt_id)

    def queue(self):
        return {"queue_running": [], "queue_pending": []}

    def history_recent(self, max_items=50):
        return self.history_data

    def submit(self, workflow, client_id):
        raise AssertionError("对账只改状态，绝不重新提交")

    def view_bytes(self, filename, subfolder="", type_="output"):
        return b"PNG-" + filename.encode()


class HookTests(unittest.TestCase):
    """卡 §1：复制挂在 apply_history/refresh/reconcile 判 done 的同一处，不开第二条轮询路。"""

    def setUp(self):
        self._jobs = dict(runner.JOBS)
        runner.JOBS.clear()
        self.seen = []

    def tearDown(self):
        runner.JOBS.clear()
        runner.JOBS.update(self._jobs)

    def live(self, pid="p1"):
        j = runner.Job("j_" + pid, copy.deepcopy(REQ))
        j.prompt_id = pid
        j.status = "running"
        j.profile = PROF
        j.sent = copy.deepcopy(SENT)
        runner.JOBS[j.id] = j
        return j

    def test_apply_history_fires_hook_once(self):
        j = self.live()
        runner.apply_history(j, entry("p1"), self.seen.append)
        self.assertEqual(self.seen, [j])
        self.assertEqual(j.status, "done")
        self.assertEqual(len(j.receipt), 13)      # T009 后的基准，钩子没动它

    def test_hook_not_fired_for_readonly(self):
        j = runner.Job("h_p1", {})
        j.readonly = True
        j.prompt_id = "p1"
        runner.apply_history(j, entry("p1"), self.seen.append)
        self.assertEqual(self.seen, [])
        self.assertEqual(j.status, "done")

    def test_hook_not_fired_without_outputs_or_on_error(self):
        j = self.live()
        runner.apply_history(j, entry("p1", images=None), self.seen.append)
        self.assertEqual(self.seen, [])
        self.assertEqual(j.status, "running")
        runner.apply_history(j, entry("p1", status_str="error"), self.seen.append)
        self.assertEqual(self.seen, [])
        self.assertEqual(j.status, "error")

    def test_refresh_passes_hook(self):
        j = self.live()
        runner.refresh(HistComfy(one={"p1": entry("p1")}), j, self.seen.append)
        self.assertEqual(self.seen, [j])

    def test_refresh_without_hook_is_unchanged(self):
        j = self.live()
        runner.refresh(HistComfy(one={"p1": entry("p1")}), j)
        self.assertEqual(j.status, "done")
        self.assertIsNone(j.copy_error)

    def test_reconcile_hooks_live_job_but_not_rebuilt_history(self):
        j = self.live("p1")
        other = entry("p2")                      # 炉子里画舫自己跑过、内存里没有的旧账
        out = runner.reconcile(HistComfy(history={"p1": entry("p1"), "p2": other}),
                               self.seen.append)
        self.assertEqual(self.seen, [j])
        rebuilt = runner.JOBS["h_p2"]
        self.assertTrue(rebuilt.readonly)
        self.assertFalse(rebuilt.copied)
        self.assertEqual(len(out["jobs"]), 2)

    def test_reconcile_without_hook_is_unchanged(self):
        j = self.live("p1")
        out = runner.reconcile(HistComfy(history={"p1": entry("p1")}))
        self.assertEqual(j.status, "done")
        self.assertEqual(out["comfy"]["history_count"], 1)

    def test_summary_carries_copy_error(self):
        j = self.live()
        j.copy_error = "已存炉子输出，复制到你的目录失败：a.png（原件没丢，在炉子的输出文件夹里）"
        self.assertEqual(runner.job_summary(j)["copy_error"], j.copy_error)
        self.assertEqual(runner.job_summary(self.live("p3"))["copy_error"], "")

    def test_new_job_starts_clean(self):
        j = runner.Job("x", {})
        self.assertIsNone(j.copy_error)
        self.assertFalse(j.copied)


class EndToEndCopyTests(TmpBase):
    """把钩子与复制接起来：reconcile 判 done → 成品落到主人目录；失败只留一句人话。"""

    def setUp(self):
        super().setUp()
        self._jobs = dict(runner.JOBS)
        runner.JOBS.clear()

    def tearDown(self):
        runner.JOBS.clear()
        runner.JOBS.update(self._jobs)
        super().tearDown()

    def live(self, pid="p1"):
        j = runner.Job("j_" + pid, copy.deepcopy(REQ))
        j.prompt_id = pid
        j.status = "running"
        j.profile = PROF
        j.sent = copy.deepcopy(SENT)
        runner.JOBS[j.id] = j
        return j

    def test_reconcile_copies_into_user_dir(self):
        j = self.live()
        runner.reconcile(HistComfy(history={"p1": entry("p1")}),
                         lambda job: OUT.copy_finals(HistComfy(), job, str(self.out)))
        self.assertEqual(j.status, "done")
        self.assertIsNone(j.copy_error)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["20261028_1.png", "20261028_2.png"])

    def test_broken_furnace_view_leaves_done_with_warning(self):
        j = self.live()
        broken = FakeComfy(down=True)
        runner.reconcile(HistComfy(history={"p1": entry("p1")}),
                         lambda job: OUT.copy_finals(broken, job, str(self.out)))
        self.assertEqual(j.status, "done")
        self.assertTrue(j.copy_error)
        self.assertEqual(runner.job_summary(j)["status"], "done")
        self.assertEqual(list(self.out.iterdir()), [])


# ------------------------------------------------------------------ settings 落盘
class SettingsFileTests(TmpBase):
    """原子写 ＋ 损坏不崩；写回的只是用户覆盖表，不把 DEFAULTS 钉死进文件。"""

    def setUp(self):
        super().setUp()
        self._real = ST.SETTINGS_FILE
        ST.SETTINGS_FILE = self.tmp / "settings.json"

    def tearDown(self):
        ST.SETTINGS_FILE = self._real
        super().tearDown()

    def write(self, text):
        ST.SETTINGS_FILE.write_text(text, encoding="utf-8")

    def test_missing_file_gets_first_start_template_and_default_output(self):
        s = ST.load_settings()
        self.assertEqual(s["output"], {"dir": ""})
        self.assertEqual(json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8")),
                         {"models_dir": "", "output_dir": ""})

    def test_corrupt_file_falls_back_and_is_not_rewritten(self):
        self.write('{"output": {"dir": "C:\\画集"')      # 半截 JSON
        s = ST.load_settings()
        self.assertEqual(s["output"], {"dir": ""})
        self.assertEqual(ST.DEFAULTS["comfy_url"], s["comfy_url"])
        self.assertTrue(ST.SETTINGS_FILE.read_text(encoding="utf-8").startswith('{"output"'))

    def test_saved_dir_is_merged(self):
        self.write(json.dumps({"output": {"dir": str(self.out)}}))
        self.assertEqual(ST.load_settings()["output"], {"dir": str(self.out)})

    def test_bad_shapes_fall_back_to_default(self):
        for saved in ({"output": "不是字典"}, {"output": []}, {"output": {"dir": 123}},
                      {"output": {"dir": None}}, {"output": {}}):
            self.write(json.dumps(saved))
            self.assertEqual(ST.load_settings()["output"], {"dir": ""}, saved)

    def test_dir_is_trimmed(self):
        self.write(json.dumps({"output": {"dir": "  " + str(self.out) + "  "}}))
        self.assertEqual(ST.load_settings()["output"], {"dir": str(self.out)})

    def test_save_output_dir_keeps_other_user_keys_and_bakes_no_defaults(self):
        self.write(json.dumps({"zh": {"model": "别的模型"}, "port": 9999}, ensure_ascii=False))
        out = ST.save_output_dir(str(self.out))
        self.assertEqual(out, {"dir": str(self.out)})
        data = json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["output"], {"dir": str(self.out)})
        self.assertEqual(data["zh"], {"model": "别的模型"})     # 用户其它覆盖没被抹掉
        self.assertEqual(data["port"], 9999)
        self.assertNotIn("comfy_url", data)                     # DEFAULTS 没被钉进文件
        self.assertNotIn("trigger_map", data)
        self.assertEqual(ST.load_settings()["output"], {"dir": str(self.out)})
        self.assertEqual(ST.load_settings()["zh"]["enabled"], True)   # 默认仍然补齐

    def test_save_creates_backup_and_leaves_no_tmp(self):
        self.write(json.dumps({"port": 8788}))
        ST.save_output_dir(str(self.out))
        bak = ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8")), {"port": 8788})
        self.assertEqual([p.name for p in self.tmp.iterdir()
                          if p.name.endswith(".tmp")], [])

    def test_save_output_dir_survives_corrupt_file(self):
        self.write("根本不是 JSON")
        ST.save_output_dir(str(self.out))
        self.assertEqual(json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8"))["output"],
                         {"dir": str(self.out)})
        self.assertEqual(ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".bak")
                         .read_text(encoding="utf-8"), "根本不是 JSON")   # 原文留在 .bak

    def test_save_settings_round_trip(self):
        ST.save_settings({"output": {"dir": ""}, "zh": {"enabled": False}})
        data = json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["zh"], {"enabled": False})
        self.assertEqual(ST.load_settings()["zh"]["enabled"], False)

    def test_defaults_have_empty_output_dir(self):
        self.assertEqual(ST.DEFAULTS["output"], {"dir": ""})   # 默认＝不复制，维持现状


# ------------------------------------------------------------------ 端点
class EndpointTests(TmpBase):
    """/api/output-dir 与 /api/reveal：守卫照旧、坏值 400＋人话、settings 不被写坏。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls._token_file = ST.TOKEN_FILE
        cls._token_before = (ST.TOKEN_FILE.read_text(encoding="utf-8")
                             if ST.TOKEN_FILE.exists() else None)
        from app import main
        cls.main = main
        cls.http = TestClient(main.app, base_url="http://127.0.0.1:8788",
                              headers={"Authorization": "Bearer " + main.TOKEN})

    @classmethod
    def tearDownClass(cls):
        if cls._token_before is not None:
            cls._token_file.write_text(cls._token_before, encoding="utf-8")

    def setUp(self):
        super().setUp()
        self._real_file = ST.SETTINGS_FILE
        ST.SETTINGS_FILE = self.tmp / "settings.json"
        ST.SETTINGS_FILE.write_text("{}", encoding="utf-8")
        self._settings = copy.deepcopy(self.main.SETTINGS)
        self.main.SETTINGS["output"] = {"dir": ""}
        self.main.SETTINGS["output_dir"] = str(self.tmp / "furnace")
        Path(self.main.SETTINGS["output_dir"]).mkdir(exist_ok=True)
        self._client = self.main.CLIENT
        self.main.CLIENT = HistComfy()          # 端点测试一律不打真炉子（此时它多半没开）
        self._reveal = OUT.reveal
        self.opened = []
        OUT.reveal = self.opened.append         # main.OUT 就是这个模块，改一处即生效

    def tearDown(self):
        OUT.reveal = self._reveal
        self.main.CLIENT = self._client
        self.main.SETTINGS.clear()
        self.main.SETTINGS.update(self._settings)
        ST.SETTINGS_FILE = self._real_file
        super().tearDown()

    def saved(self):
        return ST.SETTINGS_FILE.read_text(encoding="utf-8")

    def post(self, url, payload, token=None):
        # TestClient 的类级默认头抹不掉，所以「没 token」这一档用错 token 顶掉它（与 T005 同法）
        headers = {"Authorization": "Bearer " + (token or self.main.TOKEN)}
        return self.http.post(url, json=payload, headers=headers)

    def test_guard_needs_token(self):
        for url in ("/api/output-dir", "/api/reveal"):
            self.assertEqual(self.post(url, {}, token="wrong").status_code, 401)

    def test_guard_rejects_bad_origin(self):
        r = self.http.post("/api/output-dir", json={"dir": str(self.out)},
                           headers={"Authorization": "Bearer " + self.main.TOKEN,
                                    "Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_bootstrap_reports_output_dir(self):
        self.main.SETTINGS["output"]["dir"] = str(self.out)
        body = self.http.get("/api/bootstrap",
                             headers={"Authorization": "Bearer " + self.main.TOKEN}).json()
        self.assertEqual(body["output"], {"dir": str(self.out)})

    def test_save_valid_dir(self):
        target = self.tmp / "新目录"
        r = self.post("/api/output-dir", {"dir": str(target)})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["dir"], str(target))
        self.assertIn("复制", body["human"])
        self.assertTrue(target.is_dir())
        self.assertEqual(self.main.SETTINGS["output"]["dir"], str(target))
        self.assertEqual(json.loads(self.saved())["output"], {"dir": str(target)})

    def test_save_empty_turns_copy_off(self):
        self.main.SETTINGS["output"]["dir"] = str(self.out)
        r = self.post("/api/output-dir", {"dir": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["dir"], "")
        self.assertIn("关掉复制", r.json()["human"])
        self.assertEqual(self.main.SETTINGS["output"]["dir"], "")
        self.assertEqual(json.loads(self.saved())["output"], {"dir": ""})

    def test_missing_dir_key_is_empty(self):
        self.assertEqual(self.post("/api/output-dir", {}).json()["dir"], "")

    def test_bad_values_are_400_with_human_and_write_nothing(self):
        before = self.saved()
        furnace = self.main.SETTINGS["output_dir"]
        cases = [("相对路径", "bad_path"), ("output", "bad_path"), ("/no/drive", "bad_path"),
                 (123, "bad_path"), (furnace, "is_comfy_output"),
                 (furnace + "\\huafang", "is_comfy_output"),
                 (free_drive() + ":\\画集", "cannot_create"),
                 (str(self.tmp / "settings.json"), "cannot_create")]
        for raw, code in cases:
            r = self.post("/api/output-dir", {"dir": raw})
            self.assertEqual(r.status_code, 400, raw)
            detail = r.json()["detail"]
            self.assertEqual(detail["code"], code, raw)
            self.assertTrue(detail["human"], raw)
            self.assertNotIn("node", detail["human"])
            self.assertEqual(self.main.SETTINGS["output"]["dir"], "", raw)
        self.assertEqual(self.saved(), before)      # 一个坏值都没写进 settings.json

    def test_reveal_without_dir_opens_configured(self):
        self.main.SETTINGS["output"]["dir"] = str(self.out)
        r = self.post("/api/reveal", {})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["dir"], str(self.out))
        self.assertIn("打开", r.json()["human"])
        self.assertEqual(self.opened, [str(self.out)])

    def test_reveal_falls_back_to_furnace_output(self):
        r = self.post("/api/reveal", {})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.opened, [self.main.SETTINGS["output_dir"]])

    def test_reveal_without_initial_configuration_opens_nothing(self):
        self.main.SETTINGS["output_dir"] = ""
        r = self.post("/api/reveal", {})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "setup_required")
        self.assertEqual(self.opened, [])

    def test_reveal_accepts_only_the_configured_dir(self):
        self.main.SETTINGS["output"]["dir"] = str(self.out)
        for raw in (str(self.tmp), "C:\\画集\\我的作品", str(self.out) + "\\子目录"):
            r = self.post("/api/reveal", {"dir": raw})
            self.assertEqual(r.status_code, 400, raw)
            self.assertEqual(r.json()["detail"]["code"], "not_configured", raw)
            self.assertTrue(r.json()["detail"]["human"])
        self.assertEqual(self.opened, [])           # 一次 explorer 都没跑

    def test_reveal_accepts_configured_dir_written_differently(self):
        self.main.SETTINGS["output"]["dir"] = str(self.out)
        r = self.post("/api/reveal", {"dir": str(self.out).replace("\\", "/") + "/"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.opened, [str(self.out)])

    def test_reveal_reports_missing_dir(self):
        self.main.SETTINGS["output"]["dir"] = str(self.tmp / "没了")
        r = self.post("/api/reveal", {})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["code"], "not_found")
        self.assertEqual(self.opened, [])

    def test_reveal_without_body(self):
        r = self.http.post("/api/reveal",
                           headers={"Authorization": "Bearer " + self.main.TOKEN})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.opened, [self.main.SETTINGS["output_dir"]])

    def test_job_endpoint_reports_copy_error(self):
        job = runner.Job("j_t010_ep", {})
        job.status = "done"
        job.copy_error = "已存炉子输出，复制到你的目录失败：a.png（原件没丢，在炉子的输出文件夹里）"
        self.main.runner.JOBS[job.id] = job
        try:
            r = self.http.get("/api/job/" + job.id,
                              headers={"Authorization": "Bearer " + self.main.TOKEN})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["copy_error"], job.copy_error)
            self.assertEqual(r.json()["status"], "done")
        finally:
            self.main.runner.JOBS.pop(job.id, None)

    def test_jobs_list_reports_copy_error(self):
        job = runner.Job("j_t010_list", {})
        job.status = "done"
        job.copy_error = "已存炉子输出，复制到你的目录失败：b.png（原件没丢，在炉子的输出文件夹里）"
        self.main.runner.JOBS[job.id] = job
        try:
            r = self.http.get("/api/jobs",
                              headers={"Authorization": "Bearer " + self.main.TOKEN})
            row = [j for j in r.json()["jobs"] if j["job_id"] == job.id][0]
            self.assertEqual(row["copy_error"], job.copy_error)
        finally:
            self.main.runner.JOBS.pop(job.id, None)


if __name__ == "__main__":
    unittest.main()
