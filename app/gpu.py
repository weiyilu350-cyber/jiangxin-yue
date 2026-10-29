"""T006 显卡忙／显存紧只读提示：数据源只有炉子两个只读 HTTP 接口（D102 铁律）。

不探进程、不读日志、文案不点名训练；提示只是提示——不 disable 出图、不暂停队列、不杀进程。
判级优先级 tight > busy：显存紧时哪怕炉子队列是空的也报紧（卡正被外部程序吃着）。
"""
from .comfy import ComfyError

MB = 1024 * 1024


def _mb(value):
    """字节→MB 整除；缺字段／非数字→None（宁缺毋滥报，卡 §实测口径）。"""
    try:
        return int(value) // MB
    except (TypeError, ValueError):
        return None


def _count(items) -> int:
    """炉子队列是两条列表（T005 口径）；形状不对按 0 算——只影响提示文案，不影响出图。"""
    return len(items) if isinstance(items, (list, tuple)) else 0


def gpu_status(client, warn_mb: int) -> dict:
    """→ {ok, level, mem_free_mb, mem_total_mb, queue:{running,pending}}；
    炉子不可达／探不到 → {ok:false, level:"none", human}（端点照样回 200，与 zh 同口径）。"""
    try:
        stats = client.system_stats()
        q = client.queue()
    except ComfyError as e:
        return {"ok": False, "level": "none", "human": e.message}
    devices = stats.get("devices") if isinstance(stats, dict) else None
    dev = devices[0] if isinstance(devices, list) and devices and isinstance(devices[0], dict) else None
    free = _mb(dev.get("vram_free")) if dev else None
    total = _mb(dev.get("vram_total")) if dev else None
    if free is None or total is None:
        return {"ok": False, "level": "none",
                "human": "探不到显存信息（无显卡或字段缺失），先不显示提示"}
    running = _count(q.get("queue_running"))
    pending = _count(q.get("queue_pending"))
    if free < warn_mb:
        level = "tight"
    elif running + pending >= 1:
        level = "busy"
    else:
        level = "ok"
    return {"ok": True, "level": level, "mem_free_mb": free, "mem_total_mb": total,
            "queue": {"running": running, "pending": pending}}
