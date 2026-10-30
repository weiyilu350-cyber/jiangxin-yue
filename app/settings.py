"""画舫设置：data/settings.json 读写；启动 token 每次点火重新生成。"""
import json
import logging
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
WORKFLOWS = ROOT / "workflows"
STATIC = ROOT / "static"
SETTINGS_FILE = DATA / "settings.json"
PRESETS_FILE = DATA / "presets.json"
TOKEN_FILE = DATA / "token.txt"
# 触发词人工覆盖表（T003）；形状 {"<笔名>": "<触发词>"}，空串＝明确无触发词
TRIGGER_OVERRIDES_FILE = DATA / "trigger_overrides.json"
# 背景图目录（T007）：主人自己往里丢图就会出现在下拉里；画舫不做图片管理器
BG_DIR = STATIC / "bg"
BG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif"}
UI_DENSITY = ("loose", "compact")
log = logging.getLogger(__name__)

DEFAULTS = {
    "comfy_url": "http://127.0.0.1:8188",
    # 安装位置由首次启动配置，不猜盘符，也不扫描用户的其它目录。
    "models_dir": "",
    "output_dir": "",
    "port": 8788,
    "default_profile": "anima_t2i",
    "default_unet": "anima-base-v1.0.safetensors",
    # 可选词表不随公开副本提供；缺文件时仍可用模型元数据与人工覆盖。
    "trigger_map": str(DATA / "trigger_map.json"),
    # 中文辅助（T004）：词表只读、翻译只打本机 Ollama（CPU），缺字段用这里的默认
    "zh": {
        "enabled": True,
        "ollama_url": "http://127.0.0.1:11434",
        "model": "qwen3.5:9b",
        "timeout_sec": 20,
        "table": str(DATA / "zh_tags.json"),
    },
    # 成品复制目录（T010）：空＝不复制，成品只留在炉子 output 里（维持现状）
    "output": {"dir": ""},
    # 显存紧阈值（T006，MB）：炉子空闲时 free 本来只剩 ≈1.3G（模型驻留），定高了＝常亮误报
    "gpu": {"warn_free_mb": 700},
    # 界面（T007）：只读本地背景，密度两档，模糊 0–40px
    "ui": {"bg": "off", "density": "loose", "blur": 10},
    # 图库附加目录（C 卡）：回填时额外只读扫描的文件夹；空＝只扫炉子输出目录。
    # 里面的图不经 /api/view 取字节（那是炉子的代理），所以前端只给参数与路径，不硬编坏图框。
    "gallery": {"extra_dirs": []},
    # 起炉子（T011）：默认绝不自动起（D102），auto_start 缺键／坏值一律 false。
    # 启动器只从本地配置读，界面与端点不收路径。留空时不尝试启动任何程序。
    # 读侧一律走 comfy_settings()，别裸取 SETTINGS["comfy"]——理由见该函数。
    "comfy": {
        "auto_start": False,
        "auto_free": False,
        "idle_minutes": 15,
        "vbs": "",
        "bat": "",
    },
}


def _err(code: str, human: str) -> dict:
    return {"code": code, "human": human}


def _normcase(path) -> str:
    """Windows 路径比较口径：大小写不敏感、分隔符统一、去掉末尾斜杠。"""
    return os.path.normcase(str(path)).rstrip("\\/")


def _read_dict(path: Path) -> dict:
    """损坏/半截 JSON/根本不是字典 → 空表，且绝不改写用户已有的文件（与 presets 同法）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _absolute_dir(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    path = Path(value.strip()).expanduser()
    return str(path) if path.is_absolute() else ""


def models_root(settings=None) -> Path:
    """未配置时只看本项目的占位目录，避免 Path("") 悄悄扫到当前工作目录。"""
    saved = settings if isinstance(settings, dict) else _read_dict(SETTINGS_FILE)
    configured = _absolute_dir(saved.get("models_dir"))
    return Path(configured) if configured else DATA / "unconfigured-models"


def load_settings() -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    if SETTINGS_FILE.exists():
        saved = _read_dict(SETTINGS_FILE)
    else:
        saved = {"models_dir": "", "output_dir": ""}
        SETTINGS_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    merged = {**DEFAULTS, **saved}
    zh = saved.get("zh")
    merged["zh"] = {**DEFAULTS["zh"], **(zh if isinstance(zh, dict) else {})}
    out = saved.get("output")
    merged["output"] = {**DEFAULTS["output"], **(out if isinstance(out, dict) else {})}
    d = merged["output"].get("dir")
    merged["output"]["dir"] = d.strip() if isinstance(d, str) else ""
    ui = saved.get("ui")
    merged["ui"] = {**DEFAULTS["ui"], **(ui if isinstance(ui, dict) else {})}
    for key in ("models_dir", "output_dir"):
        merged[key] = _absolute_dir(merged.get(key))
    for target, key, default in ((merged, "trigger_map", DEFAULTS["trigger_map"]),
                                 (merged["zh"], "table", DEFAULTS["zh"]["table"])):
        value = target.get(key)
        target[key] = value.strip() if isinstance(value, str) and value.strip() else default
    if not merged["models_dir"] or not merged["output_dir"]:
        log.warning(
            "首次启动配置尚未完成：请编辑 %s，参照 %s 填写 models_dir（ComfyUI 模型目录）和 "
            "output_dir（ComfyUI 输出目录）的完整路径，然后重启画舫。"
            "可选的 comfy.vbs / comfy.bat 留空时请手动启动 ComfyUI；"
            "触发词表与中文词表需自行提供，示例配置中有说明。",
            SETTINGS_FILE, ROOT / "settings.example.json",
        )
    return merged


def save_settings(data: dict) -> None:
    """写 settings.json：旧文先转 .bak，再临时文件＋原子替换（与 presets.save_presets 同法）。"""
    DATA.mkdir(parents=True, exist_ok=True)
    if SETTINGS_FILE.exists():
        SETTINGS_FILE.replace(SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".bak"))
    tmp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


def save_output_dir(normed: str) -> dict:
    """只改 output.dir：读用户已有的覆盖表（不掺 DEFAULTS，免得把默认值钉死进文件），原子写回。"""
    saved = _read_dict(SETTINGS_FILE)
    out = saved.get("output")
    saved["output"] = {**(out if isinstance(out, dict) else {}), "dir": normed}
    save_settings(saved)
    return saved["output"]


def gpu_warn_mb() -> int:
    """显存紧阈值（MB）：每次 /api/gpu 现读 settings.json（验收 C 改文件要立刻生效），只读不写。
    缺键／损坏／非正整数（含字符串、负数、bool）→ 兜默认 700，宁缺毋滥报。"""
    g = _read_dict(SETTINGS_FILE).get("gpu")
    v = g.get("warn_free_mb") if isinstance(g, dict) else None
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        return DEFAULTS["gpu"]["warn_free_mb"]
    return v


def comfy_settings() -> dict:
    """T011 读侧：起炉子要用的三样（开关＋两条固定可执行路径）。

    每次现读磁盘（同 gpu_warn_mb／gallery_dirs／ui_settings 口径）：主人勾完复选框立刻生效、
    不用重启，所以**没有需要同步的内存副本**（裁定 6 的实质由此满足）。
    一律走这里，**别裸取 SETTINGS["comfy"]**：load_settings 是浅合并、只给 zh／output／ui 三块
    做深合并，而写侧只写 auto_start 一个键（不掺 DEFAULTS，同 save_output_dir），所以主人勾过一次
    之后存盘的 comfy 块是半截的——裸取 vbs／bat 就 KeyError。读侧收口在这一处，两种写法都不会炸。
    auto_start 只认 bool True：缺键／坏值（"yes"、1、null）／整块不是字典一律 false（D102）。
    路径只认非空字符串，否则兜默认。cwd 不单列一项：bat 里是相对路径（`.\\python_embeded\\...`），
    工作目录就取 bat 自己的父目录，主人把 bat 挪哪儿都对得上。
    """
    raw = _read_dict(SETTINGS_FILE).get("comfy")
    raw = raw if isinstance(raw, dict) else {}
    # T011 的读侧仍只返回三样；收火设置由 idle_free_settings 单独读取。
    out = {k: DEFAULTS["comfy"][k] for k in ("auto_start", "vbs", "bat")}
    if raw.get("auto_start") is True:
        out["auto_start"] = True
    for k in ("vbs", "bat"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def norm_comfy_auto(raw):
    """T011 写侧校验（POST /api/furnace/auto）：返回 (bool, 错误dict|None)。
    只认 bool，其余形状一律 400＋人话。不收路径：起炉子走哪个可执行由 settings.json 里的
    本机常量决定，界面改不了（卡面硬红线「不接受任何用户输入路径」）。"""
    if isinstance(raw, bool):
        return raw, None
    return None, _err("bad_auto_start", "这个开关只能是打开（true）或关掉（false）")


def save_comfy_auto(on: bool) -> dict:
    """只改 comfy.auto_start：读用户已有的覆盖表（不掺 DEFAULTS，免得把默认值钉死进文件），原子写回。"""
    saved = _read_dict(SETTINGS_FILE)
    c = saved.get("comfy")
    saved["comfy"] = {**(c if isinstance(c, dict) else {}), "auto_start": on}
    save_settings(saved)
    return saved["comfy"]


def idle_free_settings() -> dict:
    """T012 每次现读 comfy 块；开关只认 true，分钟数缺失／坏值回 15，整数夹到 5–120。"""
    raw = _read_dict(SETTINGS_FILE).get("comfy")
    raw = raw if isinstance(raw, dict) else {}
    minutes = raw.get("idle_minutes")
    if type(minutes) is not int:
        minutes = DEFAULTS["comfy"]["idle_minutes"]
    return {"auto_free": raw.get("auto_free") is True,
            "idle_minutes": min(120, max(5, minutes))}


def norm_idle_free(raw):
    """写侧拒绝假布尔和非整数；只接两个设置项，不接路径或额外开关。"""
    if not isinstance(raw, dict) or type(raw.get("auto_free")) is not bool:
        return None, _err("bad_auto_free", "空闲收火开关只能是打开（true）或关掉（false）")
    if type(raw.get("idle_minutes")) is not int:
        return None, _err("bad_idle_minutes", "空闲时间得是整数分钟（5–120 分钟）")
    return {"auto_free": raw["auto_free"],
            "idle_minutes": min(120, max(5, raw["idle_minutes"]))}, None


def save_idle_free(cfg: dict) -> dict:
    """只写 comfy 的两个收火字段，保留 T011 的开关、路径和其它用户设置。"""
    saved = _read_dict(SETTINGS_FILE)
    comfy = saved.get("comfy")
    saved["comfy"] = {**(comfy if isinstance(comfy, dict) else {}),
                      "auto_free": cfg["auto_free"], "idle_minutes": cfg["idle_minutes"]}
    save_settings(saved)
    return saved["comfy"]


def gallery_dirs() -> list:
    """C 卡读侧：图库回填要额外只读扫描的目录（绝不写 settings.json）。

    每次现读磁盘：主人改完 settings.json 点一下「回填」就生效，不用重启（与 gpu_warn_mb 同口径）。
    T010 的成品复制目录一并算进来（卡面「默认含 T010 复制目录」）——那里存的是复制品，
    同名的原件在炉子输出里已经登过，gallery 按文件名认重，不会重复登一行。
    只收非空的绝对路径字符串，其余形状一律忽略：不拿主人的配置文件当目录浏览器。
    """
    saved = _read_dict(SETTINGS_FILE)
    g = saved.get("gallery")
    extra = g.get("extra_dirs") if isinstance(g, dict) else None
    out = saved.get("output")
    copy_dir = out.get("dir") if isinstance(out, dict) else ""
    seen, dirs = set(), []
    for d in (list(extra) if isinstance(extra, list) else []) + [copy_dir]:
        if not isinstance(d, str):
            continue
        d = d.strip()
        if not d or not Path(d).is_absolute() or _normcase(d) in seen:
            continue
        seen.add(_normcase(d))
        dirs.append(d)
    return dirs


def bg_files() -> list:
    """T007 static/bg 下可选的背景图文件名（排序稳定；目录不存在＝空表）。
    只列图片后缀：主人往里丢图就能选，丢别的（说明文档之类）不会混进下拉。"""
    try:
        items = list(BG_DIR.iterdir())
    except OSError:
        return []
    return sorted(p.name for p in items
                  if p.is_file() and p.suffix.lower() in BG_EXTS and not p.name.startswith("."))


def ui_settings() -> dict:
    """T007 读侧：settings.json 里的 ui 块。缺字段／坏值／文件损坏一律兜默认，绝不上抛。
    bg 指的文件已经不在了（主人自己删了）→ 悄悄回 off：界面照样能用，不为一块背景报错。"""
    raw = _read_dict(SETTINGS_FILE).get("ui")
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS["ui"])
    bg = raw.get("bg")
    if isinstance(bg, str) and (bg == "off" or bg in bg_files()):
        out["bg"] = bg
    if raw.get("density") in UI_DENSITY:
        out["density"] = raw["density"]
    blur = raw.get("blur")
    if type(blur) is int and 0 <= blur <= 40:
        out["blur"] = blur
    return out


def norm_ui(raw, available=None, current=None):
    """T007 写侧校验（POST /api/ui）：返回 (规范化后的 ui, 错误dict|None)。
    只认 bg／density／blur，缺键＝沿用现值；多余的键忽略不报错。
    bg 只许 "off" 或 static/bg 里真有的文件名——白名单比对天然挡住路径分隔符与 ..，不当目录浏览器。"""
    if available is None:
        available = bg_files()
    base = {**DEFAULTS["ui"], **(current if isinstance(current, dict) else {})}
    if not isinstance(raw, dict):
        return None, _err("bad_ui", '界面设置得是一个对象，比如 {"bg":"off","density":"loose"}')
    out = dict(base)
    if "bg" in raw:
        bg = raw.get("bg")
        bg = bg.strip() if isinstance(bg, str) else None
        if bg != "off" and bg not in available:
            return None, _err("bad_bg",
                              "背景要么是「关闭背景」（off），要么是 static/bg 文件夹里真有的那个文件名")
        out["bg"] = bg
    if "density" in raw:
        if raw.get("density") not in UI_DENSITY:
            return None, _err("bad_density", "密度只有两种：loose（宽松）或 compact（紧凑）")
        out["density"] = raw["density"]
    if "blur" in raw:
        blur = raw["blur"]
        if type(blur) is not int or not 0 <= blur <= 40:
            return None, _err("bad_blur", "模糊度要填 0 到 40 之间的整数")
        out["blur"] = blur
    return out, None


def save_ui(ui: dict) -> dict:
    """只改 ui 块：读用户已有的覆盖表（不掺 DEFAULTS，免得把默认值钉死进文件），原子写回。"""
    saved = _read_dict(SETTINGS_FILE)
    saved["ui"] = dict(ui)
    save_settings(saved)
    return saved["ui"]


def norm_output_dir(raw, comfy_output: str = None, create: bool = True):
    """校验成品复制目录，返回 (规范化路径, 错误dict|None)。
    只做校验与建目录，**绝不碰目录里已有的文件**（T010 最高红线：那是主人的真作品）。
    create=False 用于「只读校验」（打开文件夹），不存在的目录直接报错、不去建。"""
    if comfy_output is None:
        comfy_output = DEFAULTS["output_dir"]
    if raw is None:
        return "", None                      # 空＝不复制
    if not isinstance(raw, str):
        return "", _err("bad_path", "输出目录得是一条文字路径；请填写自己选择的完整目录")
    text = raw.strip()
    if not text:
        return "", None
    p = Path(text)
    if not p.is_absolute() or not p.drive:
        return "", _err("bad_path", "输出目录要写完整路径（带盘符）；请填写自己选择的完整目录")
    base = _normcase(comfy_output) if comfy_output else ""
    if base and (_normcase(p) == base or any(_normcase(x) == base for x in p.parents)):
        return "", _err("is_comfy_output",
                        "这是炉子（ComfyUI）自己的输出目录，复制进去等于自己抄自己；换你自己的一个文件夹")
    if not p.exists():
        if not create:
            return "", _err("not_found", "这个文件夹不存在，先建好再填")
        try:
            os.makedirs(str(p), exist_ok=True)
        except OSError as e:
            return "", _err("cannot_create",
                            f"这个文件夹建不出来：{e.strerror or e}（看看盘符在不在、有没有写权限）")
    if not p.is_dir():
        return "", _err("cannot_create", "这条路径不是文件夹，换一个文件夹路径")
    return str(p), None


def load_token() -> str:
    """启动 token：每次点火换一把；前端经 GET / 注入拿到，不落 localStorage。"""
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    return token
