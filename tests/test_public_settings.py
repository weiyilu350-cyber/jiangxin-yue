"""R1：首次配置、现有文件保护与空路径边界；不访问真实模型或启动器。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app import settings as ST


class PublicSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix="huafang-setup-")
        self.original = ST.DATA, ST.SETTINGS_FILE
        ST.DATA = Path(self.tmp.name) / "data"
        ST.SETTINGS_FILE = ST.DATA / "settings.json"

    def tearDown(self):
        ST.DATA, ST.SETTINGS_FILE = self.original
        self.tmp.cleanup()

    def write(self, text):
        ST.DATA.mkdir(parents=True, exist_ok=True)
        ST.SETTINGS_FILE.write_text(text, encoding="utf-8")

    def test_first_start_creates_only_unconfigured_paths_and_prints_guide(self):
        with self.assertLogs("app.settings", level="WARNING") as captured:
            settings = ST.load_settings()
        self.assertEqual(json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8")),
                         {"models_dir": "", "output_dir": ""})
        self.assertEqual(settings["models_dir"], "")
        self.assertEqual(settings["output_dir"], "")
        self.assertEqual(ST.comfy_settings(), {"auto_start": False, "vbs": "", "bat": ""})
        guide = "\n".join(captured.output)
        for text in (str(ST.SETTINGS_FILE), "settings.example.json", "models_dir", "output_dir", "重启"):
            self.assertIn(text, guide)
        self.assertFalse((ST.DATA / "unconfigured-models").exists())

    def test_configured_paths_are_used_without_rewriting_existing_bytes(self):
        models, output = Path(self.tmp.name) / "models", Path(self.tmp.name) / "output"
        text = json.dumps({"models_dir": str(models), "output_dir": str(output),
                           "custom": "保留此值"}, ensure_ascii=False, indent=3) + "\n"
        self.write(text)
        before = ST.SETTINGS_FILE.read_bytes()
        with self.assertNoLogs("app.settings", level="WARNING"):
            settings = ST.load_settings()
        self.assertEqual(ST.models_root(settings), models)
        self.assertEqual(ST.models_root(), models)
        self.assertEqual(settings["output_dir"], str(output))
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)
        self.assertFalse(models.exists())
        self.assertFalse(output.exists())

    def test_corrupt_existing_file_is_preserved_and_guided(self):
        self.write('{"models_dir":')
        before = ST.SETTINGS_FILE.read_bytes()
        with self.assertLogs("app.settings", level="WARNING"):
            settings = ST.load_settings()
        self.assertEqual(settings["models_dir"], "")
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)

    def test_relative_and_invalid_model_roots_never_become_current_directory(self):
        for value in (None, "", "  ", "models", "../models", False, [], {}):
            with self.subTest(value=value):
                root = ST.models_root({"models_dir": value})
                self.assertEqual(root, ST.DATA / "unconfigured-models")
                self.assertNotEqual(root, Path.cwd())

    def test_blank_optional_tables_use_public_local_defaults(self):
        self.write('{"trigger_map":"", "zh":{"table":""}}')
        with self.assertLogs("app.settings", level="WARNING"):
            settings = ST.load_settings()
        self.assertEqual(settings["trigger_map"], ST.DEFAULTS["trigger_map"])
        self.assertEqual(settings["zh"]["table"], ST.DEFAULTS["zh"]["table"])

    def test_saving_existing_switches_preserves_configured_roots(self):
        configured = {"models_dir": str(Path(self.tmp.name) / "models"),
                      "output_dir": str(Path(self.tmp.name) / "output")}
        self.write(json.dumps(configured))
        ST.save_comfy_auto(False)
        ST.save_output_dir("")
        saved = json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8"))
        for key, value in configured.items():
            self.assertEqual(saved[key], value)

    def test_non_object_existing_file_is_preserved(self):
        self.write('["不是设置对象"]')
        before = ST.SETTINGS_FILE.read_bytes()
        with self.assertLogs("app.settings", level="WARNING"):
            ST.load_settings()
        self.assertEqual(ST.SETTINGS_FILE.read_bytes(), before)
