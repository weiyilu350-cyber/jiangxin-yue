"""T005 队列对账：必写异常路径 ＋ 状态映射 ＋ 幂等 ＋ 不重新提交。"""
import copy
import unittest
from pathlib import Path

from app import runner
from app import workflow_profile as wp
from app.comfy import ComfyError

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "workflows"

OURS = "huafang"


def qitem(pid, n=1):
    """/queue 的真形状：`[序号, prompt_id, 工作流, extra_data, outputs_to_execute]`。"""
    return [n, pid, {"9": {"inputs": {}}}, {"client_id": OURS, "create_time": 1.7e12}, []]


def entry(pid, workflow=None, images=None, status_str="success", client_id=OURS,
          create_time=1761600000000.0, messages=None):
    """/history 的一条记录（实测 `extra_data` 是 null，client_id 只在 prompt[3] 里）。"""
    outputs = {}
    if images is not None:
        outputs = {"9": {"images": images}}
    return {
        "prompt": [1, pid, workflow if workflow is not None else {},
                   {"client_id": client_id, "create_time": create_time}, []],
        "outputs": outputs,
        "status": {"status_str": status_str, "completed": status_str == "success",
                   "messages": messages if messages is not None else []},
        "extra_data": None,
        "meta": None,
    }


IMG = [{"filename": "a.png", "subfolder": "huafang/anima_t2i", "type": "output"}]


class FakeComfy:
    """只实现 runner/main 真会调的四个方法；submit 一旦被调就直接判失败（雷区 2）。"""

    def __init__(self, queue=None, history=None, one=None, down=False, hist_down=False):
        self.queue_data = {"queue_running": [], "queue_pending": []} if queue is None else queue
        self.history_data = {} if history is None else history
        self.one = one or {}
        self.down = down
        self.hist_down = hist_down
        self.calls = []

    def _boom(self):
        if self.down:
            raise ComfyError("comfy_down", "连不上 ComfyUI，它启动了吗？", "fake")

    def queue(self):
        self.calls.append("queue")
        self._boom()
        return self.queue_data

    def history_recent(self, max_items=50):
        self.calls.append(("history_recent", max_items))
        self._boom()
        if self.hist_down:
            raise ComfyError("comfy_bad_json", "历史读取异常：炉子回的不是 JSON", "fake")
        return self.history_data

    def history(self, prompt_id):
        self.calls.append(("history", prompt_id))
        self._boom()
        return self.one.get(prompt_id)

    def submit(self, workflow, client_id):
        raise AssertionError("对账只改状态，绝不重新提交（雷区 2）")


class QueueBroken(FakeComfy):
    """/queue 自己坏掉（非 comfy_down）：端点该用它自带的 message 当人话。"""

    def queue(self):
        self.calls.append("queue")
        raise ComfyError("comfy_bad_json", "队列读取异常：炉子回的不是 JSON", "fake")


def mk_job(pid, status="running", request=None, profile=None, sent=None):
    j = runner.Job("j_" + pid, {"seed": 7} if request is None else request)
    j.prompt_id = pid
    j.status = status
    j.profile = profile
    j.sent = sent
    return j


WF, PROF = wp.load_pair(WORKFLOWS / "anima_t2i.json")
REQ = {"profile_id": "anima_t2i", "unet": "anima-base-v1.0.safetensors", "loras": [None],
       "positive": "1girl", "negative": "", "width": 832, "height": 1216, "steps": 30,
       "cfg": 4.0, "seed": 12345, "sampler_name": "euler", "scheduler": "simple"}
SENT = runner.build_workflow(WF, PROF, REQ)


def real_job(pid, status="running"):
    """跑起来的 job 一定带 profile 与提交副本（main.generate 就是这么建的），回执拿它们当基准。"""
    return mk_job(pid, status, copy.deepcopy(REQ), PROF, copy.deepcopy(SENT))


def done_entry(pid, images=IMG):
    """history 里一条跑完的记录，工作流＝画舫当初提交的那份（回执该 13/13，T009 起含 batch_size）。"""
    return entry(pid, copy.deepcopy(SENT), images)


class ReconcileBase(unittest.TestCase):
    def setUp(self):
        self._jobs = dict(runner.JOBS)
        runner.JOBS.clear()

    def tearDown(self):
        runner.JOBS.clear()
        runner.JOBS.update(self._jobs)

    def ids(self):
        return sorted(runner.JOBS)


class HappyPathTests(ReconcileBase):
    def test_memory_job_moves_running_to_done_with_receipt(self):
        job = real_job("p1", "running")
        runner.JOBS[job.id] = job
        fake = FakeComfy(history={"p1": done_entry("p1")})
        out = runner.reconcile(fake)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.images, [{"filename": "a.png", "subfolder": "huafang/anima_t2i"}])
        self.assertEqual(len(job.receipt), 13)
        self.assertEqual([i["key"] for i in job.receipt if not i["ok"]], [])
        self.assertEqual(out["comfy"]["history_count"], 1)
        self.assertEqual(fake.calls.count("queue"), 1)

    def test_status_mapping_queued_running_done_error(self):
        j_q = real_job("pq", "pending")
        j_r = real_job("pr", "pending")
        j_d = real_job("pd", "running")
        j_e = real_job("pe", "running")
        for j in (j_q, j_r, j_d, j_e):
            runner.JOBS[j.id] = j
        fake = FakeComfy(
            queue={"queue_running": [qitem("pr")], "queue_pending": [qitem("pq", 2)]},
            history={"pd": done_entry("pd"),
                     "pe": entry("pe", {}, None, "error", messages=[["execution_error", {}]])},
        )
        runner.reconcile(fake)
        self.assertEqual((j_q.status, j_r.status, j_d.status, j_e.status),
                         ("queued", "running", "done", "error"))
        self.assertTrue(j_e.error["human"])
        self.assertEqual(len(j_d.receipt), 13)

    def test_history_wins_over_queue(self):
        """雷区 5：正好在 running→done 之间，状态取 history。"""
        job = real_job("p1", "running")
        runner.JOBS[job.id] = job
        fake = FakeComfy(queue={"queue_running": [qitem("p1")], "queue_pending": []},
                         history={"p1": done_entry("p1")})
        runner.reconcile(fake)
        self.assertEqual(job.status, "done")

    def test_unknown_but_ours_rebuilds_readonly_without_receipt(self):
        """雷区 4：重建的记录是只读的，没有 request/profile，也不许伪造回执。"""
        fake = FakeComfy(history={"px": entry("px", {}, IMG)})
        out = runner.reconcile(fake)
        job = runner.JOBS["h_px"]
        self.assertTrue(job.readonly)
        self.assertEqual(job.status, "done")
        self.assertIsNone(job.receipt)
        self.assertEqual(job.request, {})
        self.assertEqual(job.images, [{"filename": "a.png", "subfolder": "huafang/anima_t2i"}])
        self.assertEqual(out["jobs"][0]["receipt_summary"], "历史记录：本次会话没有它的回执")

    def test_foreign_history_entry_is_ignored(self):
        """炉子里也有 ComfyUI 自带界面跑的图：client_id 不是画舫就不认。"""
        fake = FakeComfy(history={"other": entry("other", {}, IMG, client_id="comfy-web")})
        out = runner.reconcile(fake)
        self.assertEqual(self.ids(), [])
        self.assertEqual(out["jobs"], [])

    def test_readonly_created_comes_from_create_time(self):
        fake = FakeComfy(history={"px": entry("px", {}, IMG, create_time=1761600000000.0)})
        runner.reconcile(fake)
        self.assertEqual(runner.JOBS["h_px"].created, "2025-10-28T05:20:00")

    def test_jobs_sorted_by_created(self):
        fake = FakeComfy(history={"p1": entry("p1", {}, IMG, create_time=1761600000000.0),
                                  "p2": entry("p2", {}, IMG, create_time=1761700000000.0)})
        out = runner.reconcile(fake)
        self.assertEqual([j["prompt_id"] for j in out["jobs"]], ["p1", "p2"])


class IdempotencyTests(ReconcileBase):
    def test_three_reconciles_keep_same_job_set(self):
        """验收 C：连着对账三次，任务数不变、炉子队列不涨、一次都不提交。"""
        fake = FakeComfy(history={"p1": entry("p1", {}, IMG), "p2": entry("p2", {}, IMG)})
        first = runner.reconcile(fake)
        second = runner.reconcile(fake)
        third = runner.reconcile(fake)
        self.assertEqual(len(first["jobs"]), 2)
        self.assertEqual([j["job_id"] for j in second["jobs"]], [j["job_id"] for j in first["jobs"]])
        self.assertEqual([j["job_id"] for j in third["jobs"]], [j["job_id"] for j in first["jobs"]])
        self.assertEqual(self.ids(), ["h_p1", "h_p2"])

    def test_terminal_state_is_never_rewritten(self):
        job = real_job("p1", "done")
        job.receipt = [{"key": "seed", "requested": 1, "actual": 1, "ok": True}]
        runner.JOBS[job.id] = job
        fake = FakeComfy(queue={"queue_running": [qitem("p1")], "queue_pending": []},
                         history={"p1": entry("p1", {}, None, "error")})
        runner.reconcile(fake)
        self.assertEqual(job.status, "done")
        self.assertEqual(len(job.receipt), 1)
        self.assertIsNone(job.error)

    def test_reconcile_one_skips_terminal_and_memoryless(self):
        done = mk_job("p1", "done")
        nopid = runner.Job("j2", {})
        fake = FakeComfy()
        runner.reconcile_one(fake, done)
        runner.reconcile_one(fake, nopid)
        self.assertEqual(fake.calls, [])


class ExceptionPathTests(ReconcileBase):
    def test_comfy_down_raises_for_endpoint_to_translate(self):
        runner.JOBS["j1"] = mk_job("p1", "running")
        with self.assertRaises(ComfyError) as cm:
            runner.reconcile(FakeComfy(down=True))
        self.assertEqual(cm.exception.code, "comfy_down")

    def test_empty_queue_and_empty_history(self):
        out = runner.reconcile(FakeComfy())
        self.assertEqual(out, {"jobs": [], "comfy": {"queue_running": 0, "queue_pending": 0,
                                                     "history_count": 0, "history_ok": True}})

    def test_history_unreadable_degrades_to_queue_only(self):
        """§4：历史读不出来不该把任务列表搞没，顶部提示一句就行。"""
        runner.JOBS["j1"] = mk_job("p1", "pending")
        fake = FakeComfy(queue={"queue_running": [], "queue_pending": [qitem("p1", 2)]},
                         hist_down=True)
        out = runner.reconcile(fake)
        self.assertFalse(out["comfy"]["history_ok"])
        self.assertEqual(out["comfy"]["history_count"], 0)
        self.assertEqual(out["jobs"][0]["status"], "queued")

    def test_prompt_not_a_list_does_not_crash(self):
        """雷区 1 ＋ §4：prompt 是 dict／字符串／短 list／list[2] 非 dict 都要兜住。"""
        self.assertEqual(runner.history_workflow({"prompt": [1, "p"]}), {})
        self.assertEqual(runner.history_workflow({"prompt": [1, "p", "not-a-dict"]}), {})
        self.assertEqual(runner.history_workflow({"prompt": "junk"}), {})
        self.assertEqual(runner.history_workflow({"prompt": None}), {})
        self.assertEqual(runner.history_workflow({}), {})
        self.assertEqual(runner.history_workflow({"prompt": {"9": {"inputs": {}}}}),
                         {"9": {"inputs": {}}})
        # prompt 整个是字符串 → 连 client_id 都读不出来：宁可不显示，也不冒充本次会话任务
        out = runner.reconcile(FakeComfy(history={
            "p1": {"prompt": "junk", "outputs": {"9": {"images": IMG}},
                   "status": {"status_str": "success"}}}))
        self.assertEqual(out["jobs"], [])
        self.assertEqual(out["comfy"]["history_count"], 1)
        # prompt[2]（工作流）坏了但 client_id 还在 → 照样恢复成 done，回执拿空节点比，全 ✗ 也不崩
        job = real_job("p2", "running")
        runner.JOBS[job.id] = job
        out2 = runner.reconcile(FakeComfy(history={"p2": entry("p2", "not-a-dict", IMG)}))
        self.assertEqual(out2["jobs"][0]["status"], "done")
        self.assertEqual(len(job.receipt), 13)
        # 空槽的 lora1.name 按 T001 语义恒为 ✓（D65），其余 12 项拿不到真值就是 ✗
        self.assertEqual([i["key"] for i in job.receipt if i["ok"]], ["lora1.name"])

    def test_malformed_entries_are_skipped_one_by_one(self):
        """坏条目只跳过自己：不是 dict 的、以及没图没报错的（重建出来只会是空卡片）。"""
        fake = FakeComfy(history={"good": entry("good", {}, IMG), "junk": "not-a-dict",
                                  "noout": entry("noout", {}, None)})
        out = runner.reconcile(fake)
        self.assertEqual(self.ids(), ["h_good"])
        self.assertEqual(out["comfy"]["history_count"], 3)   # 计数照实报，坏条目只是不建 job
        self.assertTrue(runner.worth_rebuilding(entry("e", {}, None, "error")))
        self.assertFalse(runner.worth_rebuilding(entry("e", {}, None)))

    def test_queue_items_of_wrong_shape_are_ignored(self):
        runner.JOBS["j1"] = mk_job("p1", "pending")
        fake = FakeComfy(queue={"queue_running": ["junk", [], [1, 2, 3], qitem("p1")],
                                "queue_pending": None})
        out = runner.reconcile(fake)
        self.assertEqual(out["comfy"]["queue_running"], 1)
        self.assertEqual(out["comfy"]["queue_pending"], 0)
        self.assertEqual(runner.JOBS["j1"].status, "running")


class RefreshTests(ReconcileBase):
    def test_queued_job_also_checks_history(self):
        job = real_job("p1", "queued")
        runner.JOBS[job.id] = job
        fake = FakeComfy(one={"p1": done_entry("p1")})
        runner.refresh(fake, job)
        self.assertEqual(job.status, "done")
        self.assertEqual(len(job.receipt), 13)

    def test_done_job_does_not_hit_the_furnace(self):
        fake = FakeComfy()
        runner.refresh(fake, mk_job("p1", "done"))
        runner.refresh(fake, mk_job("p2", "error"))
        runner.refresh(fake, mk_job("p3", "pending"))
        self.assertEqual(fake.calls, [])

    def test_no_history_yet_keeps_running(self):
        job = mk_job("p1", "running")
        runner.refresh(FakeComfy(), job)
        self.assertEqual(job.status, "running")


class SummaryTests(ReconcileBase):
    def test_receipt_summary_all_ok_and_one_bad(self):
        job = mk_job("p1", "done")
        job.receipt = [{"key": k, "ok": True} for k in ("a", "b", "c")]
        self.assertEqual(runner.receipt_summary(job), "3/3 ✓")
        job.receipt[1]["ok"] = False
        self.assertEqual(runner.receipt_summary(job), "2/3（✗ b）")

    def test_receipt_summary_for_pending_and_readonly(self):
        self.assertEqual(runner.receipt_summary(mk_job("p1", "running")), "")
        ro = mk_job("p2", "done")
        ro.readonly = True
        self.assertEqual(runner.receipt_summary(ro), "历史记录：本次会话没有它的回执")

    def test_job_summary_shape(self):
        job = mk_job("p1", "error")
        job.error = {"code": "comfy_error", "human": "出图失败了", "detail": "x"}
        s = runner.job_summary(job)
        # T010 起多一行 copy_error（复制失败的人话）；其余八项与 T005 逐项相同
        self.assertEqual(sorted(s), ["copy_error", "created_at", "human", "images", "job_id",
                                     "prompt_id", "readonly", "receipt_summary", "status"])
        self.assertEqual(s["human"], "出图失败了")
        self.assertEqual(s["receipt_summary"], "")
        self.assertEqual(s["copy_error"], "")
        self.assertFalse(s["readonly"])


class EndpointTests(ReconcileBase):
    """三个端点的形状与人话；用 TestClient 走真中间件（token/Origin）。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.settings import TOKEN_FILE
        cls._token_file = TOKEN_FILE
        cls._token_before = (
            TOKEN_FILE.read_text(encoding="utf-8") if TOKEN_FILE.exists() else None)
        from app import main
        cls.main = main
        cls.http = TestClient(main.app, base_url="http://127.0.0.1:8788",
                              headers={"Authorization": "Bearer " + main.TOKEN})

    @classmethod
    def tearDownClass(cls):
        # 导入 app.main 会重生 token.txt；还原，别跟跑着的服务对不上
        if cls._token_before is not None:
            cls._token_file.write_text(cls._token_before, encoding="utf-8")

    def setUp(self):
        super().setUp()
        self._client = self.main.CLIENT

    def tearDown(self):
        self.main.CLIENT = self._client
        super().tearDown()

    def use(self, fake):
        self.main.CLIENT = fake
        return fake

    def test_jobs_and_reconcile_shapes(self):
        self.use(FakeComfy(history={"p1": entry("p1", {}, IMG)}))
        for r in (self.http.get("/api/jobs"), self.http.post("/api/reconcile")):
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertTrue(body["ok"])
            self.assertEqual(sorted(body), ["comfy", "jobs", "ok"])
            self.assertEqual(sorted(body["comfy"]),
                             ["history_count", "history_ok", "queue_pending", "queue_running"])
            self.assertEqual([j["prompt_id"] for j in body["jobs"]], ["p1"])

    def test_comfy_down_returns_200_with_human_error(self):
        """验收 D：炉子掉线也得回人话，不吐 500。"""
        self.use(FakeComfy(down=True))
        for r in (self.http.get("/api/jobs"), self.http.post("/api/reconcile")):
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertFalse(body["ok"])
            self.assertEqual(body["jobs"], [])
            self.assertEqual(body["error"]["human"], "画舫连不上炉子，先确认 ComfyUI 开着")

    def test_other_comfy_error_uses_own_message(self):
        fake = QueueBroken()
        self.use(fake)
        body = self.http.get("/api/jobs").json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["human"], "队列读取异常：炉子回的不是 JSON")

    def test_needs_token(self):
        self.use(FakeComfy())
        r = self.http.get("/api/jobs", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_unknown_job_id_is_404(self):
        self.use(FakeComfy())
        self.assertEqual(self.http.get("/api/job/nope-not-here").status_code, 404)

    def test_job_status_reports_readonly_and_light_reconcile(self):
        fake = self.use(FakeComfy(queue={"queue_running": [], "queue_pending": [qitem("p1", 2)]}))
        job = mk_job("p1", "pending")
        runner.JOBS[job.id] = job
        body = self.http.get("/api/job/" + job.id).json()
        self.assertEqual(body["status"], "queued")
        self.assertFalse(body["readonly"])
        self.assertIn("queue", fake.calls)

        ro = runner.rebuild_job("px", entry("px", {}, IMG))
        runner.JOBS[ro.id] = ro
        body2 = self.http.get("/api/job/" + ro.id).json()
        self.assertTrue(body2["readonly"])
        self.assertEqual(body2["status"], "done")
        self.assertIsNone(body2["receipt"])

    def test_history_endpoint_reachable_without_resubmit(self):
        """雷区 2 的端点级证据：整轮对账下来 submit 从没被碰过。"""
        fake = self.use(FakeComfy(history={"p1": entry("p1", {}, IMG)}))
        self.http.get("/api/jobs")
        self.http.post("/api/reconcile")
        self.http.get("/api/job/h_p1")
        self.assertEqual(self.ids(), ["h_p1"])


if __name__ == "__main__":
    unittest.main()
