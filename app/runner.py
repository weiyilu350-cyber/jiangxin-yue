"""出图 runner：按 profile 填工作流 → 提交 → 对账 → 参数回执。"""
import copy
import json
from datetime import datetime

from . import workflow_profile as wp
from .comfy import ComfyClient, ComfyError, translate


class Job:
    def __init__(self, job_id: str, request: dict):
        self.id = job_id
        self.request = request
        self.profile = None
        self.status = "pending"  # pending / queued / running / done / error
        self.prompt_id = None
        self.sent = None  # 提交给 ComfyUI 的工作流副本，回执拿它当「界面没管项」的基准
        self.receipt = None
        self.error = None
        self.images = []
        self.positive_final = None  # 触发词入链后真正提交的正词，与 request["positive"] 不同
        self.created = datetime.now().isoformat(timespec="seconds")
        # T005：从炉子历史恢复的记录＝只读，没有本次会话的请求与提交副本，绝不伪造回执（雷区 4）
        self.readonly = False
        # T010：成品复制到主人目录的结果；只记一句人话，绝不改 done 状态（原件在炉子 output）
        self.copy_error = None
        self.copied = False


JOBS: dict = {}

COUNT_MIN, COUNT_MAX = 1, 8     # T009：张数范围，对齐早期版本的 Spinbox（1–8）


def clamp_count(value) -> int:
    """张数夹到 1..8：非数字／缺省一律当 1（与 T004 `_clamp_limit` 兜法同款）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = COUNT_MIN
    return max(COUNT_MIN, min(n, COUNT_MAX))


_MISSING = object()     # 「界面根本没发这一项」与「发了个 None」必须分得开，否则回执会自己打自己


def stage_value(req: dict, stage_id: str, key: str, default=_MISSING):
    """逐段取值（B 卡 §3）：先看这一段自己的 `stage_params[stage_id]`，没有再退回全局同名项。

    单段界面（今天的 static/app.js）只发全局项，所以退回那一步让它行为逐字不变；
    多段界面发 stage_params，第二段就能有自己的 steps/cfg/denoise。
    **build_workflow 与 build_receipt 必须共用这一个解析**，否则写进去的值与回执比的值会岔开。
    请求体是外部输入：stage_params 形状不对就当没发过，退回全局项，不拿 500 糊主人脸。
    """
    sp = req.get("stage_params")
    own = sp.get(stage_id) if isinstance(sp, dict) else None
    if isinstance(own, dict) and key in own:
        return own[key]
    if key in req:
        return req[key]
    return default


def build_workflow(workflow: dict, profile: dict, req: dict, positive: str = None) -> dict:
    """按 profile 填工作流。positive 为 None 时用界面原文；传值则用触发词入链后的成品。"""
    wf = copy.deepcopy(workflow)
    nodes = wf

    for slot in profile["model_slots"]:
        val = req.get(slot["role"])
        if val:
            nodes[slot["node"]]["inputs"][slot["input"]] = val

    loras = req.get("loras") or []
    for i, ls in enumerate(profile["lora_slots"]):
        entry = loras[i] if i < len(loras) else None
        inputs = nodes[ls["node"]]["inputs"]
        if not entry:
            inputs[ls["strength_input"]] = 0.0  # 空槽：保留默认笔、强度归零，不报缺笔
        else:
            if entry.get("name"):
                inputs[ls["name_input"]] = entry["name"]
            inputs[ls["strength_input"]] = float(entry.get("strength", 0.0))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    for ss in profile.get("save_slots", []):
        if ss.get("final"):
            nodes[ss["node"]]["inputs"][ss["prefix_input"]] = (
                f"huafang/{profile['workflow_id']}/{ts}"
            )

    pos = req.get("positive", "") if positive is None else positive
    # 全局宽／高＝最终成品尺寸。段内的 latent／缩放节点按 profile 的 size_scale 倍率换算，
    # 所以「段1＝全局×0.5、段2＝段1×2」的关系跟着全局走，不会冻在某组字面值上（主人裁定 2026-10-28）。
    gw = gh = None      # 惰性取：整份 profile 一个尺寸槽都没有时不许去 int(None)（改前就是这个口径）
    for st in profile["stages"]:
        for key, ref in st["params"].items():
            val = stage_value(req, st["id"], key)
            if val is not _MISSING:
                nodes[ref["node"]]["inputs"][ref["input"]] = val
        # links 只用于追踪/校验，不写值
        for src in st["prompt"]["positive_source"]:
            nodes[src["node"]]["inputs"][src["input"]] = pos
        for src in st["prompt"]["negative_source"]:
            nodes[src["node"]]["inputs"][src["input"]] = req.get("negative", "")
        # latent_size＝这段的输入画布，size_out＝这段的成品尺寸（hires 的 ImageScale）；两者都按倍率写
        for kind in ("latent_size", "size_out"):
            sz = st.get(kind)
            if not sz:
                continue
            if gw is None:
                gw, gh = int(req.get("width", 832)), int(req.get("height", 1216))
            w, h = wp.scaled_size(sz, gw, gh)
            nodes[sz["node"]]["inputs"][sz["width_input"]] = w
            nodes[sz["node"]]["inputs"][sz["height_input"]] = h
            # T009：张数走 profile 的槽名，不认死节点 ID；只有输入画布有 batch_size
            if kind == "latent_size" and sz.get("batch_input"):
                nodes[sz["node"]]["inputs"][sz["batch_input"]] = clamp_count(req.get("count"))
    return wf


def _eq(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-9
    return a == b


def build_receipt(history_prompt: dict, req: dict, profile: dict, sent: dict = None) -> list:
    """炉子实际收到的 vs 界面要的，逐项比对；任何一项不一致 ok=False。
    界面没管的项（如 clip/vae 跟工作流默认）以提交副本 sent 为基准。"""
    nodes = history_prompt or {}
    sent_nodes = sent or {}
    items = []

    def add(key, requested, ref):
        actual = nodes.get(ref["node"], {}).get("inputs", {}).get(ref["input"])
        items.append({"key": key, "requested": requested, "actual": actual, "ok": _eq(requested, actual)})

    for slot in profile["model_slots"]:
        wanted = req.get(slot["role"])
        if not wanted:  # 界面没选 → 基准是画舫提交时的值（工作流默认）
            wanted = sent_nodes.get(slot["node"], {}).get("inputs", {}).get(slot["input"])
        add(slot["role"], wanted, {"node": slot["node"], "input": slot["input"]})

    loras = req.get("loras") or []
    for i, ls in enumerate(profile["lora_slots"]):
        entry = loras[i] if i < len(loras) else None
        node = nodes.get(ls["node"], {}).get("inputs", {})
        if not entry:
            items.append({
                "key": f"{ls['role']}.name",
                "requested": "（空槽）",
                "actual": node.get(ls["name_input"]),
                "ok": True,
            })
            items.append({
                "key": f"{ls['role']}.strength",
                "requested": 0.0,
                "actual": node.get(ls["strength_input"]),
                "ok": _eq(0.0, node.get(ls["strength_input"])),
            })
        else:
            items.append({
                "key": f"{ls['role']}.name",
                "requested": entry.get("name"),
                "actual": node.get(ls["name_input"]),
                "ok": _eq(entry.get("name"), node.get(ls["name_input"])),
            })
            items.append({
                "key": f"{ls['role']}.strength",
                "requested": float(entry.get("strength", 0.0)),
                "actual": node.get(ls["strength_input"]),
                "ok": _eq(float(entry.get("strength", 0.0)), node.get(ls["strength_input"])),
            })

    # ---- 逐段增行（B 卡 §3 口径变更声明）
    # 段1 的键名与请求值口径**逐字照改前**：`steps`／`cfg`／`width`／`height`／`batch_size`，
    # 所以内置三条仍是 13／15／17 项，A 阶段验过的基线不动。
    # 段2+ 一律加段号前缀（`stage2.steps`），否则两段都叫 steps 时回执里两行同名、看不出谁是谁；
    # 前端 renderReceipt 把 key 原样印出来，与今天 `lora1.name` 同款风格，不需要额外翻译表。
    # 「界面要的」尺寸＝按 size_scale 换算后真正提交的那组值，不是全局原值。
    gw, gh = req.get("width"), req.get("height")
    has_size = gw is not None and gh is not None
    for i, st in enumerate(profile["stages"]):
        pfx = "" if i == 0 else f"{st['id']}."
        for key, ref in st["params"].items():
            val = stage_value(req, st["id"], key)
            if val is not _MISSING:
                add(pfx + key, val, ref)
        for kind in ("latent_size", "size_out"):
            sz = st.get(kind)
            if not sz:
                continue
            # 界面没发尺寸时，改前的行为是「requested=None、判 ✗」而不是崩；换算要数字，所以先兜住
            want = wp.scaled_size(sz, gw, gh) if has_size else (None, None)
            add(pfx + "width", want[0], {"node": sz["node"], "input": sz["width_input"]})
            add(pfx + "height", want[1], {"node": sz["node"], "input": sz["height_input"]})
            # T009 第 13 项：界面要的＝夹取后的张数，炉子收到的＝history 回读值（口径变更 12→13）
            if kind == "latent_size" and sz.get("batch_input"):
                add(pfx + "batch_size", clamp_count(req.get("count")),
                    {"node": sz["node"], "input": sz["batch_input"]})
    return items


def refresh(client: ComfyClient, job: Job, on_done=None):
    # queued 是 T005 新增的状态；它也要查 history，因为跑完了的状态以 history 为准（雷区 5）
    if job.status not in ("running", "queued"):
        return
    h = client.history(job.prompt_id)
    if not h:
        return
    apply_history(job, h, on_done)


# ---------------------------------------------------------------- T005 队列对账
HISTORY_MAX = 50            # 卡 §1：GET /history?max_items=50
CLIENT_ID = "huafang"       # submit 时带的 client_id；对账只认自己提交过的任务


def _pid(item) -> str:
    """/queue 的每条是 `[序号, prompt_id, 工作流, extra_data, outputs_to_execute]`：取第 2 个。"""
    if isinstance(item, (list, tuple)) and len(item) > 1 and isinstance(item[1], str):
        return item[1]
    return ""


def final_nodes(profile) -> set:
    """这份 profile 里哪些保存口出成品（D92）。

    一个 final 都没标就返回 None＝不筛：多段工作流的中间图不上结果墙，
    但「不知道哪个是成品」时宁可全显示，也绝不把真成品藏起来。
    只读记录没有 profile（从炉子历史重建），拿到的就是 None。
    """
    if not profile:
        return None
    finals = {ss["node"] for ss in profile.get("save_slots", []) if ss.get("final")}
    return finals or None


def history_images(entry, only_nodes=None) -> list:
    """history 的 outputs → `[{filename, subfolder}]`；结构不对就当没有图，不崩（§4）。

    only_nodes 给了就只收这些节点出的图（D92）；None＝全收，与改前逐字同口径。
    """
    out = []
    outputs = entry.get("outputs") if isinstance(entry, dict) else None
    if not isinstance(outputs, dict):
        return out
    for nid, node_out in outputs.items():
        if only_nodes is not None and nid not in only_nodes:
            continue
        if not isinstance(node_out, dict):
            continue
        for img in node_out.get("images") or []:
            if isinstance(img, dict) and img.get("filename"):
                out.append({"filename": img["filename"], "subfolder": img.get("subfolder", "")})
    return out


def history_workflow(entry) -> dict:
    """雷区 1：history 的 prompt 是 list `[序号, prompt_id, 工作流dict, extra_data, …]`，别再当 dict。"""
    hp = entry.get("prompt") if isinstance(entry, dict) else None
    if isinstance(hp, list):
        return hp[2] if len(hp) > 2 and isinstance(hp[2], dict) else {}
    return hp if isinstance(hp, dict) else {}


def history_created(entry) -> str:
    """history 里唯一的时刻是 `prompt[3].create_time`（毫秒）；读不到就退回「画舫看见它的时间」。"""
    hp = entry.get("prompt") if isinstance(entry, dict) else None
    extra = hp[3] if isinstance(hp, list) and len(hp) > 3 and isinstance(hp[3], dict) else {}
    ms = extra.get("create_time")
    if isinstance(ms, (int, float)) and ms > 0:
        return datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")
    return datetime.now().isoformat(timespec="seconds")


def is_ours(entry) -> bool:
    """炉子的 history 里也有别人（ComfyUI 自带界面）跑的图：只认 client_id＝画舫的。
    实测 entry["extra_data"] 是 null，client_id 在 prompt[3] 里；读不出来就不认，宁可不显示也不冒充。"""
    hp = entry.get("prompt") if isinstance(entry, dict) else None
    extra = hp[3] if isinstance(hp, list) and len(hp) > 3 and isinstance(hp[3], dict) else {}
    return extra.get("client_id") == CLIENT_ID


def history_failed(entry) -> bool:
    status = entry.get("status") if isinstance(entry, dict) else None
    return isinstance(status, dict) and status.get("status_str") == "error"


def apply_history(job: Job, entry: dict, on_done=None) -> None:
    """按 history 的一条记录改 job 状态；终态由调用方拦着，这里只管往前推。
    on_done：T010 成品复制钩子，只在「本次会话的任务真跑完」那一下调，且不许抛回来。"""
    if history_failed(entry):
        detail = json.dumps((entry.get("status") or {}).get("messages", []), ensure_ascii=False)
        job.status = "error"
        job.error = {"code": "comfy_error", "human": translate(detail), "detail": detail}
        return
    outputs = entry.get("outputs") if isinstance(entry, dict) else None
    if not outputs:
        return
    # D92：本次会话的任务有 profile，只收成品口出的图（中间图不上结果墙、也不进主人目录）；
    # 只读记录没有 profile → final_nodes 给 None → 全收，与改前一致（那道口子见 §3 交差说明）。
    job.images = history_images(entry, final_nodes(job.profile))
    if job.readonly:
        job.status = "done"     # 只读记录没有 request/profile：不给回执，不伪造（雷区 4）
        return                  # 也不复制：那是炉子里的旧账，不该往主人目录里倒
    job.receipt = build_receipt(history_workflow(entry), job.request, job.profile, job.sent)
    job.status = "done"
    if on_done:
        on_done(job)


def worth_rebuilding(entry) -> bool:
    """没图也没报错的历史条目不值得重建：那样只会多一张永远停在「待提交」的空卡片。"""
    return bool(history_images(entry)) or history_failed(entry)


def rebuild_job(prompt_id: str, entry: dict) -> Job:
    """内存里没有、但炉子说它跑完了 → 建一条只读记录供结果墙显示。
    id 由 prompt_id 派生，所以对账多少次都只会有同一条（雷区 2：幂等、不重新提交）。"""
    job = Job("h_" + prompt_id, {})
    job.prompt_id = prompt_id
    job.readonly = True
    job.created = history_created(entry)
    apply_history(job, entry)
    return job


def receipt_summary(job: Job) -> str:
    if not job.receipt:
        return "历史记录：本次会话没有它的回执" if job.readonly else ""
    bad = [it["key"] for it in job.receipt if not it["ok"]]
    n = len(job.receipt)
    if not bad:
        return f"{n}/{n} ✓"
    return f"{n - len(bad)}/{n}（✗ " + "、".join(bad) + "）"


def job_summary(job: Job) -> dict:
    """卡 §2 的 /api/jobs 一行；readonly/human 两项是前端画卡片要的（只读记录不给回执详情）。"""
    return {
        "job_id": job.id,
        "status": job.status,
        "prompt_id": job.prompt_id,
        "images": job.images,
        "receipt_summary": "" if job.status == "error" else receipt_summary(job),
        "created_at": job.created,
        "readonly": job.readonly,
        "human": (job.error or {}).get("human", "") if job.status == "error" else "",
        "copy_error": job.copy_error or "",   # T010：复制失败只加一句黄字，done 状态不动
    }


def reconcile_one(client: ComfyClient, job: Job) -> None:
    """/api/job/{id} 的轻量对账：只查一次 /queue，把排队中与出图中分清；终态不动。"""
    if job.status in ("done", "error") or not job.prompt_id:
        return
    q = client.queue()
    if job.prompt_id in {_pid(x) for x in q.get("queue_running") or []}:
        job.status = "running"
    elif job.prompt_id in {_pid(x) for x in q.get("queue_pending") or []}:
        job.status = "queued"


def reconcile(client: ComfyClient, on_done=None) -> dict:
    """对账 /queue 与 /history（卡 §1）：只改状态、只补只读记录，**不重新提交、不为只读记录下载图片**。
    炉子整体不可达时 ComfyError 往上抛（由端点翻成人话）；只是历史读不出来则降级成按队列对状态。
    on_done（T010）：本次会话的任务在对账中被判成 done 时，交给调用方复制成品；只读记录不走它。"""
    q = client.queue()
    running = {_pid(x) for x in q.get("queue_running") or []}
    pending = {_pid(x) for x in q.get("queue_pending") or []}
    running.discard("")
    pending.discard("")
    hist, hist_ok = {}, True
    try:
        hist = client.history_recent(HISTORY_MAX)
    except ComfyError:
        hist_ok = False     # §4：历史读取异常不该把整个任务列表搞没，顶部提示一句就行
    by_pid = {j.prompt_id: j for j in JOBS.values() if j.prompt_id}
    for pid, entry in hist.items():
        if not isinstance(entry, dict):
            continue        # 单条结构变了就跳过这条，不崩（§4）
        job = by_pid.get(pid)
        if job is None:
            if not is_ours(entry) or not worth_rebuilding(entry):
                continue    # 别人在炉子上跑的图不冒充；没图没报错的条目重建出来只是空卡片
            job = rebuild_job(pid, entry)
            JOBS[job.id] = job
            by_pid[pid] = job
        elif job.status not in ("done", "error"):
            apply_history(job, entry, on_done)     # 雷区 5：history 优先于 queue
    for job in JOBS.values():
        if job.status in ("done", "error") or not job.prompt_id:
            continue
        if job.prompt_id in running:
            job.status = "running"
        elif job.prompt_id in pending:
            job.status = "queued"
    return {
        "jobs": [job_summary(j) for j in sorted(JOBS.values(), key=lambda x: x.created)],
        "comfy": {"queue_running": len(running), "queue_pending": len(pending),
                  "history_count": len(hist), "history_ok": hist_ok},
    }

