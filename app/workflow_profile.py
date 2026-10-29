"""profile 载入与契约检查（D31）：让错误在开发期暴露，不糊到主人脸上。

B 卡 §2 起契约管两件事：
1. **两级结果（D76）**：`errors` ＝ 这份 profile 不能用；`warnings` ＝ 能用，但界面必须
   跟主人说清楚（典型：双模型接力时「第二段无笔」）。`validate()` 仍只返 errors，
   A 阶段所有 `== []` 的断言口径不变。
2. **尺寸倍率（主人裁定 2026-10-28）**：尺寸槽存的是「倍率」不是「字面值」。
   `size_scale` ＝ 这个节点的原始宽／高 ÷ 全局成品尺寸，语义写死为
   **段1 latent＝全局×0.5、段2 成品＝段1×2＝全局×1.0**；缺键＝倍率 1.0（内置三条就是这种）。
   所以主人把全局从 832×1216 改成 1024×1536 时，段1 跟着变 512×768、段2 变 1024×1536，
   2× 关系不会被冻在 416→832 那组字面值上。换算规则只有 `scaled_size()` 一处实现。
"""
import json
from pathlib import Path

# 底模链的合法尽头：UNETLoader 只出底模（内置三条），CheckpointLoaderSimple 把
# 底模／TE／VAE 打在一个包里（通用工作流常见）。只认前者会把后者一律判成「链断了」。
UNET_TERMINALS = {"UNETLoader", "CheckpointLoaderSimple"}
# 笔加载器两型：ModelOnly 只喂底模，LoraLoader 还多一路 clip 强度。链上两种都算笔。
LORA_CLASSES = {"LoraLoaderModelOnly", "LoraLoader"}

SIZE_ALIGN = 8        # latent 空间对 8 的硬要求；倍率换算后按它对齐


class ProfileError(Exception):
    pass


def load_pair(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["workflow"], data["profile"]


def stage_hires(st: dict) -> bool:
    """这段是不是「精修放大」：有独立成品尺寸节点（size_out），或导入工具认出了倍率型放大器。"""
    return bool(st.get("size_out")) or bool(st.get("hires"))


def derive_capabilities(profile: dict) -> dict:
    """按结构推导能力位，前端据此决定画不画段卡、开几个笔槽（B 卡 §2）。
    `negative_prompt` 仍写死 True：卡 §2 没列它，推导它＝改 A 阶段验过的语义。"""
    stages = profile.get("stages", [])
    return {
        "lora_max": len(profile.get("lora_slots", [])),
        "stages": len(stages),
        "negative_prompt": True,
        "reference_images": len(profile.get("image_slots") or []),
        "hires": any(stage_hires(st) for st in stages),
        "multi_model": len({s.get("model_role") for s in stages}) > 1,
    }


def _align(n) -> int:
    """按 8 对齐并夹住下限：倍率算出 3 像素这种值写进 latent 节点会让炉子直接报错。"""
    return max(SIZE_ALIGN, int(round(n / SIZE_ALIGN)) * SIZE_ALIGN)


def scaled_size(slot: dict, width, height) -> tuple:
    """把全局成品尺寸换算成这个尺寸槽该写的 (宽, 高)。倍率语义见模块 docstring。

    倍率 1.0（缺键）时**原样返回**、一个像素都不动——内置三条走的就是这条路，
    所以 A 阶段验过的 832×1216 直出行为逐字不变。
    """
    scale = slot.get("size_scale") or [1.0, 1.0]
    if list(scale) == [1.0, 1.0]:
        return int(width), int(height)
    return _align(width * scale[0]), _align(height * scale[1])


def covers(ls: dict, stage_id: str) -> bool:
    """这支笔管不管这一段。缺 `stages` 键＝管全部段（内置三条没写这个键，口径不变）。"""
    stages = ls.get("stages")
    return stage_id in stages if stages else True


# 段卡上能调的参数与它们的排列顺序（B 卡 §4）。导入工具的词表就这几个（`STAGE_PARAMS`），
# 所以界面不必为「不认识的参数」准备控件：不在这里的＝照工作流原值走，界面上不出现。
UI_STAGE_PARAMS = ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise")


def stage_ui(workflow: dict, profile: dict) -> dict:
    """前端画段卡要的一份「段视图」（B 卡 §4）：只出结构与字面值，不出节点 ID（D83）。

    - `stages[i].params`：这段界面上能调哪些，按 `UI_STAGE_PARAMS` 的顺序。
      **第一段不含 denoise**——主人裁定 2026-10-28（B 卡裁定第 6 款）：段1 是满生成，
      给它控件就多一行回执，A 阶段的 13 项基线当场破。这条钉在后端而不是前端，
      因为「回执行数」的口径归后端管。
    - `stages[i].defaults`：取工作流里那个节点的字面值。导入时炉子导出成什么样、
      界面就预填什么样，任何导入的工作流都「打开就能出图」（D96 ≤3 次点击）。
    - `lora_stages`：与 bootstrap 的 `lora_slots`（个数）平行的数组，第 i 项＝第 i 支笔
      管哪些段的 id。缺 `stages` 键＝管全部段，这里就地摊开，前端不必再懂这条口径。
    """
    stages = profile.get("stages", [])
    ids = [st["id"] for st in stages]
    lora_slots = profile.get("lora_slots", [])
    view = []
    for i, st in enumerate(stages):
        declared = st.get("params", {})
        names = [k for k in UI_STAGE_PARAMS
                 if k in declared and not (i == 0 and k == "denoise")]
        defaults = {}
        for k in names:
            ref = declared[k]
            inputs = (workflow.get(ref["node"]) or {}).get("inputs") or {}
            if ref["input"] in inputs:
                defaults[k] = inputs[ref["input"]]
        view.append({
            "id": st["id"],
            "label": st.get("label", st["id"]),
            "params": names,
            "defaults": defaults,
            "pen_roles": [ls["role"] for ls in lora_slots if covers(ls, st["id"])],
        })
    return {
        "stages": view,
        "lora_stages": [ids if not ls.get("stages") else list(ls["stages"])
                        for ls in lora_slots],
    }


def _has(nodes, node, inp) -> bool:
    return node in nodes and inp in nodes[node].get("inputs", {})


def _model_chain(nodes, st: dict, errors: list):
    """沿 model 链往回走到底模加载器，返回 (链上的笔节点集合, 走通了没)。

    走不通时把人话原因写进 errors 并返回「没走通」——不让调用方再拿半截链去比对，
    否则会顺带报一条「链上笔与声明不一致」的假错，把真原因盖掉。
    """
    m = st.get("links", {}).get("model")
    if not m:
        errors.append(f"stage {st['id']}: 没声明 model 连线，查不了底模链"
                      "（导入工具认不准时会留空，请人工补）")
        return set(), False
    cur, first, chain = m["node"], True, []
    while cur is not None and cur not in chain:
        chain.append(cur)
        node = nodes.get(cur)
        if node is None:
            errors.append(f"stage {st['id']}: model 链上节点 {cur} 不存在")
            return set(), False
        if node.get("class_type") in UNET_TERMINALS:
            return {n for n in chain if nodes[n].get("class_type") in LORA_CLASSES}, True
        src = node.get("inputs", {}).get(m["input"] if first else "model")
        first = False
        cur = src[0] if isinstance(src, list) else None
    errors.append(f"stage {st['id']}: model 链没落到底模加载器"
                  "（UNETLoader／CheckpointLoaderSimple），可能是环或断链")
    return set(), False


def _check_size_slot(nodes, st: dict, label: str, slot: dict, errors: list):
    """尺寸槽（latent_size／size_out）通用查法：宽／高输入在不在、倍率是不是两个正数。"""
    for inp in (slot["width_input"], slot["height_input"]):
        if not _has(nodes, slot["node"], inp):
            errors.append(f"stage {st['id']} {label}: 节点 {slot['node']} 缺输入 {inp}")
    scale = slot.get("size_scale")
    if scale is None:
        return          # 缺键＝倍率 1.0（照全局尺寸写），这是合法且最常见的形状
    ok = (isinstance(scale, (list, tuple)) and len(scale) == 2
          and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0
                  for x in scale))
    if not ok:
        errors.append(f"stage {st['id']} {label}: size_scale 得是两个正数（如 [0.5, 0.5]），"
                      f"现在是 {scale!r}")


def validate_full(workflow: dict, profile: dict, exist_fn=None) -> tuple:
    """契约检查，返回 `(errors, warnings)` 两级（D76）。

    查的东西：节点／输入存在、段参数与连线可写、底模链落到底模加载器、链上笔与声明一致、
    笔覆盖的段真存在、尺寸槽与倍率合法、成品保存口在、模型文件在盘上（给了 exist_fn 才查）。
    """
    errors, warnings = [], []
    nodes = workflow
    stages = profile.get("stages", [])
    lora_slots = profile.get("lora_slots", [])
    stage_ids = {st["id"] for st in stages}

    for slot in profile.get("model_slots", []):
        if not _has(nodes, slot["node"], slot["input"]):
            errors.append(f"model_slots {slot['role']}: 节点 {slot['node']} 缺输入 {slot['input']}")

    for ls in lora_slots:
        if not _has(nodes, ls["node"], ls["name_input"]) or not _has(
            nodes, ls["node"], ls["strength_input"]
        ):
            errors.append(f"lora_slots {ls['role']}: 节点 {ls['node']} 缺 name/strength 输入")
        for sid in ls.get("stages") or []:
            if sid not in stage_ids:
                errors.append(f"lora_slots {ls['role']}: stages 里写了不存在的段 {sid}")

    for st in stages:
        for key, ref in st.get("params", {}).items():
            if not _has(nodes, ref["node"], ref["input"]):
                errors.append(f"stage {st['id']} 参数 {key}: 节点 {ref['node']} 缺输入 {ref['input']}")
        for key, ref in st.get("links", {}).items():
            if not _has(nodes, ref["node"], ref["input"]):
                errors.append(f"stage {st['id']} 连线 {key}: 节点 {ref['node']} 缺输入 {ref['input']}")

        on_chain, walked = _model_chain(nodes, st, errors)
        declared = {ls["node"] for ls in lora_slots if covers(ls, st["id"])}
        if walked:
            if on_chain != declared:
                errors.append(
                    f"stage {st['id']}: 链上 LoRA 节点 {sorted(on_chain)} "
                    f"与 profile 声明 {sorted(declared)} 不一致"
                )
            # D78：一支笔只覆盖部分段＝双模型接力的正常形状，不是错；但界面必须说得出「此段无笔」
            elif not declared and len(stages) > 1:
                warnings.append(f"stage {st['id']}（{st.get('label', '')}）: 此段无笔")

        for src in st.get("prompt", {}).get("positive_source", []) + st.get(
            "prompt", {}
        ).get("negative_source", []):
            if not _has(nodes, src["node"], src["input"]):
                errors.append(f"stage {st['id']}: 提示词源节点 {src['node']} 缺输入 {src['input']}")
        if st.get("latent_size"):
            _check_size_slot(nodes, st, "latent 尺寸源", st["latent_size"], errors)
        if st.get("size_out"):
            _check_size_slot(nodes, st, "成品尺寸源", st["size_out"], errors)
        lz, so = st.get("latent_size"), st.get("size_out")
        if lz and so and lz["node"] == so["node"]:
            errors.append(f"stage {st['id']}: latent 尺寸源与成品尺寸源是同一个节点 "
                          f"{lz['node']}，必有一个认错了")

    for ss in profile.get("save_slots", []):
        if not _has(nodes, ss["node"], ss["prefix_input"]):
            errors.append(f"save_slots: 节点 {ss['node']} 缺 {ss['prefix_input']}")

    if exist_fn:
        for slot in profile.get("model_slots", []):
            name = nodes[slot["node"]]["inputs"][slot["input"]]
            if not exist_fn(slot["role"], name):
                errors.append(f"模型文件不存在: {name}（{slot['role']}）")
        for ls in lora_slots:
            name = nodes[ls["node"]]["inputs"][ls["name_input"]]
            if not exist_fn("lora", name):
                errors.append(f"笔文件不存在: {name}")

    return errors, warnings


def validate(workflow: dict, profile: dict, exist_fn=None) -> list:
    """只看 errors（A 阶段口径）：调用方拿它判「这份 profile 能不能用」。warnings 走 validate_full。"""
    return validate_full(workflow, profile, exist_fn=exist_fn)[0]
