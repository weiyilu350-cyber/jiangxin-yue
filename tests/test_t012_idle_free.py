"""T012 空闲收火：假时钟、假炉子、临时设置；不启动服务、不向真炉子发请求。"""
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from app import idle_free
from app import runner
from app import settings as ST
from app.comfy import ComfyError

ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeClient:
    base = "http://127.0.0.1:8188"

    def __init__(self):
        self.up = True
        self.probes = 0
        self.queues = 0
        self.q = {"queue_running": [], "queue_pending": []}
        self.probe_error = self.queue_error = self.submit_error = None
        self.on_queue = None
        self.submits = []

    def alive(self):
        self.probes += 1
        if self.probe_error:
            raise self.probe_error
        return self.up

    def queue(self):
        self.queues += 1
        if self.queue_error:
            raise self.queue_error
        if self.on_queue:
            self.on_queue()
        return self.q

    def submit(self, workflow, client_id):
        self.submits.append((workflow, client_id))
        if self.submit_error:
            raise self.submit_error
        return "fake-prompt"


class IdleBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = TemporaryDirectory(prefix="t012-", dir=ROOT / "tests")
        self.tmp = Path(self.tmpdir.name)
        self.real_settings = ST.SETTINGS_FILE
        ST.SETTINGS_FILE = self.tmp / "settings.json"
        self.addCleanup(self.tmpdir.cleanup)
        self.addCleanup(setattr, ST, "SETTINGS_FILE", self.real_settings)
        self.write({})
        self.clock = Clock()
        self.idle = idle_free.IdleFree(self.clock)
        self.client = FakeClient()
        self.posts = []
        self.post_error = None
        self.post_status = 200
        self.real_post = idle_free.httpx.post
        idle_free.httpx.post = self.post
        self.addCleanup(setattr, idle_free.httpx, "post", self.real_post)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.post_error:
            raise self.post_error
        return httpx.Response(self.post_status, request=httpx.Request("POST", url))

    def write(self, data):
        ST.SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def enable(self, minutes=15):
        self.write({"comfy": {"auto_free": True, "idle_minutes": minutes}})


class SettingsTests(IdleBase):
    def test_defaults_are_off_and_fifteen_minutes(self):
        self.assertIs(ST.DEFAULTS["comfy"]["auto_free"], False)
        self.assertEqual(ST.DEFAULTS["comfy"]["idle_minutes"], 15)
        self.assertEqual(ST.idle_free_settings(), {"auto_free": False, "idle_minutes": 15})

    def test_missing_corrupt_or_wrong_shaped_settings_are_safe(self):
        for raw in ("{半截", "[]", '"坏值"', "{}", '{"comfy":null}', '{"comfy":[]}'):
            with self.subTest(raw=raw):
                ST.SETTINGS_FILE.write_text(raw, encoding="utf-8")
                self.assertEqual(ST.idle_free_settings(), {"auto_free": False, "idle_minutes": 15})
                self.assertEqual(ST.SETTINGS_FILE.read_text(encoding="utf-8"), raw)
        ST.SETTINGS_FILE.unlink()
        self.assertEqual(ST.idle_free_settings(), {"auto_free": False, "idle_minutes": 15})
        self.assertFalse(ST.SETTINGS_FILE.exists())

    def test_switch_accepts_only_true_and_bad_minutes_fall_back(self):
        for value in ("true", 1, 0, None, [], {}):
            self.write({"comfy": {"auto_free": value, "idle_minutes": value}})
            got = ST.idle_free_settings()
            self.assertIs(got["auto_free"], False)
            self.assertEqual(got["idle_minutes"], min(120, max(5, value)) if type(value) is int else 15)
        for value in (True, False, "5", 5.5, None, [], {}):
            self.write({"comfy": {"auto_free": True, "idle_minutes": value}})
            self.assertEqual(ST.idle_free_settings(), {"auto_free": True, "idle_minutes": 15})

    def test_integer_minutes_are_clamped_on_both_sides(self):
        for given, want in ((-1, 5), (0, 5), (5, 5), (15, 15), (120, 120), (999, 120)):
            self.enable(given)
            self.assertEqual(ST.idle_free_settings()["idle_minutes"], want)
            cfg, err = ST.norm_idle_free({"auto_free": True, "idle_minutes": given})
            self.assertIsNone(err)
            self.assertEqual(cfg, {"auto_free": True, "idle_minutes": want})

    def test_write_rejects_bad_values_without_writing(self):
        before = ST.SETTINGS_FILE.read_bytes()
        for bad in (None, {}, [], {"auto_free": 1, "idle_minutes": 15},
                    {"auto_free": True}, {"auto_free": True, "idle_minutes": True},
                    {"auto_free": False, "idle_minutes": "15"},
                    {"auto_free": True, "idle_minutes": 5.5}):
            cfg, err = ST.norm_idle_free(bad)
            self.assertIsNone(cfg)
            self.assertIn("human", err)
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)

    def test_save_preserves_t011_and_other_blocks_and_ignores_extra_keys(self):
        before = {"comfy": {"auto_start": True, "vbs": "X:/fixed.vbs", "bat": "X:/fixed.bat"},
                  "ui": {"blur": 7}, "output": {"dir": "X:/pictures"}}
        self.write(before)
        cfg, err = ST.norm_idle_free({"auto_free": True, "idle_minutes": 8, "vbs": "evil"})
        self.assertIsNone(err)
        self.idle.save_settings(cfg)
        saved = json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved, {**before, "comfy": {**before["comfy"], **cfg}})
        self.assertEqual(ST.comfy_settings(), before["comfy"])
        ST.save_comfy_auto(False)
        self.assertEqual(ST.idle_free_settings(), cfg)
        self.assertFalse(ST.SETTINGS_FILE.with_suffix(".json.tmp").exists())

    def test_settings_take_effect_without_restart(self):
        self.assertFalse(ST.idle_free_settings()["auto_free"])
        self.idle.save_settings({"auto_free": True, "idle_minutes": 5})
        self.assertEqual(ST.idle_free_settings(), {"auto_free": True, "idle_minutes": 5})
        self.idle.save_settings({"auto_free": False, "idle_minutes": 120})
        self.assertEqual(ST.idle_free_settings(), {"auto_free": False, "idle_minutes": 120})


class IdleFreeTests(IdleBase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.clock.now = 900

    def test_auto_free_false_never_probes_or_frees(self):
        self.write({"comfy": {"auto_free": False}})
        self.assertFalse(self.idle.check(self.client))
        self.assertEqual((self.client.probes, self.client.queues, self.posts), (0, 0, []))

    def test_running_or_pending_queue_never_frees(self):
        for key in ("queue_running", "queue_pending"):
            with self.subTest(key=key):
                self.client.q = {"queue_running": [], "queue_pending": [], key: [["fake-job"]]}
                self.assertFalse(self.idle.check(self.client))
        self.assertEqual(self.posts, [])

    def test_missing_or_malformed_queue_is_not_empty(self):
        for bad in ({}, None, [], {"queue_running": []}, {"queue_pending": []},
                    {"queue_running": None, "queue_pending": []},
                    {"queue_running": [], "queue_pending": ""},
                    {"queue_running": {}, "queue_pending": []}):
            self.client.q = bad
            self.assertFalse(self.idle.check(self.client), repr(bad))
        self.assertEqual(self.posts, [])

    def test_empty_queue_and_timeout_free_once_with_correct_body(self):
        with self.assertLogs("app.idle_free", level="INFO") as logs:
            self.assertTrue(self.idle.check(self.client))
            self.assertFalse(self.idle.check(self.client))
            self.assertFalse(self.idle.check(self.client))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(len(self.posts), 1)
        url, kwargs = self.posts[0]
        self.assertEqual(url, self.client.base + "/free")
        self.assertEqual(kwargs["json"], {"unload_models": True, "free_memory": True})
        self.assertIs(kwargs["trust_env"], False)
        self.assertEqual(self.idle.status(self.client), {"alive": True, "standby": True})

    def test_unreachable_furnace_does_not_read_queue_or_free(self):
        self.client.up = False
        self.assertFalse(self.idle.check(self.client))
        self.assertEqual((self.client.queues, self.posts), (0, []))

    def test_probe_exception_is_safe(self):
        for error in (httpx.ReadTimeout("探活超时"), RuntimeError("探活异常")):
            self.client.probe_error = error
            self.assertFalse(self.idle.check(self.client))
        self.assertEqual((self.client.queues, self.posts), (0, []))

    def test_not_due_and_exact_deadline(self):
        self.clock.now = 899.999
        self.assertFalse(self.idle.check(self.client))
        self.assertEqual(self.posts, [])
        self.clock.now = 900
        self.assertTrue(self.idle.check(self.client))

    def test_submit_refreshes_timer_and_rearms_after_standby(self):
        self.assertTrue(self.idle.check(self.client))
        self.clock.now = 1000
        with self.idle.submitting():
            self.client.submit({}, "huafang")
        self.assertFalse(self.idle.status(self.client)["standby"])
        self.clock.now = 1899.999
        self.assertFalse(self.idle.check(self.client))
        self.assertEqual(len(self.posts), 1)
        self.clock.now = 1900
        self.assertTrue(self.idle.check(self.client))
        self.assertEqual(len(self.posts), 2)

    def test_queue_exception_logs_once_and_never_frees(self):
        self.client.queue_error = RuntimeError("读队列失败")
        with self.assertLogs("app.idle_free", level="INFO") as logs:
            self.assertFalse(self.idle.check(self.client))
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(self.posts, [])

    def test_free_exceptions_are_logged_once_without_standby(self):
        for error in (httpx.ConnectError("断连"), httpx.ReadTimeout("超时"), RuntimeError("异常")):
            with self.subTest(error=error):
                self.post_error = error
                with self.assertLogs("app.idle_free", level="INFO") as logs:
                    self.assertFalse(self.idle.check(self.client))
                self.assertEqual(len(logs.output), 1)
                self.assertFalse(self.idle.status(self.client)["standby"])

    def test_http_error_is_not_success_and_next_tick_can_retry(self):
        self.post_status = 500
        with self.assertLogs("app.idle_free", level="INFO") as logs:
            self.assertFalse(self.idle.check(self.client))
        self.assertEqual(len(logs.output), 1)
        self.assertFalse(self.idle.status(self.client)["standby"])
        self.post_status = 200
        self.assertTrue(self.idle.check(self.client))

    def test_settings_exception_is_swallowed(self):
        real = ST.idle_free_settings
        def boom():
            raise RuntimeError("设置读取失败")
        ST.idle_free_settings = boom
        try:
            with self.assertLogs("app.idle_free", level="INFO") as logs:
                self.assertFalse(self.idle.check(self.client))
            self.assertEqual(len(logs.output), 1)
            self.assertEqual(self.posts, [])
        finally:
            ST.idle_free_settings = real

    def test_switch_disabled_during_probe_prevents_free(self):
        self.client.on_queue = lambda: self.write({"comfy": {"auto_free": False}})
        self.assertFalse(self.idle.check(self.client))
        self.assertEqual(self.posts, [])

    def test_shutdown_during_probe_prevents_free(self):
        stopping = threading.Event()
        self.client.on_queue = stopping.set
        self.assertFalse(self.idle.check(self.client, stopping))
        self.assertEqual(self.posts, [])

    def test_offline_and_external_queue_activity_clear_standby(self):
        self.assertTrue(self.idle.check(self.client))
        self.client.q["queue_pending"] = [["external"]]
        self.assertEqual(self.idle.status(self.client), {"alive": True, "standby": False})
        self.client.q["queue_pending"] = []
        self.assertTrue(self.idle.check(self.client))
        self.client.up = False
        self.assertEqual(self.idle.status(self.client), {"alive": False, "standby": False})
        self.client.up = True
        self.assertFalse(self.idle.status(self.client)["standby"])

    def test_status_queue_exception_is_quiet(self):
        self.assertTrue(self.idle.check(self.client))
        self.client.queue_error = RuntimeError("队列读不到")
        self.assertEqual(self.idle.status(self.client), {"alive": False, "standby": False})

    def test_submit_in_flight_blocks_check_and_refreshes_timestamp(self):
        entered, release, checked = threading.Event(), threading.Event(), threading.Event()
        results = []
        def submit():
            with self.idle.submitting():
                entered.set()
                release.wait(3)
                self.client.submit({}, "huafang")
        def check():
            results.append(self.idle.check(self.client))
            checked.set()
        submitter = threading.Thread(target=submit)
        checker = threading.Thread(target=check)
        submitter.start()
        try:
            self.assertTrue(entered.wait(2))
            checker.start()
            self.assertFalse(checked.wait(0.02))
        finally:
            release.set()
            submitter.join(3)
            if checker.ident is not None:
                checker.join(3)
        self.assertFalse(submitter.is_alive())
        self.assertFalse(checker.is_alive())
        self.assertEqual(results, [False])
        self.assertEqual(self.posts, [])

    def test_two_checks_do_not_free_twice(self):
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.idle.check(self.client)))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(self.posts), 1)


class WorkerTests(IdleBase):
    def test_package_closes_idle_free_gate(self):
        import importlib
        import tests
        real = os.environ.pop("HUAFANG_SKIP_IDLE_FREE", None)
        try:
            importlib.reload(tests)
            self.assertEqual(os.environ["HUAFANG_SKIP_IDLE_FREE"], "1")
            self.assertIsNone(self.idle.start(self.client))
        finally:
            if real is None:
                os.environ.pop("HUAFANG_SKIP_IDLE_FREE", None)
            else:
                os.environ["HUAFANG_SKIP_IDLE_FREE"] = real

    def test_worker_waits_sixty_seconds_between_checks(self):
        waits = []
        class Stop:
            def wait(self, seconds):
                waits.append(seconds)
                return len(waits) > 2
            def is_set(self):
                return False
        self.enable(5)
        self.clock.now = 300
        self.idle._worker(self.client, Stop())
        self.assertEqual(waits, [60, 60, 60])
        self.assertEqual(len(self.posts), 1)


class EndpointTests(IdleBase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        real_token, real_reconcile = ST.load_token, runner.reconcile
        ST.load_token = lambda: "unit-test-only"
        runner.reconcile = lambda *a, **k: {}
        try:
            from app import main
            cls.main = main
        finally:
            ST.load_token, runner.reconcile = real_token, real_reconcile
        cls.TestClient = TestClient

    def setUp(self):
        super().setUp()
        self.real_client, self.real_idle = self.main.CLIENT, self.main.IDLE_FREE
        self.main.CLIENT, self.main.IDLE_FREE = self.client, self.idle
        self.addCleanup(setattr, self.main, "CLIENT", self.real_client)
        self.addCleanup(setattr, self.main, "IDLE_FREE", self.real_idle)
        self.http = self.TestClient(self.main.app, base_url="http://127.0.0.1:8788",
                                   headers={"Authorization": "Bearer " + self.main.TOKEN})
        self.addCleanup(self.http.close)

    def test_guard_rejects_wrong_token_and_origin(self):
        for method in ("get", "post"):
            for headers, want in (({"Authorization": "Bearer wrong"}, 401),
                                  ({"Origin": "http://evil.example"}, 403)):
                response = getattr(self.http, method)("/api/furnace/idle-free", headers=headers)
                self.assertEqual(response.status_code, want)

    def test_save_and_bootstrap_round_trip_with_clamped_minutes(self):
        response = self.http.post("/api/furnace/idle-free", json={"auto_free": True, "idle_minutes": 1})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["idle_minutes"], 5)
        boot = self.http.get("/api/bootstrap").json()
        self.assertEqual(boot["idle_free"], {"auto_free": True, "idle_minutes": 5})
        self.assertEqual(boot["comfy"], {"auto_start": False})
        self.assertNotIn("vbs", boot["idle_free"])
        self.assertEqual(self.posts, [])

    def test_invalid_settings_return_chinese_400_without_writes(self):
        before = ST.SETTINGS_FILE.read_bytes()
        for bad in ({}, {"auto_free": "true", "idle_minutes": 15},
                    {"auto_free": True, "idle_minutes": 15.5},
                    {"auto_free": True, "idle_minutes": False}):
            response = self.http.post("/api/furnace/idle-free", json=bad)
            self.assertEqual(response.status_code, 400)
            self.assertIn("human", response.json()["detail"])
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)

    def test_save_exception_returns_200_and_does_not_touch_real_settings(self):
        def boom(cfg):
            raise RuntimeError("写入异常")
        self.idle.save_settings = boom
        before = ST.SETTINGS_FILE.read_bytes()
        response = self.http.post("/api/furnace/idle-free", json={"auto_free": True, "idle_minutes": 15})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["ok"])
        self.assertIn("存不进磁盘", response.json()["human"])
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)

    def test_status_standby_and_probe_failure_are_200(self):
        self.enable(5)
        self.clock.now = 300
        self.assertTrue(self.idle.check(self.client))
        response = self.http.get("/api/furnace/idle-free")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"alive": True, "standby": True})
        self.client.probe_error = RuntimeError("探活失败")
        response = self.http.get("/api/furnace/idle-free")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"alive": False, "standby": False})

    def test_generate_refreshes_timestamp_and_next_generate_reloads_normally(self):
        old_jobs = dict(runner.JOBS)
        old_profile = self.main.PROFILES["anima_t2i"]
        self.main.PROFILES["anima_t2i"] = {**old_profile, "errors": []}
        self.enable(5)
        self.clock.now = 300
        self.assertTrue(self.idle.check(self.client))
        req = {"profile_id": "anima_t2i", "seed": 123, "steps": 4, "cfg": 4.0,
               "width": 256, "height": 256, "sampler_name": "euler", "scheduler": "simple",
               "unet": "anima-base-v1.0.safetensors", "positive": "1girl", "negative": "", "loras": []}
        try:
            for now in (400, 600):
                self.clock.now = now
                response = self.http.post("/api/generate", json=dict(req))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "running")
                self.assertFalse(self.idle.status(self.client)["standby"])
            self.assertEqual(len(self.client.submits), 2)
            self.assertTrue(all(client_id == "huafang" for _, client_id in self.client.submits))
            self.clock.now = 899.999
            self.assertFalse(self.idle.check(self.client))
            self.assertEqual(len(self.posts), 1)
            self.clock.now = 900
            self.assertTrue(self.idle.check(self.client))
        finally:
            self.main.PROFILES["anima_t2i"] = old_profile
            runner.JOBS.clear()
            runner.JOBS.update(old_jobs)

    def test_lifespan_starts_daemon_and_stops_it_without_waiting_a_minute(self):
        real = os.environ.pop("HUAFANG_SKIP_IDLE_FREE", None)
        worker = None
        try:
            with self.http:
                worker = next(t for t in threading.enumerate() if t.name == "furnace-idle-free")
                self.assertTrue(worker.daemon)
                self.assertEqual(self.posts, [])
            self.assertFalse(worker.is_alive())
        finally:
            if real is None:
                os.environ.pop("HUAFANG_SKIP_IDLE_FREE", None)
            else:
                os.environ["HUAFANG_SKIP_IDLE_FREE"] = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
