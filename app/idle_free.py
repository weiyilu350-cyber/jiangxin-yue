"""T012 空闲收火：只用炉子的 HTTP 接口卸载模型，不管理进程。"""
import logging
import os
import threading
import time
from contextlib import contextmanager

import httpx

from . import furnace
from . import settings as ST

log = logging.getLogger("app.idle_free")
CHECK_INTERVAL = 60.0


def _empty_queue(queue) -> bool:
    # 缺字段或坏形状不能当成空队列，否则读不懂炉子的回答也会卸载模型。
    return isinstance(queue, dict) and all(
        isinstance(queue.get(key), list) and not queue[key]
        for key in ("queue_running", "queue_pending")
    )


class IdleFree:
    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._last_submit = self._clock()
        self._standby = False
        self._lock = threading.RLock()

    @contextmanager
    def submitting(self):
        # 与检查、/free 共用锁：队列刚判空时，不能夹进一发新提交。
        with self._lock:
            self._last_submit = self._clock()
            self._standby = False
            yield

    def save_settings(self, cfg: dict) -> None:
        # 关开关与收火也要排队，确保保存成功之后不会补发旧检查的 /free。
        with self._lock:
            ST.save_idle_free(cfg)

    def check(self, client, stopping=None) -> bool:
        try:
            with self._lock:
                cfg = ST.idle_free_settings()
                if not cfg["auto_free"]:
                    return False
                if not furnace._safe_probe(client.alive):
                    self._standby = False
                    return False
                if not _empty_queue(client.queue()):
                    self._standby = False
                    return False
                # 已收过火就安静待命；有新提交或观测到队列忙，才允许下一轮收火。
                if self._standby:
                    return False
                cfg = ST.idle_free_settings()
                if (not cfg["auto_free"]
                        or self._clock() - self._last_submit < cfg["idle_minutes"] * 60
                        or (stopping is not None and stopping.is_set())):
                    return False
                response = httpx.post(
                    client.base + "/free",
                    json={"unload_models": True, "free_memory": True},
                    timeout=10, trust_env=False,
                )
                response.raise_for_status()
                self._standby = True
            log.info("空闲收火成功：已请求卸载模型、释放显存，炉子待命")
            return True
        except Exception as e:
            # 后台辅助动作失败只记一条，不能把出图端点或前端拖成报错。
            log.info("空闲收火失败：%s", e)
            return False

    def status(self, client) -> dict:
        try:
            with self._lock:
                alive = furnace._safe_probe(client.alive)
                if not alive or (self._standby and not _empty_queue(client.queue())):
                    self._standby = False
                return {"alive": alive, "standby": self._standby}
        except Exception:
            with self._lock:
                self._standby = False
            return {"alive": False, "standby": False}

    def start(self, client):
        """只由 lifespan 拉起；环境闸在创建线程之前，import 不会触炉。"""
        if os.environ.get("HUAFANG_SKIP_IDLE_FREE") == "1":
            return None
        stopping = threading.Event()
        thread = threading.Thread(target=self._worker, args=(client, stopping),
                                  name="furnace-idle-free", daemon=True)
        thread.start()
        return stopping, thread

    def _worker(self, client, stopping) -> None:
        # Event.wait 让服务退出时能立刻唤醒，不必再等满一分钟。
        while not stopping.wait(CHECK_INTERVAL):
            self.check(client, stopping)
