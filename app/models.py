"""画舫 T003：模型中心（MVP）——只读扫描 ComfyUI 模型目录 ＋ 触发词人工覆盖表。

覆盖表落在 `data/trigger_overrides.json`，形状 `{"<笔名>": "<触发词>"}`；
空串＝这支笔明确无触发词（D20），不是「没有覆盖」。
读写口径与 `presets.py` 同法：读绝不改写用户文件（缺失/损坏/半截 JSON 一律当空表），
写先把旧的备份成 `.bak`，再临时文件＋原子替换。
"""
import json
import os
from pathlib import Path

from . import settings as ST

MODELS_ROOT = ST.models_root()
MODEL_DIRS = {"unet": "diffusion_models", "clip": "text_encoders", "vae": "vae", "lora": "loras"}
SUFFIX = ".safetensors"
MAX_TRIGGER = 512


def normalize_name(name) -> str:
    """笔名归一：大小写、`.safetensors` 后缀、前后空格都算同一支笔（雷区 3）。"""
    if not isinstance(name, str):
        return ""
    key = name.strip().casefold()
    return key[: -len(SUFFIX)] if key.endswith(SUFFIX) else key


def load_overrides(path) -> dict:
    """读覆盖表：缺失/损坏/半截 JSON/不是字典 → 空表，且绝不动盘上那份（雷区 2）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, val in data.items():
        pen = normalize_name(key)
        # 只收字符串值：空串是合法覆盖（明确无触发词），None/数字之类当没写过
        if pen and isinstance(val, str):
            out[pen] = val
    return out


def save_overrides(path, data: dict) -> None:
    """写覆盖表：先备份 `.bak` 再原子替换，避免写一半毁掉主人手填的表。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def put_override(path, name: str, value: str) -> dict:
    """存一支笔的覆盖；键用归一后的笔名，免得同一支笔在表里占两条。"""
    data = load_overrides(path)
    data[normalize_name(name)] = value
    save_overrides(path, data)
    return data


def drop_override(path, name: str) -> dict:
    """清除覆盖：没这条也不算错（幂等），而且不白写一次盘。"""
    data = load_overrides(path)
    pen = normalize_name(name)
    if pen not in data:
        return data
    data.pop(pen)
    save_overrides(path, data)
    return data


def _files(role: str, root) -> list:
    """某角色目录下的 *.safetensors；目录不在就当空，不抛。

    按文件名字符串排序（不是按 Path）：Windows 下 Path 比较不分大小写，
    会把 A1 时界面下拉框的顺序改掉（回归保护）。
    """
    d = Path(root) / MODEL_DIRS[role]
    if not d.exists():
        return []
    return sorted((p for p in d.glob("*" + SUFFIX) if p.is_file()), key=lambda p: p.name)


def model_names(role: str, root=MODELS_ROOT) -> list:
    return [p.name for p in _files(role, root)]


def find_pen(name, root=MODELS_ROOT):
    """按归一笔名对回盘上真名；对不上返回 None（API 据此 404）。"""
    key = normalize_name(name)
    if not key:
        return None
    for p in _files("lora", root):
        if normalize_name(p.name) == key:
            return p.name
    return None


def scan_models(root=MODELS_ROOT, resolver=None) -> dict:
    """只读扫描：只 stat，不下载、不移动、不写、不改 mtime（雷区 1）。

    给了 resolver 时，笔另带触发词三元组；底模/TE/VAE 只列名称与大小（绑定后置）。
    """
    out = {}
    for role in MODEL_DIRS:
        items = []
        for p in _files(role, root):
            try:
                st = p.stat()
            except OSError:
                continue  # 扫的当口文件被搬走：跳过这一项，不整盘失败
            item = {"name": p.name, "size": st.st_size, "mtime": st.st_mtime}
            if role == "lora" and resolver is not None:
                item["trigger"] = resolver.details(p.name)
            items.append(item)
        out[role] = items
    return out
