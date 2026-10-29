"""B 卡 §1：把 ComfyUI 导出的 API 格式工作流认成画舫的 {workflow, profile} 对。

铁律：认不准的宁可留空让人修，**绝不瞎猜接线**；报告里每个判断都给依据（节点类型／连线）。
工作流 JSON 按不可信数据处理：只解析、不执行、不 eval（D73）。
产物默认落 `workflows/_staging/`——`_load_profiles()` 只 glob `workflows/*.json`（不递归），
所以半成品不会被正在跑的服务捡起来；§5 考题过了再挪进 `workflows/`。
"""
import argparse
import json
import sys
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "workflows" / "_staging"
REPORTS = ROOT / "acceptance"

# 采样器：一段＝一个采样器（卡 §1）
SAMPLERS = ("KSampler", "KSamplerAdvanced", "SDTurbo")
# 底模加载器 → model_slots 的 unet 角色；CheckpointLoaderSimple 的文件在 models/checkpoints，
# 与 MODEL_DIRS["unet"]=diffusion_models 不是一个目录，这层错配留到 §2 定（报告里点名）
UNET_LOADERS = {"UNETLoader": "unet_name", "CheckpointLoaderSimple": "ckpt_name"}
CLIP_LOADERS = {
    "CLIPLoader": ["clip_name"],
    "DualCLIPLoader": ["clip_name1", "clip_name2"],
    "TripleCLIPLoader": ["clip_name1", "clip_name2", "clip_name3"],
}
VAE_LOADERS = {"VAELoader": ["vae_name"]}
# 笔加载器：(笔名输入, 模型强度输入, CLIP 强度输入|None)
LORA_LOADERS = {
    "LoraLoaderModelOnly": ("lora_name", "strength_model", None),
    "LoraLoader": ("lora_name", "strength_model", "strength_clip"),
}
# 空 latent：本段自己的尺寸来源
LATENT_SOURCES = {
    "EmptyLatentImage": ("width", "height", "batch_size"),
    "EmptySD3LatentImage": ("width", "height", "batch_size"),
    "EmptyFluxLatentImage": ("width", "height", "batch_size"),
    "EmptyHunyuanLatentImage": ("width", "height", "batch_size"),
}
# 绝对尺寸缩放节点：本段的成品尺寸来源（hires 的 ImageScale）
SCALE_NODES = {"ImageScale": ("width", "height")}
# 倍率型放大：只能标 hires，尺寸规则画舫契约还没这一型 → 留给人工
RATIO_UPSCALERS = ("ImageScaleBy", "ImageUpscaleWithModel", "UpscaleModelLoader")

STAGE_PARAMS = ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise")
DEFAULT_KEYS = ("steps", "cfg", "sampler_name", "scheduler")
# 算回执行数用的「界面会发哪些逐段参数」。段1＝今天 static/app.js 的 PARAM_FIELDS 里属于段参数的那几个
# （denoise 不在其中，所以段1 是 5 行、内置三条守住 13／15／17）；段2+ 按主人裁定 2026-10-28 多出 denoise。
UI_SENDS_STAGE1 = ("seed", "steps", "cfg", "sampler_name", "scheduler")
UI_SENDS_LATER = UI_SENDS_STAGE1 + ("denoise",)
TEXT_ENCODE = "CLIPTextEncode"
LOAD_IMAGE = "LoadImage"
SAVE_IMAGE = "SaveImage"
LATENT_INPUTS = ("latent_image", "latent")
MAX_HOPS = 400          # 环／超大图的保险丝，不是业务限制

CN_NUM = "零一二三四五六七八九十"


class Reject(Exception):
    """导入不了：message 是给主人看的人话（D96），不带堆栈。"""


def _cn(i: int) -> str:
    return CN_NUM[i] if 0 <= i < len(CN_NUM) else str(i)


def _int(value):
    """不可信 JSON 里的尺寸／张数：转不出整数就当没读到，绝不抛。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _src(inputs: dict, name: str):
    """取一条连线的源节点号；不是 `["<号>", 槽]` 形状（＝写死的字面值）就返回 None。"""
    v = inputs.get(name)
    return v[0] if isinstance(v, list) and v and isinstance(v[0], str) else None


def _cls(nodes: dict, nid) -> str:
    n = nodes.get(nid)
    return n.get("class_type", "") if isinstance(n, dict) else ""


def _consumers(nodes: dict, nid) -> list:
    """谁的某个输入接在 `nid` 上（按节点号排序）。参考图槽要报「被谁消费」。"""
    out = []
    for cid in nodes:
        ins = _inputs(nodes, cid)
        if any(_src(ins, k) == nid for k in ins):
            out.append(cid)
    return sorted(out)


def _inputs(nodes: dict, nid) -> dict:
    n = nodes.get(nid)
    ins = n.get("inputs") if isinstance(n, dict) else None
    return ins if isinstance(ins, dict) else {}


class Report:
    """识别过程的账本：auto＝认出来了（依据是节点类型／连线），manual＝没认、留空给人修。"""

    def __init__(self):
        self.rows = []

    def auto(self, item, where, why):
        self.rows.append({"kind": "auto", "item": item, "where": where, "why": why})

    def manual(self, item, where, why):
        self.rows.append({"kind": "manual", "item": item, "where": where, "why": why})

    def of(self, kind):
        return [r for r in self.rows if r["kind"] == kind]


# ------------------------------------------------------------------ 入料归一

def load_nodes(path: Path):
    """吃三种壳：`{prompt:{...}}`（ComfyUI 存的 API 文件）、裸节点表、`{workflow,profile}`（拒）。
    返回 (nodes, shell 说明)。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        raise Reject(f"读不了这个文件：{e.strerror or e}")
    except ValueError as e:
        raise Reject(f"这个文件不是合法 JSON：{e}")
    if not isinstance(raw, dict):
        raise Reject("这个文件顶层不是 JSON 对象，认不出是工作流")
    if isinstance(raw.get("workflow"), dict) and isinstance(raw.get("profile"), dict):
        raise Reject("这已经是画舫的 {workflow, profile} 对了，不用再导入")
    if isinstance(raw.get("nodes"), list) or isinstance(raw.get("links"), list):
        raise Reject("这是 ComfyUI 界面导出的（Save），不是 API 格式；"
                     "请在 ComfyUI 设置里开 Dev mode Options，改用「Save (API Format)」重导一份")
    if isinstance(raw.get("prompt"), dict):
        nodes, shell = raw["prompt"], "API 外壳 {prompt:{…}}（client_id=%r）" % raw.get("client_id")
    else:
        nodes, shell = raw, "裸节点表（API 格式，无外壳）"
    if not nodes:
        raise Reject("节点表是空的，没东西可认")
    for nid, node in nodes.items():
        if not isinstance(node, dict) or not isinstance(node.get("class_type"), str) \
                or not isinstance(node.get("inputs"), dict):
            raise Reject(f"节点 {nid} 不是 API 格式的形状（要有 class_type 与 inputs），"
                         "这份文件像是界面导出或半截保存")
    return nodes, shell


# ------------------------------------------------------------------ 图遍历

def walk_back(nodes, start, stop_classes=(), stop_nodes=()):
    """从 (节点号, 输入名) 往回按连线遍历，返回访问到的节点号序列。
    遇到 stop_classes/stop_nodes 就停在那儿（记下来但不再往里走）；带环保护与跳数上限。"""
    seen, out, queue = set(), [], deque()
    first = _src(_inputs(nodes, start[0]), start[1])
    if first is None:
        return out
    queue.append(first)
    while queue and len(out) < MAX_HOPS:
        nid = queue.popleft()
        if nid in seen:
            continue
        seen.add(nid)
        out.append(nid)
        if _cls(nodes, nid) in stop_classes or nid in stop_nodes:
            continue
        ins = _inputs(nodes, nid)
        for name in ins:
            src = _src(ins, name)
            if src is not None and src not in seen:
                queue.append(src)
    return out


def model_chain(nodes, sampler_id):
    """从采样器的 model 输入一路往回走到底模加载器。
    返回 (正序 LoRA 节点号＝底模→采样器, 终点节点号|None, 断链原因|None)。"""
    back, cur = [], _src(_inputs(nodes, sampler_id), "model")
    while cur is not None and cur not in back and len(back) < MAX_HOPS:
        back.append(cur)
        cls = _cls(nodes, cur)
        if cls in UNET_LOADERS:
            break
        if cls not in LORA_LOADERS:
            return [], None, (f"model 链上有认不出的节点 {cur}（{cls or '未知类型'}），不敢往下接")
        cur = _src(_inputs(nodes, cur), "model")
    else:
        return [], None, "model 链没落到底模加载器（UNETLoader／CheckpointLoaderSimple），可能是环或断链"
    terminal = back[-1]
    if _cls(nodes, terminal) not in UNET_LOADERS:
        return [], None, f"model 链尽头是 {_cls(nodes, terminal) or '未知类型'}，不是底模加载器"
    # 往回走是「采样器→底模」，笔槽顺序要的是「底模→采样器」，所以倒过来（与内置三条同口径）
    loras = [n for n in reversed(back[:-1]) if _cls(nodes, n) in LORA_LOADERS]
    return loras, terminal, None


def sampler_deps(nodes, sampler_id, sampler_ids):
    """本段的 latent 是从哪些采样器来的（撞上采样器就停，不往里走）→ 用来定段序。"""
    inp = next((n for n in LATENT_INPUTS if _src(_inputs(nodes, sampler_id), n)), None)
    if inp is None:
        return set(), None
    return {n for n in walk_back(nodes, (sampler_id, inp), stop_nodes=sampler_ids)
            if n in sampler_ids and n != sampler_id}, inp


def stage_depth(nodes, sid, sampler_ids, memo=None, guard=None):
    """段深＝它上游最长的采样器链长度；有环就当 0 并记下来（不上抛）。"""
    memo = {} if memo is None else memo
    guard = set() if guard is None else guard
    if sid in memo:
        return memo[sid]
    if sid in guard:
        return 0
    guard.add(sid)
    deps, _ = sampler_deps(nodes, sid, sampler_ids)
    d = 0 if not deps else 1 + max(stage_depth(nodes, x, sampler_ids, memo, guard) for x in deps)
    guard.discard(sid)
    memo[sid] = d
    return d


def nearest_of(nodes, start, classes):
    """从 (节点, 输入) 往回找最近的一类节点；撞上采样器就停。返回 (节点号|None, 同类候选数)。"""
    inp = start[1]
    hits = [n for n in walk_back(nodes, (start[0], inp), stop_classes=SAMPLERS)
            if _cls(nodes, n) in classes]
    return (hits[0] if hits else None), len(hits)


def trace_samplers(nodes, start, sampler_ids):
    """从一个节点往回撞到的采样器集合（撞上就停）→ 用来判 SaveImage 是不是成品。"""
    return {n for n in walk_back(nodes, start, stop_nodes=sampler_ids) if n in sampler_ids}


# ------------------------------------------------------------------ 识别主体

def import_workflow(path, workflow_id=None, label=None, family=None):
    """返回 {"data": {workflow, profile}, "report": Report, "meta": {...}}；认不了抛 Reject。"""
    path = Path(path)
    nodes, shell = load_nodes(path)
    rep = Report()
    rep.auto("外壳", path.name, f"{shell}；节点表逐字照抄进 workflow 块，一个值都不改")

    sampler_ids = [nid for nid in nodes if _cls(nodes, nid) in SAMPLERS]
    if not sampler_ids:
        raise Reject(f"整张图里找不到采样器（{'／'.join(SAMPLERS)}），认不出这是出图工作流")
    sids = set(sampler_ids)

    # 段序：先按「谁的 latent 来自谁」定深浅，同深再按节点号数字序（稳定、可复现）
    memo = {}
    order = sorted(sampler_ids, key=lambda n: (stage_depth(nodes, n, sids, memo),
                                               len(str(n)), str(n)))
    for i, sid in enumerate(order):
        rep.auto(f"段 stage{i + 1}", f"节点 {sid}（{_cls(nodes, sid)}）",
                 f"一个采样器＝一段；上游采样器数＝{sorted(sampler_deps(nodes, sid, sids)[0]) or '无'}，"
                 f"段深 {stage_depth(nodes, sid, sids, memo)}")

    # ---- 底模／CLIP／VAE 槽
    model_slots, seen_loader = [], set()
    for sid in order:
        _, terminal, why = model_chain(nodes, sid)
        if terminal is None:
            rep.manual("底模链", f"段 {sid}", why)
            continue
        if terminal in seen_loader:
            continue
        seen_loader.add(terminal)
        inp = UNET_LOADERS[_cls(nodes, terminal)]
        role = "unet"
        model_slots.append({"role": role, "node": terminal, "input": inp,
                            "family": family or "", "label": "底模"})
        rep.auto(f"model_slots·{role}", f"节点 {terminal}",
                 f"{_cls(nodes, terminal)} 的 {inp}＝{_inputs(nodes, terminal).get(inp)!r}")
        if _cls(nodes, terminal) == "CheckpointLoaderSimple":
            rep.manual("底模目录错配", f"节点 {terminal}",
                       "CheckpointLoaderSimple 的文件在 models/checkpoints，"
                       "而 MODEL_DIRS['unet']＝diffusion_models；role 仍写 unet 以复用现有下拉，"
                       "目录怎么指留给 §2 定，这里不猜")
    for cls, inps in list(CLIP_LOADERS.items()) + list(VAE_LOADERS.items()):
        role = "clip" if cls in CLIP_LOADERS else "vae"
        for nid in nodes:
            if _cls(nodes, nid) != cls:
                continue
            present = [i for i in inps if i in _inputs(nodes, nid)]
            if not present:
                rep.manual(f"model_slots·{role}", f"节点 {nid}", f"{cls} 上没有 {inps} 任何一个输入")
                continue
            if len(present) > 1:
                rep.manual(f"model_slots·{role}", f"节点 {nid}",
                           f"{cls} 有 {len(present)} 个编码器输入（{present}），"
                           "画舫一个角色只有一个槽，不敢替你挑")
                continue
            model_slots.append({"role": role, "node": nid, "input": present[0],
                                "family": family or "",
                                "label": "文本编码器" if role == "clip" else "VAE"})
            rep.auto(f"model_slots·{role}", f"节点 {nid}",
                     f"{cls} 的 {present[0]}＝{_inputs(nodes, nid).get(present[0])!r}")

    # ---- 笔槽：按段的链序登记，同一支笔覆盖多段就并到一条
    lora_slots, lora_by_node = [], {}
    for i, sid in enumerate(order):
        loras, _, _ = model_chain(nodes, sid)
        stage_id = f"stage{i + 1}"
        for nid in loras:
            name_in, str_in, clip_in = LORA_LOADERS[_cls(nodes, nid)]
            if nid in lora_by_node:
                lora_by_node[nid]["stages"].append(stage_id)
                continue
            slot = {"role": f"lora{len(lora_slots) + 1}", "node": nid, "name_input": name_in,
                    "strength_input": str_in, "clip_strength_input": clip_in,
                    "stages": [stage_id]}
            lora_by_node[nid] = slot
            lora_slots.append(slot)
            rep.auto(f"笔槽 {slot['role']}", f"节点 {nid}",
                     f"{_cls(nodes, nid)} 的 {name_in}＝{_inputs(nodes, nid).get(name_in)!r}、"
                     f"{str_in}＝{_inputs(nodes, nid).get(str_in)!r}；"
                     f"链序＝从底模往采样器数第 {loras.index(nid) + 1} 个")
            if clip_in and clip_in not in _inputs(nodes, nid):
                rep.manual(f"笔槽 {slot['role']}·CLIP 强度", f"节点 {nid}",
                           f"{_cls(nodes, nid)} 上没有 {clip_in}，已置 null")
                slot["clip_strength_input"] = None
    for slot in lora_slots:
        if len(slot["stages"]) < len(order):
            rep.auto(f"笔槽 {slot['role']} 只覆盖部分段", f"节点 {slot['node']}",
                     f"覆盖 {slot['stages']}（D78：正常，界面要说清哪段无笔）")

    # ---- 尺寸关系：全局＝最终成品尺寸；每段尺寸节点存的是**倍率**不是字面值（主人裁定 2026-10-28）
    size_nodes = {}
    stages = []
    for i, sid in enumerate(order):
        stage_id = f"stage{i + 1}"
        loras, terminal, why = model_chain(nodes, sid)
        lat_inp = next((n for n in LATENT_INPUTS if _src(_inputs(nodes, sid), n)), None)
        role_of = {s["node"]: s["role"] for s in model_slots}
        st = {"id": stage_id, "label": "", "model_role": role_of.get(terminal, "unet"),
              "lora_roles": [lora_by_node[n]["role"] for n in loras if n in lora_by_node],
              "links": {}, "params": {}, "prompt": {}}
        if why:
            rep.manual(f"{stage_id}·底模链", f"节点 {sid}", why)
        st["links"]["model"] = {"node": sid, "input": "model"}
        for key in ("positive", "negative"):
            if _src(_inputs(nodes, sid), key):
                st["links"][key] = {"node": sid, "input": key}
            else:
                rep.manual(f"{stage_id}·{key} 连线", f"节点 {sid}",
                           f"{_cls(nodes, sid)} 上没有 {key} 输入或它不是连线")
        if lat_inp:
            st["links"]["latent"] = {"node": sid, "input": lat_inp}
        else:
            rep.manual(f"{stage_id}·latent 连线", f"节点 {sid}", "找不到 latent_image／latent 输入")

        for key in STAGE_PARAMS:
            if key in _inputs(nodes, sid):
                st["params"][key] = {"node": sid, "input": key}
        missing = [k for k in STAGE_PARAMS if k not in st["params"]]
        if missing:
            rep.manual(f"{stage_id}·缺参数", f"节点 {sid}",
                       f"{_cls(nodes, sid)} 上没有 {'、'.join(missing)}；不替它编")

        # 正负词：只认「采样器的 positive/negative 口往回第一个 CLIPTextEncode」
        for key, field in (("positive", "positive_source"), ("negative", "negative_source")):
            src = _src(_inputs(nodes, sid), key)
            st["prompt"][field] = []
            if src is None:
                continue
            if _cls(nodes, src) == TEXT_ENCODE and "text" in _inputs(nodes, src):
                st["prompt"][field] = [{"node": src, "input": "text"}]
                rep.auto(f"{stage_id}·{'正词' if key == 'positive' else '负词'}", f"节点 {src}",
                         f"采样器 {sid} 的 {key} 口直接接 {TEXT_ENCODE}，取它的 text")
                continue
            hits = [n for n in walk_back(nodes, (sid, key), stop_classes=SAMPLERS)
                    if _cls(nodes, n) == TEXT_ENCODE and "text" in _inputs(nodes, n)]
            if len(hits) == 1:
                st["prompt"][field] = [{"node": hits[0], "input": "text"}]
                rep.auto(f"{stage_id}·{'正词' if key == 'positive' else '负词'}", f"节点 {hits[0]}",
                         f"采样器 {sid} 的 {key} 口往回只有唯一一个 {TEXT_ENCODE}（隔着别的节点）")
            elif not hits:
                rep.manual(f"{stage_id}·{'正词' if key == 'positive' else '负词'}", f"节点 {src}",
                           f"{key} 口往回找不到 {TEXT_ENCODE}（尽头是 {_cls(nodes, src)}），留空让人修")
            else:
                rep.manual(f"{stage_id}·{'正词' if key == 'positive' else '负词'}", f"节点 {src}",
                           f"{key} 口往回撞到 {len(hits)} 个 {TEXT_ENCODE}（{hits}），"
                           "不知道界面该往哪个写，全留空")

        # 本段的 latent 尺寸源 与 成品尺寸源
        if lat_inp:
            lz, n_lz = nearest_of(nodes, (sid, lat_inp), set(LATENT_SOURCES))
            sc, n_sc = nearest_of(nodes, (sid, lat_inp), set(SCALE_NODES))
            if lz:
                w_in, h_in, b_in = LATENT_SOURCES[_cls(nodes, lz)]
                st["latent_size"] = {"node": lz, "width_input": w_in, "height_input": h_in,
                                     "batch_input": b_in if b_in in _inputs(nodes, lz) else None}
                size_nodes[lz] = st["latent_size"]
                rep.auto(f"{stage_id}·latent 尺寸源", f"节点 {lz}",
                         f"latent 往回最近的 {_cls(nodes, lz)}＝{_int(_inputs(nodes, lz).get(w_in))}"
                         f"×{_int(_inputs(nodes, lz).get(h_in))}"
                         + (f"（同类候选 {n_lz} 个，取最近）" if n_lz > 1 else ""))
                if n_lz > 1:
                    rep.manual(f"{stage_id}·latent 尺寸源不唯一", f"节点 {lz}",
                               f"往上撞到 {n_lz} 个空 latent，取了最近的一个，请人工确认")
                if st["latent_size"]["batch_input"] is None:
                    rep.manual(f"{stage_id}·张数槽", f"节点 {lz}",
                               f"{_cls(nodes, lz)} 上没有 batch_size，这一段的张数没法接管")
            if sc:
                w_in, h_in = SCALE_NODES[_cls(nodes, sc)]
                st["size_out"] = {"node": sc, "width_input": w_in, "height_input": h_in}
                size_nodes[sc] = st["size_out"]
                rep.auto(f"{stage_id}·成品尺寸源", f"节点 {sc}",
                         f"latent 往回最近的 {_cls(nodes, sc)}＝{_int(_inputs(nodes, sc).get(w_in))}"
                         f"×{_int(_inputs(nodes, sc).get(h_in))}"
                         + (f"（同类候选 {n_sc} 个，取最近）" if n_sc > 1 else ""))
                st["hires"] = True
            for nid in walk_back(nodes, (sid, lat_inp), stop_classes=SAMPLERS):
                if _cls(nodes, nid) in RATIO_UPSCALERS:
                    st["hires"] = True
                    rep.manual(f"{stage_id}·倍率型放大", f"节点 {nid}",
                               f"{_cls(nodes, nid)} 用的是倍率不是绝对宽／高，"
                               "画舫的尺寸槽只认绝对尺寸（size_out＋size_scale）；"
                               "这段标了 hires 让界面说得出「是放大段」，但尺寸规则留给人工")
        stages.append(st)

    # ---- 全局尺寸＝最后一段的成品尺寸源，没有就退到它的 latent 尺寸源
    last = stages[-1]
    g_node = last.get("size_out") or last.get("latent_size")
    global_wh = None
    if g_node:
        gw = _int(_inputs(nodes, g_node["node"]).get(g_node["width_input"]))
        gh = _int(_inputs(nodes, g_node["node"]).get(g_node["height_input"]))
        if gw and gh:
            global_wh = (gw, gh)
    if not global_wh:
        rep.manual("全局尺寸", "整张图",
                   "找不到最终成品的尺寸来源（最后一段既没有缩放节点也没有空 latent），"
                   "defaults 的 width/height 留空，请人工填")

    # ---- 倍率：每个尺寸节点写「全局 × 倍率」，绝不冻结字面值。
    # 倍率正好是 1 就整个键不写：缺键＝1.0，这样内置三条 profile 能被逐字复刻（零改动兼容）。
    # 算不出倍率时同样不写键（写 None 会让 runner 的 `get(…, [1.0,1.0])` 拿到 None 而崩），
    # 靠下面的 manual 条目把人叫来修——认不准宁可留空，绝不塞个假倍率进去。
    for nid, slot in size_nodes.items():
        if not global_wh:
            continue
        w = _int(_inputs(nodes, nid).get(slot["width_input"]))
        h = _int(_inputs(nodes, nid).get(slot["height_input"]))
        if not w or not h:
            rep.manual("尺寸倍率", f"节点 {nid}",
                       "这个节点上的宽／高不是正整数，算不出倍率；契约会当成 1.0（＝照全局尺寸写），"
                       "放大关系会丢，请人工补 size_scale")
            continue
        scale = [round(w / global_wh[0], 6), round(h / global_wh[1], 6)]
        if scale == [1.0, 1.0]:
            rep.auto("尺寸倍率", f"节点 {nid}",
                     f"原值 {w}×{h} 就等于全局尺寸 → 倍率 1.0，键不写（缺键＝1.0，与内置三条同款）")
            continue
        slot["size_scale"] = scale
        rep.auto("尺寸倍率", f"节点 {nid}",
                 f"原值 {w}×{h} ÷ 全局 {global_wh[0]}×{global_wh[1]}＝{scale[0]}×{scale[1]}"
                 "（界面改全局尺寸时按倍率重算，放大关系不破）")

    # ---- 段标签
    # `hires` 标记只在「倍率型放大、没有 size_out」时才留着——那种形状契约表达不了尺寸规则，
    # 得靠这个标记让界面说得出「这段是放大段」。有 size_out 时标记是冗余的，删掉。
    for i, st in enumerate(stages):
        if len(stages) == 1:
            st["label"] = "直出"
        elif i == 0:
            st["label"] = "第一段·出图"
        else:
            st["label"] = f"第{_cn(i + 1)}段·" + (
                "精修放大" if st.get("size_out") or st.get("hires") else "续采样")
        if st.get("size_out"):
            st.pop("hires", None)

    # ---- SaveImage：接到最后一段的才是成品，其余是中间图（D92）
    save_slots = []
    last_sid = order[-1]
    for nid in nodes:
        if _cls(nodes, nid) != SAVE_IMAGE:
            continue
        ins = _inputs(nodes, nid)
        if "filename_prefix" not in ins:
            rep.manual("save_slots", f"节点 {nid}", "SaveImage 上没有 filename_prefix")
            continue
        img_src = _src(ins, "images")
        if img_src is None:
            rep.manual("save_slots", f"节点 {nid}", "SaveImage 的 images 不是连线，判不出成品／中间")
            save_slots.append({"node": nid, "prefix_input": "filename_prefix", "final": False})
            continue
        reached = trace_samplers(nodes, (nid, "images"), sids)
        final = last_sid in reached
        save_slots.append({"node": nid, "prefix_input": "filename_prefix", "final": final})
        rep.auto("save_slots·成品" if final else "save_slots·中间图", f"节点 {nid}",
                 f"images 往回撞到的采样器＝{sorted(reached) or '无'}，最后一段是 {last_sid}"
                 f"；prefix＝{ins.get('filename_prefix')!r}"
                 + ("" if final else "（中间图不上结果墙，D92）"))
        if not reached:
            rep.manual("save_slots·判不出成品", f"节点 {nid}",
                       "images 往回撞不到任何采样器（存的可能是输入图），先按中间图处理")

    # ---- 参考图输入：登记不暴露（卡 §1）
    image_slots = []
    for nid in nodes:
        if _cls(nodes, nid) != LOAD_IMAGE:
            continue
        ins = _inputs(nodes, nid)
        if "image" not in ins:
            rep.manual("image_slots", f"节点 {nid}", "LoadImage 上没有 image 输入")
            continue
        used_by = _consumers(nodes, nid)
        slot = {"role": f"ref{len(image_slots) + 1}", "node": nid, "input": "image",
                "label": "参考图", "exposed": False, "used_by": used_by}
        image_slots.append(slot)
        rep.auto(f"image_slots·{slot['role']}", f"节点 {nid}",
                 f"{LOAD_IMAGE} 的 image＝{ins.get('image')!r}；被 {used_by or '（没人用）'} 消费。"
                 "只登记不暴露到界面（卡 §1）")
        if not used_by:
            rep.manual(f"image_slots·{slot['role']} 没人用", f"节点 {nid}",
                       "这个 LoadImage 不接任何东西，导入后是死节点，请人工确认要不要删")

    # ---- 不被画舫接管的采样器输入：只登记，不动值
    keep = []
    for i, sid in enumerate(order):
        for name in sorted(_inputs(nodes, sid)):
            if name in STAGE_PARAMS or name in ("model", "positive", "negative") \
                    or name in LATENT_INPUTS:
                continue
            keep.append(f"stage{i + 1}.{name}")
            rep.auto("keep_workflow_value", f"节点 {sid}·{name}",
                     f"值＝{_inputs(nodes, sid).get(name)!r}；画舫不接管，提交时原样带走")

    # ---- defaults：逐段参数取第一段的原值，尺寸取全局（与内置三条同款六个键）
    defaults = {}
    for key in DEFAULT_KEYS:
        if key in stages[0]["params"]:
            v = _inputs(nodes, stages[0]["params"][key]["node"]).get(key)
            if v is not None:
                defaults[key] = v
    if global_wh:
        defaults["width"], defaults["height"] = global_wh
    missing = [k for k in DEFAULT_KEYS if k not in defaults]
    if missing:
        rep.manual("defaults 缺项", "第一段", f"{'、'.join(missing)} 取不到原值，界面会没有预填")
    if not global_wh:
        rep.manual("defaults 缺尺寸", "整张图", "width/height 没预填，界面会空着")

    wid = workflow_id or _slug(path.stem)
    profile = {
        "schema_version": 2,
        "workflow_id": wid,
        "label": label or wid,
        "family": family or "",
        "source_format": "api",
        "model_slots": model_slots,
        "lora_slots": lora_slots,
        "stages": stages,
        "save_slots": save_slots,
        "image_slots": image_slots,
        "params": {"per_stage": True, "keep_workflow_value": keep},
        "defaults": defaults,
        "notes": f"B 卡 §1 导入自 {path.name}：{len(stages)} 段、{len(lora_slots)} 笔槽、"
                 f"{len(save_slots)} 个存图口（成品 {sum(1 for s in save_slots if s['final'])} 个）",
    }
    if not family:
        rep.manual("family", "profile", "留空了：family 从节点类型／连线推不出来（只能靠底模文件名猜），"
                                        "要填请给 --family")
    return {"data": {"workflow": nodes, "profile": profile}, "report": rep,
            "meta": {"source": str(path), "shell": shell, "size": path.stat().st_size,
                     "mtime": path.stat().st_mtime, "global_wh": global_wh,
                     "stages": len(stages), "lora_slots": len(lora_slots),
                     "manual": len(rep.of("manual")), "id": wid}}


def _slug(stem: str) -> str:
    """文件名 → 合法 workflow_id：只留字母数字下划线，其余换下划线。"""
    out = "".join(c if c.isalnum() or c == "_" else "_" for c in stem).strip("_").lower()
    return out or "imported"


# ------------------------------------------------------------------ 报告

def confidence(meta, rep) -> str:
    m = meta["manual"]
    if m == 0:
        return "高（全自动认出，没有留空项）"
    if m <= 2:
        return f"中（{m} 处留空需人工，仍在卡 §5 的 ≤2 门槛内）"
    return f"低（{m} 处留空需人工，超出卡 §5 的 ≤2 门槛）"


def stage_table(profile) -> list:
    """§3 要按什么口径写回执：每段自己管哪些参数、尺寸槽在哪个节点、倍率多少、行数怎么加。"""
    L = ["## 三、段与尺寸：§3 回执／填值要按这张表走", "",
         "| 段 | 标签 | 逐段参数 | 尺寸槽（节点·倍率） | 张数槽 | 笔槽 |", "|---|---|---|---|---|---|"]
    for st in profile["stages"]:
        sizes = []
        for key in ("latent_size", "size_out"):
            s = st.get(key)
            if s:
                sc = s.get("size_scale", [1.0, 1.0])   # 缺键＝1.0；None＝算不出来
                sizes.append(f"{key}＝节点 {s['node']}·"
                             + (f"×{sc[0]}/×{sc[1]}" if sc else "**倍率没算出来**"))
        lz = st.get("latent_size") or {}
        L.append("| {id} | {label} | {params} | {sizes} | {batch} | {loras} |".format(
            id=st["id"], label=st["label"], params="、".join(sorted(st["params"])) or "（无）",
            sizes="；".join(sizes) or "（本段不接管尺寸）",
            batch=f"节点 {lz['node']}·{lz['batch_input']}" if lz.get("batch_input") else "（无）",
            loras="、".join(st["lora_roles"]) or "**此段无笔**"))

    n_model, n_lora = len(profile["model_slots"]), len(profile["lora_slots"])
    base = n_model + n_lora * 2
    L += ["", f"### 回执行数怎么加（基准：{n_model} 模型槽 ＋ {n_lora}×2 笔槽 ＝ {base} 行，与段数无关）", ""]
    total = base
    for i, st in enumerate(profile["stages"]):
        sends = UI_SENDS_STAGE1 if i == 0 else UI_SENDS_LATER
        n_param = sum(1 for k in sends if k in st["params"])
        n_size = 2 if (st.get("latent_size") or st.get("size_out")) else 0
        n_batch = 1 if (st.get("latent_size") or {}).get("batch_input") else 0
        total += n_param + n_size + n_batch
        L.append(f"- `{st['id']}`：{n_param} 逐段参数（界面会发的 {'、'.join(k for k in sends if k in st['params']) or '无'}）"
                 f" ＋ {n_size} 尺寸 ＋ {n_batch} 张数 ＝ **{n_param + n_size + n_batch} 行**")
    L += ["", f"- **合计 {total} 项。**",
          f"- 内置三条的基准是 13／15／17 项（1／2／3 笔槽）；这份是 {total} 项，"
          "差的是笔槽数与段数，**不是回归**。§3 的口径变更声明按这个数写。",
          "- ⚠ 岔路口（请 review 时定）：**第一段的 denoise 要不要给界面控件**。"
          "今天 `static/app.js` 的 `PARAM_FIELDS` 里没有 denoise，所以段1 只算 5 行、内置三条保持 13／15／17。"
          "若给段1 也加 denoise，内置三条会变成 14／16／18——那就动了 A 阶段验收过的基准。"
          "我的建议是**只给段2+ 加 denoise**（段1 的 denoise 本来就是 1.0 满生成，没有调它的理由），"
          "这样既有行一字不动，符合卡的禁止条款。", ""]
    return L


def render_report(meta, rep, out_rel, profile) -> str:
    L = [f"# B 卡 §1 映射报告：{meta['id']}", ""]
    L += [f"- 源文件：`{meta['source']}`（{meta['size']} B）",
          f"- 外壳：{meta['shell']}",
          f"- 产物：`{out_rel}`（**staging**，§5 考题过了才挪进 `workflows/`）",
          f"- 规模：{meta['stages']} 段／{meta['lora_slots']} 笔槽",
          (f"- 全局成品尺寸：{meta['global_wh'][0]}×{meta['global_wh'][1]}"
           if meta["global_wh"] else "- 全局成品尺寸：**没认出来**（见下）"),
          f"- 置信度：{confidence(meta, rep)}",
          f"- 人工修正处数：**{meta['manual']}**（卡 §5 门槛 ≤2）", ""]
    for title, kind, note in (
        ("一、自动识别（每项都给依据）", "auto", None),
        ("二、需人工修（一律留空，没有替你接线）", "manual",
         "铁律：认不准宁可留空让人修，绝不瞎猜接线。"),
    ):
        rows = rep.of(kind)
        L += [f"## {title}", ""]
        if note:
            L += [note, ""]
        if not rows:
            L += ["（无）", ""]
            continue
        L += ["| 认成什么 | 落在哪 | 依据 |", "|---|---|---|"]
        L += [f"| {r['item']} | {r['where']} | {r['why']} |" for r in rows]
        L += [""]
    L += stage_table(profile)
    return "\n".join(L)


def main(argv=None):
    # Windows 控制台默认 GBK：报告里的 ⚠ 会让 print 抛 UnicodeEncodeError，把整份报告吞掉。
    # 只换编不出的字符（落盘的报告文件仍是 UTF-8）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description="把 ComfyUI API 格式工作流导入成画舫的 {workflow, profile}")
    ap.add_argument("source", help="ComfyUI「Save (API Format)」导出的 .json")
    ap.add_argument("--id", help="workflow_id（不给就用文件名）")
    ap.add_argument("--label", help="界面下拉里显示的名字")
    ap.add_argument("--family", help="模型家族（推不出来，只能人给）")
    ap.add_argument("--out", help="产物路径（默认 workflows/_staging/<id>.json）")
    ap.add_argument("--report", help="报告路径（默认 acceptance/B-map-<id>.md）")
    ap.add_argument("--dry-run", action="store_true", help="只打报告，不落盘")
    a = ap.parse_args(argv)

    try:
        res = import_workflow(a.source, a.id, a.label, a.family)
    except Reject as e:
        print(f"导入不了：{e}", file=sys.stderr)
        return 2
    meta, rep = res["meta"], res["report"]
    out = Path(a.out) if a.out else STAGING / f"{meta['id']}.json"
    rpt = Path(a.report) if a.report else REPORTS / f"B-map-{meta['id']}.md"
    try:
        rel = out.relative_to(ROOT).as_posix()
    except ValueError:
        rel = str(out)
    text = render_report(meta, rep, rel, res["data"]["profile"])
    if a.dry_run:
        print(text)
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    rpt.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res["data"], ensure_ascii=False, indent=2), encoding="utf-8")
    rpt.write_text(text, encoding="utf-8")
    print(f"产物：{out}\n报告：{rpt}\n人工修正 {meta['manual']} 处，置信度{confidence(meta, rep)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
