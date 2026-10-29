"""T011 起炉子：顶栏按钮点一下才起，另有一个默认关闭的自动拉起开关。

D102 铁律不破：默认绝不自动起炉子；画舫不训练、不管理训练、**不探进程、不读训练日志**，
判「炉子活没活」只用它自己那个只读 HTTP 接口（与 gpu.py 同一个数据源）。
起炉子只走 settings.comfy 里两条**固定可执行**（本机常量，任何端点都不收路径参数）。
"""
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from . import settings as ST

log = logging.getLogger("app.furnace")

# 裁定 5：90 秒改 180（早期版本的口径是每 2 秒探一次、最多 90 次 ≈ 3 分钟）。
# 冷启动要把模型吃进显存，卡着 90 秒容易「其实还在起、画舫说起不来」，主人再点一次就双开。
POLL_TIMEOUT = 180.0
POLL_INTERVAL = 2.0
FALLBACK_PORT = 8188

# 进程内状态：in-flight 锁＋「自动拉起每进程最多一次」的旗。
# 端口探活挡不住双开——炉子还在加载那几十秒里它压根没在监听，正是第二次点火会得逞的窗口期。
_LOCK = threading.Lock()
_IN_FLIGHT = False
_AUTO_TRIED = False


def _safe_probe(probe) -> bool:
    """探活一律吞异常当「没活」。

    炉子正在起来的那几秒，端口可能已经能连上但 HTTP 还不答话，httpx 会抛 ReadTimeout；
    而 ComfyClient.alive() 只接 ConnectError，漏出来就是一发 500——正是本模块要保证不出现的。
    """
    try:
        return bool(probe())
    except Exception:
        return False


def _port_of(url: str) -> int:
    try:
        return urlparse(url).port or FALLBACK_PORT
    except ValueError:
        return FALLBACK_PORT


def launcher(cfg: dict):
    """挑一条既有路子：vbs 在盘上才用它，不在退回 bat；两条都不在 → kind="none"。

    → (kind, argv, cwd)。cwd 只给 bat 那条路：bat 里写的是相对路径（`.\\python_embeded\\python.exe`、
    `ComfyUI\\main.py`），不在它自己那个目录里跑就什么都找不到。vbs 的工作目录由脚本自己指定。
    """
    vbs = Path(cfg.get("vbs") or "")
    if vbs.is_file():
        # //B：脚本自己出错时别弹模态框——弹了永远没人点，这一步就无声卡死在那儿
        return "vbs", ["wscript.exe", "//B", str(vbs)], None
    bat = Path(cfg.get("bat") or "")
    if bat.is_file():
        # 经 `start` 转一手：cmd 立刻退出，炉子不挂成 uvicorn 的子进程（裁定 2）。
        # 否则收工那句 taskkill /T 会顺着进程树把正在出图的炉子一起带走。
        #
        # 这一条给 Popen 的必须是**原样命令行字符串**，不能是 argv 列表：`start` 前面得先有个空标题
        # `""`，而列表形式经 subprocess.list2cmdline 会把那对引号转义成 `\"\"`——cmd 不认反斜杠转义，
        # 结果 start 一个进程都没起、外层 cmd 原地挂死（T011 验收 C2 真机撞上的就是这个）。
        return "bat", 'cmd.exe /c start "" /b ' + subprocess.list2cmdline([str(bat)]), str(bat.parent)
    return "none", None, None


def spawn(argv, cwd):
    """真点火。单独一个函数＝测试的假接缝（同 output.reveal 的先例，测试直接换掉它）。

    DETACHED_PROCESS：不继承画舫的控制台，所以 bat 结尾那个 `pause` 不会在主人桌面上留一个
    永远不关的黑窗（AGENTS §2 里那个要手工关掉的窗口）。三个流一律 DEVNULL：`pause` 拿到 EOF
    就过，不会隐形地赖着不走；代价是 bat 那一路炉子的输出无处可去（vbs 那一路由启动器自己
    重定向到脚本自己指定的日志，裁定 7 认它是启动器行为）。
    """
    return subprocess.Popen(
        argv, cwd=cwd,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )


def start(probe, url: str, cfg=None, timeout=None, interval=None) -> dict:
    """起炉子。已在监听→「已经开着」；不在→点一次火，然后最多轮询 timeout 秒等它应答。

    任何分支都回 200 形状的字典，**绝不吐 500**（与 gpu／zh 同口径）：起不来是给主人的一句人话，
    不是服务端故障。阻塞式轮询是本来的用意——请求挂着，前端按钮 disabled，主人看得见它在起。
    """
    global _IN_FLIGHT
    cfg = cfg if isinstance(cfg, dict) else ST.comfy_settings()
    timeout = POLL_TIMEOUT if timeout is None else timeout
    interval = POLL_INTERVAL if interval is None else interval
    port = _port_of(url)

    if _safe_probe(probe):
        return {"ok": True, "already": True, "port": port,
                "human": f"炉子已经开着（{port}），不用再起"}

    with _LOCK:
        if _IN_FLIGHT:
            # busy 单列一个键：这不是失败，前端不该红着报，也不该让主人以为要再点一次。
            # 分钟数取本次实际用的 timeout（不是模块常量），max(1,…) 是防止短超时说出「0 分钟」。
            return {"ok": False, "busy": True, "port": port,
                    "human": f"正在起炉子，稍等一下（最多 {max(1, int(timeout // 60))} 分钟）；"
                             "别再点了，点两次会双开"}
        kind, argv, cwd = launcher(cfg)
        if argv is None:
            return {"ok": False, "port": port, "kind": kind,
                    "human": "找不到起炉子要用的启动文件（设置里那两条路径都不在盘上），"
                             "这次没起成；先照老办法自己双击炉子"}
        try:
            spawn(argv, cwd)
        except OSError as e:
            return {"ok": False, "port": port, "kind": kind,
                    "human": f"炉子没点着：{e.strerror or e}；先照老办法自己双击炉子"}
        _IN_FLIGHT = True
        log.info("起炉子：走 %s 那条路（%s）", kind, argv[-1])

    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(interval)
            if _safe_probe(probe):
                return {"ok": True, "port": port, "kind": kind,
                        "human": f"炉子起好了（{port}），可以出图了"}
        return {"ok": False, "port": port, "kind": kind,
                "human": f"炉子点了火，但 {int(timeout)} 秒内没在 {port} 上应答。"
                         "可能还在往显存里装模型，过一会儿刷新看看；**别急着再点一次**，那会双开"}
    finally:
        with _LOCK:
            _IN_FLIGHT = False


def auto_start_once(probe, url: str) -> bool:
    """自动拉起：只有 lifespan 该调它（裁定 4 **禁止模块级**——import app.main 就会跑，
    而 tests/ 里的端点测试正是靠 import 起 TestClient 的，那样每跑一次单测就点一次炉子、吃 8G 显存）。

    返回有没有真去起（单测与验收 D 都靠它数次数）。三道闸，任一不过就一声不响：
    ① `HUAFANG_SKIP_AUTOSTART=1`（测试默认设它）② 本进程已经试过 ③ `comfy.auto_start` 不是 True。
    起在后台守护线程里，**绝不拖住 uvicorn 启动**——否则炉子没起来那三分钟里主人打开页面没人应。
    """
    global _AUTO_TRIED
    if os.environ.get("HUAFANG_SKIP_AUTOSTART") == "1":
        return False
    with _LOCK:
        if _AUTO_TRIED:
            return False
        _AUTO_TRIED = True
    if ST.comfy_settings()["auto_start"] is not True:
        return False
    if _safe_probe(probe):
        log.info("自动拉起跳过：炉子已经在 %s 上应答了", url)
        return False
    log.info("自动拉起：主人开了 comfy.auto_start，画舫这就去点炉子（会吃掉约 8G 显存）")
    threading.Thread(target=_auto_worker, args=(probe, url), daemon=True,
                     name="furnace-auto-start").start()
    return True


def _auto_worker(probe, url: str) -> None:
    """后台那一次拉起：成败都只记一条日志，失败不重试（卡面：每进程最多一次）。"""
    try:
        r = start(probe, url)
        log.info("自动拉起结果：%s", r.get("human"))
    except Exception as e:              # 线程里漏出去的异常没人接，会把 traceback 打到主人控制台上
        log.info("自动拉起没跑完：%s", e)
