"""T007 v2：上传边界、模糊补丁、代理环境与启动异常隔离。"""
import io
import json
import runpy
import tempfile
import unittest
from pathlib import Path

from fastapi import UploadFile
from fastapi.testclient import TestClient

from app import backgrounds as BG
from app import comfy, main, settings as ST, zh

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="red"/></svg>'


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="huafang-bg-")
        self.real_bg, self.real_settings = ST.BG_DIR, ST.SETTINGS_FILE
        ST.BG_DIR = Path(self.tmp.name) / "bg"
        ST.SETTINGS_FILE = Path(self.tmp.name) / "settings.json"
        ST.SETTINGS_FILE.write_text('{"output":{"dir":""}}', encoding="utf-8")
        self.http = TestClient(main.app, base_url="http://127.0.0.1:8788",
                               headers={"Authorization": "Bearer " + main.TOKEN})

    def tearDown(self):
        self.http.close()
        ST.BG_DIR, ST.SETTINGS_FILE = self.real_bg, self.real_settings
        self.tmp.cleanup()

    def upload(self, name="自己的图.svg", data=SVG, **kw):
        return self.http.post("/api/bg/upload", files={"file": (name, data, "image/svg+xml")}, **kw)

    def test_upload_duplicate_never_overwrites_and_does_not_save_ui(self):
        before = ST.SETTINGS_FILE.read_bytes()
        a, b = self.upload(), self.upload()
        self.assertEqual((a.status_code, b.status_code), (200, 200))
        self.assertEqual(a.json()["filename"], "自己的图.svg")
        self.assertEqual(b.json()["filename"], "自己的图_1.svg")
        self.assertEqual((ST.BG_DIR / a.json()["filename"]).read_bytes(), SVG)
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)
        self.assertIn(b.json()["filename"], ST.bg_files())

    def test_paths_and_windows_names_sanitized(self):
        for name, expected in [("../../a.svg", "a.svg"), (r"C:\fakepath\a?.svg", "a_.svg"), ("CON.svg", "_CON.svg")]:
            self.assertEqual(BG.clean_name(name), expected)
        r = self.upload("../../escape.svg")
        self.assertEqual(r.status_code, 200)
        self.assertTrue((ST.BG_DIR / "escape.svg").is_file())
        self.assertFalse((Path(self.tmp.name) / "escape.svg").exists())

    def test_fake_and_wrong_extension_leave_settings_and_directory_unchanged(self):
        before = ST.SETTINGS_FILE.read_bytes()
        for name, data in [("fake.png", b"not an image"), ("fake.png", SVG), ("a.txt", SVG), ("empty.jpg", b"")]:
            r = self.upload(name, data)
            self.assertEqual(r.status_code, 400)
            self.assertTrue(r.json()["detail"]["human"])
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)
        self.assertEqual(ST.bg_files(), [])

    def test_oversize_400(self):
        r = self.upload("big.svg", b"x" * (BG.MAX_BYTES + 1))
        self.assertEqual(r.status_code, 400)
        self.assertIn("20MB", r.json()["detail"]["human"])

    def test_request_limit_without_content_length(self):
        r = self.http.post("/api/bg/upload", content=iter([b"x" * (BG.MAX_BYTES + 65537)]),
                           headers={"Content-Type": "multipart/form-data; boundary=x"})
        self.assertEqual(r.status_code, 400)

    def test_malformed_missing_and_multiple_files_400(self):
        for kwargs in ({"content": b"bad"}, {"files": {"other": ("a.svg", SVG)}},
                       {"files": [("file", ("a.svg", SVG)), ("file", ("b.svg", SVG))]}):
            self.assertEqual(self.http.post("/api/bg/upload", **kwargs).status_code, 400)

    def test_guard_upload(self):
        self.assertEqual(self.upload(headers={"Authorization": "wrong"}).status_code, 401)
        self.assertEqual(self.upload(headers={"Origin": "http://evil.example"}).status_code, 403)

    def test_write_failure_400(self):
        ST.BG_DIR.write_text("not a directory", encoding="utf-8")
        r = self.upload()
        self.assertEqual(r.status_code, 400)
        self.assertTrue(r.json()["detail"]["human"])

    def test_svg_active_content_rejected(self):
        for data in (b'<svg><script>alert(1)</script></svg>', b'<svg onload="alert(1)"/>',
                     b'<svg><image href="http://example.com/x.png"/></svg>'):
            self.assertEqual(self.upload(data=data).status_code, 400)

    def test_real_png_and_corrupt_crc(self):
        # 使用自含的有效 PNG 夹具，避免依赖随包背景文件。
        data = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d49444154789c63f8cfc0f01f00050001ff89993d1d000000004945"
            "4e44ae426082"
        )
        self.assertEqual(self.upload("sample.png", data).status_code, 200)
        self.assertEqual(self.upload("bad.png", data[:-5]).status_code, 400)

    def test_svg_local_gradients_remain_usable(self):
        data = (self.real_bg / 'sakura-day.svg').read_bytes()
        self.assertEqual(self.upload('gradient.svg', data).status_code, 200)

    def test_blur_defaults_invalid_read_and_patch_preserves_other_fields(self):
        for bad in (None, "10", True, -1, 41, 1.5, [], {}):
            ST.SETTINGS_FILE.write_text(json.dumps({"ui": {"blur": bad}}), encoding="utf-8")
            self.assertEqual(ST.ui_settings()["blur"], 10)
        ST.SETTINGS_FILE.write_text('{"ui":{"density":"compact"}}', encoding="utf-8")
        for value in (0, 10, 40):
            r = self.http.post("/api/ui", json={"blur": value})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["ui"], {"bg": "off", "density": "compact", "blur": value})

    def test_invalid_blur_writes_nothing(self):
        before = ST.SETTINGS_FILE.read_bytes()
        for bad in (None, "10", True, -1, 41, 1.5, [], {}):
            r = self.http.post("/api/ui", json={"blur": bad})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(r.json()["detail"]["code"], "bad_blur")
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)


class ProxyTests(unittest.TestCase):
    def test_all_http_calls_ignore_environment(self):
        calls = []
        real_get, real_post = comfy.httpx.get, comfy.httpx.post
        class Reply:
            status_code = 200
            def json(self):
                return {"prompt_id": "example"}
        def fake(*args, **kw):
            calls.append(kw)
            return Reply()
        comfy.httpx.get = comfy.httpx.post = fake
        try:
            c = comfy.ComfyClient("http://127.0.0.1:8188")
            c._get("/queue")
            c.submit({}, "huafang")
            zh._default_probe("http://127.0.0.1:11434", 1)
            zh._default_post("http://127.0.0.1:11434", {}, 1)
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(k.get("trust_env") is False for k in calls))
        finally:
            comfy.httpx.get, comfy.httpx.post = real_get, real_post

    def test_startup_survives_arbitrary_reconcile_exception(self):
        real_reconcile, real_token = main.runner.reconcile, ST.load_token
        def explode(*args):
            raise RuntimeError("startup probe failed")
        main.runner.reconcile = explode
        ST.load_token = lambda: "unit-test-only"
        try:
            namespace = runpy.run_module("app.main", run_name="app._t007_startup_check")
            self.assertIsNotNone(namespace["app"])
        finally:
            main.runner.reconcile, ST.load_token = real_reconcile, real_token
