"""T006 显卡忙／显存紧只读提示：判级矩阵＋探不到路径＋阈值兜底＋端点守卫。

假 ComfyUI 客户端顶掉 httpx（与 zh 同法，不引入 mock 框架）。
实测口径照卡面：vram_free 是字节、空闲只剩 ≈1.3G、阈值默认 700MB、tight > busy。
"""
import json
import tempfile
import unittest
from pathlib import Path

from app import gpu as GPU
from app import settings as ST
from app.comfy import ComfyError

TOTAL = 8585216000          # 小唯实测：8G 卡 vram_total（字节）
FREE_IDLE = 1396844042      # 炉子空闲（模型驻留）时 vram_free ≈1.3G（字节）
MB = 1024 * 1024


def stats(free=FREE_IDLE, total=TOTAL):
    return {"devices": [{"vram_free": free, "vram_total": total}]}


def queue(running=0, pending=0):
    """炉子 /queue 的真形状：两条列表。"""
    return {"queue_running": [0] * running, "queue_pending": [0] * pending}


class FakeComfy:
    """只实现 gpu_status 真会调的两个只读方法。"""

    def __init__(self, st=None, q=None, st_err=None, q_err=None):
        self.st, self.q, self.st_err, self.q_err = st, q, st_err, q_err

    def system_stats(self):
        if self.st_err:
            raise self.st_err
        return self.st

    def queue(self):
        if self.q_err:
            raise self.q_err
        return self.q


DOWN = ComfyError("comfy_down", "连不上 ComfyUI，它启动了吗？", "fake")


class LevelMatrixTests(unittest.TestCase):
    def call(self, free=FREE_IDLE, running=0, pending=0, warn=700):
        return GPU.gpu_status(FakeComfy(st=stats(free), q=queue(running, pending)), warn)

    def test_idle_is_ok_with_mb_and_counts(self):
        b = self.call()
        self.assertEqual((b["ok"], b["level"]), (True, "ok"))
        self.assertEqual(b["mem_free_mb"], FREE_IDLE // MB)
        self.assertEqual(b["mem_total_mb"], TOTAL // MB)
        self.assertEqual(b["queue"], {"running": 0, "pending": 0})

    def test_low_free_is_tight(self):
        self.assertEqual(self.call(free=600 * MB)["level"], "tight")

    def test_tight_beats_busy(self):
        """显存紧时哪怕队列空也报紧；队列忙＋显存紧＝紧（优先级 tight > busy）。"""
        self.assertEqual(self.call(free=600 * MB, running=1, pending=2)["level"], "tight")

    def test_running_alone_is_busy(self):
        b = self.call(running=1)
        self.assertEqual((b["level"], b["queue"]["running"]), ("busy", 1))

    def test_pending_alone_is_busy(self):
        b = self.call(pending=2)
        self.assertEqual((b["level"], b["queue"]["pending"]), ("busy", 2))

    def test_free_equal_warn_is_ok(self):
        """判紧是严格小于：正好等于阈值还不算紧（宁缺毋滥报）。"""
        self.assertEqual(self.call(free=700 * MB)["level"], "ok")

    def test_free_one_below_warn_is_tight(self):
        self.assertEqual(self.call(free=700 * MB - 1)["level"], "tight")

    def test_warn_is_configurable(self):
        self.assertEqual(self.call(warn=2000)["level"], "tight")


class ProbeFailTests(unittest.TestCase):
    def call(self, **kw):
        return GPU.gpu_status(FakeComfy(**kw), 700)

    def test_devices_empty_list_is_none(self):
        b = self.call(st={"devices": []})
        self.assertEqual((b["ok"], b["level"]), (False, "none"))
        self.assertTrue(b["human"])

    def test_missing_vram_fields_is_none(self):
        self.assertEqual(self.call(st={"devices": [{}]})["level"], "none")

    def test_vram_free_not_number_is_none(self):
        self.assertEqual(self.call(st={"devices": [{"vram_free": "abc",
                                                    "vram_total": TOTAL}]})["level"], "none")

    def test_stats_empty_dict_is_none(self):
        self.assertEqual(self.call(st={})["level"], "none")

    def test_stats_not_dict_is_none(self):
        self.assertEqual(self.call(st=["devices"])["level"], "none")

    def test_queue_missing_keys_counts_zero(self):
        b = self.call(st=stats(), q={})
        self.assertEqual((b["ok"], b["level"], b["queue"]),
                         (True, "ok", {"running": 0, "pending": 0}))

    def test_queue_counts_not_list_counts_zero(self):
        """雷区 4 的形狀兜底：计数读不出列表就当 0，提示不崩也不乱报忙。"""
        b = self.call(st=stats(), q={"queue_running": 3, "queue_pending": "x"})
        self.assertEqual((b["level"], b["queue"]), ("ok", {"running": 0, "pending": 0}))

    def test_comfy_down_is_200_shape_not_raise(self):
        b = self.call(st_err=DOWN)
        self.assertEqual((b["ok"], b["level"]), (False, "none"))
        self.assertEqual(b["human"], "连不上 ComfyUI，它启动了吗？")

    def test_queue_error_also_silent(self):
        b = self.call(st=stats(), q_err=ComfyError("comfy_bad_json", "队列读取异常：炉子回的不是 JSON", "x"))
        self.assertEqual((b["ok"], b["level"]), (False, "none"))
        self.assertIn("队列", b["human"])


class WarnMbTests(unittest.TestCase):
    def setUp(self):
        self._real = ST.SETTINGS_FILE
        self._tmp = Path(tempfile.mkdtemp(prefix="huafang-gpu-"))
        ST.SETTINGS_FILE = self._tmp / "settings.json"

    def tearDown(self):
        ST.SETTINGS_FILE = self._real
        for p in self._tmp.iterdir():
            p.unlink()
        self._tmp.rmdir()

    def write(self, obj):
        ST.SETTINGS_FILE.write_text(json.dumps(obj), encoding="utf-8")

    def test_missing_file_is_default(self):
        self.assertEqual(ST.gpu_warn_mb(), 700)

    def test_custom_value(self):
        self.write({"gpu": {"warn_free_mb": 1200}})
        self.assertEqual(ST.gpu_warn_mb(), 1200)

    def test_string_falls_back(self):
        self.write({"gpu": {"warn_free_mb": "900"}})
        self.assertEqual(ST.gpu_warn_mb(), 700)

    def test_negative_falls_back(self):
        self.write({"gpu": {"warn_free_mb": -5}})
        self.assertEqual(ST.gpu_warn_mb(), 700)

    def test_zero_falls_back(self):
        self.write({"gpu": {"warn_free_mb": 0}})
        self.assertEqual(ST.gpu_warn_mb(), 700)

    def test_bool_falls_back(self):
        self.write({"gpu": {"warn_free_mb": True}})
        self.assertEqual(ST.gpu_warn_mb(), 700)

    def test_gpu_block_not_dict_falls_back(self):
        self.write({"gpu": [700]})
        self.assertEqual(ST.gpu_warn_mb(), 700)

    def test_corrupt_file_falls_back(self):
        ST.SETTINGS_FILE.write_text("{半截", encoding="utf-8")
        self.assertEqual(ST.gpu_warn_mb(), 700)


class EndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app import main
        cls.main = main
        cls.http = TestClient(main.app, base_url="http://127.0.0.1:8788",
                              headers={"Authorization": "Bearer " + main.TOKEN})

    def setUp(self):
        self._real = ST.SETTINGS_FILE
        self._tmp = Path(tempfile.mkdtemp(prefix="huafang-gpu-ep-"))
        ST.SETTINGS_FILE = self._tmp / "settings.json"
        ST.SETTINGS_FILE.write_text("{}", encoding="utf-8")
        self._client = self.main.CLIENT
        self.main.CLIENT = FakeComfy(st=stats(), q=queue())

    def tearDown(self):
        self.main.CLIENT = self._client
        ST.SETTINGS_FILE = self._real
        for p in self._tmp.iterdir():
            p.unlink()
        self._tmp.rmdir()

    def test_ok_shape(self):
        r = self.http.get("/api/gpu")
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual(b["ok"], True)
        self.assertEqual(b["level"], "ok")
        self.assertEqual(set(b), {"ok", "level", "mem_free_mb", "mem_total_mb", "queue"})

    def test_wrong_token_is_401(self):
        r = self.http.get("/api/gpu", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r.status_code, 401)

    def test_bad_origin_is_403(self):
        r = self.http.get("/api/gpu", headers={"Origin": "http://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_furnace_down_is_200_ok_false(self):
        self.main.CLIENT = FakeComfy(st_err=DOWN)
        r = self.http.get("/api/gpu")
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual((b["ok"], b["level"]), (False, "none"))
        self.assertTrue(b["human"])

    def test_warn_read_from_file_per_call(self):
        ST.SETTINGS_FILE.write_text(json.dumps({"gpu": {"warn_free_mb": 99999999}}),
                                    encoding="utf-8")
        b = self.http.get("/api/gpu").json()
        self.assertEqual(b["level"], "tight")
        self.assertEqual(b["mem_free_mb"], FREE_IDLE // MB)

    def test_bad_warn_in_file_falls_back_to_ok(self):
        ST.SETTINGS_FILE.write_text(json.dumps({"gpu": {"warn_free_mb": "abc"}}),
                                    encoding="utf-8")
        self.assertEqual(self.http.get("/api/gpu").json()["level"], "ok")


if __name__ == "__main__":
    unittest.main()
