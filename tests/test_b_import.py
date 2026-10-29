"""B 卡 §6 识别器层单测（`tools/import_workflow.py`）。

用合成的 API 格式小图，故意造内置三条与五份考题里都没有的形状——参考图 LoadImage、
KSamplerAdvanced 缺 cfg、双 CLIP、model 链成环、正词歧义——否则这些分支永远走不到，
等于没验（§1 就有这么一条：LoadImage 的消费者扫描写错了参数序，五份考题都没有参考图，
所以一直没崩，直到这里补上测试才暴露）。

铁律两条，逐条对着测：
1. 认不准的**留空**＋报告里点名，绝不瞎猜接线；
2. 报告里每个判断都要给依据（节点类型／连线）。
节点号只出现在测试与产物 JSON 里，不进 app/static（D83/D96 由 test_profiles 守）。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from app import workflow_profile as wp
from tools.import_workflow import Reject, import_workflow, main as cli_main

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"

# 五份考题：源文件 → 已挪进 workflows/ 的产物（B 卡 §5）
EXAMS = [("wf_pattern_hires.json", "anima_hires"),
         ("wf.json", "anima_turbo"),
         ("wf_chosen_irises_yfs.json", "chosen_irises"),
         ("wf_two_lora_yfs.json", "anima_aesthetic_dual"),
         ("wf_three_lora_yfs.json", "anima_aesthetic_tri")]


def node(cls, **inputs):
    return {"class_type": cls, "inputs": inputs}


def base_graph():
    """单段直出的最小可用图：底模／TE／VAE 分开加载，正负词各一个编码节点。"""
    return {
        "1": node("UNETLoader", unet_name="base.safetensors"),
        "2": node("CLIPLoader", clip_name="te.safetensors", type="anima"),
        "3": node("VAELoader", vae_name="vae.safetensors"),
        "4": node("CLIPTextEncode", text="pos", clip=["2", 0]),
        "5": node("CLIPTextEncode", text="neg", clip=["2", 0]),
        "6": node("EmptyLatentImage", width=832, height=1216, batch_size=1),
    }


SAMPLER_IN = dict(model=["1", 0], positive=["4", 0], negative=["5", 0], latent_image=["6", 0],
                  seed=1, steps=30, cfg=4.0, sampler_name="euler", scheduler="simple",
                  denoise=1.0)


def tail(graph, sampler="7", decode="8", save="9", **save_kw):
    """接上采样器→解码→存图；save_kw 可覆盖 SaveImage 的输入。"""
    ins = {"images": [decode, 0], "filename_prefix": "out"}
    ins.update(save_kw)
    graph[decode] = node("VAEDecode", samples=[sampler, 0], vae=["3", 0])
    graph[save] = node("SaveImage", **ins)
    return graph


def direct(pens=0, sampler="KSampler", loader="UNETLoader", latent="EmptyLatentImage",
           **sampler_over):
    """单段直出图。pens＝链式笔数（底模→采样器方向），loader/latent 换形状用。"""
    g = base_graph()
    if loader == "CheckpointLoaderSimple":
        g["1"] = node(loader, ckpt_name="base.safetensors")
    else:
        g["1"] = node(loader, unet_name="base.safetensors")
    lat = {"width": 832, "height": 1216, "batch_size": 1}
    if latent == "EmptySD3LatentImage":
        lat.pop("batch_size")       # 这一型没有 batch_size：张数没法接管，得留空报人话
    g["6"] = node(latent, **lat)
    model = ["1", 0]
    for i in range(pens):
        nid = str(20 + i)
        g[nid] = node("LoraLoaderModelOnly", model=model,
                      lora_name=f"pen{i}.safetensors", strength_model=0.7)
        model = [nid, 0]
    ins = dict(SAMPLER_IN, model=model, **sampler_over)
    if sampler == "KSamplerAdvanced":
        ins.pop("cfg")              # 这一型没有 cfg
        ins["noise_mask"] = False
    if sampler == "SDTurbo":
        ins.pop("scheduler")        # 这一型没有 scheduler
    g["7"] = node(sampler, **ins)
    return tail(g)


def hires_graph(scale_cls="ImageScale", shared_pen=False):
    """两段放大图（考题 2 的形状）：段1 出 512、ImageScale 到 1024、段2 精修。
    scale_cls 换成倍率型放大器（ImageScaleBy）就变成「画舫契约表达不了」的形状。
    shared_pen＝True 时一支笔同时喂两段（D78 的并槽形状）。"""
    g = base_graph()
    g["6"] = node("EmptyLatentImage", width=512, height=512, batch_size=1)
    g["7"] = node("KSampler", **dict(SAMPLER_IN, seed=1, steps=30, cfg=4.0, denoise=1.0))
    g["11"] = node("VAEDecode", samples=["7", 0], vae=["3", 0])
    g["8"] = node("SaveImage", images=["11", 0], filename_prefix="mid")     # 中间图
    if scale_cls == "ImageScale":
        g["12"] = node("ImageScale", image=["11", 0], width=1024, height=1024,
                       upscale_method="lanczos", crop="disabled")
    else:
        g["12"] = node(scale_cls, image=["11", 0], scale_factor=2.0)
    g["13"] = node("VAEEncode", pixels=["12", 0], vae=["3", 0])
    model = ["1", 0]
    if shared_pen:
        g["20"] = node("LoraLoaderModelOnly", model=["1", 0],
                       lora_name="pen.safetensors", strength_model=0.7)
        model = ["20", 0]
        g["7"] = node("KSampler", **dict(g["7"]["inputs"], model=model))
    g["14"] = node("KSampler", model=model, positive=["4", 0], negative=["5", 0],
                   latent_image=["13", 0], seed=2, steps=15, cfg=3.0,
                   sampler_name="euler", scheduler="simple", denoise=0.45)
    g["15"] = node("VAEDecode", samples=["14", 0], vae=["3", 0])
    g["16"] = node("SaveImage", images=["15", 0], filename_prefix="final")   # 成品
    return g


def do_import(graph, name="wf.json", **kw):
    """把图写成临时文件再导入（导入器吃的是路径，不是 dict——它要记源文件名与大小）。"""
    kw.setdefault("family", "anima")
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False)
        return import_workflow(p, **kw)


def import_text(text, name="wf.json"):
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return import_workflow(p, family="anima")


def manuals(res):
    return [r["item"] for r in res["report"].of("manual")]


def manual_of(res, keyword):
    return [r for r in res["report"].of("manual") if keyword in r["item"]]


class ImportCase(unittest.TestCase):
    def prof(self, res):
        return res["data"]["profile"]

    def stage(self, res, i=0):
        return self.prof(res)["stages"][i]


# ------------------------------------------------------------------ 入料壳与拒收
class ShellAndRejectTests(ImportCase):
    def test_bare_node_table_is_accepted(self):
        g = direct()
        res = do_import(g)
        self.assertEqual(res["data"]["workflow"], g)      # 节点表逐字照抄，一个值都不改
        self.assertIn("裸节点表", res["meta"]["shell"])

    def test_prompt_shell_is_unwrapped(self):
        g = direct()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "wf.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"prompt": g, "client_id": "abc"}, f)
            res = import_workflow(p, family="anima")
        self.assertEqual(res["data"]["workflow"], g)
        self.assertIn("API 外壳", res["meta"]["shell"])

    def test_ui_export_is_rejected_in_plain_language(self):
        """界面导出（Save）不是 API 格式：要说清怎么重导，不能只说「格式不对」。"""
        with self.assertRaises(Reject) as cm:
            import_text(json.dumps({"nodes": [], "links": [], "revision": 1}))
        msg = str(cm.exception)
        self.assertIn("不是 API 格式", msg)
        self.assertIn("Save (API Format)", msg)

    def test_huafang_pair_is_rejected(self):
        with self.assertRaises(Reject) as cm:
            import_text(json.dumps({"workflow": {}, "profile": {}}))
        self.assertIn("已经是画舫的", str(cm.exception))

    def test_non_object_top_level_is_rejected(self):
        with self.assertRaises(Reject):
            import_text(json.dumps([1, 2, 3]))

    def test_bad_json_is_rejected(self):
        with self.assertRaises(Reject) as cm:
            import_text("{not json")
        self.assertIn("不是合法 JSON", str(cm.exception))

    def test_half_saved_node_is_rejected(self):
        with self.assertRaises(Reject) as cm:
            import_text(json.dumps({"1": {"inputs": {}}}))
        self.assertIn("不是 API 格式的形状", str(cm.exception))

    def test_no_sampler_is_rejected(self):
        g = base_graph()
        g["9"] = node("SaveImage", images=["6", 0], filename_prefix="out")
        with self.assertRaises(Reject) as cm:
            do_import(g)
        self.assertIn("找不到采样器", str(cm.exception))

    def test_missing_file_is_rejected_not_traceback(self):
        with self.assertRaises(Reject) as cm:
            import_workflow(Path(tempfile.gettempdir()) / "画舫没有这个文件.json")
        self.assertIn("读不了这个文件", str(cm.exception))

    def test_reject_message_has_no_stack_or_node_id_leak(self):
        """人话里不该出现 Traceback／文件路径以外的内部细节（D96）。"""
        with self.assertRaises(Reject) as cm:
            import_text(json.dumps({"nodes": []}))
        self.assertNotIn("Traceback", str(cm.exception))


# ------------------------------------------------------------------ 节点形状
class NodeShapeTests(ImportCase):
    def test_three_sampler_types_each_make_one_stage(self):
        for cls in ("KSampler", "KSamplerAdvanced", "SDTurbo"):
            res = do_import(direct(sampler=cls))
            self.assertEqual(res["meta"]["stages"], 1, cls)
            self.assertEqual(self.prof(res)["stages"][0]["label"], "直出", cls)

    def test_sampler_inputs_beyond_the_vocab_are_kept_not_taken_over(self):
        """KSamplerAdvanced 的 noise_mask：登记进 keep_workflow_value，提交时原样带走。"""
        res = do_import(direct(sampler="KSamplerAdvanced"))
        self.assertIn("stage1.noise_mask", self.prof(res)["params"]["keep_workflow_value"])
        self.assertNotIn("noise_mask", self.stage(res)["params"])

    def test_missing_sampler_param_is_left_blank_with_a_reason(self):
        """这一型没有 cfg／scheduler：不替它编一个值，报告点名。"""
        res = do_import(direct(sampler="KSamplerAdvanced"))
        self.assertNotIn("cfg", self.stage(res)["params"])
        self.assertNotIn("cfg", self.prof(res)["defaults"])
        self.assertTrue(manual_of(res, "缺参数"), manuals(res))
        self.assertTrue(manual_of(res, "defaults 缺项"), manuals(res))

        res = do_import(direct(sampler="SDTurbo"))
        self.assertNotIn("scheduler", self.stage(res)["params"])
        self.assertTrue(manual_of(res, "缺参数"), manuals(res))

    def test_unet_loader_maps_to_unet_role(self):
        res = do_import(direct())
        slots = {s["role"]: s for s in self.prof(res)["model_slots"]}
        self.assertEqual(sorted(slots), ["clip", "unet", "vae"])
        self.assertEqual((slots["unet"]["node"], slots["unet"]["input"]), ("1", "unet_name"))
        self.assertEqual((slots["clip"]["input"], slots["vae"]["input"]),
                         ("clip_name", "vae_name"))

    def test_checkpoint_loader_is_accepted_but_flags_the_directory_mismatch(self):
        """CheckpointLoaderSimple 的文件在 checkpoints，不在 MODEL_DIRS['unet']：
        role 仍写 unet 复用下拉，但错配必须在报告里点名，不装作没事。"""
        res = do_import(direct(loader="CheckpointLoaderSimple"))
        slots = {s["role"]: s for s in self.prof(res)["model_slots"]}
        self.assertEqual(slots["unet"]["input"], "ckpt_name")
        self.assertTrue(manual_of(res, "底模目录错配"), manuals(res))

    def test_dual_clip_loader_is_not_guessed(self):
        """双 CLIP（裁定第 3 款的补考结构之一）：一个角色只有一个槽，不敢替主人挑。"""
        g = direct()
        g["2"] = node("DualCLIPLoader", clip_name1="a.safetensors",
                      clip_name2="b.safetensors", type="anima")
        res = do_import(g)
        roles = [s["role"] for s in self.prof(res)["model_slots"]]
        self.assertNotIn("clip", roles)
        self.assertTrue(manual_of(res, "model_slots·clip"), manuals(res))

    def test_latent_source_gives_size_and_batch(self):
        res = do_import(direct())
        lz = self.stage(res)["latent_size"]
        self.assertEqual((lz["node"], lz["width_input"], lz["height_input"], lz["batch_input"]),
                         ("6", "width", "height", "batch_size"))
        self.assertNotIn("size_scale", lz)          # 单段＝全局尺寸，倍率 1.0 就不写键
        self.assertEqual((self.prof(res)["defaults"]["width"],
                          self.prof(res)["defaults"]["height"]), (832, 1216))

    def test_latent_without_batch_size_leaves_the_slot_blank(self):
        res = do_import(direct(latent="EmptySD3LatentImage"))
        self.assertIsNone(self.stage(res)["latent_size"]["batch_input"])
        self.assertTrue(manual_of(res, "张数槽"), manuals(res))

    def test_save_image_on_the_last_stage_is_final(self):
        res = do_import(direct())
        self.assertEqual(self.prof(res)["save_slots"],
                         [{"node": "9", "prefix_input": "filename_prefix", "final": True}])

    def test_save_image_without_prefix_is_not_registered(self):
        g = direct()
        g["9"] = node("SaveImage", images=["8", 0])
        res = do_import(g)
        self.assertEqual(self.prof(res)["save_slots"], [])
        self.assertTrue(manual_of(res, "save_slots"), manuals(res))

    def test_save_image_with_literal_images_counts_as_intermediate(self):
        """images 不是连线＝判不出成品，宁可当中间图（不上结果墙），并叫人来看。"""
        g = direct()
        g["9"]["inputs"]["images"] = "not-a-link"
        res = do_import(g)
        self.assertEqual(self.prof(res)["save_slots"],
                         [{"node": "9", "prefix_input": "filename_prefix", "final": False}])
        self.assertTrue(manual_of(res, "save_slots"), manuals(res))

    def test_reference_image_is_registered_but_not_exposed(self):
        """卡 §1：LoadImage 只登记 image_slots、不暴露界面。used_by 要说得出被谁消费。"""
        g = direct()
        g["41"] = node("LoadImage", image="ref.png")
        g["42"] = node("Foo", image=["41", 0])
        res = do_import(g)
        slots = self.prof(res)["image_slots"]
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["role"], "ref1")
        self.assertEqual((slots[0]["node"], slots[0]["input"]), ("41", "image"))
        self.assertFalse(slots[0]["exposed"])
        self.assertEqual(slots[0]["used_by"], ["42"])
        self.assertFalse(manual_of(res, "没人用"), manuals(res))
        self.assertEqual(wp.derive_capabilities(self.prof(res))["reference_images"], 1)

    def test_unused_reference_image_is_flagged(self):
        g = direct()
        g["41"] = node("LoadImage", image="ref.png")
        res = do_import(g)
        self.assertEqual(self.prof(res)["image_slots"][0]["used_by"], [])
        self.assertTrue(manual_of(res, "没人用"), manuals(res))

    def test_load_image_without_image_input_is_left_for_a_human(self):
        g = direct()
        g["41"] = node("LoadImage", foo="bar")
        res = do_import(g)
        self.assertEqual(self.prof(res)["image_slots"], [])
        self.assertTrue(manual_of(res, "image_slots"), manuals(res))


# ------------------------------------------------------------------ 链式笔
class ChainedLoraTests(ImportCase):
    def test_three_pens_keep_base_to_sampler_order(self):
        """链序＝从底模往采样器数，与内置三条同口径（往回走的结果要倒过来）。"""
        res = do_import(direct(pens=3))
        slots = self.prof(res)["lora_slots"]
        self.assertEqual([s["role"] for s in slots], ["lora1", "lora2", "lora3"])
        self.assertEqual([s["node"] for s in slots], ["20", "21", "22"])
        self.assertEqual(self.stage(res)["lora_roles"], ["lora1", "lora2", "lora3"])
        for s in slots:
            self.assertEqual((s["name_input"], s["strength_input"]),
                             ("lora_name", "strength_model"))
            self.assertEqual(s["stages"], ["stage1"])

    def test_penless_workflow_has_no_lora_slot(self):
        res = do_import(direct(pens=0))
        self.assertEqual(self.prof(res)["lora_slots"], [])
        self.assertEqual(wp.derive_capabilities(self.prof(res))["lora_max"], 0)

    def test_lora_loader_keeps_its_clip_strength_input(self):
        g = direct()
        g["20"] = node("LoraLoader", model=["1", 0], clip=["2", 0],
                       lora_name="pen.safetensors", strength_model=0.7, strength_clip=0.7)
        g["7"] = node("KSampler", **dict(SAMPLER_IN, model=["20", 0]))
        res = do_import(g)
        self.assertEqual(self.prof(res)["lora_slots"][0]["clip_strength_input"], "strength_clip")

    def test_lora_loader_without_clip_strength_is_left_null(self):
        g = direct()
        g["20"] = node("LoraLoader", model=["1", 0], clip=["2", 0],
                       lora_name="pen.safetensors", strength_model=0.7)
        g["7"] = node("KSampler", **dict(SAMPLER_IN, model=["20", 0]))
        res = do_import(g)
        self.assertIsNone(self.prof(res)["lora_slots"][0]["clip_strength_input"])
        self.assertTrue(manual_of(res, "CLIP 强度"), manuals(res))

    def test_one_pen_feeding_two_stages_is_merged_into_one_slot(self):
        """D78：同一支笔覆盖多段＝并成一条，stages 写全；不是复制成两支笔。"""
        res = do_import(hires_graph(shared_pen=True))
        slots = self.prof(res)["lora_slots"]
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0]["stages"], ["stage1", "stage2"])
        self.assertEqual([st["lora_roles"] for st in self.prof(res)["stages"]],
                         [["lora1"], ["lora1"]])
        self.assertFalse(manual_of(res, "只覆盖部分段"), manuals(res))


# ------------------------------------------------------------------ 两段与尺寸倍率
class TwoStageTests(ImportCase):
    def test_stage_order_follows_the_sampler_chain(self):
        """节点号顺序不等于段序（14 在 8 之后）：段序只看谁的 latent 来自谁。"""
        res = do_import(hires_graph())
        stages = self.prof(res)["stages"]
        self.assertEqual([s["id"] for s in stages], ["stage1", "stage2"])
        self.assertEqual([s["links"]["model"]["node"] for s in stages], ["7", "14"])
        self.assertEqual([s["label"] for s in stages], ["第一段·出图", "第二段·精修放大"])

    def test_sizes_are_ratios_not_literals(self):
        """裁定第 1 款：段1＝全局×0.5 写成倍率，段2 成品＝全局×1.0（倍率 1 就不写键）。"""
        res = do_import(hires_graph())
        s1, s2 = self.prof(res)["stages"]
        self.assertEqual(s1["latent_size"]["size_scale"], [0.5, 0.5])
        self.assertNotIn("size_scale", s2["size_out"])
        self.assertEqual(res["meta"]["global_wh"], (1024, 1024))
        self.assertEqual((self.prof(res)["defaults"]["width"],
                          self.prof(res)["defaults"]["height"]), (1024, 1024))

    def test_ratio_survives_a_different_global_size(self):
        """倍率不是冻在 512/1024 那组字面值上：改全局尺寸，2× 关系仍成立。"""
        g = hires_graph()
        g["6"]["inputs"].update(width=768, height=1152)
        g["12"]["inputs"].update(width=1536, height=2304)
        res = do_import(g)
        s1, s2 = self.prof(res)["stages"]
        self.assertEqual(s1["latent_size"]["size_scale"], [0.5, 0.5])
        self.assertEqual(wp.scaled_size(s1["latent_size"], 1536, 2304), (768, 1152))
        self.assertEqual(wp.scaled_size(s2["size_out"], 1536, 2304), (1536, 2304))

    def test_only_the_last_stage_save_is_final(self):
        """D92：中间图不进结果墙，所以 final 标记必须在导入时就分清。"""
        res = do_import(hires_graph())
        finals = {s["node"]: s["final"] for s in self.prof(res)["save_slots"]}
        self.assertEqual(finals, {"8": False, "16": True})

    def test_imported_two_stage_profile_passes_the_contract(self):
        res = do_import(hires_graph())
        prof = self.prof(res)
        self.assertEqual(wp.validate(res["data"]["workflow"], prof), [])
        caps = wp.derive_capabilities(prof)
        self.assertEqual((caps["stages"], caps["hires"], caps["multi_model"]), (2, True, False))

    def test_second_stage_has_no_latent_of_its_own(self):
        """段2 的 latent 来自 VAEEncode（不是空 latent）：不许跨着采样器去抢段1 的尺寸节点。"""
        res = do_import(hires_graph())
        self.assertIsNone(self.stage(res, 1).get("latent_size"))
        self.assertEqual(self.stage(res, 1)["size_out"]["node"], "12")

    def test_ratio_upscaler_is_flagged_and_no_scale_is_invented(self):
        """ImageScaleBy 用倍率：契约表达不了 → 标 hires 让界面说得出「是放大段」，
        尺寸规则留空给人修，**绝不塞个假倍率进去**（塞了 runner 会照着写错尺寸）。"""
        res = do_import(hires_graph(scale_cls="ImageScaleBy"))
        s1, s2 = self.prof(res)["stages"]
        self.assertTrue(s2.get("hires"))
        self.assertIsNone(s2.get("size_out"))
        self.assertNotIn("size_scale", s1["latent_size"])
        self.assertTrue(manual_of(res, "倍率型放大"), manuals(res))
        self.assertTrue(manual_of(res, "全局尺寸"), manuals(res))
        self.assertNotIn("width", self.prof(res)["defaults"])


# ------------------------------------------------------------------ 铁律：留空不瞎猜
class BlankNotGuessTests(ImportCase):
    def test_ambiguous_positive_is_left_blank(self):
        """正词口往回撞到两个 CLIPTextEncode：不知道界面该往哪个写 → 全留空。"""
        g = direct()
        g["40"] = node("CLIPTextEncode", text="pos2", clip=["2", 0])
        g["30"] = node("Foo", a=["4", 0], b=["40", 0])
        g["7"] = node("KSampler", **dict(SAMPLER_IN, positive=["30", 0]))
        res = do_import(g)
        self.assertEqual(self.stage(res)["prompt"]["positive_source"], [])
        self.assertEqual(self.stage(res)["prompt"]["negative_source"],
                         [{"node": "5", "input": "text"}])      # 负词没歧义，照认
        hit = manual_of(res, "正词")
        self.assertTrue(hit, manuals(res))
        self.assertIn("2 个", hit[0]["why"])

    def test_positive_with_no_encode_behind_it_is_left_blank(self):
        g = direct()
        g["30"] = node("Foo", a=1)
        g["7"] = node("KSampler", **dict(SAMPLER_IN, positive=["30", 0]))
        res = do_import(g)
        self.assertEqual(self.stage(res)["prompt"]["positive_source"], [])
        self.assertTrue(manual_of(res, "正词"), manuals(res))

    def test_unknown_node_on_the_model_chain_stops_the_walk(self):
        """链上有认不出的节点：不往下接、不登记 unet 槽，报告说清是哪个节点。"""
        g = direct()
        g["31"] = node("Foo", model=["1", 0])
        g["7"] = node("KSampler", **dict(SAMPLER_IN, model=["31", 0]))
        res = do_import(g)
        self.assertNotIn("unet", [s["role"] for s in self.prof(res)["model_slots"]])
        hit = manual_of(res, "底模链")
        self.assertTrue(hit, manuals(res))
        self.assertIn("认不出的节点 31", hit[0]["why"])

    def test_model_chain_loop_does_not_hang_or_crash(self):
        g = direct()
        g["32"] = node("LoraLoaderModelOnly", model=["32", 0],
                       lora_name="p.safetensors", strength_model=0.7)
        g["7"] = node("KSampler", **dict(SAMPLER_IN, model=["32", 0]))
        res = do_import(g)
        self.assertEqual(self.prof(res)["lora_slots"], [])
        self.assertTrue(manual_of(res, "底模链"), manuals(res))

    def test_non_integer_size_leaves_defaults_blank(self):
        """不可信 JSON 里尺寸写成 "832px"：算不出倍率就别猜，defaults 留空＋报人话。"""
        g = direct()
        g["6"]["inputs"]["width"] = "832px"
        res = do_import(g)
        self.assertIsNone(res["meta"]["global_wh"])
        self.assertNotIn("width", self.prof(res)["defaults"])
        self.assertTrue(manual_of(res, "全局尺寸"), manuals(res))

    def test_every_report_row_gives_a_reason(self):
        """铁律：报告里每个判断（自动的与留空的）都要给依据，否则 review 无从核。"""
        for g in (direct(pens=2), hires_graph(), hires_graph(scale_cls="ImageScaleBy")):
            res = do_import(g)
            for row in res["report"].rows:
                self.assertIn(row["kind"], ("auto", "manual"))
                self.assertTrue(row["item"].strip(), row)
                self.assertTrue(str(row["where"]).strip(), row)
                self.assertGreater(len(row["why"]), 10, row)

    def test_no_family_is_a_manual_item_not_a_guess(self):
        res = do_import(direct(), family=None)
        self.assertEqual(self.prof(res)["family"], "")
        self.assertTrue(manual_of(res, "family"), manuals(res))

    def test_workflow_block_is_copied_verbatim(self):
        """导入器不许改工作流里的任何值（D73：只解析、不执行、不改写）。"""
        g = direct()
        res = do_import(g)
        self.assertEqual(res["data"]["workflow"], g)
        self.assertIsNot(res["data"]["workflow"], g)      # 但要是能独立落盘的副本


class UntrustedInputTests(ImportCase):
    def test_code_looking_literals_are_data_not_instructions(self):
        """D73：JSON 里塞一段像代码的字符串，只会被逐字抄进产物，不会被执行。"""
        g = direct()
        payload = "__import__('os').system('echo pwned')"
        g["9"]["inputs"]["filename_prefix"] = payload
        g["4"]["inputs"]["text"] = payload
        res = do_import(g)
        self.assertEqual(res["data"]["workflow"]["9"]["inputs"]["filename_prefix"], payload)
        self.assertEqual(self.stage(res)["prompt"]["positive_source"],
                         [{"node": "4", "input": "text"}])
        json.dumps(res["data"], ensure_ascii=False)       # 产物仍是可落盘的纯数据

    def test_huge_and_weird_values_do_not_crash(self):
        """不可信 JSON 里的离谱值：不许崩、不许偷偷改成「合理值」，原样抄进去＋报告说清。
        （负数尺寸会在炉子那边报错，由既有错误翻译报人话；导入器不替它兜。）"""
        g = direct()
        g["6"]["inputs"].update(width=10 ** 12, height=-5, batch_size=None)
        res = do_import(g)
        self.assertEqual(res["meta"]["global_wh"], (10 ** 12, -5))
        self.assertEqual((self.prof(res)["defaults"]["width"],
                          self.prof(res)["defaults"]["height"]), (10 ** 12, -5))
        self.assertEqual(res["data"]["workflow"]["6"]["inputs"]["batch_size"], None)


# ------------------------------------------------------------------ §5 门槛：五份考题
class ExamReproduceTests(ImportCase):
    """§5 量化门槛：盘上四件 wf_*.json（＋hires 料）100% 自动映射、人工修正 ≤2。

    这里比的是「重导一遍能不能逐字复现已挪进 workflows/ 的产物」——
    复现＝当初没有手改结构；差异只允许出现在 --id/--label/--family 这三样身份字段上。
    """

    def exam(self, src_name, pid):
        disk = json.loads((WORKFLOWS / f"{pid}.json").read_text(encoding="utf-8"))
        prof = disk["profile"]
        # 公开工作流本身提供 API 节点表；验证重导能复现 profile，不再读取私人源文件。
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / src_name
            src.write_text(json.dumps(disk["workflow"]), encoding="utf-8")
            result = import_workflow(src, prof["workflow_id"], prof["label"], prof["family"])
        return result, disk

    def test_each_exam_reimports_to_the_promoted_pair(self):
        for src_name, pid in EXAMS:
            res, disk = self.exam(src_name, pid)
            self.assertEqual(res["data"], disk, f"{pid} 重导与盘上产物不一致")

    def test_each_exam_needs_zero_manual_fixes(self):
        for src_name, pid in EXAMS:
            res, _ = self.exam(src_name, pid)
            self.assertEqual(res["meta"]["manual"], 0, f"{pid}：{manuals(res)}")
            self.assertLessEqual(res["meta"]["manual"], 2)      # 卡 §5 门槛

    def test_exam_shapes(self):
        want = {"anima_hires": (2, 1), "anima_turbo": (1, 0), "chosen_irises": (1, 1),
                "anima_aesthetic_dual": (1, 2), "anima_aesthetic_tri": (1, 3)}
        for src_name, pid in EXAMS:
            res, _ = self.exam(src_name, pid)
            self.assertEqual((res["meta"]["stages"], res["meta"]["lora_slots"]), want[pid], pid)


class CliTests(ImportCase):
    """主人日常只有一个动作：把 JSON 丢给工具。CLI 的落盘／dry-run 行为也得测。"""

    def test_cli_writes_pair_and_report_into_the_given_paths(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "my-wf (v2).json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(direct(pens=1), f)
            out = os.path.join(d, "sub", "pair.json")
            rpt = os.path.join(d, "sub", "report.md")
            code = cli_main([src, "--id", "my_wf", "--label", "我的工作流",
                             "--family", "anima", "--out", out, "--report", rpt])
            self.assertEqual(code, 0)
            data = json.loads(Path(out).read_text(encoding="utf-8"))
            self.assertEqual(sorted(data), ["profile", "workflow"])
            self.assertEqual(data["profile"]["workflow_id"], "my_wf")
            self.assertEqual(data["profile"]["label"], "我的工作流")
            text = Path(rpt).read_text(encoding="utf-8")
            self.assertIn("映射报告", text)
            self.assertIn("直出", text)                 # 段表把段标签印出来，§3 照它写回执
            self.assertIn("人工修正处数：**0**", text)

    def test_cli_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "wf.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(direct(), f)
            out = os.path.join(d, "pair.json")
            self.assertEqual(cli_main([src, "--out", out, "--report",
                                       os.path.join(d, "r.md"), "--dry-run"]), 0)
            self.assertFalse(os.path.exists(out))

    def test_cli_reject_returns_2_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "ui.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump({"nodes": [], "links": []}, f)
            out = os.path.join(d, "pair.json")
            self.assertEqual(cli_main([src, "--out", out, "--report",
                                       os.path.join(d, "r.md")]), 2)
            self.assertFalse(os.path.exists(out))

    def test_id_from_filename_keeps_chinese_and_drops_punctuation(self):
        """workflow_id 会变成文件名与输出目录前缀：主人的中文文件名要保住，
        空格／括号这类捣乱的换成下划线；全是标点就退回 imported（不许出空 id）。"""
        want = {"My Workflow (v2)": "my_workflow__v2",
                "画舫 出图 (最终版)": "画舫_出图__最终版",
                "---": "imported"}
        for stem, wid in want.items():
            with tempfile.TemporaryDirectory() as d:
                src = os.path.join(d, stem + ".json")
                with open(src, "w", encoding="utf-8") as f:
                    json.dump(direct(), f)
                got = import_workflow(src)["meta"]["id"]
            self.assertEqual(got, wid, stem)
            self.assertNotIn(" ", got)


if __name__ == "__main__":
    unittest.main()
