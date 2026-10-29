"""B 卡 §6 第四层：端点全链路（假 Comfy 顶 httpx，不引 mock 框架）。

盯的是「界面点下去 → 炉子回话 → 回执上墙」这条真路，unit 层测不到的三件事：
1. **多段请求真能落到 21 行回执**，且段2 的键一律带 `stage2.` 前缀、段1 逐字照 13 项基线
   （主人裁定第 2 款：段1 不发 denoise，所以回执里不许冒出无前缀的 denoise 行）。
2. **D92 只上成品**：hires 的中间图（save_slots 里 final=False 的那个口）不进结果墙。
3. **✗ 路互不牵连**：段2 某个值对不上只翻它自己那行，段1 的行照旧 ✓，反之亦然。
"""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import gallery
from app import runner
from app import workflow_profile as wp
from app.comfy import ComfyError

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"

HIRES_WF, HIRES_PROF = wp.load_pair(WORKFLOWS / "anima_hires.json")
T2I_WF, T2I_PROF = wp.load_pair(WORKFLOWS / "anima_t2i.json")

# 段2 该出现的 8 行（6 个参数＋成品宽／高）；段1 的 13 行口径由 test_receipt.py 与本文件的
# test_t2i_regression_still_13_rows 双向钉住。
STAGE2_KEYS = {"stage2.seed", "stage2.steps", "stage2.cfg", "stage2.sampler_name",
               "stage2.scheduler", "stage2.denoise", "stage2.width", "stage2.height"}


def disk_names(wf, prof):
    """按 profile 的槽位从工作流字面值里取在盘模型名：exist_fn 要查真文件，编名字会 409。"""
    out = {}
    for slot in prof["model_slots"]:
        out[slot["role"]] = wf[slot["node"]]["inputs"][slot["input"]]
    pens = []
    for ls in prof["lora_slots"]:
        pens.append({"name": wf[ls["node"]]["inputs"][ls["name_input"]], "strength": 0.8})
    return out, pens


HIRES_MODELS, HIRES_PENS = disk_names(HIRES_WF, HIRES_PROF)
T2I_MODELS, T2I_PENS = disk_names(T2I_WF, T2I_PROF)


def hires_req(**over):
    """全局 1024×1024＋段2 自己一套参数：正是 §4 前端 collectStageParams() 发的形状。"""
    req = dict(HIRES_MODELS)
    req.update({
        "profile_id": "anima_hires",
        "loras": copy.deepcopy(HIRES_PENS),
        "positive": "1girl", "negative": "bad hands",
        "width": 1024, "height": 1024, "count": 1,
        "seed": 12345, "steps": 30, "cfg": 4.0,
        "sampler_name": "euler", "scheduler": "simple",
        "stage_params": {"stage2": {"seed": 999, "steps": 20, "cfg": 3.5,
                                    "sampler_name": "dpmpp_2m", "scheduler": "karras",
                                    "denoise": 0.35}},
    })
    req.update(over)
    return req


def t2i_req(**over):
    req = dict(T2I_MODELS)
    req.update({
        "profile_id": "anima_t2i",
        "loras": copy.deepcopy(T2I_PENS),
        "positive": "1girl", "negative": "",
        "width": 832, "height": 1216, "count": 2,
        "seed": 777, "steps": 30, "cfg": 4.0,
        "sampler_name": "euler", "scheduler": "simple",
    })
    req.update(over)
    return req


def hist_entry(pid, workflow, outputs):
    """/history 一条记录的真形状：prompt 是 list，client_id 在 prompt[3]（雷区 1）。"""
    return {"prompt": [1, pid, workflow, {"client_id": "huafang", "create_time": 1.7e12}, []],
            "outputs": outputs,
            "status": {"status_str": "success", "completed": True, "messages": []},
            "extra_data": None}


class FakeComfy:
    """只实现 main/runner 真会调的那几个方法。

    `echo` ＝ history 原样回提交副本（回执该全 ✓）；给了 `mutate` 就在回读前动手脚，
    用来造 ✗ 路——这正是「炉子实际收到的 vs 界面要的」要比出来的东西。
    """

    def __init__(self, mutate=None, boom=None, outputs=None):
        self.mutate = mutate
        self.boom = boom
        self.outputs = outputs
        self.submitted = None
        self.calls = []

    def submit(self, workflow, client_id):
        self.calls.append(("submit", client_id))
        if self.boom:
            raise ComfyError("comfy_down", "连不上 ComfyUI，它启动了吗？", "fake detail")
        self.submitted = workflow
        return "pid_hires"

    def queue(self):
        self.calls.append("queue")
        return {"queue_running": [["1", "pid_hires", {}, {}, []]], "queue_pending": []}

    def history(self, prompt_id):
        """真 `ComfyClient.history` 回的是**单条 entry**（它自己剥掉了 prompt_id 那层）。"""
        self.calls.append(("history", prompt_id))
        if self.submitted is None:
            return None
        wf = copy.deepcopy(self.submitted)
        if self.mutate:
            self.mutate(wf)
        return hist_entry(prompt_id, wf, self.outputs if self.outputs is not None
                          else self.default_outputs())

    def history_recent(self, max_items=50):
        self.calls.append(("history_recent", max_items))
        return {}

    def default_outputs(self):
        """成品口与中间口都出图：D92 该只把成品那一份放上墙。"""
        finals = [ss["node"] for ss in HIRES_PROF["save_slots"] if ss.get("final")]
        mids = [ss["node"] for ss in HIRES_PROF["save_slots"] if not ss.get("final")]
        out = {n: {"images": [{"filename": f"final_{n}.png", "subfolder": "huafang/anima_hires",
                               "type": "output"}]} for n in finals}
        out.update({n: {"images": [{"filename": f"mid_{n}.png", "subfolder": "huafang/anima_hires",
                                    "type": "output"}]} for n in mids})
        return out


def node_of(prof, stage_id, key):
    """取某段某参数指向的节点号——测试用它改值造 ✗，产品代码里不许出现节点 ID（D83）。"""
    for st in prof["stages"]:
        if st["id"] == stage_id:
            return st["params"][key]["node"], st["params"][key]["input"]
    raise AssertionError(stage_id)


class EndpointBase(unittest.TestCase):
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
        self._client = self.main.CLIENT
        self._jobs = dict(runner.JOBS)
        runner.JOBS.clear()
        self._profiles = dict(self.main.PROFILES)
        self._settings = copy.deepcopy(self.main.SETTINGS)
        self.main.SETTINGS["output"] = {"dir": ""}      # 端点测试不往主人目录里倒图（T010）
        # C 卡起 job 到 done 会顺手登进图库索引：这里的 job 全是假炉子编的（final_9.png 之类），
        # 登进真库就是主人图库里几张点开 502 的鬼图。所以索引也一律换成临时库。
        self._tmp = tempfile.mkdtemp(prefix="hf_b_ep_")
        self._gal = self.main.GAL
        self.main.GAL = gallery.Gallery(Path(self._tmp) / "gallery.sqlite")

    def tearDown(self):
        self.main.GAL.close()
        self.main.GAL = self._gal
        shutil.rmtree(self._tmp, ignore_errors=True)
        self.main.SETTINGS.clear()
        self.main.SETTINGS.update(self._settings)
        self.main.PROFILES.clear()
        self.main.PROFILES.update(self._profiles)
        runner.JOBS.clear()
        runner.JOBS.update(self._jobs)
        self.main.CLIENT = self._client

    def use(self, fake):
        self.main.CLIENT = fake
        return fake

    def run_job(self, req, fake=None):
        """generate → job 轮询到终态，返回 (提交响应, 任务响应)。"""
        fake = self.use(fake or FakeComfy())
        r1 = self.http.post("/api/generate", json=req)
        self.assertEqual(r1.status_code, 200, r1.text)
        jid = r1.json()["job_id"]
        r2 = self.http.get(f"/api/job/{jid}")
        return r1.json(), r2.json(), fake

    @staticmethod
    def rows(body):
        return body["receipt"] or []

    @staticmethod
    def keys(body):
        return [it["key"] for it in body["receipt"] or []]

    @staticmethod
    def bad(body):
        return [it["key"] for it in body["receipt"] or [] if not it["ok"]]


class HiresFullChainTests(EndpointBase):
    """双段 profile 的 generate/job 全链路。"""

    def test_21_rows_all_ok_and_stage2_keys_are_prefixed(self):
        _, body, _ = self.run_job(hires_req())
        self.assertEqual(body["status"], "done")
        rows = self.rows(body)
        self.assertEqual(len(rows), 21, self.keys(body))
        self.assertEqual(self.bad(body), [])
        self.assertEqual(set(self.keys(body)) & STAGE2_KEYS, STAGE2_KEYS)
        # 段1 逐字照 13 项基线：键名无段号前缀，且**没有 denoise 行**（裁定第 2 款）
        stage1 = [k for k in self.keys(body) if not k.startswith("stage2.")]
        self.assertEqual(len(stage1), 13, stage1)
        self.assertNotIn("denoise", stage1)
        # 段2 的行一律排在段1 之后，前端 renderReceipt 才印得出「先段1 后段2」
        first2 = min(i for i, k in enumerate(self.keys(body)) if k.startswith("stage2."))
        self.assertEqual(first2, 13)

    def test_stage2_values_come_from_stage_params_not_global(self):
        _, body, fake = self.run_job(hires_req())
        got = {it["key"]: it["actual"] for it in self.rows(body)}
        self.assertEqual(got["stage2.denoise"], 0.35)
        self.assertEqual(got["stage2.steps"], 20)
        self.assertEqual(got["steps"], 30)          # 段1 仍是全局值
        self.assertNotIn("denoise", got)            # 段1 没这一项
        # 段1 latent＝全局×0.5、段2 成品＝全局×1.0（倍率语义）
        self.assertEqual((got["width"], got["height"]), (512, 512))
        self.assertEqual((got["stage2.width"], got["stage2.height"]), (1024, 1024))
        self.assertTrue(fake.submitted)

    def test_only_final_images_reach_the_wall(self):
        """D92：中间图（final=False 的保存口）不上结果墙。"""
        _, body, _ = self.run_job(hires_req())
        names = [i["filename"] for i in body["images"]]
        self.assertTrue(names, "成品图没了")
        self.assertTrue(all(n.startswith("final_") for n in names), names)
        self.assertFalse(any(n.startswith("mid_") for n in names), names)

    def test_stage2_mismatch_flips_only_its_own_row(self):
        n2, inp2 = node_of(HIRES_PROF, "stage2", "denoise")

        def mutate(wf):
            wf[n2]["inputs"][inp2] = 0.9        # 炉子实际跑的不是界面要的那个重绘幅度

        _, body, _ = self.run_job(hires_req(), FakeComfy(mutate=mutate))
        self.assertEqual(self.bad(body), ["stage2.denoise"], self.keys(body))
        row = [it for it in self.rows(body) if it["key"] == "stage2.denoise"][0]
        self.assertEqual([row["requested"], row["actual"]], [0.35, 0.9])

    def test_stage1_mismatch_does_not_blame_stage2(self):
        n1, inp1 = node_of(HIRES_PROF, "stage1", "steps")

        def mutate(wf):
            wf[n1]["inputs"][inp1] = 8

        _, body, _ = self.run_job(hires_req(), FakeComfy(mutate=mutate))
        self.assertEqual(self.bad(body), ["steps"])
        self.assertNotIn("stage2.steps", self.bad(body))

    def test_size_mismatch_is_reported_per_stage(self):
        """段2 成品尺寸被炉子改了：只有 stage2.width 翻 ✗，段1 的 width 照旧 ✓。"""
        so = [st for st in HIRES_PROF["stages"] if st.get("size_out")][0]["size_out"]

        def mutate(wf):
            wf[so["node"]]["inputs"][so["width_input"]] = 768

        _, body, _ = self.run_job(hires_req(), FakeComfy(mutate=mutate))
        self.assertEqual(self.bad(body), ["stage2.width"])

    def test_stage_params_of_wrong_shape_falls_back_to_global(self):
        """请求体是外部输入：stage_params 形状不对就当没发过，不拿 500 糊主人脸。

        退回全局＝段2 抄段1 的值，所以只剩 20 行：`stage2.denoise` 整行没了（裁定第 2 款
        规定全局不发 denoise，段2 退回全局自然也无从取值）。这是静默降级、不是报错，
        但界面发不出 stage_params 时重绘幅度会照工作流原值走——记进交差的已知限制。
        """
        for junk in ("nope", 42, ["stage2"], {"stage2": "junk"}):
            with self.subTest(repr(junk)):
                _, body, _ = self.run_job(hires_req(stage_params=junk))
                self.assertEqual(body["status"], "done")
                ks = self.keys(body)
                self.assertEqual(len(ks), 20, ks)
                self.assertEqual(self.bad(body), [], f"{junk!r} 让回执翻 ✗ 了")
                self.assertNotIn("stage2.denoise", ks)
                got = {it["key"]: it["actual"] for it in self.rows(body)}
                self.assertEqual(got["stage2.steps"], got["steps"])   # 段2 退回全局值
                self.assertEqual(got["steps"], 30)

    def test_seed_minus_one_is_filled_and_count_is_clamped(self):
        _, body, _ = self.run_job(hires_req(seed=-1, count=99))
        self.assertEqual(body["status"], "done")
        got = {it["key"]: it["actual"] for it in self.rows(body)}
        self.assertEqual(got["batch_size"], runner.COUNT_MAX)   # T009：夹到 8
        self.assertEqual(self.bad(body), [])
        seed = body["request"]["seed"]
        self.assertIsInstance(seed, int)
        self.assertGreaterEqual(seed, 0)

    def test_comfy_down_is_human_not_500(self):
        r1, body, _ = self.run_job(hires_req(), FakeComfy(boom=True))
        self.assertEqual(r1["status"], "error")
        self.assertEqual(body["status"], "error")
        self.assertTrue(body["error"]["human"], "掉线也得说人话")
        self.assertIsNone(body["receipt"])      # 没提交成功就没有回执，绝不伪造（雷区 4）

    def test_furnace_error_entry_becomes_human(self):
        """history 回 status_str=error：翻成人话，不吐 500。"""
        fake = self.use(FakeComfy())
        r1 = self.http.post("/api/generate", json=hires_req())
        jid = r1.json()["job_id"]

        def bad_history(pid):
            return {"prompt": [1, pid, {}, {"client_id": "huafang"}, []],
                    "outputs": None,
                    "status": {"status_str": "error", "completed": False,
                               "messages": [["execution_error", {"node_id": "14"}]]}}

        fake.history = bad_history
        body = self.http.get(f"/api/job/{jid}").json()
        self.assertEqual(body["status"], "error")
        self.assertTrue(body["error"]["human"])


class GuardAndContractTests(EndpointBase):
    def test_unknown_profile_is_404(self):
        r = self.http.post("/api/generate", json={"profile_id": "no_such"})
        self.assertEqual(r.status_code, 404)

    def test_contract_errors_are_409_with_human(self):
        """errors 拦着不让用（D76）：409＋人话＋原始 errors，warnings 不参与拦。"""
        self.main.PROFILES["broken"] = {"workflow": {}, "profile": {"workflow_id": "broken"},
                                        "errors": ["stage stage1: 没声明 model 连线"],
                                        "warnings": ["stage stage2: 此段无笔"], "file": "x.json"}
        r = self.http.post("/api/generate", json={"profile_id": "broken"})
        self.assertEqual(r.status_code, 409)
        d = r.json()["detail"]
        self.assertTrue(d["human"])
        self.assertEqual(d["errors"], ["stage stage1: 没声明 model 连线"])

    def test_warning_only_profile_still_generates(self):
        """warnings 只是要界面说一句：不许因为「此段无笔」就把出图拦住。"""
        entry = copy.deepcopy(self.main.PROFILES["anima_hires"])
        entry["warnings"] = ["stage stage2（第二段）: 此段无笔"]
        self.main.PROFILES["anima_hires"] = entry
        r1, body, _ = self.run_job(hires_req())
        self.assertEqual(r1["status"], "running")
        self.assertEqual(body["status"], "done")
        self.assertEqual(self.bad(body), [])

    def test_token_guard_rejects_wrong_token(self):
        r = self.http.post("/api/generate", json=hires_req(),
                           headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_node_ids_never_leave_the_backend(self):
        """D83：回执与任务体里不许出现节点号——那是 profile 的私事。"""
        _, body, _ = self.run_job(hires_req())
        blob = json.dumps({k: v for k, v in body.items() if k != "request"}, ensure_ascii=False)
        for st in HIRES_PROF["stages"]:
            for ref in list(st["params"].values()) + list(st.get("links", {}).values()):
                self.assertNotIn(f'"node": "{ref["node"]}"', blob)
        self.assertNotIn("latent_size", blob)
        self.assertNotIn("size_out", blob)


class SingleStageRegressionTests(EndpointBase):
    """A 阶段基线：单段 profile 走端点仍是 13 行全 ✓，且请求里没 stage_params 也照样跑。"""

    def test_t2i_regression_still_13_rows(self):
        req = t2i_req()
        self.assertNotIn("stage_params", req)
        _, body, _ = self.run_job(req)
        self.assertEqual(body["status"], "done")
        self.assertEqual(len(self.rows(body)), 13, self.keys(body))
        self.assertEqual(self.bad(body), [])
        self.assertFalse(any(k.startswith("stage") for k in self.keys(body)), self.keys(body))
        got = {it["key"]: it["actual"] for it in self.rows(body)}
        self.assertEqual((got["width"], got["height"]), (832, 1216))   # 倍率 1.0：一个像素不动
        self.assertEqual(got["batch_size"], 2)

    def test_every_on_disk_profile_reports_no_contract_error(self):
        """§5 门槛：promote 上来的五份考题＋内置三条，errors 必须全空。"""
        for pid, entry in sorted(self.main.PROFILES.items()):
            with self.subTest(pid):
                self.assertEqual(entry["errors"], [], f"{pid} 契约没过")


if __name__ == "__main__":
    unittest.main()
