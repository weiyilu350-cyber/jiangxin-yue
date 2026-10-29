import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import runner
from app import workflow_profile as wp
from app.triggers import TriggerResolver, compose

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"

TRI_PENS = [
    {"name": "sample_style.safetensors", "strength": 0.9},
    {"name": "sample_style.safetensors", "strength": 0.65},
    {"name": "sample_style.safetensors", "strength": 0.5},
]


def load(pid):
    wf, prof = wp.load_pair(WORKFLOWS / f"{pid}.json")
    return wf, prof


def base_req(pid, loras):
    return {
        "profile_id": pid,
        "unet": "anima-base-v1.0.safetensors",
        "loras": loras,
        "positive": "newest, safe, 1girl, solo, dress",
        "negative": "worst quality",
        "width": 832,
        "height": 1216,
        "steps": 30,
        "cfg": 4.0,
        "seed": 12345,
        "sampler_name": "euler",
        "scheduler": "simple",
    }


def pos_node(prof):
    return prof["stages"][0]["prompt"]["positive_source"][0]["node"]


def slot(prof, i):
    return prof["lora_slots"][i]


class ReceiptTests(unittest.TestCase):
    def test_tri_three_slots_gives_16_items_all_ok(self):
        wf, prof = load("anima_tri")
        req = base_req("anima_tri", TRI_PENS)
        sent = runner.build_workflow(wf, prof, req)
        items = runner.build_receipt(sent, req, prof, sent)
        self.assertEqual(len(items), 17)   # T009 起 tri＝17 项（原 16＋batch_size）
        self.assertEqual([i["key"] for i in items if not i["ok"]], [])

    def test_tri_each_slot_reports_name_and_strength(self):
        wf, prof = load("anima_tri")
        req = base_req("anima_tri", TRI_PENS)
        sent = runner.build_workflow(wf, prof, req)
        items = {i["key"]: i for i in runner.build_receipt(sent, req, prof, sent)}
        for n, pen in enumerate(TRI_PENS, start=1):
            self.assertEqual(items[f"lora{n}.name"]["actual"], pen["name"])
            self.assertTrue(items[f"lora{n}.name"]["ok"])
            self.assertEqual(items[f"lora{n}.strength"]["actual"], pen["strength"])
            self.assertTrue(items[f"lora{n}.strength"]["ok"])

    def test_strengths_actually_land_on_distinct_nodes(self):
        """强度分别生效＝三个节点各自拿到自己的值，不是共用一个。"""
        wf, prof = load("anima_tri")
        sent = runner.build_workflow(wf, prof, base_req("anima_tri", TRI_PENS))
        got = [sent[ls["node"]]["inputs"][ls["strength_input"]] for ls in prof["lora_slots"]]
        self.assertEqual(got, [0.9, 0.65, 0.5])
        names = [sent[ls["node"]]["inputs"][ls["name_input"]] for ls in prof["lora_slots"]]
        self.assertEqual(names, [p["name"] for p in TRI_PENS])

    def test_empty_slot_keeps_default_name_and_zero_strength(self):
        """D65：空槽保留默认笔名、强度归零、不报缺笔。"""
        wf, prof = load("anima_tri")
        req = base_req("anima_tri", [TRI_PENS[0], None, TRI_PENS[2]])
        sent = runner.build_workflow(wf, prof, req)
        slot2 = prof["lora_slots"][1]
        default_name = wf[slot2["node"]]["inputs"][slot2["name_input"]]
        self.assertEqual(sent[slot2["node"]]["inputs"][slot2["name_input"]], default_name)
        self.assertEqual(sent[slot2["node"]]["inputs"][slot2["strength_input"]], 0.0)
        items = {i["key"]: i for i in runner.build_receipt(sent, req, prof, sent)}
        self.assertEqual(items["lora2.name"]["requested"], "（空槽）")
        self.assertTrue(items["lora2.name"]["ok"])
        self.assertTrue(items["lora2.strength"]["ok"])
        self.assertTrue(items["lora1.name"]["ok"])
        self.assertTrue(items["lora3.name"]["ok"])

    def test_short_loras_list_pads_with_empty_slots(self):
        wf, prof = load("anima_tri")
        req = base_req("anima_tri", [TRI_PENS[0]])
        sent = runner.build_workflow(wf, prof, req)
        for i in (1, 2):
            s = slot(prof, i)
            self.assertEqual(sent[s["node"]]["inputs"][s["strength_input"]], 0.0)
        self.assertEqual([i["key"] for i in runner.build_receipt(sent, req, prof, sent) if not i["ok"]], [])

    def test_mismatch_flips_ok_to_false(self):
        wf, prof = load("anima_tri")
        req = base_req("anima_tri", TRI_PENS)
        sent = runner.build_workflow(wf, prof, req)
        history = copy.deepcopy(sent)
        s3 = slot(prof, 2)
        history[s3["node"]]["inputs"][s3["strength_input"]] = 0.99
        bad = [i["key"] for i in runner.build_receipt(history, req, prof, sent) if not i["ok"]]
        self.assertEqual(bad, ["lora3.strength"])

    def test_t2i_regression_still_13_items(self):
        """AGENTS.md §4 回归义务：场景 1（直出无笔）回执全 ✓；T009 起基准 12→13 项（多的那行是 batch_size）。"""
        wf, prof = load("anima_t2i")
        req = base_req("anima_t2i", [None])
        sent = runner.build_workflow(wf, prof, req)
        items = runner.build_receipt(sent, req, prof, sent)
        self.assertEqual(len(items), 13)
        self.assertEqual([i["key"] for i in items if not i["ok"]], [])

    def test_dual_regression_still_15_items(self):
        wf, prof = load("anima_dual")
        req = base_req("anima_dual", [TRI_PENS[0], TRI_PENS[1]])
        sent = runner.build_workflow(wf, prof, req)
        items = runner.build_receipt(sent, req, prof, sent)
        self.assertEqual(len(items), 15)
        self.assertEqual([i["key"] for i in items if not i["ok"]], [])

    def test_clip_vae_baseline_comes_from_sent_copy(self):
        wf, prof = load("anima_tri")
        req = base_req("anima_tri", TRI_PENS)
        sent = runner.build_workflow(wf, prof, req)
        items = {i["key"]: i for i in runner.build_receipt(sent, req, prof, sent)}
        self.assertEqual(items["clip"]["requested"], "qwen_3_06b_base.safetensors")
        self.assertTrue(items["clip"]["ok"])
        self.assertTrue(items["vae"]["ok"])


class TriggerIntoWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        table = root / "triggers.json"
        # 独立合成名称保留多笔触发词顺序测试，避免样例笔统一后让三种触发词撞成一项。
        self.pens = [{**pen, "name": f"sample_{i}.safetensors"}
                     for i, pen in enumerate(TRI_PENS, 1)]
        table.write_text(json.dumps({"sample_1": "first_tag", "sample_2": "second_tag",
                                     "sample_3": "third_tag"}), encoding="utf-8")
        self.r = TriggerResolver(table, root)

    def test_triggers_reach_positive_node_in_slot_order(self):
        wf, prof = load("anima_tri")
        req = base_req("anima_tri", self.pens)
        pos = compose(req["positive"], self.r.for_loras(req["loras"]))
        sent = runner.build_workflow(wf, prof, req, pos)
        node = prof["stages"][0]["prompt"]["positive_source"][0]["node"]
        self.assertEqual(
            sent[node]["inputs"]["text"],
            "newest, safe, 1girl, solo, first_tag, second_tag, third_tag, dress",
        )

    def test_two_pens_slot_order_and_dedupe(self):
        wf, prof = load("anima_dual")
        req = base_req("anima_dual", [self.pens[1], self.pens[2]])
        pos = compose("newest, 1girl, third_tag, solo, dress", self.r.for_loras(req["loras"]))
        sent = runner.build_workflow(wf, prof, req, pos)
        node = prof["stages"][0]["prompt"]["positive_source"][0]["node"]
        self.assertEqual(
            sent[node]["inputs"]["text"],
            "newest, 1girl, solo, second_tag, third_tag, dress",
        )

    def test_no_pen_leaves_positive_untouched(self):
        wf, prof = load("anima_t2i")
        req = base_req("anima_t2i", [None])
        pos = compose(req["positive"], self.r.for_loras(req["loras"]))
        sent = runner.build_workflow(wf, prof, req, pos)
        self.assertEqual(sent[pos_node(prof)]["inputs"]["text"], req["positive"])

    def test_positive_override_is_optional(self):
        """不传 positive 时行为与 A1 一致，仍用界面原文。"""
        wf, prof = load("anima_tri")
        req = base_req("anima_tri", self.pens)
        sent = runner.build_workflow(wf, prof, req)
        self.assertEqual(sent["4"]["inputs"]["text"], req["positive"])


if __name__ == "__main__":
    unittest.main()
