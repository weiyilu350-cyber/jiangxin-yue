"""B 卡 §2/§3/§4 单测：契约两级结果、尺寸倍率派生、逐段取值、D92 成品分流、段视图。

用的是一份合成的两段工作流（CheckpointLoaderSimple ＋ 带 clip 强度的 LoraLoader），
故意挑内置三条里没有的形状——否则这些新分支永远不会被走到，等于没验。
节点号只出现在测试与 profile 里，不进 app/static（D83/D96 红线由 test_profiles 守着）。
"""
import json
import unittest
from pathlib import Path

from app import runner
from app import workflow_profile as wp

WORKFLOWS = Path(__file__).resolve().parent.parent / "workflows"

# 底模打成一个包（CheckpointLoaderSimple）＋ 笔带 clip 强度（LoraLoader）：
# 改前的契约只认 UNETLoader／LoraLoaderModelOnly，这份会被判「链断了」＋「链上笔与声明不一致」两条假错。
CKPT_WF = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}},
    "2": {"class_type": "LoraLoader",
          "inputs": {"model": ["1", 0], "clip": ["1", 1], "lora_name": "pen.safetensors",
                     "strength_model": 0.8, "strength_clip": 0.8}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "pos", "clip": ["1", 1]}},
    "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "neg", "clip": ["1", 1]}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "6": {"class_type": "KSampler",
          "inputs": {"model": ["2", 0], "positive": ["3", 0], "negative": ["4", 0],
                     "latent_image": ["5", 0], "seed": 1, "steps": 20, "cfg": 4.0,
                     "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
    "7": {"class_type": "KSampler",
          "inputs": {"model": ["2", 0], "positive": ["3", 0], "negative": ["4", 0],
                     "latent_image": ["6", 0], "seed": 2, "steps": 15, "cfg": 3.0,
                     "sampler_name": "euler", "scheduler": "simple", "denoise": 0.5}},
    "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "out"}},
    "9": {"class_type": "ImageScale",
          "inputs": {"image": ["7", 0], "width": 1024, "height": 1024,
                     "upscale_method": "lanczos", "crop": "disabled"}},
}

# 同一张图去掉笔：采样器直接吃底模。用来验「单段无笔」不该被 warning 烦主人（anima_turbo 的形状）。
NO_PEN_WF = {k: v for k, v in CKPT_WF.items() if k != "2"}
NO_PEN_WF["6"] = dict(CKPT_WF["6"], inputs=dict(CKPT_WF["6"]["inputs"], model=["1", 0]))
NO_PEN_WF["7"] = dict(CKPT_WF["7"], inputs=dict(CKPT_WF["7"]["inputs"], model=["1", 0]))

# 双模型接力（D78 的真实形状）：第二段自带一个底模加载器，它的 model 链上根本没有笔。
# 这时 `stages: ["stage1"]` 才是实话；写成「管全部段」反倒该被判错。
RELAY_WF = dict(CKPT_WF,
                **{"10": {"class_type": "UNETLoader", "inputs": {"unet_name": "relay.safetensors"}}})
RELAY_WF["7"] = dict(CKPT_WF["7"], inputs=dict(CKPT_WF["7"]["inputs"], model=["10", 0]))


def ckpt_profile(pen_stages=None, stage2_size_out=True):
    """合成 profile。pen_stages＝None 时不写 stages 键（＝这支笔管全部段，内置三条的形状）。"""
    ls = {"role": "lora1", "node": "2", "name_input": "lora_name",
          "strength_input": "strength_model", "clip_strength_input": "strength_clip"}
    if pen_stages is not None:
        ls["stages"] = pen_stages
    stage2 = {
        "id": "stage2", "label": "第二段·精修放大", "model_role": "unet", "lora_roles": [],
        "links": {"model": {"node": "7", "input": "model"}},
        "params": {"steps": {"node": "7", "input": "steps"}},
        "prompt": {"positive_source": [{"node": "3", "input": "text"}],
                   "negative_source": [{"node": "4", "input": "text"}]},
    }
    if stage2_size_out:
        stage2["size_out"] = {"node": "9", "width_input": "width", "height_input": "height"}
    return {
        "schema_version": 2, "workflow_id": "synthetic_ckpt", "label": "合成两段",
        "model_slots": [{"role": "unet", "node": "1", "input": "ckpt_name"}],
        "lora_slots": [ls],
        "stages": [
            {"id": "stage1", "label": "第一段·出图", "model_role": "unet", "lora_roles": ["lora1"],
             "links": {"model": {"node": "6", "input": "model"}},
             "params": {"steps": {"node": "6", "input": "steps"}},
             "prompt": {"positive_source": [{"node": "3", "input": "text"}],
                        "negative_source": [{"node": "4", "input": "text"}]},
             "latent_size": {"node": "5", "width_input": "width", "height_input": "height",
                             "batch_input": "batch_size", "size_scale": [0.5, 0.5]}},
            stage2,
        ],
        "save_slots": [{"node": "8", "prefix_input": "filename_prefix", "final": True}],
        "image_slots": [],
        "params": {"per_stage": True, "keep_workflow_value": []},
        "defaults": {"steps": 20, "width": 1024, "height": 1024},
    }


def req_of(**over):
    req = {"positive": "p", "negative": "n", "width": 1024, "height": 1024,
           "count": 1, "seed": 7, "steps": 20, "loras": []}
    req.update(over)
    return req


class ContractTwoLevelTests(unittest.TestCase):
    """D76：errors 拦着不让用，warnings 只是要界面说一句。"""

    def test_checkpoint_and_lora_loader_are_legal(self):
        """改前这两型都不认，会报两条假错；现在必须零错。"""
        errors, warnings = wp.validate_full(CKPT_WF, ckpt_profile())
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_validate_is_still_errors_only(self):
        """A 阶段口径：validate() 只返 errors，所有 `== []` 的断言意思不变。"""
        self.assertEqual(wp.validate(CKPT_WF, ckpt_profile()), [])

    def test_d78_partial_pen_coverage_is_warning_not_error(self):
        """一支笔只管第一段＝双模型接力的正常形状：不许判错，但要说得出「第二段无笔」。"""
        errors, warnings = wp.validate_full(RELAY_WF, ckpt_profile(pen_stages=["stage1"]))
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("stage2", warnings[0])
        self.assertIn("此段无笔", warnings[0])

    def test_relay_pen_claiming_stage2_is_an_error(self):
        """反过来说实话：第二段链上没笔却声明它管第二段＝profile 与图不符，必须拦。"""
        errors, _ = wp.validate_full(RELAY_WF, ckpt_profile(pen_stages=["stage1", "stage2"]))
        self.assertTrue(any("stage2" in e and "不一致" in e for e in errors), errors)

    def test_full_coverage_and_missing_key_have_no_warning(self):
        """缺 stages 键＝管全部段（内置三条没写这个键），显式写全也一样：都不该有 warning。"""
        for stages in (None, ["stage1", "stage2"]):
            self.assertEqual(wp.validate_full(CKPT_WF, ckpt_profile(pen_stages=stages))[1], [],
                             f"pen_stages={stages}")

    def test_single_stage_without_pen_is_not_noisy(self):
        """单段无笔（如 anima_turbo）是合法配方，不该拿「此段无笔」烦主人。"""
        prof = ckpt_profile()
        prof["lora_slots"] = []
        prof["stages"][0]["lora_roles"] = []
        del prof["stages"][1]
        errors, warnings = wp.validate_full(NO_PEN_WF, prof)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_pen_on_chain_but_not_declared_is_still_an_error(self):
        """反过来：图上有笔、profile 没声明＝画舫管不到它，必须拦（别把真错降级成 warning）。"""
        prof = ckpt_profile()
        prof["lora_slots"] = []
        errors, _ = wp.validate_full(CKPT_WF, prof)
        self.assertTrue(any("不一致" in e for e in errors), errors)

    def test_missing_model_link_is_plain_error_not_keyerror(self):
        """改前 `st["links"]["model"]` 直接 KeyError 崩掉；现在必须是人话 error。"""
        prof = ckpt_profile()
        del prof["stages"][0]["links"]
        errors, _ = wp.validate_full(CKPT_WF, prof)
        self.assertTrue(any("没声明 model 连线" in e for e in errors), errors)
        # 链没走通时不许再顺带报一条「链上笔不一致」的假错，把真原因盖掉
        self.assertEqual([e for e in errors if "不一致" in e], [])

    def test_chain_that_never_reaches_a_loader_is_an_error(self):
        wf = dict(CKPT_WF, **{"1": {"class_type": "SomethingElse", "inputs": {}}})
        errors, _ = wp.validate_full(wf, ckpt_profile())
        self.assertTrue(any("没落到底模加载器" in e for e in errors), errors)

    def test_bad_size_scale_is_plain_error(self):
        for bad in ([0.5], "0.5", [-1.0, 2.0], [0, 1], [True, True], [1, 2, 3]):
            prof = ckpt_profile()
            prof["stages"][0]["latent_size"]["size_scale"] = bad
            errors, _ = wp.validate_full(CKPT_WF, prof)
            self.assertTrue(any("size_scale" in e for e in errors), f"{bad!r} 没被拦：{errors}")

    def test_size_scale_absent_means_one(self):
        """缺键＝1.0，是合法且最常见的形状（内置三条就是这样），不许报错。"""
        prof = ckpt_profile()
        del prof["stages"][0]["latent_size"]["size_scale"]
        self.assertEqual(wp.validate_full(CKPT_WF, prof)[0], [])

    def test_lora_stage_id_must_exist(self):
        prof = ckpt_profile(pen_stages=["stage9"])
        errors, _ = wp.validate_full(CKPT_WF, prof)
        self.assertTrue(any("不存在的段 stage9" in e for e in errors), errors)

    def test_latent_and_size_out_must_not_be_the_same_node(self):
        """同一个节点既是输入画布又是成品尺寸＝必有一个认错，得拦下来。"""
        prof = ckpt_profile()
        prof["stages"][1]["size_out"] = dict(prof["stages"][0]["latent_size"])
        prof["stages"][1]["size_out"].pop("batch_input")
        prof["stages"][1]["latent_size"] = dict(prof["stages"][0]["latent_size"])
        errors, _ = wp.validate_full(CKPT_WF, prof)
        self.assertTrue(any("必有一个认错了" in e for e in errors), errors)

    def test_size_out_missing_inputs_is_an_error(self):
        prof = ckpt_profile()
        prof["stages"][1]["size_out"] = {"node": "8", "width_input": "width",
                                         "height_input": "height"}
        errors, _ = wp.validate_full(CKPT_WF, prof)
        self.assertTrue(any("成品尺寸源" in e for e in errors), errors)

    def test_covers_helper(self):
        self.assertTrue(wp.covers({"role": "lora1"}, "stage2"))          # 缺键＝管全部段
        self.assertTrue(wp.covers({"stages": ["stage1"]}, "stage1"))
        self.assertFalse(wp.covers({"stages": ["stage1"]}, "stage2"))
        self.assertTrue(wp.covers({"stages": []}, "stage1"))             # 空列表按缺键处理

    def test_capabilities_derive_hires_and_reference_images(self):
        caps = wp.derive_capabilities(ckpt_profile())
        self.assertEqual(caps["stages"], 2)
        self.assertTrue(caps["hires"])
        self.assertEqual(caps["lora_max"], 1)
        self.assertEqual(caps["reference_images"], 0)
        self.assertTrue(caps["negative_prompt"])     # 卡 §2 没列它，仍写死 True

        no_hires = wp.derive_capabilities(ckpt_profile(stage2_size_out=False))
        self.assertFalse(no_hires["hires"])
        # 倍率型放大器（ImageScaleBy 那类）只有 hires 标记、没有 size_out，也算放大段
        marked = ckpt_profile(stage2_size_out=False)
        marked["stages"][1]["hires"] = True
        self.assertTrue(wp.derive_capabilities(marked)["hires"])

        with_ref = ckpt_profile()
        with_ref["image_slots"] = [{"role": "ref1", "node": "9", "input": "image"}]
        self.assertEqual(wp.derive_capabilities(with_ref)["reference_images"], 1)


class ScaledSizeTests(unittest.TestCase):
    """倍率语义：段1＝全局×0.5、段2＝段1×2＝全局×1.0；存倍率不存字面值。"""

    def test_scale_one_passes_through_untouched(self):
        """倍率 1.0（缺键）时一个像素都不动：A 阶段验过的直出行为靠这条守住。"""
        for slot in ({}, {"size_scale": None}, {"size_scale": [1.0, 1.0]}):
            self.assertEqual(wp.scaled_size(slot, 832, 1216), (832, 1216), slot)

    def test_half_scale_follows_the_global_size(self):
        """主人点的那道实测的单元版：换全局尺寸，段1 跟着走，2× 关系不破。"""
        slot = {"size_scale": [0.5, 0.5]}
        for gw, gh, want in ((832, 1216, (416, 608)), (1024, 1536, (512, 768)),
                             (1024, 1024, (512, 512))):
            got = wp.scaled_size(slot, gw, gh)
            self.assertEqual(got, want, f"全局 {gw}×{gh}")
            self.assertEqual(wp.scaled_size({}, got[0] * 2, got[1] * 2), (gw, gh))

    def test_result_is_aligned_to_eight_and_never_zero(self):
        """latent 空间对 8 有硬要求；倍率算出 0 会让炉子直接报错，得夹住下限。"""
        self.assertEqual(wp.scaled_size({"size_scale": [0.5, 0.5]}, 100, 100), (48, 48))
        self.assertEqual(wp.scaled_size({"size_scale": [0.001, 0.001]}, 64, 64), (8, 8))
        w, h = wp.scaled_size({"size_scale": [0.3, 0.7]}, 1000, 1000)
        self.assertEqual((w % 8, h % 8), (0, 0))


class StageValueTests(unittest.TestCase):
    def test_stage_params_win_over_top_level(self):
        req = req_of(steps=30, stage_params={"stage2": {"steps": 12}})
        self.assertEqual(runner.stage_value(req, "stage1", "steps"), 30)
        self.assertEqual(runner.stage_value(req, "stage2", "steps"), 12)

    def test_missing_everywhere_is_not_the_same_as_none(self):
        """「界面没发」与「发了个 None」必须分得开，否则回执会把没管的项判成 ✗。"""
        self.assertIs(runner.stage_value({}, "stage1", "denoise"), runner._MISSING)
        self.assertIsNone(runner.stage_value({"denoise": None}, "stage1", "denoise"))

    def test_malformed_stage_params_fall_back_to_top_level(self):
        """请求体是外部输入：stage_params 形状不对就当没发过，退回全局项，不拿 500 糊主人脸。"""
        for bad in (None, "x", 3, {"stage1": "x"}, {"stage1": 3}, {"stage9": {"steps": 1}}):
            self.assertEqual(runner.stage_value({"steps": 20, "stage_params": bad},
                                                "stage1", "steps"), 20, repr(bad))
        self.assertEqual(runner.stage_value({"steps": 20}, "stage1", "steps"), 20)


class BuildAndReceiptTests(unittest.TestCase):
    def test_build_writes_both_size_slots_by_ratio(self):
        sent = runner.build_workflow(CKPT_WF, ckpt_profile(), req_of())
        self.assertEqual((sent["5"]["inputs"]["width"], sent["5"]["inputs"]["height"]), (512, 512))
        self.assertEqual((sent["9"]["inputs"]["width"], sent["9"]["inputs"]["height"]), (1024, 1024))
        self.assertEqual(sent["5"]["inputs"]["batch_size"], 1)

    def test_build_splits_per_stage_params(self):
        prof = ckpt_profile()
        req = req_of(steps=30, stage_params={"stage2": {"steps": 12}})
        sent = runner.build_workflow(CKPT_WF, prof, req)
        self.assertEqual(sent["6"]["inputs"]["steps"], 30)
        self.assertEqual(sent["7"]["inputs"]["steps"], 12)

    def test_profile_without_any_size_slot_never_reads_width(self):
        """整份 profile 一个尺寸槽都没有时，不许去 int(None) 崩掉——改前就是这个惰性口径。
        导入工具认不出 latent 源时会留空，这种 profile 真会出现。"""
        prof = ckpt_profile(stage2_size_out=False)
        del prof["stages"][0]["latent_size"]
        sent = runner.build_workflow(CKPT_WF, prof, req_of(width=None, height=None))
        self.assertEqual((sent["5"]["inputs"]["width"], sent["5"]["inputs"]["height"]), (512, 512))

    def test_receipt_is_all_ok_and_stage2_keys_are_prefixed(self):
        """回执与填工作流必须共用同一套派生规则；不一致这里就会出 ✗。"""
        prof = ckpt_profile()
        req = req_of(stage_params={"stage2": {"steps": 12}})
        sent = runner.build_workflow(CKPT_WF, prof, req)
        items = runner.build_receipt(sent, req, prof, sent)
        self.assertEqual([it["key"] for it in items if not it["ok"]], [], items)
        keys = [it["key"] for it in items]
        self.assertIn("width", keys)                 # 段1 键名逐字照改前
        self.assertIn("stage2.width", keys)          # 段2 加段号，不与段1 撞名
        self.assertNotIn("stage1.width", keys)
        # 段1 的「界面要的」＝换算后的 512，不是全局 1024
        self.assertEqual(next(it for it in items if it["key"] == "width")["requested"], 512)
        self.assertEqual(next(it for it in items if it["key"] == "stage2.width")["requested"], 1024)

    def test_receipt_without_size_is_a_bad_row_not_a_crash(self):
        """界面没发尺寸时，改前的行为是 requested=None 判 ✗，不是 TypeError；这条守住它。"""
        prof = ckpt_profile()
        req = req_of()
        del req["width"], req["height"]
        sent = runner.build_workflow(CKPT_WF, prof, req_of())
        items = runner.build_receipt(sent, req, prof, sent)
        row = next(it for it in items if it["key"] == "width")
        self.assertIsNone(row["requested"])
        self.assertFalse(row["ok"])


class FinalImageTests(unittest.TestCase):
    """D92：中间图不上结果墙、也不进主人目录（copy_finals 走的就是 job.images）。"""

    def entry(self, *nodes):
        return {"outputs": {n: {"images": [{"filename": f"{n}.png", "subfolder": ""}]}
                           for n in nodes}}

    def test_final_nodes(self):
        prof = ckpt_profile()
        prof["save_slots"] = [{"node": "8", "prefix_input": "filename_prefix", "final": True},
                              {"node": "9", "prefix_input": "filename_prefix", "final": False}]
        self.assertEqual(runner.final_nodes(prof), {"8"})

    def test_no_final_marked_means_do_not_filter(self):
        """一个 final 都没标 → None＝不筛：宁可多显示，也绝不把真成品藏起来。"""
        self.assertIsNone(runner.final_nodes({"save_slots": [{"node": "8", "final": False}]}))
        self.assertIsNone(runner.final_nodes({"save_slots": []}))
        self.assertIsNone(runner.final_nodes(None))     # 只读记录没有 profile

    def test_history_images_filters_only_when_asked(self):
        e = self.entry("8", "9")
        self.assertEqual([i["filename"] for i in runner.history_images(e, {"8"})], ["8.png"])
        self.assertEqual(len(runner.history_images(e)), 2)          # 默认全收＝改前口径
        self.assertEqual(len(runner.history_images(e, None)), 2)
        self.assertEqual(runner.history_images({"outputs": "坏形状"}, {"8"}), [])

    def test_apply_history_drops_intermediate_for_live_jobs(self):
        prof = ckpt_profile()
        prof["save_slots"] = [{"node": "8", "prefix_input": "filename_prefix", "final": True},
                              {"node": "9", "prefix_input": "filename_prefix", "final": False}]
        live = runner.Job("j1", req_of())
        live.profile = prof
        runner.apply_history(live, self.entry("8", "9"))
        self.assertEqual([i["filename"] for i in live.images], ["8.png"])

        ro = runner.Job("h_x", {})          # 只读记录：没有 profile，不该被筛
        ro.readonly = True
        runner.apply_history(ro, self.entry("8", "9"))
        self.assertEqual(len(ro.images), 2)
        self.assertIsNone(ro.receipt)       # 雷区 4：只读记录不伪造回执


class StageUiTests(unittest.TestCase):
    """B 卡 §4 的「段视图」：前端段卡照它画，节点 ID 一个字都不许出去（D83）。"""

    ALL_SIX = ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise")

    def prof6(self):
        """两段都声明全部六个参数——`ckpt_profile` 每段只有一个 steps，测不出取舍。"""
        prof = ckpt_profile()
        for st, node in zip(prof["stages"], ("6", "7")):
            st["params"] = {k: {"node": node, "input": k} for k in self.ALL_SIX}
        return prof

    def test_first_stage_never_gets_a_denoise_control(self):
        """主人裁定 2026-10-28（B 卡裁定第 6 款）：段1 是满生成，给它控件就多一行回执，
        A 阶段的 13 项基线当场破。段2+ 才有 denoise。"""
        stages = wp.stage_ui(CKPT_WF, self.prof6())["stages"]
        self.assertNotIn("denoise", stages[0]["params"])
        self.assertIn("denoise", stages[1]["params"])
        self.assertNotIn("denoise", stages[0]["defaults"])
        self.assertEqual(stages[1]["defaults"]["denoise"], 0.5)   # 节点 7 的字面值

    def test_defaults_are_the_workflow_literals(self):
        """导入时炉子导出成什么样、界面就预填什么样：任何导入件都「打开就能出图」（D96）。"""
        stages = wp.stage_ui(CKPT_WF, self.prof6())["stages"]
        self.assertEqual(stages[0]["defaults"]["steps"], 20)      # 节点 6
        self.assertEqual(stages[1]["defaults"]["steps"], 15)      # 节点 7
        self.assertEqual(stages[1]["defaults"]["seed"], 2)
        self.assertEqual(stages[0]["label"], "第一段·出图")

    def test_defaults_follow_ui_param_order_not_declaration_order(self):
        prof = self.prof6()
        prof["stages"][1]["params"] = {k: v for k, v in reversed(list(prof["stages"][1]["params"].items()))}
        self.assertEqual(wp.stage_ui(CKPT_WF, prof)["stages"][1]["params"], list(self.ALL_SIX))

    def test_params_without_a_control_are_left_alone(self):
        """导入器的词表就那六个；手写 profile 里多出来的参数不给框，照工作流原值走。"""
        prof = self.prof6()
        prof["stages"][1]["params"]["sigma"] = {"node": "7", "input": "steps"}
        view = wp.stage_ui(CKPT_WF, prof)["stages"][1]
        self.assertNotIn("sigma", view["params"])
        self.assertNotIn("sigma", view["defaults"])

    def test_broken_profile_does_not_take_bootstrap_down(self):
        """契约没过的 profile 仍会进 bootstrap（下拉上要显示「契约失败！」），
        所以段视图遇到指错的节点必须少给一个默认值，而不是 KeyError 把整个界面端掉。"""
        prof = self.prof6()
        prof["stages"][1]["params"]["steps"] = {"node": "999", "input": "steps"}
        view = wp.stage_ui(CKPT_WF, prof)["stages"][1]
        self.assertIn("steps", view["params"])
        self.assertNotIn("steps", view["defaults"])

    def test_lora_stages_is_parallel_to_lora_slots(self):
        """bootstrap 的 `lora_slots` 是个「个数」，所以覆盖范围走这条平行数组。"""
        ids = ["stage1", "stage2"]
        for pen_stages, want in ((None, ids), (["stage1"], ["stage1"]), ([], ids)):
            got = wp.stage_ui(CKPT_WF, ckpt_profile(pen_stages=pen_stages))["lora_stages"]
            self.assertEqual(got, [want], f"pen_stages={pen_stages}")
        empty = ckpt_profile()
        empty["lora_slots"] = []
        self.assertEqual(wp.stage_ui(NO_PEN_WF, empty)["lora_stages"], [])

    def test_pen_roles_drive_the_no_pen_notice(self):
        """D78：接力第二段链上没笔——段卡靠这个字段说「此段无笔」，不靠去解析 warning 文字。"""
        relay = wp.stage_ui(RELAY_WF, ckpt_profile(pen_stages=["stage1"]))["stages"]
        self.assertEqual(relay[0]["pen_roles"], ["lora1"])
        self.assertEqual(relay[1]["pen_roles"], [])
        both = wp.stage_ui(CKPT_WF, ckpt_profile())["stages"]
        self.assertEqual([s["pen_roles"] for s in both], [["lora1"], ["lora1"]])

    def test_no_node_ids_in_the_view(self):
        view = wp.stage_ui(CKPT_WF, self.prof6())
        self.assertEqual(sorted(view), ["lora_stages", "stages"])
        for st in view["stages"]:
            self.assertEqual(sorted(st), ["defaults", "id", "label", "params", "pen_roles"])
        self.assertNotIn("node", json.dumps(view, ensure_ascii=False))


class OnDiskProfileViewTests(unittest.TestCase):
    """盘上每一份 profile 都得画得出段卡；§5 把 _staging 挪进 workflows/ 后这组自动扩到八份。"""

    def test_every_profile_on_disk_renders(self):
        files = sorted(WORKFLOWS.glob("*.json"))
        self.assertGreaterEqual(len(files), 3, "workflows/ 里至少该有内置三条")
        for f in files:
            wf, prof = wp.load_pair(f)
            view = wp.stage_ui(wf, prof)
            with self.subTest(f.name):
                self.assertEqual(len(view["stages"]), len(prof["stages"]))
                self.assertEqual(len(view["lora_stages"]), len(prof["lora_slots"]))
                self.assertNotIn("denoise", view["stages"][0]["params"])
                ids = {st["id"] for st in view["stages"]}
                for st in view["stages"]:
                    self.assertTrue(set(st["params"]) <= set(wp.UI_STAGE_PARAMS))
                    self.assertTrue(set(st["defaults"]) <= set(st["params"]))
                    self.assertTrue(st["defaults"], "真 profile 的每个参数都该有预填值")
                for cov in view["lora_stages"]:
                    self.assertTrue(set(cov) <= ids, "笔覆盖的段必须是真存在的段")


class BootstrapStageViewTests(unittest.TestCase):
    """前端只认 bootstrap 里的键名，所以这条得盯着端点本身，不能只测 stage_ui。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app import main
        cls.http = TestClient(main.app, base_url="http://127.0.0.1:8788",
                              headers={"Authorization": "Bearer " + main.TOKEN})

    def test_bootstrap_carries_stages_and_lora_stages(self):
        body = self.http.get("/api/bootstrap").json()
        self.assertTrue(body["profiles"])
        for p in body["profiles"]:
            with self.subTest(p["id"]):
                self.assertEqual(len(p["stages"]), p["capabilities"]["stages"])
                self.assertEqual(len(p["lora_stages"]), p["lora_slots"])
                self.assertNotIn("denoise", p["stages"][0]["params"])
                self.assertNotIn("node", json.dumps(p["stages"], ensure_ascii=False))
                self.assertNotIn("node", json.dumps(p["lora_stages"], ensure_ascii=False))


class LegacyAndPromptLinkTests(unittest.TestCase):
    """老 profile（没有 `stages` 键）与提示词源连线：都得是人话，不许崩、不许凭空捏能力位。"""

    def test_missing_positive_source_input_is_a_plain_error(self):
        prof = ckpt_profile()
        prof["stages"][0]["prompt"]["positive_source"][0]["input"] = "nope"
        errors, _ = wp.validate_full(CKPT_WF, prof)
        self.assertTrue(any("提示词源节点" in e and "nope" in e for e in errors), errors)

    def test_missing_negative_source_node_is_a_plain_error(self):
        prof = ckpt_profile()
        prof["stages"][1]["prompt"]["negative_source"][0]["node"] = "999"
        errors, _ = wp.validate_full(CKPT_WF, prof)
        self.assertTrue(any("提示词源节点 999" in e for e in errors), errors)

    def test_profile_without_stages_key_is_tolerated(self):
        """A 阶段之前的 profile 没有 `stages`：契约／能力位／段视图三处都该当「零段」而不是 KeyError。"""
        prof = ckpt_profile()
        del prof["stages"]
        errors, warnings = wp.validate_full(CKPT_WF, prof)
        self.assertEqual((errors, warnings), ([], []))
        self.assertEqual(wp.derive_capabilities(prof)["stages"], 0)
        self.assertEqual(wp.stage_ui(CKPT_WF, prof)["stages"], [])

    def test_capabilities_are_derived_not_declared(self):
        """能力位一律由结构推导：profile 里自己写一份 `capabilities` 不算数。"""
        prof = ckpt_profile()
        prof["capabilities"] = {"stages": 99, "hires": False, "lora_max": 7,
                                "reference_images": 3, "negative_prompt": False}
        caps = wp.derive_capabilities(prof)
        self.assertEqual(caps["stages"], 2)
        self.assertTrue(caps["hires"])
        self.assertEqual(caps["lora_max"], 1)
        self.assertEqual(caps["reference_images"], 0)


if __name__ == "__main__":
    unittest.main()
