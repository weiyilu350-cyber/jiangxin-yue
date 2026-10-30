"""画舫 A1：本地 Web 出图台后端。端口 8788，启动 token + Origin/Host 校验。"""
import os
import random
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import gpu as GPU
from . import backgrounds as BG
from . import furnace
from . import idle_free
from . import gallery
from . import models as MS
from . import output as OUT
from . import presets as PS
from . import runner
from . import settings as ST
from . import workflow_profile as wp
from . import zh as ZS
from .comfy import ComfyClient, ComfyError
from .settings import (PRESETS_FILE, STATIC, TRIGGER_OVERRIDES_FILE, WORKFLOWS,
                       load_settings, load_token)
from .triggers import TriggerResolver, compose

MODELS_ROOT = MS.MODELS_ROOT          # 单一来源；测试换根只改这一处
MODEL_DIRS = MS.MODEL_DIRS
ALLOWED_HOSTS = {"127.0.0.1:8788", "localhost:8788"}
ALLOWED_ORIGINS = {"http://127.0.0.1:8788", "http://localhost:8788"}


@asynccontextmanager
async def _lifespan(_app):
    """T011 自动拉起只挂在这里（裁定 4 **禁止模块级**）。

    模块级等于 import 就跑，而 tests/ 里十个端点测试文件全是靠 import app.main 拿 TestClient 的
    （且都是裸用、不进 with，所以 lifespan 根本不会跑）——写在模块级的话，主人一旦勾上那个开关，
    之后每跑一次全量单测都会去点一次炉子、吃掉 8G 显存和训练抢卡。
    """
    furnace.auto_start_once(CLIENT.alive, SETTINGS["comfy_url"])
    worker = IDLE_FREE.start(CLIENT)
    try:
        yield
    finally:
        if worker is not None:
            stopping, thread = worker
            stopping.set()
            thread.join(timeout=1)


app = FastAPI(title="huafang", lifespan=_lifespan)
SETTINGS = load_settings()
MODELS_ROOT = ST.models_root(SETTINGS)
TOKEN = load_token()
CLIENT = ComfyClient(SETTINGS["comfy_url"])
IDLE_FREE = idle_free.IdleFree()
TRIGGERS = TriggerResolver(SETTINGS["trigger_map"], MODELS_ROOT / MODEL_DIRS["lora"],
                           TRIGGER_OVERRIDES_FILE)
ZH = ZS.ZhService(SETTINGS["zh"])   # 词表懒加载＋缓存，进程活着就只读一遍
PROFILES: dict = {}
# C 卡图库索引：连库是懒的（第一次用到才建 data/gallery.sqlite），所以导进来不会凭空多一个文件
GAL = gallery.Gallery()


def exist_fn(role: str, name: str) -> bool:
    sub = MODEL_DIRS.get(role)
    if not sub:
        return True
    return (MODELS_ROOT / sub / name).exists()


def _load_profiles():
    for f in sorted(WORKFLOWS.glob("*.json")):
        wf, prof = wp.load_pair(f)
        # D76 两级：errors 拦住不让出图（/api/generate 会 409），warnings 只是要界面跟主人说一句
        errors, warnings = wp.validate_full(wf, prof, exist_fn=exist_fn)
        PROFILES[prof["workflow_id"]] = {
            "workflow": wf,
            "profile": prof,
            "errors": errors,
            "warnings": warnings,
            "file": f.name,
        }


_load_profiles()

# 内置「默认」配方不落盘：值＝settings 默认底模＋默认工作流参数，保证一键出图（D96/D98）
DEFAULT_RECIPE = PS.default_recipe(
    SETTINGS,
    PROFILES[SETTINGS["default_profile"]]["profile"]["defaults"]
    if SETTINGS["default_profile"] in PROFILES else None,
)

# T010／C 卡：任务一到 done 就干两件事——成品复制到主人配的目录（空＝不复制）＋登进图库索引。
# 两件互不相干，各自吞掉异常：出图本身已经成功了，谁也不许把它拖成失败（原件永远在炉子 output，
# 索引漏了一行主人点一次「回填」就补上，回填幂等）。
def _on_done(job) -> None:
    OUT.copy_finals(CLIENT, job, SETTINGS["output"]["dir"])
    try:
        GAL.register_job(job)
    except Exception:
        pass


# T005：启动时对账一次，把炉子历史里画舫自己跑完的图恢复成只读记录（不重新提交）。
# 炉子没开也得能把服务起起来——所以掉线只是安静跳过，前端刷新时会再对账。
try:
    runner.reconcile(CLIENT)
except Exception:
    pass


def preset_ctx(profile_id: str):
    """取当前工作流的笔槽数与在盘底模；profile 不存在直接 404。"""
    p = PROFILES.get(profile_id or "")
    if not p:
        raise HTTPException(404, "没找到这个工作流")
    return (p["profile"]["workflow_id"], len(p["profile"]["lora_slots"]),
            MS.model_names("unet", MODELS_ROOT))


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        if host not in ALLOWED_HOSTS or (origin and origin not in ALLOWED_ORIGINS):
            return Response("Origin/Host 校验不过", status_code=403)
        if request.headers.get("authorization", "") != f"Bearer {TOKEN}":
            return Response("token 不对", status_code=401)
    return await call_next(request)


def _page(name: str) -> HTMLResponse:
    """发一个页面并把启动 token 注进去（token 只走 Authorization 头，不进 URL/query，D135）。"""
    html = (STATIC / name).read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__HUAFANG_TOKEN__", TOKEN))


@app.get("/", response_class=HTMLResponse)
def index():
    return _page("index.html")


@app.get("/gallery", response_class=HTMLResponse)
def gallery_page():
    return _page("gallery.html")


@app.get("/compare", response_class=HTMLResponse)
def compare_page():
    return _page("compare.html")


@app.get("/api/bootstrap")
def bootstrap():
    profiles = []
    for pid, p in PROFILES.items():
        prof = p["profile"]
        view = wp.stage_ui(p["workflow"], prof)   # B 卡 §4：段卡与「这支笔管哪段」都照它画
        profiles.append({
            "id": pid,
            "label": prof.get("label", pid),
            "lora_slots": len(prof["lora_slots"]),
            "defaults": prof["defaults"],
            "capabilities": wp.derive_capabilities(prof),
            "stages": view["stages"],
            "lora_stages": view["lora_stages"],
            "errors": p["errors"],
            "warnings": p["warnings"],   # D78：如「第二段无笔」——不拦出图，界面要说给主人听
        })
    return {
        "profiles": profiles,
        # 只给名字：界面下拉框用；要看大小/触发词走 /api/models
        "models": {role: MS.model_names(role, MODELS_ROOT) for role in MODEL_DIRS},
        "comfy_alive": CLIENT.alive(),
        "settings": {k: SETTINGS[k] for k in ("comfy_url", "default_profile", "default_unet")},
        "output": dict(SETTINGS["output"]),   # T010：前端据此预填「输出目录」文本框
        # T007：界面设置（背景／密度）＋ static/bg 里现有哪些背景可选；读侧已把坏值兜回默认
        "ui": ST.ui_settings(),
        "bg_files": ST.bg_files(),
        # T011：只吐开关值给前端显示复选框（裁定 3）。经 comfy_settings() 宽容读——
        # 存盘的 comfy 块可能是半截的（写侧只写 auto_start 一个键），裸取 SETTINGS["comfy"] 会 KeyError。
        # 两条可执行路径不出后端：界面用不着，也不该由前端知道去哪点火。
        "comfy": {"auto_start": ST.comfy_settings()["auto_start"]},
        "idle_free": ST.idle_free_settings(),
    }


@app.post("/api/generate")
def generate(req: dict):
    p = PROFILES.get(req.get("profile_id", ""))
    if not p:
        raise HTTPException(404, "没找到这个工作流")
    if p["errors"]:
        raise HTTPException(409, detail={"human": "工作流契约检查没过，先找小唯修", "errors": p["errors"]})
    if req.get("seed", -1) in (-1, None, ""):
        req["seed"] = random.randint(0, 2**31 - 1)
    req["count"] = runner.clamp_count(req.get("count"))   # T009：老客户端不传＝1；夹取值进 request，回执与回填都以它为准
    job = runner.Job(secrets.token_hex(6), req)
    job.profile = p["profile"]
    runner.JOBS[job.id] = job
    try:
        job.positive_final = compose(
            req.get("positive", ""), TRIGGERS.for_loras(req.get("loras"))
        )
        wf = runner.build_workflow(p["workflow"], p["profile"], req, job.positive_final)
        job.sent = wf
        with IDLE_FREE.submitting():
            job.prompt_id = CLIENT.submit(wf, client_id="huafang")
        job.status = "running"
    except ComfyError as e:
        job.status = "error"
        job.error = {"code": e.code, "human": e.message, "detail": e.detail}
    return {"job_id": job.id, "status": job.status, "error": job.error}


@app.get("/api/models")
def models_list():
    """模型中心：只读扫描；笔另带触发词三元组（表/元数据/覆盖），其余只列名称与大小。"""
    return MS.scan_models(MODELS_ROOT, TRIGGERS)


def real_pen(name):
    """把界面传来的笔名对回盘上真名；对不上＝非法笔名（雷区 6）。"""
    pen = MS.find_pen(name, MODELS_ROOT)
    if not pen:
        raise HTTPException(404, detail={"human": "模型目录里没有这支笔", "code": "no_such_pen"})
    return pen


@app.post("/api/models/trigger")
def trigger_save(req: dict):
    """存人工覆盖；空串是合法值（＝这支笔明确无触发词，D20）。返回存完的三元组。"""
    pen = real_pen(req.get("name"))
    value = req.get("trigger")
    if not isinstance(value, str):
        raise HTTPException(400, detail={
            "human": "触发词得是文字；要表示「这支笔没有触发词」就存空串", "code": "bad_trigger"})
    if len(value) > MS.MAX_TRIGGER:
        raise HTTPException(400, detail={
            "human": f"触发词太长了，最多 {MS.MAX_TRIGGER} 个字符", "code": "trigger_too_long"})
    MS.put_override(TRIGGER_OVERRIDES_FILE, pen, value)
    return TRIGGERS.details(pen)


@app.delete("/api/models/trigger")
def trigger_clear(name: str = ""):
    """清除覆盖，回到表/元数据；本来就没覆盖也算成功（幂等）。"""
    pen = real_pen(name)
    MS.drop_override(TRIGGER_OVERRIDES_FILE, pen)
    return TRIGGERS.details(pen)


@app.get("/api/zh/status")
def zh_status():
    """前端据此置灰「整句翻译」；探活失败只回 false，不吐 500。"""
    return ZH.status()


@app.get("/api/zh/complete")
def zh_complete(q: str = "", limit: str = "20"):
    """本地词表补全，不出网；limit 收字符串是为了坏输入也兜到默认而不是 422。"""
    return {"items": ZH.complete(q, limit)}


@app.post("/api/zh/translate")
def zh_translate(req: dict):
    """翻译只是辅助：失败也回 200＋ok=false＋人话，绝不拦住出图（雷区 4）。"""
    text = req.get("text")
    if not isinstance(text, str):
        raise HTTPException(400, detail={"human": "要翻的文字得是文本", "code": "bad_text"})
    if len(text) > ZS.MAX_TEXT:
        raise HTTPException(400, detail={
            "human": f"一次最多翻 {ZS.MAX_TEXT} 个字，太长了本机模型翻不动",
            "code": "text_too_long"})
    try:
        out = ZH.translate(text)
    except ZS.ZhError as e:
        return {"ok": False, "text": text, "changed": False,
                "error": {"human": e.human, "code": e.code}}
    return {"ok": True, **out}


@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    job = runner.JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "没这个任务")
    try:
        runner.reconcile_one(CLIENT, job)   # T005：轻量对账，先分清排队中／出图中
        runner.refresh(CLIENT, job, _on_done)   # 再查 history：跑完了以 history 为准（雷区 5）
    except ComfyError as e:  # ComfyUI 中途掉线也要说人话，不吐 500
        job.status = "error"
        job.error = {"code": e.code, "human": e.message, "detail": e.detail}
    return {
        "status": job.status,
        "images": job.images,
        "receipt": job.receipt,
        "error": job.error,
        "request": job.request,
        "positive_final": job.positive_final,
        "readonly": job.readonly,
        "copy_error": job.copy_error,   # T010：复制失败的人话；没有就是 null
    }


def reconcile_payload() -> dict:
    """/api/jobs 与 /api/reconcile 共用：炉子不可达也回 200 ＋ 人话，不吐 500（卡 §2）。"""
    try:
        return {"ok": True, **runner.reconcile(CLIENT, _on_done)}
    except ComfyError as e:
        human = "画舫连不上炉子，先确认 ComfyUI 开着" if e.code == "comfy_down" else e.message
        return {"ok": False, "jobs": [], "error": {"human": human, "code": e.code}}


@app.get("/api/jobs")
def jobs_list():
    """本次会话任务列表（顺带对账一次，刷新页面就能把结果墙恢复回来）。"""
    return reconcile_payload()


@app.post("/api/reconcile")
def reconcile_now():
    """手动触发对账；只改状态，不重新提交任何任务（雷区 2）。"""
    return reconcile_payload()


@app.get("/api/view")
def view(filename: str, subfolder: str = "", type: str = "output"):
    try:
        data = CLIENT.view_bytes(filename, subfolder, type)
    except ComfyError as e:
        raise HTTPException(502, e.message)
    return Response(content=data, media_type="image/png")


@app.post("/api/output-dir")
def output_dir_save(req: dict):
    """T010 存「成品复制目录」：校验通过就原子写进 settings.json，失败 400＋人话、一个字不写。
    校验只读＋按需建目录，绝不碰目标目录里已有的文件（那是主人的真作品）。"""
    normed, err = ST.norm_output_dir(req.get("dir"), SETTINGS["output_dir"])
    if err:
        raise HTTPException(400, detail=err)
    try:
        ST.save_output_dir(normed)
    except OSError as e:
        raise HTTPException(400, detail={
            "human": f"设置存不进磁盘：{e.strerror or e}", "code": "save_failed"})
    SETTINGS["output"]["dir"] = normed      # 内存里同步，下一次出图立刻生效
    return {"dir": normed,
            "human": ("已记住：以后每次出完图，成品会复制一份到这里" if normed
                      else "已关掉复制：成品只留在炉子的输出文件夹里")}


@app.post("/api/reveal")
def reveal(req: dict = None):
    """T010 打开成品文件夹：不传 dir＝已配置的目录（没配置就开炉子输出）。
    只开这两个既定目录，不当任意路径浏览器；explorer 只收一个校验过的路径参数。"""
    configured = SETTINGS["output"]["dir"]
    raw = req.get("dir") if isinstance(req, dict) else None
    raw = raw.strip() if isinstance(raw, str) else ""
    if raw and (not configured
                or os.path.normcase(raw.rstrip("\\/")) != os.path.normcase(configured.rstrip("\\/"))):
        raise HTTPException(400, detail={
            "human": "只能打开你在「输出目录」里配好的那个文件夹", "code": "not_configured"})
    directory = configured or SETTINGS["output_dir"]
    if not directory:
        raise HTTPException(400, detail={
            "human": "还没配置输出目录，请按启动提示填写 data/settings.json 后重启画舫",
            "code": "setup_required"})
    normed, err = ST.norm_output_dir(directory,
                                     comfy_output="", create=False)
    if err:
        raise HTTPException(400, detail=err)
    OUT.reveal(normed)
    return {"dir": normed, "human": f"已让系统打开这个文件夹：{normed}"}


@app.get("/api/gpu")
def gpu_status_endpoint():
    """T006 显卡忙／显存紧只读提示：炉子不可达／探不到 → 200＋ok:false＋人话（与 zh 同口径，不吐 500）。
    阈值现读 settings（gpu.warn_free_mb），改文件立刻生效，不用重启。"""
    return GPU.gpu_status(CLIENT, ST.gpu_warn_mb())


@app.post("/api/furnace/start")
def furnace_start():
    """T011 起炉子：主人点了才起，画舫绝不偷偷点（D102）。

    阻塞轮询最多 180 秒等炉子应答（冷启动要往显存里装模型，90 秒容易误报「起不来」，
    主人再点一次就双开）；这期间前端按钮是 disabled 的。起炉中再来一发只回「正在起，稍等」。
    走哪条路（vbs／bat）由 settings 里的本机常量决定，**这个端点不收任何路径参数**。
    任何结果都是 200＋一句人话，不吐 500。
    """
    return furnace.start(CLIENT.alive, SETTINGS["comfy_url"])


@app.post("/api/furnace/auto")
def furnace_auto_save(req: dict = None):
    """T011 存「开机自动拉起炉子」开关：只认 bool，非法值 400＋人话、一个字不写。

    写盘沿用 settings.save_settings（旧文转 .bak＋临时文件原子替换），与 ui／输出目录同一套容错。
    读侧是 comfy_settings() 每次现读磁盘，所以勾完**立刻生效、不用重启**（裁定 6）；
    也正因为读侧现读，这里不需要再同步一份内存副本——同时维护两份就成了裁定 3 禁止的「混」。
    """
    on, err = ST.norm_comfy_auto(req.get("auto_start") if isinstance(req, dict) else None)
    if err:
        raise HTTPException(400, detail=err)
    try:
        ST.save_comfy_auto(on)
    except OSError as e:
        raise HTTPException(400, detail={
            "human": f"设置存不进磁盘：{e.strerror or e}", "code": "save_failed"})
    return {"auto_start": on,
            "human": ("已打开：画舫启动时发现炉子没开，就自己去点一次（会吃掉约 8G 显存，和训练抢卡）"
                      if on else "已关掉：炉子要你自己起，画舫绝不偷偷点")}


@app.get("/api/furnace/idle-free")
def idle_free_status():
    """收火状态只作提示；探活或队列异常都安静降级，不报 500。"""
    return IDLE_FREE.status(CLIENT)


@app.post("/api/furnace/idle-free")
def idle_free_save(req: dict = None):
    cfg, err = ST.norm_idle_free(req)
    if err:
        raise HTTPException(400, detail=err)
    try:
        IDLE_FREE.save_settings(cfg)
    except Exception:
        return {"ok": False, "human": "空闲收火设置存不进磁盘，这次没有记住，请稍后再试"}
    return {"ok": True, **cfg,
            "human": (f"已记住：空闲 {cfg['idle_minutes']} 分钟后自动收火，下次出图会自动装回模型"
                      if cfg["auto_free"] else "已关掉空闲收火")}


@app.post("/api/bg/upload")
async def bg_upload(request: Request):
    """T007：本机表单只接一张背景，不接受用户指定存盘目录。"""
    try:
        file: UploadFile = await BG.read_upload(request)
        try:
            name = BG.save_upload(file)
        finally:
            await file.close()
    except ValueError as e:
        raise HTTPException(400, detail={"code": "bad_bg_upload", "human": str(e)})
    except OSError:
        # 传输层读流／关文件也可能抛 OSError；与写盘失败同一句人话，不把堆栈丢给主人
        raise HTTPException(400, detail={"code": "bad_bg_upload", "human": "背景图存不进磁盘，请检查 static/bg 的空间和写入权限"})
    return {"filename": name, "human": "背景图已导入"}


@app.post("/api/ui")
def ui_save(req: dict):
    """T007 存界面设置（背景／密度）：非法值 400＋人话、一个字不写；合法就原子写进 settings.json。
    写盘沿用 settings.save_settings（旧文转 .bak＋临时文件替换），与 presets／输出目录同一套容错。
    bootstrap 是现读磁盘的，所以存完刷新页面就还在（不用重启服务）。"""
    ui, err = ST.norm_ui(req, ST.bg_files(), ST.ui_settings())
    if err:
        raise HTTPException(400, detail=err)
    try:
        ST.save_ui(ui)
    except OSError as e:
        raise HTTPException(400, detail={
            "human": f"设置存不进磁盘：{e.strerror or e}", "code": "save_failed"})
    return {"ui": dict(ui), "human": "界面设置已记住"}


@app.get("/api/presets")
def presets_get(profile_id: str, show_all: str = Query("", alias="all")):
    """默认只列当前工作流可用的配方；`?all=1` 连不兼容的一起给（带 usable＋warnings）。"""
    pid, slots, unets = preset_ctx(profile_id)
    data = PS.load_presets(PRESETS_FILE)
    items = PS.list_presets(
        data, DEFAULT_RECIPE, pid, slots, unets,
        show_all=show_all.strip().lower() not in ("", "0", "false", "no"),
    )
    return {"profile_id": pid, "lora_slots": slots, "presets": items}


@app.post("/api/presets")
def presets_save(req: dict):
    """把当前界面状态存成一支配方；空名 400、同名 409，都不静默覆盖。"""
    data = PS.load_presets(PRESETS_FILE)
    try:
        recipe = PS.add_recipe(
            data,
            name=req.get("name", ""),
            unet=req.get("unet", ""),
            loras=req.get("loras"),
            positive=req.get("positive", ""),
            negative=req.get("negative", ""),
            params=req.get("params"),
            reserved_names=(DEFAULT_RECIPE["name"],),
        )
    except PS.PresetError as e:
        raise HTTPException(409 if e.code == "dup_name" else 400,
                            detail={"human": e.human, "code": e.code})
    PS.save_presets(PRESETS_FILE, data)
    return {"id": recipe["id"], "name": recipe["name"]}


@app.post("/api/presets/{recipe_id}/load")
def presets_load(recipe_id: str, req: dict):
    """full＝底模＋笔＋提示词＋参数；params＝只换提示词＋参数，不带笔（雷区 4）。"""
    pid, slots, unets = preset_ctx(req.get("profile_id", ""))
    if not PS.valid_id(recipe_id):
        raise HTTPException(404, "没这个预设")
    data = PS.load_presets(PRESETS_FILE)
    recipe = PS.find_by_id(data["recipes"], recipe_id)
    if recipe is None:
        if recipe_id != PS.DEFAULT_ID:
            raise HTTPException(404, "没这个预设")
        recipe = DEFAULT_RECIPE
    try:
        return PS.build_load(recipe, data["overrides"], pid,
                             req.get("mode", "full"), slots, unets)
    except PS.PresetError as e:
        raise HTTPException(400 if e.code == "bad_mode" else 409,
                            detail={"human": e.human, "code": e.code})


@app.delete("/api/presets/{recipe_id}")
def presets_delete(recipe_id: str):
    """删配方连带删它在各工作流下的 override；内置「默认」不许删。"""
    if not PS.valid_id(recipe_id):
        raise HTTPException(404, "没这个预设")
    data = PS.load_presets(PRESETS_FILE)
    try:
        found = PS.delete_recipe(data, recipe_id)
    except PS.PresetError as e:
        raise HTTPException(409, detail={"human": e.human, "code": e.code})
    if not found:
        raise HTTPException(404, "没这个预设")
    PS.save_presets(PRESETS_FILE, data)
    return {"deleted": recipe_id}


# ------------------------------------------------------------------ C 卡：图库／对比
COMPARE_MIN, COMPARE_MAX = 2, 8      # 卡 §4：两种对比都是 2–8 张
VARIANT_LOCKED = ("seed", "count", "profile_id")


def _gallery_error(e: gallery.GalleryError):
    """图库的人话错 → 404（没这张图）／400（值不对），都不吐堆栈（D96）。"""
    return HTTPException(404 if e.code == "no_such_image" else 400,
                         detail={"human": e.human, "code": e.code})


@app.get("/api/gallery")
def gallery_list(request: Request):
    """分页列表＋筛选（卡 §2 筛选栏）。筛选值一律走 SQL 参数绑定，不拼字符串。"""
    out = GAL.query(dict(request.query_params))
    out["stats"] = GAL.stats()
    return out


@app.get("/api/gallery/tags")
def gallery_tags():
    """所有标签＋各自多少张（前端做标签下拉）。**必须声明在 /{image_id} 之前**，否则 tags 会被当成编号。"""
    return {"tags": GAL.tags()}


@app.get("/api/gallery/{image_id}")
def gallery_one(image_id: str):
    """编号收字符串：`/api/gallery/abc` 也要回人话 404，而不是 FastAPI 的 422。"""
    try:
        return GAL.get(image_id)
    except gallery.GalleryError as e:
        raise _gallery_error(e)


@app.patch("/api/gallery/{image_id}")
def gallery_patch(image_id: str, req: dict):
    """局部改收藏／评分／标签：只动给了的字段，坏值 400 人话，绝不串字段（卡 §2）。"""
    try:
        return GAL.patch(image_id, req)
    except gallery.GalleryError as e:
        raise _gallery_error(e)


@app.post("/api/gallery/backfill")
def gallery_backfill():
    """小唯的按钮：只读扫炉子历史＋附加目录，把旧图补登进索引（卡 §1）。

    扫哪些目录只由 settings 说了算（`gallery.extra_dirs` ＋ T010 复制目录 ＋ 炉子输出目录），
    请求体里不接目录——那等于开一个「拿着 token 就能让画舫去扫任意盘」的口子。
    """
    rep = GAL.backfill(client=CLIENT, profiles=PROFILES, dirs=ST.gallery_dirs(),
                       furnace_root=SETTINGS["output_dir"])
    rep["stats"] = GAL.stats()
    if GAL.rebuilt:
        rep["rebuilt"] = GAL.rebuilt      # 索引库坏过要交代清楚，别悄悄换一个新库了事
    return rep


@app.post("/api/compare")
def compare(req: dict):
    """对比板提交（卡 §4），两种模式：

    - `variants`（模式 A）：同 seed、同工作流，只换参数，一次提 N 个任务——**直接复用
      /api/generate 那个函数**，不另开一条出图路；每个变体出 1 张，前端拼板。
    - `pick`（模式 B）：从图库挑 2–8 张并排，只取记录、不出图。
    """
    mode = req.get("mode", "variants")
    if mode == "pick":
        return _compare_pick(req.get("ids"))
    if mode == "variants":
        return _compare_variants(req)
    raise HTTPException(400, detail={
        "human": "对比只有两种模式：variants（同 seed 出变体）或 pick（从图库挑图）", "code": "bad_mode"})


def _compare_pick(ids):
    if not isinstance(ids, list):
        raise HTTPException(400, detail={"human": "要挑哪几张得给一串图库编号（ids）", "code": "bad_ids"})
    nums, seen = [], set()
    for i in ids:
        try:
            n = int(i)
        except (TypeError, ValueError):
            raise HTTPException(400, detail={"human": f"图库编号得是数字，{i!r} 不是", "code": "bad_ids"})
        if n in seen:
            continue                      # 同一张挑两遍＝并排看同一张图，没意义，去重不算错
        seen.add(n)
        nums.append(n)
    # 张数先于「这张在不在」判：挑了 9 张时该说的是「最多 8 张」，而不是其中某张查不到
    if not COMPARE_MIN <= len(nums) <= COMPARE_MAX:
        raise HTTPException(400, detail={
            "human": f"对比要挑 {COMPARE_MIN}–{COMPARE_MAX} 张，现在挑了 {len(nums)} 张",
            "code": "bad_pick_count"})
    picked = []
    for n in nums:
        try:
            picked.append(GAL.get(n))
        except gallery.GalleryError as e:
            raise _gallery_error(e)
    return {"mode": "pick", "items": picked}


def _compare_variants(req):
    base = req.get("base")
    variants = req.get("variants")
    if not isinstance(base, dict) or not base.get("profile_id"):
        raise HTTPException(400, detail={
            "human": "得先给一份基准参数（base，里面要有 profile_id）", "code": "bad_base"})
    if not isinstance(variants, list) or not all(isinstance(v, dict) for v in variants):
        raise HTTPException(400, detail={"human": "变体得是一串参数对象（variants）", "code": "bad_variants"})
    if not COMPARE_MIN <= len(variants) <= COMPARE_MAX:
        raise HTTPException(400, detail={
            "human": f"一次比 {COMPARE_MIN}–{COMPARE_MAX} 个变体，现在给了 {len(variants)} 个",
            "code": "bad_variants"})
    for v in variants:
        locked = [k for k in VARIANT_LOCKED if k in v]
        if locked:
            raise HTTPException(400, detail={
                "human": "变体里不许改 " + "／".join(locked)
                         + "：模式 A 比的就是同 seed、同工作流、同张数下的参数差",
                "code": "locked_field"})
    seed = base.get("seed")
    if seed in (-1, None, ""):
        # 同 seed 是对比的前提，所以在这里定一次、N 个变体共用；
        # 交给 generate 各自随机的话，比的就不是参数而是 seed 了。
        seed = random.randint(0, 2**31 - 1)
    jobs = [{"variant": v, **generate({**base, **v, "seed": seed, "count": 1})} for v in variants]
    return {"mode": "variants", "seed": seed, "jobs": jobs}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=SETTINGS["port"])
