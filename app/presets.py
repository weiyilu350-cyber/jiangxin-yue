"""画舫 T002：预设＝配方（recipe）＋工作流覆盖（override），D98。

配方不绑工作流；切工作流时只用该工作流的 override 补差异。
本模块只碰 `data/presets.json`，不认节点 ID、不碰 ComfyUI。
"""
import copy
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

DEFAULT_ID = "r_default"
PARAM_KEYS = ("width", "height", "steps", "cfg", "seed", "sampler_name", "scheduler")
_ID_RE = re.compile(r"r_[0-9a-f]{8}")


class PresetError(Exception):
    """给界面看的预设错误：code 定 HTTP 状态，human 说人话（D96）。"""

    def __init__(self, code: str, human: str):
        super().__init__(human)
        self.code = code
        self.human = human


def empty_table() -> dict:
    return {"recipes": [], "overrides": {}}


def load_presets(path) -> dict:
    """读表：缺失/损坏/半截 JSON 一律当空表，且绝不改写用户已有文件。"""
    path = Path(path)
    if not path.exists():
        return empty_table()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_table()
    if not isinstance(data, dict):
        return empty_table()
    recipes = data.get("recipes")
    overrides = data.get("overrides")
    return {
        "recipes": [r for r in recipes if isinstance(r, dict)] if isinstance(recipes, list) else [],
        "overrides": overrides if isinstance(overrides, dict) else {},
    }


def save_presets(path, data: dict) -> None:
    """写表：先把旧文件备份成 .bak，再临时文件＋原子替换，避免写一半毁全表。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def gen_recipe_id() -> str:
    return "r_" + secrets.token_hex(4)


def valid_id(rid) -> bool:
    """只认后端自己发的 id 形状；路径片段一律不算（雷区 9）。"""
    return bool(isinstance(rid, str) and (rid == DEFAULT_ID or _ID_RE.fullmatch(rid)))


def norm_name(name) -> str:
    return (name or "").strip().casefold()


def find_by_id(recipes, rid):
    for r in recipes or []:
        if r.get("id") == rid:
            return r
    return None


def default_recipe(settings: dict, profile_defaults: dict = None) -> dict:
    """内置「默认」配方：值＝settings 默认底模＋当前工作流默认参数，保证一键出图（D96）。"""
    params = {"width": 832, "height": 1216, "steps": 30, "cfg": 4.0,
              "seed": -1, "sampler_name": "euler", "scheduler": "simple"}
    params.update({k: v for k, v in (profile_defaults or {}).items() if k in params})
    return {
        "id": DEFAULT_ID,
        "name": "默认",
        "unet": settings.get("default_unet", ""),
        "loras": [],
        "positive": "",
        "negative": "",
        "params": params,
        "created_at": "",
    }


def _clean_loras(loras) -> list:
    """只存真笔：空槽（None/空名）不进配方，槽位顺序保留（D115 有序笔列表）。"""
    out = []
    for pen in loras or []:
        if not isinstance(pen, dict):
            continue
        name = (pen.get("name") or "").strip()
        if not name:
            continue
        try:
            strength = float(pen.get("strength", 0) or 0)
        except (TypeError, ValueError):
            strength = 0.0
        out.append({"name": name, "strength": strength})
    return out


def _clean_params(params) -> dict:
    src = params if isinstance(params, dict) else {}
    return {k: src[k] for k in PARAM_KEYS if k in src}


def add_recipe(data: dict, name, unet, loras, positive, negative, params,
               reserved_names=()) -> dict:
    """存一支配方。空名/同名直接报错，不静默覆盖（雷区 2）。"""
    key = norm_name(name)
    if not key:
        raise PresetError("empty_name", "预设得有个名字")
    taken = {norm_name(r.get("name")) for r in data.get("recipes", [])}
    taken |= {norm_name(n) for n in reserved_names}
    if key in taken:
        raise PresetError("dup_name", f"已经有一支配方叫「{(name or '').strip()}」了，换个名字吧")
    recipes = data.setdefault("recipes", [])
    rid = gen_recipe_id()
    while find_by_id(recipes, rid):
        rid = gen_recipe_id()
    recipe = {
        "id": rid,
        "name": (name or "").strip(),
        "unet": (unet or "").strip(),
        "loras": _clean_loras(loras),
        "positive": positive or "",
        "negative": negative or "",
        "params": _clean_params(params),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    recipes.append(recipe)
    return recipe


def delete_recipe(data: dict, rid, protected_ids=(DEFAULT_ID,)) -> bool:
    """删配方，连带删掉所有 profile 下它的 override，不留孤儿（雷区 6）。"""
    if rid in (protected_ids or ()):
        raise PresetError("protected", "内置的「默认」配方不能删")
    recipes = data.get("recipes", [])
    hit = find_by_id(recipes, rid)
    if not hit:
        return False
    recipes.remove(hit)
    overrides = data.get("overrides", {})
    for pid in list(overrides):
        bucket = overrides.get(pid)
        if isinstance(bucket, dict):
            bucket.pop(rid, None)
            if not bucket:
                overrides.pop(pid, None)
    return True


def merge_override(recipe: dict, overrides: dict, profile_id: str) -> dict:
    """叠加某工作流的差异值：只覆盖出现的键，字典逐键合并、列表整段替换（雷区 3）。"""
    merged = copy.deepcopy(recipe)
    bucket = (overrides or {}).get(profile_id) or {}
    over = bucket.get(recipe.get("id"))
    if not isinstance(over, dict):
        return merged
    for key, val in over.items():
        if key == "id":
            continue
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key].update(val)
        else:
            merged[key] = val
    return merged


def compatibility(recipe: dict, lora_slots: int, unet_list) -> tuple:
    """返回 (usable, warnings)：笔数超槽或底模不在盘上都不算可用（雷区 5，D65 不静默丢槽）。"""
    warnings = []
    pens = len(recipe.get("loras") or [])
    if pens > lora_slots:
        warnings.append(f"这支配方有 {pens} 支笔，当前工作流只有 {lora_slots} 槽")
    unet = recipe.get("unet") or ""
    if unet and unet not in (unet_list or []):
        warnings.append(f"底模 {unet} 现在不在模型目录里")
    return (not warnings), warnings


def build_load(recipe: dict, overrides: dict, profile_id: str, mode: str,
               lora_slots: int, unet_list) -> dict:
    """算出载入回填包。不兼容就报错，绝不「载入了但少了笔」（雷区 7）。"""
    if mode not in ("full", "params"):
        raise PresetError("bad_mode", "载入只认「整套载入」和「只换参数＋触发词」两种")
    merged = merge_override(recipe, overrides, profile_id)
    usable, warnings = compatibility(merged, lora_slots, unet_list)
    if not usable:
        raise PresetError("incompatible", "；".join(warnings))
    out = {
        "mode": mode,
        "positive": merged.get("positive", ""),
        "negative": merged.get("negative", ""),
        "params": dict(merged.get("params") or {}),
    }
    if mode == "full":
        out["unet"] = merged.get("unet", "")
        out["loras"] = copy.deepcopy(merged.get("loras") or [])
    return out


def list_presets(data: dict, default_recipe_item: dict, profile_id: str,
                 lora_slots: int, unet_list, show_all: bool = False) -> list:
    """默认只列当前工作流可用的；勾「显示全部配方」才带不兼容项＋warning（雷区 8）。"""
    overrides = data.get("overrides", {})
    items = []
    for r in [default_recipe_item] + list(data.get("recipes", [])):
        merged = merge_override(r, overrides, profile_id)
        usable, warnings = compatibility(merged, lora_slots, unet_list)
        if not usable and not show_all:
            continue
        items.append({
            "id": r.get("id"),
            "name": r.get("name", ""),
            "builtin": r.get("id") == DEFAULT_ID,
            "unet": r.get("unet", ""),
            "loras": copy.deepcopy(r.get("loras") or []),
            "positive": r.get("positive", ""),
            "negative": r.get("negative", ""),
            "params": dict(r.get("params") or {}),
            "pens": len(r.get("loras") or []),
            "usable": usable,
            "warnings": warnings,
        })
    return items
