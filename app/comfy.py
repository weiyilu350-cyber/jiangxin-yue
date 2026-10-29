"""ComfyUI HTTP API 薄封装：只走公开接口，不 import 它的内部代码。"""
import httpx


class ComfyError(Exception):
    def __init__(self, code: str, message: str, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


_TRANSLATE = [
    ("value_not_in_list", "模型/笔文件不在 ComfyUI 清单里（被删、改名或目录没扫到）"),
    ("connection refused", "连不上 ComfyUI，它启动了吗？"),
    ("out of memory", "显存不够：先关掉别的出图或训练任务再试"),
    ("cuda", "显卡报错（CUDA）：点「高级详情」看原文"),
]


def translate(detail: str) -> str:
    low = (detail or "").lower()
    for key, msg in _TRANSLATE:
        if key in low:
            return msg
    return "出图失败了，点「高级详情」看原始错误"


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 60.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, timeout=None, **kw):
        try:
            return httpx.get(self.base + path, timeout=timeout or self.timeout, trust_env=False, **kw)
        except httpx.ConnectError as e:
            raise ComfyError("comfy_down", "连不上 ComfyUI，它启动了吗？", str(e)) from e

    def alive(self) -> bool:
        try:
            return self._get("/system_stats", timeout=5).status_code == 200
        except ComfyError:
            return False

    def submit(self, workflow: dict, client_id: str) -> str:
        try:
            r = httpx.post(
                self.base + "/prompt",
                json={"prompt": workflow, "client_id": client_id},
                timeout=30,
                trust_env=False,
            )
        except httpx.ConnectError as e:
            raise ComfyError("comfy_down", "连不上 ComfyUI，它启动了吗？", str(e)) from e
        if r.status_code != 200:
            raise ComfyError("submit_failed", translate(r.text), r.text[:2000])
        return r.json()["prompt_id"]

    def history(self, prompt_id: str):
        r = self._get(f"/history/{prompt_id}", timeout=15)
        return (r.json() or {}).get(prompt_id)

    def queue(self) -> dict:
        """T005 对账用：`queue_running`／`queue_pending` 两条列表。读不懂就报人话，不吐 500。"""
        return self._json("/queue", "队列", timeout=10)

    def system_stats(self) -> dict:
        """T006 显卡状态只读接口：`devices[0]` 里有 vram_total/vram_free（字节）。"""
        return self._json("/system_stats", "显卡状态", timeout=10)

    def history_recent(self, max_items: int = 50) -> dict:
        """T005 对账用：最近 max_items 条已完成记录（prompt_id → entry）。"""
        return self._json("/history", "历史", timeout=15, params={"max_items": max_items})

    def _json(self, path: str, what: str, timeout=None, **kw) -> dict:
        r = self._get(path, timeout=timeout, **kw)
        if r.status_code != 200:
            raise ComfyError("comfy_http", f"炉子的{what}接口回了 {r.status_code}", r.text[:500])
        try:
            data = r.json()
        except ValueError as e:
            raise ComfyError("comfy_bad_json", f"{what}读取异常：炉子回的不是 JSON", r.text[:500]) from e
        return data if isinstance(data, dict) else {}

    def view_bytes(self, filename: str, subfolder: str = "", type_: str = "output") -> bytes:
        r = self._get(
            "/view",
            params={"filename": filename, "subfolder": subfolder, "type": type_},
            timeout=60,
        )
        if r.status_code != 200:
            raise ComfyError("view_failed", "取不到图片", r.text[:500])
        return r.content
