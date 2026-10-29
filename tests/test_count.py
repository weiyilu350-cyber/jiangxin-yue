"""T009 张数（batch_size 1–8）：夹取矩阵／batch 槽隔离／回执第 13 项／多张解析／预设不含张数。"""
import copy
import unittest
from pathlib import Path

from app import presets as PS
from app import runner
from app import workflow_profile as wp

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"
PIDS = ("anima_t2i", "anima_dual", "anima_tri")
UNETS = ["anima-base-v1.0.safetensors"]
PENS = [{"name": "sample_style.safetensors", "strength": 0.9},
        {"name": "sample_style.safetensors", "strength": 0.8},
        {"name": "sample_style.safetensors", "strength": 0.5}]
PARAMS = {"width": 512, "height": 512, "steps": 8, "cfg": 4.0, "seed": 77001,
          "sampler_name": "euler", "scheduler": "simple"}


def load(pid="anima_t2i"):
    return wp.load_pair(WORKFLOWS / f"{pid}.json")


def req(pid="anima_t2i", loras=None, count=None, **over):
    """界面请求；不传 count ＝老客户端（向后兼容，一律当 1）。"""
    r = {"profile_id": pid, "unet": UNETS[0], "positive": "1girl", "negative": "", **PARAMS}
    r["loras"] = [None] if loras is None else loras
    if count is not None:
        r["count"] = count
    r.update(over)
    return r


def lz(prof, stage=0):
    return prof["stages"][stage]["latent_size"]


def legacy(prof):
    """T009 之前的 profile 形状：latent_size 里没有 batch_input 槽。"""
    p = copy.deepcopy(prof)
    del lz(p)["batch_input"]
    return p


def entry(pid, workflow, images, client_id="huafang"):
    """/history 的一条记录（形状同 test_reconcile：client_id 只在 prompt[3] 里）。"""
    return {"prompt": [1, pid, workflow, {"client_id": client_id, "create_time": 1.7e12}, []],
            "outputs": {"9": {"images": images}} if images is not None else {},
            "status": {"status_str": "success", "completed": True, "messages": []},
            "extra_data": None}


def imgs(n, sub="huafang/anima_t2i"):
    return [{"filename": f"hf_{i:05d}_.png", "subfolder": sub, "type": "output"}
            for i in range(n)]


def diff_inputs(a, b):
    """两份工作流的 inputs 差异，返回 [(节点, 输入名)]：用来证明只动了 batch 一个槽。"""
    out = []
    for node in sorted(set(a) | set(b)):
        ia = (a.get(node) or {}).get("inputs", {})
        ib = (b.get(node) or {}).get("inputs", {})
        for key in sorted(set(ia) | set(ib)):
            if ia.get(key) != ib.get(key):
                out.append((node, key))
    return out


class ClampTests(unittest.TestCase):
    """卡 §3：夹取矩阵（0／1／8／9／-3／"abc"／缺省）。"""

    def test_matrix(self):
        cases = [(0, 1), (1, 1), (8, 8), (9, 8), (-3, 1), ("abc", 1), ("", 1), (None, 1),
                 ("3", 3), (2.7, 2), (1000, 8), (-0.5, 1), ([], 1), ({}, 1)]
        for given, want in cases:
            self.assertEqual(runner.clamp_count(given), want, f"count={given!r}")

    def test_range_constants(self):
        self.assertEqual((runner.COUNT_MIN, runner.COUNT_MAX), (1, 8))

    def test_clamp_is_idempotent(self):
        for v in (0, 1, 5, 8, 9, "abc", None):
            once = runner.clamp_count(v)
            self.assertEqual(runner.clamp_count(once), once)


class BatchSlotTests(unittest.TestCase):
    """卡 §3：batch 写进 profile 的 batch_input 槽，且不影响其余槽。"""

    def test_all_three_profiles_carry_batch_slot(self):
        for pid in PIDS:
            _, prof = load(pid)
            self.assertEqual(lz(prof)["batch_input"], "batch_size", pid)

    def test_count_lands_in_batch_slot_for_every_profile(self):
        for pid, pens in ((PIDS[0], [None]), (PIDS[1], PENS[:2]), (PIDS[2], PENS)):
            wf, prof = load(pid)
            sent = runner.build_workflow(wf, prof, req(pid, pens, count=3))
            s = lz(prof)
            self.assertEqual(sent[s["node"]]["inputs"][s["batch_input"]], 3, pid)

    def test_clamped_value_is_what_gets_written(self):
        wf, prof = load()
        s = lz(prof)
        for given, want in ((99, 8), ("abc", 1), (0, 1), (None, 1)):
            sent = runner.build_workflow(wf, prof, req(count=given))
            self.assertEqual(sent[s["node"]]["inputs"][s["batch_input"]], want, f"count={given!r}")

    def test_absent_count_keeps_one(self):
        """老客户端不传 count：仍是 1 张，与 T001 一致。"""
        wf, prof = load()
        sent = runner.build_workflow(wf, prof, req())
        s = lz(prof)
        self.assertEqual(sent[s["node"]]["inputs"][s["batch_input"]], 1)

    def test_only_the_batch_input_changes(self):
        wf, prof = load(PIDS[2])
        one = runner.build_workflow(wf, prof, req(PIDS[2], PENS, count=1))
        eight = runner.build_workflow(wf, prof, req(PIDS[2], PENS, count=8))
        s = lz(prof)
        self.assertEqual(diff_inputs(one, eight), [(s["node"], s["batch_input"])])

    def test_profile_without_batch_slot_still_builds(self):
        """槽名不存在就不写（profile 是不可信数据，不许因为缺一个键就崩）。"""
        wf, prof = load()
        old = legacy(prof)
        sent = runner.build_workflow(wf, old, req(count=4))
        self.assertEqual(sent[lz(prof)["node"]]["inputs"]["batch_size"], 1)   # 留工作流默认


class ReceiptTests(unittest.TestCase):
    """卡 §3：新行 ✓／✗ 两路 ＋ 既有 12 行回归（张张相等）。"""

    def test_item_counts_are_13_15_17(self):
        for pid, pens, want in ((PIDS[0], [None], 13), (PIDS[1], PENS[:2], 15), (PIDS[2], PENS, 17)):
            wf, prof = load(pid)
            r = req(pid, pens, count=2)
            sent = runner.build_workflow(wf, prof, r)
            items = runner.build_receipt(sent, r, prof, sent)
            self.assertEqual(len(items), want, pid)
            self.assertEqual([i["key"] for i in items if not i["ok"]], [], pid)

    def test_batch_row_values(self):
        wf, prof = load()
        r = req(count=2)
        sent = runner.build_workflow(wf, prof, r)
        row = {i["key"]: i for i in runner.build_receipt(sent, r, prof, sent)}["batch_size"]
        self.assertEqual((row["requested"], row["actual"], row["ok"]), (2, 2, True))

    def test_legacy_twelve_rows_are_identical(self):
        """口径变更只加一行：前 12 项与没有 batch 槽时的旧回执逐项逐字段相等。"""
        wf, prof = load()
        r = req(count=4)
        sent = runner.build_workflow(wf, prof, r)
        new = runner.build_receipt(sent, r, prof, sent)
        old_prof = legacy(prof)
        old_sent = runner.build_workflow(wf, old_prof, r)
        old = runner.build_receipt(old_sent, r, old_prof, old_sent)
        self.assertEqual(len(old), 12)
        self.assertEqual(len(new), 13)
        self.assertEqual(new[:12], old)
        self.assertEqual(new[12]["key"], "batch_size")

    def test_legacy_rows_identical_for_tri_profile(self):
        wf, prof = load(PIDS[2])
        r = req(PIDS[2], PENS, count=8)
        sent = runner.build_workflow(wf, prof, r)
        new = runner.build_receipt(sent, r, prof, sent)
        old_prof = legacy(prof)
        old_sent = runner.build_workflow(wf, old_prof, r)
        old = runner.build_receipt(old_sent, r, old_prof, old_sent)
        self.assertEqual((len(old), len(new)), (16, 17))
        self.assertEqual(new[:16], old)

    def test_mismatch_flips_only_batch_row(self):
        wf, prof = load(PIDS[2])
        r = req(PIDS[2], PENS, count=2)
        sent = runner.build_workflow(wf, prof, r)
        history = copy.deepcopy(sent)
        s = lz(prof)
        history[s["node"]]["inputs"][s["batch_input"]] = 1     # 炉子只出了一张
        items = runner.build_receipt(history, r, prof, sent)
        self.assertEqual([i["key"] for i in items if not i["ok"]], ["batch_size"])
        self.assertEqual(len(items), 17)

    def test_requested_is_clamped_not_raw(self):
        """框里填 99：界面要的＝夹取后的 8，回执不会因为「99≠8」自己报 ✗。"""
        wf, prof = load()
        r = req(count=99)
        sent = runner.build_workflow(wf, prof, r)
        row = {i["key"]: i for i in runner.build_receipt(sent, r, prof, sent)}["batch_size"]
        self.assertEqual((row["requested"], row["actual"], row["ok"]), (8, 8, True))

    def test_missing_count_reports_one_and_still_ok(self):
        wf, prof = load()
        r = req()
        sent = runner.build_workflow(wf, prof, r)
        row = {i["key"]: i for i in runner.build_receipt(sent, r, prof, sent)}["batch_size"]
        self.assertEqual((row["requested"], row["actual"], row["ok"]), (1, 1, True))

    def test_summary_counts_the_new_row(self):
        wf, prof = load()
        r = req(count=3)
        sent = runner.build_workflow(wf, prof, r)
        job = runner.Job("j1", r)
        job.profile = prof
        job.sent = sent
        runner.apply_history(job, entry("p1", sent, imgs(3)))
        self.assertEqual(runner.receipt_summary(job), "13/13 ✓")


class MultiImageTests(unittest.TestCase):
    """卡 §3：多张 outputs 解析成 images[N]、顺序保持。"""

    def test_history_images_keeps_all_in_order(self):
        got = runner.history_images(entry("p1", {}, imgs(8)))
        self.assertEqual([i["filename"] for i in got],
                         [f"hf_{n:05d}_.png" for n in range(8)])

    def test_history_images_across_several_nodes(self):
        e = {"outputs": {"9": {"images": imgs(2)}, "12": {"images": imgs(2, "other")}}}
        got = runner.history_images(e)
        self.assertEqual(len(got), 4)
        self.assertEqual([i["subfolder"] for i in got],
                         ["huafang/anima_t2i"] * 2 + ["other"] * 2)

    def test_apply_history_puts_every_image_on_the_job(self):
        wf, prof = load()
        r = req(count=3)
        sent = runner.build_workflow(wf, prof, r)
        job = runner.Job("j1", r)
        job.profile = prof
        job.sent = sent
        runner.apply_history(job, entry("p1", sent, imgs(3)))
        self.assertEqual(job.status, "done")
        self.assertEqual(len(job.images), 3)
        self.assertEqual([i["key"] for i in job.receipt if not i["ok"]], [])

    def test_partial_batch_history_leaves_job_not_done(self):
        """history 还没写出来（outputs 空）＝还没跑完，不提前判 done、不摆半墙。"""
        wf, prof = load()
        r = req(count=4)
        sent = runner.build_workflow(wf, prof, r)
        job = runner.Job("j1", r)
        job.profile = prof
        job.sent = sent
        job.status = "running"
        runner.apply_history(job, entry("p1", sent, None))
        self.assertEqual((job.status, job.images, job.receipt), ("running", [], None))

    def test_readonly_rebuild_carries_all_images_without_receipt(self):
        job = runner.rebuild_job("p9", entry("p9", {}, imgs(5)))
        self.assertTrue(job.readonly)
        self.assertEqual(job.status, "done")
        self.assertEqual(len(job.images), 5)
        self.assertIsNone(job.receipt)


class PresetIsolationTests(unittest.TestCase):
    """卡 §禁止：张数不进预设（PARAMS 结构不许扩）；存→载→count 不动结构。"""

    def test_param_keys_do_not_include_count(self):
        self.assertNotIn("count", PS.PARAM_KEYS)
        self.assertEqual(PS.PARAM_KEYS,
                         ("width", "height", "steps", "cfg", "seed", "sampler_name", "scheduler"))

    def test_add_recipe_drops_count(self):
        data = PS.empty_table()
        r = PS.add_recipe(data, "张数验收", UNETS[0], [PENS[0]], "1girl", "",
                          dict(PARAMS, count=8))
        self.assertNotIn("count", r["params"])
        self.assertEqual(sorted(r["params"]), sorted(PS.PARAM_KEYS))

    def test_default_recipe_has_no_count(self):
        r = PS.default_recipe({"default_unet": UNETS[0]}, {"steps": 20, "count": 8, "junk": 1})
        self.assertNotIn("count", r["params"])
        self.assertEqual(r["params"]["steps"], 20)

    def test_load_both_modes_have_no_count(self):
        data = PS.empty_table()
        r = PS.add_recipe(data, "张数验收", UNETS[0], [PENS[0]], "1girl", "",
                          dict(PARAMS, count=8))
        for mode in ("full", "params"):
            got = PS.build_load(r, {}, PIDS[0], mode, 1, UNETS)
            self.assertNotIn("count", got["params"], mode)
            self.assertEqual(got["params"]["width"], PARAMS["width"], mode)

    def test_load_never_refills_the_count_box(self):
        """存配方→改张数→载配方：回填包与落盘的配方里都没有 count，前端按 PARAM_FIELDS 回填碰不到张数框。"""
        data = PS.empty_table()
        PS.add_recipe(data, "张数验收", UNETS[0], [], "1girl", "", dict(PARAMS, count=2))
        stored = data["recipes"][0]
        got = PS.build_load(stored, {}, PIDS[0], "full", 1, UNETS)
        self.assertNotIn("count", got["params"])
        self.assertNotIn("count", stored["params"])
        self.assertNotIn("count", stored)


if __name__ == "__main__":
    unittest.main()
