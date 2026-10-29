import json
import os
import re
import unittest
from pathlib import Path

from app import workflow_profile as wp
from app import settings as ST

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"
# 真模型不随公开副本分发；显式指定环境变量才做盘上集成检查。
MODELS_ROOT = Path(os.environ.get("HUAFANG_TEST_MODELS_DIR", str(ST.DATA / "optional-models")))
MODEL_DIRS = {"unet": "diffusion_models", "clip": "text_encoders", "vae": "vae", "lora": "loras"}


def real_exist(role, name):
    sub = MODEL_DIRS.get(role)
    return (MODELS_ROOT / sub / name).exists() if sub else True


def chain_forward(nodes, sampler_node, model_input):
    """从采样器往回走到 UNETLoader，返回正序（底模 → 采样器）的 LoRA 节点号。"""
    back, cur, first = [], sampler_node, True
    while cur is not None and cur not in back:
        back.append(cur)
        node = nodes.get(cur)
        if node is None or node.get("class_type") == "UNETLoader":
            break
        src = node.get("inputs", {}).get(model_input if first else "model")
        first = False
        cur = src[0] if isinstance(src, list) else None
    return [n for n in reversed(back) if nodes.get(n, {}).get("class_type") == "LoraLoaderModelOnly"]


class ProfileTests(unittest.TestCase):
    BUILTIN = {"anima_t2i", "anima_dual", "anima_tri"}
    # B 卡 §5：四件 wf_*.json 的导入件＋hires 已从 workflows\_staging\ 挪进来，盘上共 8 份
    IMPORTED = {"anima_turbo", "chosen_irises", "anima_aesthetic_dual",
                "anima_aesthetic_tri", "anima_hires"}

    def setUp(self):
        self.pairs = {}
        for f in sorted(WORKFLOWS.glob("*.json")):
            wf, prof = wp.load_pair(f)
            self.pairs[prof["workflow_id"]] = (wf, prof)

    def test_builtin_trio_and_b_imports_present(self):
        """改前叫 `test_three_profiles_present`（B 卡 §5 点名要放宽的那条）。
        内置三条一份不许少（A 阶段语义），B 卡导入的五份也得在——少一份下拉上就选不到。"""
        self.assertEqual(set(self.pairs), self.BUILTIN | self.IMPORTED)
        self.assertTrue(self.BUILTIN <= set(self.pairs))

    def test_b_import_shapes_match_their_mapping_reports(self):
        """形状＝acceptance\\B-map-*.md 里写的那些（段数／笔槽数／成品存图口数）。"""
        want = {                       # pid: (段数, 笔槽数, 成品口数)
            "anima_turbo": (1, 0, 1),   # 考题 1：wf.json 直出，无笔
            "chosen_irises": (1, 1, 1),  # 考题 3：混调底模直出
            "anima_aesthetic_dual": (1, 2, 1),   # 加试：wf_two_lora_yfs.json
            "anima_aesthetic_tri": (1, 3, 1),    # 加试：wf_three_lora_yfs.json
            "anima_hires": (2, 1, 1),   # 考题 2：两段放大，中间图不算成品
        }
        for pid, (nst, npen, nfinal) in want.items():
            prof = self.pairs[pid][1]
            with self.subTest(pid):
                self.assertEqual(len(prof["stages"]), nst)
                self.assertEqual(len(prof["lora_slots"]), npen)
                self.assertEqual(sum(1 for s in prof["save_slots"] if s["final"]), nfinal)
                self.assertEqual(wp.derive_capabilities(prof)["stages"], nst)

    def test_hires_size_relation_is_a_ratio_not_a_literal(self):
        """主人裁定 2026-10-28：段1＝全局×0.5、段2 成品＝段1×2。存倍率不存字面值，
        所以主人把全局从 832×1216 改成 1024×1536 时，2× 关系仍然成立。"""
        _, prof = self.pairs["anima_hires"]
        s1, s2 = prof["stages"]
        self.assertEqual(s1["latent_size"]["size_scale"], [0.5, 0.5])
        self.assertNotIn("size_scale", s2["size_out"])       # 缺键＝1.0＝成品就是全局尺寸
        self.assertEqual(wp.scaled_size(s1["latent_size"], 832, 1216), (416, 608))
        self.assertEqual(wp.scaled_size(s2["size_out"], 832, 1216), (832, 1216))
        self.assertEqual(wp.scaled_size(s1["latent_size"], 1024, 1536), (512, 768))
        self.assertEqual(wp.scaled_size(s2["size_out"], 1024, 1536), (1024, 1536))

    def test_slot_counts(self):
        want = {"anima_t2i": 1, "anima_dual": 2, "anima_tri": 3}
        for pid, n in want.items():
            self.assertEqual(len(self.pairs[pid][1]["lora_slots"]), n, pid)
            self.assertEqual(wp.derive_capabilities(self.pairs[pid][1])["lora_max"], n, pid)

    def test_contract_zero_errors(self):
        for pid, (wf, prof) in self.pairs.items():
            self.assertEqual(wp.validate(wf, prof), [], f"{pid} 契约有错")

    def test_contract_zero_errors_against_real_models(self):
        if not MODELS_ROOT.exists():
            self.skipTest("未提供真模型；可设置 HUAFANG_TEST_MODELS_DIR 启用集成检查")
        for pid, (wf, prof) in self.pairs.items():
            self.assertEqual(wp.validate(wf, prof, exist_fn=real_exist), [],
                             f"{pid} 契约有错（对真盘）")

    def test_tri_lora_chain_reaches_unet_in_slot_order(self):
        wf, prof = self.pairs["anima_tri"]
        stage = prof["stages"][0]
        m = stage["links"]["model"]
        order = chain_forward(wf, m["node"], m["input"])
        self.assertEqual(order, [ls["node"] for ls in prof["lora_slots"]])
        self.assertEqual(len(order), 3)
        self.assertEqual(stage["lora_roles"], ["lora1", "lora2", "lora3"])

    def test_tri_slots_are_lora_loader_model_only(self):
        wf, prof = self.pairs["anima_tri"]
        for ls in prof["lora_slots"]:
            self.assertEqual(wf[ls["node"]]["class_type"], "LoraLoaderModelOnly")

    def test_node_ids_only_live_in_profiles(self):
        """红线：前后端代码不许按下标写死节点号（D83/D96）。
        只查 ["<号>"] / ["<号>", 0] 这类真在索引工作流的写法，
        免得把 st.max = "2"（笔力上限）这种普通数字字面量误伤。"""
        declared = set()
        for wf, prof in self.pairs.values():
            declared |= set(wf)
        offenders = []
        pattern = re.compile(r"""[\[(]\s*["'](\d+)["']\s*[\],)]""")
        # 守卫自检：真写死节点号必须被抓到，普通数字字面量必须放过
        self.assertEqual(pattern.findall('nodes["10"]["inputs"]'), ["10"])
        self.assertEqual(pattern.findall('model: ["12", 0]'), ["12"])
        self.assertEqual(pattern.findall('st.max = "2"'), [])
        for f in list((ROOT / "app").glob("*.py")) + list((ROOT / "static").glob("*.js")):
            text = f.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                for node in pattern.findall(line):
                    if node in declared:
                        offenders.append(f"{f.name}:{lineno} 节点 {node}")
        self.assertEqual(offenders, [])

    def test_default_lora_files_exist_on_disk(self):
        if not MODELS_ROOT.exists():
            self.skipTest("未提供真模型；可设置 HUAFANG_TEST_MODELS_DIR 启用集成检查")
        for pid, (wf, prof) in self.pairs.items():
            for ls in prof["lora_slots"]:
                name = wf[ls["node"]]["inputs"][ls["name_input"]]
                self.assertTrue(real_exist("lora", name), f"{pid} 默认笔不在盘上: {name}")

    def test_profile_json_is_parseable_and_versioned(self):
        for f in sorted(WORKFLOWS.glob("*.json")):
            data = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual(set(data), {"workflow", "profile"})
            self.assertEqual(data["profile"]["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
