"""T011 起炉子开关：宽容读侧／写侧／launcher 三条路／start 四路＋in-flight 锁／自动拉起三道闸／端点守卫。

红线兜底：**本文件一次真 Popen 都不发**——真点炉子要吃 8G 显存、和训练抢卡，
所以 `furnace.spawn` 一律换成记录仪（同 test_output 里 `OUT.reveal = self.opened.append` 的先例）。
`ST.SETTINGS_FILE` 全程指到临时文件（C 卡血泪：端点测试会写真 settings.json）；
`data/token.txt` 在 tearDownClass 里复原（B 卡裁定 5）。
"""
import ast
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from app import furnace
from app import settings as ST

ROOT = Path(__file__).resolve().parent.parent

# 公开副本不替用户猜启动器路径；HTTP 端点仍不允许传入或保存启动路径。
VBS_DEFAULT = ""
BAT_DEFAULT = ""
AUTO_THREAD = "furnace-auto-start"


class FakeSpawn:
    """顶掉 furnace.spawn 的记录仪。err 非空＝模拟点火失败（OSError）。"""

    def __init__(self, err=None):
        self.calls = []                 # [(argv, cwd)]
        self.err = err

    def __call__(self, argv, cwd):
        if self.err is not None:
            raise self.err
        self.calls.append((argv, cwd))      # 原样收着：bat 那条路给的是一条命令行**字符串**
        return "<FakeProc>"

    @property
    def n(self):
        return len(self.calls)

    def argvs(self):
        return [a for a, _ in self.calls]


def cmdline(argv):
    """Popen 真交给 CreateProcess 的那一条命令行（列表就按 subprocess 的规矩渲染一遍）。

    断言 argv **列表**的形状验不出真 bug：列表里那个 `'""'` 元素经 subprocess.list2cmdline
    会变成 `\\"`，而 cmd 不认反斜杠转义——T011 验收 C2 真机就是这么挂的：start 一个进程都没起、
    外层 cmd 原地僵死、炉子 180 秒没上来。要钉就得钉渲染之后的字符串。
    """
    return argv if isinstance(argv, str) else subprocess.list2cmdline(list(argv))


class FakeProbe:
    """假探活：照脚本一次一个值，脚本走完就重复最后一个（＝稳态，与真炉子一致）。"""

    def __init__(self, script):
        self.script = list(script) if script else [False]
        self.n = 0

    def __call__(self):
        v = self.script[min(self.n, len(self.script) - 1)]
        self.n += 1
        return bool(v)


class BoomProbe:
    """探活抛异常：炉子正在起来那几秒端口通了但 HTTP 不答话，httpx 就是这么抛的。"""

    def __init__(self, exc):
        self.exc = exc
        self.n = 0

    def __call__(self):
        self.n += 1
        raise self.exc


class GateProbe:
    """第一发快速回 False（放过 start 开头那句「已经开着吗」），之后卡在闸门上不动。

    直接拿 FakeProbe 验并发是错的：start() 开头就探一次，那一下要是卡住，
    主线程根本看不到「已经进了轮询、锁已经立起来」那个状态。
    """

    def __init__(self, gate, entered):
        self.gate, self.entered = gate, entered
        self.n = 0

    def __call__(self):
        self.n += 1
        if self.n > 1:
            self.entered.set()
            self.gate.wait(10)
        return False


class FakeAlive:
    """顶掉 main.CLIENT：只实现端点真会调的 alive()。"""

    def __init__(self, script):
        self.probe = FakeProbe(script)

    def alive(self):
        return self.probe()


class FurnaceBase(unittest.TestCase):
    """公共夹具：临时 settings＋假 spawn＋进程内旗子归零＋短轮询（别真等 180 秒）。"""

    def setUp(self):
        self._tmpdir = TemporaryDirectory(prefix="huafang-t011-")
        self.tmp = Path(self._tmpdir.name)
        self._real_file = ST.SETTINGS_FILE
        ST.SETTINGS_FILE = self.tmp / "settings.json"
        ST.SETTINGS_FILE.write_text("{}", encoding="utf-8")

        self._real_spawn = furnace.spawn
        self._real_start = furnace.start
        self.fake = FakeSpawn()
        furnace.spawn = self.fake

        self._flags = (furnace._IN_FLIGHT, furnace._AUTO_TRIED)
        furnace._IN_FLIGHT = False
        furnace._AUTO_TRIED = False
        self._poll = (furnace.POLL_TIMEOUT, furnace.POLL_INTERVAL)
        furnace.POLL_TIMEOUT = 0.3
        furnace.POLL_INTERVAL = 0.02

        self._env = os.environ.get("HUAFANG_SKIP_AUTOSTART")
        os.environ["HUAFANG_SKIP_AUTOSTART"] = "1"        # 裁定 4：测试默认设它

    def tearDown(self):
        # 顺序要紧：先收干净自动拉起的野线程，再还原 spawn。
        # 反过来的话，一个还在轮询的线程会在还原之后摸到真 Popen——那就是真起炉子吃 8G 显存。
        self.join_auto()
        furnace.spawn = self._real_spawn
        furnace.start = self._real_start
        furnace._IN_FLIGHT, furnace._AUTO_TRIED = self._flags
        furnace.POLL_TIMEOUT, furnace.POLL_INTERVAL = self._poll
        if self._env is None:
            os.environ.pop("HUAFANG_SKIP_AUTOSTART", None)
        else:
            os.environ["HUAFANG_SKIP_AUTOSTART"] = self._env
        ST.SETTINGS_FILE = self._real_file
        self._tmpdir.cleanup()

    # ---- 小工具
    def join_auto(self, timeout=10.0):
        for t in list(threading.enumerate()):
            if t.name == AUTO_THREAD:
                t.join(timeout)

    def allow_autostart(self):
        """解开裁定 4 那道环境闸（只有真要验自动拉起的测试才解）。"""
        os.environ.pop("HUAFANG_SKIP_AUTOSTART", None)

    def write_settings(self, obj):
        ST.SETTINGS_FILE.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def raw(self):
        return ST.SETTINGS_FILE.read_text(encoding="utf-8")

    def fake_file(self, name):
        """在临时目录里造一个真文件，喂 launcher 的 is_file 判断。"""
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("rem 假启动器，单测用，别真跑\n", encoding="utf-8")
        return str(p)

    def cfg_with(self, vbs=True, bat=True, auto_start=False):
        """一份 launcher 能吃的 cfg。True＝造个真文件；False＝指一条不存在的路；字符串＝照字面用。"""
        def resolve(v, name, missing):
            if v is True:
                return self.fake_file(name)
            if v is False:
                return str(self.tmp / missing)
            return str(v)
        return {"auto_start": auto_start,
                "vbs": resolve(vbs, "fake\\假启动器.vbs", "没有.vbs"),
                "bat": resolve(bat, "fake\\假启动器.bat", "没有.bat")}

    def cfg_on_disk(self, **over):
        """把 cfg 写进临时 settings.json，返回它——验「读侧现读磁盘」那条路用。"""
        cfg = dict(self.cfg_with(**over))
        self.write_settings({"comfy": cfg})
        return cfg


# ------------------------------------------------------------------ A 读侧宽容（裁定 3）
class ComfySettingsReadTests(FurnaceBase):
    """comfy_settings()：缺键／坏值／半截块／文件损坏一律兜默认，auto_start 只认 bool True。"""

    def test_defaults_are_off_and_paths_are_machine_constants(self):
        c = ST.DEFAULTS["comfy"]
        self.assertIs(c["auto_start"], False)               # D102：默认绝不自动起
        self.assertEqual(c["vbs"], VBS_DEFAULT)             # 裁定 1
        self.assertEqual(c["bat"], BAT_DEFAULT)

    def test_empty_settings_file_falls_back_to_defaults(self):
        self.assertEqual(ST.comfy_settings(),
                         {"auto_start": False, "vbs": VBS_DEFAULT, "bat": BAT_DEFAULT})

    def test_missing_settings_file_falls_back_to_defaults(self):
        ST.SETTINGS_FILE.unlink()
        self.assertIs(ST.comfy_settings()["auto_start"], False)
        self.assertEqual(ST.comfy_settings()["vbs"], VBS_DEFAULT)

    def test_corrupt_settings_file_falls_back_not_raises(self):
        for junk in ("{半截", "[]", '"不是字典"', ""):
            ST.SETTINGS_FILE.write_text(junk, encoding="utf-8")
            c = ST.comfy_settings()
            self.assertIs(c["auto_start"], False, junk)
            self.assertEqual(c["bat"], BAT_DEFAULT, junk)

    def test_auto_start_true_only_from_a_real_bool(self):
        """D102 的写法：只有 JSON 真 true 才算打开，看着像真值的一律 false。"""
        for bad in ("yes", "true", "TRUE", "1", 1, 0, -1, None, [], {}, [True]):
            self.write_settings({"comfy": {"auto_start": bad}})
            self.assertIs(ST.comfy_settings()["auto_start"], False, repr(bad))
        self.write_settings({"comfy": {"auto_start": True}})
        self.assertIs(ST.comfy_settings()["auto_start"], True)

    def test_comfy_block_not_a_dict_falls_back(self):
        for bad in ("true", 1, None, [], [{"auto_start": True}]):
            self.write_settings({"comfy": bad})
            self.assertIs(ST.comfy_settings()["auto_start"], False, repr(bad))

    def test_half_written_comfy_block_does_not_keyerror(self):
        """裁定 3 的靶心：写侧只写 auto_start 一个键，存盘的 comfy 块是半截的。

        裸取 SETTINGS["comfy"]["vbs"] 在这里就 KeyError 了；读侧收口在 comfy_settings()，
        所以主人勾过一次开关之后，起炉子照样找得着那两条路径。
        """
        self.write_settings({"comfy": {"auto_start": True}})
        c = ST.comfy_settings()
        self.assertIs(c["auto_start"], True)
        self.assertEqual((c["vbs"], c["bat"]), (VBS_DEFAULT, BAT_DEFAULT))
        # load_settings 的浅合并也证明一遍：它给出的 comfy 块确实只有半截
        self.assertEqual(ST.load_settings()["comfy"], {"auto_start": True})

    def test_bad_path_values_fall_back_to_defaults(self):
        for bad in (None, "", "   ", 123, [], {}, True):
            self.write_settings({"comfy": {"vbs": bad, "bat": bad}})
            c = ST.comfy_settings()
            self.assertEqual(c["vbs"], VBS_DEFAULT, repr(bad))
            self.assertEqual(c["bat"], BAT_DEFAULT, repr(bad))

    def test_paths_are_stripped(self):
        self.write_settings({"comfy": {"vbs": "  C:\\x\\a.vbs  ", "bat": "\tC:\\y\\b.bat\n"}})
        c = ST.comfy_settings()
        self.assertEqual(c["vbs"], "C:\\x\\a.vbs")
        self.assertEqual(c["bat"], "C:\\y\\b.bat")

    def test_owner_paths_survive_and_are_used(self):
        """主人手改了路径（比如把炉子挪了盘）就该照他的走，不许被默认值盖掉。"""
        vbs, bat = self.fake_file("a.vbs"), self.fake_file("b.bat")
        self.write_settings({"comfy": {"vbs": vbs, "bat": bat}})
        self.assertEqual((ST.comfy_settings()["vbs"], ST.comfy_settings()["bat"]), (vbs, bat))

    def test_reader_hits_disk_every_call_so_no_restart_needed(self):
        """裁定 6 的实质：读侧每次现读磁盘，所以没有需要同步的内存副本，勾完立刻生效。"""
        self.assertIs(ST.comfy_settings()["auto_start"], False)
        ST.save_comfy_auto(True)
        self.assertIs(ST.comfy_settings()["auto_start"], True)      # 没重启、没重新 load_settings
        ST.save_comfy_auto(False)
        self.assertIs(ST.comfy_settings()["auto_start"], False)

    def test_reader_never_writes_the_settings_file(self):
        before = self.raw()
        for _ in range(3):
            ST.comfy_settings()
        self.assertEqual(self.raw(), before)
        self.assertFalse(ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".bak").exists())

    def test_hostile_path_shapes_never_reach_launcher_as_non_strings(self):
        """读侧就是边界：settings 里塞数字／列表也只会兜回默认字符串，launcher 拿不到会炸的形状。

        （Path(123) 是 TypeError；真漏出去就是一发 500，正是本卡要保证不出现的。）
        """
        self.write_settings({"comfy": {"vbs": 123, "bat": ["x"], "auto_start": "yes"}})
        c = ST.comfy_settings()
        self.assertIsInstance(c["vbs"], str)
        self.assertIsInstance(c["bat"], str)
        self.assertEqual((c["vbs"], c["bat"]), (VBS_DEFAULT, BAT_DEFAULT))
        self.assertIn(furnace.launcher(c)[0], ("vbs", "bat", "none"))


# ------------------------------------------------------------------ B 写侧
class ComfyAutoWriteTests(FurnaceBase):
    """norm_comfy_auto／save_comfy_auto：只认 bool；写盘不掺 DEFAULTS、不碰主人已有的块。"""

    def test_norm_accepts_only_bool(self):
        self.assertEqual(ST.norm_comfy_auto(True), (True, None))
        self.assertEqual(ST.norm_comfy_auto(False), (False, None))
        for bad in ("yes", "true", 1, 0, None, [], {}, [True]):
            on, err = ST.norm_comfy_auto(bad)
            self.assertIsNone(on, repr(bad))
            self.assertEqual(err["code"], "bad_auto_start", repr(bad))
            self.assertIn("开关", err["human"])                     # 中文人话（D96）

    def test_norm_never_accepts_a_path(self):
        """卡面硬红线：起炉子走哪个可执行由本机常量决定，界面改不了。"""
        for bad in ({"auto_start": True, "vbs": "C:\\evil.bat"}, "C:\\evil.bat",
                    {"vbs": "C:\\evil.bat"}):
            on, err = ST.norm_comfy_auto(bad)
            self.assertIsNone(on, repr(bad))
            self.assertEqual(err["code"], "bad_auto_start", repr(bad))

    def test_save_writes_only_the_one_key(self):
        """同 save_output_dir／save_ui 的口径：不掺 DEFAULTS，免得把默认值钉死进主人的文件。"""
        ST.save_comfy_auto(True)
        self.assertEqual(json.loads(self.raw()), {"comfy": {"auto_start": True}})

    def test_save_preserves_owner_other_blocks(self):
        """真 settings.json 现在就有个 ui 块（T007）：存开关不许把它抹掉。"""
        self.write_settings({"ui": {"bg": "off", "density": "loose", "blur": 10},
                             "output": {"dir": "C:\\画集"},
                             "gpu": {"warn_free_mb": 900}})
        ST.save_comfy_auto(True)
        got = json.loads(self.raw())
        self.assertEqual(got["ui"], {"bg": "off", "density": "loose", "blur": 10})
        self.assertEqual(got["output"], {"dir": "C:\\画集"})
        self.assertEqual(got["gpu"], {"warn_free_mb": 900})
        self.assertEqual(got["comfy"], {"auto_start": True})

    def test_save_preserves_owner_paths_in_the_same_block(self):
        vbs, bat = self.fake_file("a.vbs"), self.fake_file("b.bat")
        self.write_settings({"comfy": {"vbs": vbs, "bat": bat, "auto_start": False}})
        ST.save_comfy_auto(True)
        self.assertEqual(json.loads(self.raw())["comfy"],
                         {"vbs": vbs, "bat": bat, "auto_start": True})

    def test_save_flips_both_ways(self):
        ST.save_comfy_auto(True)
        self.assertIs(ST.comfy_settings()["auto_start"], True)
        ST.save_comfy_auto(False)
        self.assertIs(ST.comfy_settings()["auto_start"], False)
        self.assertEqual(json.loads(self.raw())["comfy"], {"auto_start": False})

    def test_save_backs_up_and_leaves_no_tmp(self):
        self.write_settings({"ui": {"blur": 10}})
        ST.save_comfy_auto(True)
        bak = ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".bak")
        self.assertTrue(bak.exists())
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8")), {"ui": {"blur": 10}})
        self.assertFalse(ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".tmp").exists())
        self.assertEqual(json.loads(ST.SETTINGS_FILE.read_text(encoding="utf-8")),
                         {"ui": {"blur": 10}, "comfy": {"auto_start": True}})

    def test_save_over_corrupt_file_keeps_the_bytes_in_bak(self):
        """主人的文件是半截 JSON 也不许丢字：旧文进 .bak，新文只带这一个键。"""
        ST.SETTINGS_FILE.write_text('{"ui": {"blur": 1', encoding="utf-8")
        ST.save_comfy_auto(True)
        bak = ST.SETTINGS_FILE.with_name(ST.SETTINGS_FILE.name + ".bak")
        self.assertEqual(bak.read_text(encoding="utf-8"), '{"ui": {"blur": 1')
        self.assertEqual(json.loads(self.raw()), {"comfy": {"auto_start": True}})


# ------------------------------------------------------------------ C launcher 三条路
class LauncherTests(FurnaceBase):
    """裁定 1／2：vbs 在盘上才用它、不在退回 bat；bat 那一路必须带 cwd 且经 start 脱离进程树。"""

    def test_vbs_branch_wins_when_the_file_exists(self):
        cfg = self.cfg_with()
        kind, argv, cwd = furnace.launcher(cfg)
        self.assertEqual(kind, "vbs")
        self.assertEqual(argv[:2], ["wscript.exe", "//B"])
        self.assertEqual(argv[2], cfg["vbs"])
        self.assertIsNone(cwd)                        # vbs 自己 cd /d，画舫不用管

    def test_bat_fallback_when_vbs_missing(self):
        cfg = self.cfg_with(vbs=False)
        kind, argv, cwd = furnace.launcher(cfg)
        self.assertEqual(kind, "bat")
        line = cmdline(argv)
        self.assertTrue(line.startswith("cmd.exe /c start "), line)
        self.assertIn("/b", line)                              # 裁定 2：别弹 pause 黑窗
        self.assertIn(cfg["bat"], line)
        self.assertEqual(cwd, str(Path(cfg["bat"]).parent))    # 裁定 2：bat 里是相对路径

    def test_bat_command_line_is_one_cmd_can_actually_parse(self):
        """验收 C2 血泪回归：必须是**原样命令行字符串**，不能退回 argv 列表。

        列表形式下那个当空标题的 `'""'` 会被 list2cmdline 转义成 `\\"`，cmd 把它当成路径的一部分，
        start 一个进程都没起、外层 cmd 原地僵死（真机撞上过，还留下三个僵尸 cmd）。
        """
        cfg = self.cfg_with(vbs=False)
        argv = furnace.launcher(cfg)[1]
        self.assertIsInstance(argv, str, "bat 那条路一旦退回列表，start 的空标题就会被转义坏")
        self.assertIn('""', argv, "start 少了空标题，带空格的路径会被它当成窗口标题")
        self.assertNotIn('\\"', argv, f"cmd 不认反斜杠转义的引号：{argv}")

    def test_bat_fallback_when_vbs_is_a_directory(self):
        d = self.tmp / "dir.vbs"
        d.mkdir()
        cfg = self.cfg_with(vbs=d)
        kind, argv, cwd = furnace.launcher(cfg)
        self.assertEqual(kind, "bat")
        self.assertIn(cfg["bat"], cmdline(argv))
        self.assertEqual(cwd, str(Path(cfg["bat"]).parent))

    def test_none_when_both_missing(self):
        kind, argv, cwd = furnace.launcher(self.cfg_with(vbs=False, bat=False))
        self.assertEqual((kind, argv, cwd), ("none", None, None))

    def test_empty_or_none_paths_go_to_none_without_raising(self):
        for cfg in ({"vbs": None, "bat": None}, {"vbs": "", "bat": ""}, {},
                    {"vbs": "   ", "bat": "  "}):
            self.assertEqual(furnace.launcher(cfg), ("none", None, None), cfg)

    def test_unconfigured_defaults_do_not_select_a_launcher(self):
        """首次启动尚未配置时不能选中当前目录或机器上碰巧存在的启动器。"""
        kind, argv, cwd = furnace.launcher(dict(ST.DEFAULTS["comfy"]))
        self.assertEqual((kind, argv, cwd), ("none", None, None))


# ------------------------------------------------------------------ D spawn 形状（裁定 2）
class SpawnShapeTests(FurnaceBase):
    """真 Popen 的参数形状：脱离进程树＋不继承控制台＋三个流全 DEVNULL（不留 pause 黑窗）。"""

    def test_spawn_detaches_and_swallows_the_streams(self):
        seen = {}
        real_popen = subprocess.Popen

        def fake_popen(argv, **kw):
            seen["argv"] = argv
            seen.update(kw)
            return "<FakeProc>"

        subprocess.Popen = fake_popen
        try:
            out = self._real_spawn('cmd.exe /c start "" /b C:\\ComfyUI\\run.bat', "C:\\ComfyUI")
        finally:
            subprocess.Popen = real_popen
        self.assertEqual(out, "<FakeProc>")
        self.assertEqual(seen["argv"], 'cmd.exe /c start "" /b C:\\ComfyUI\\run.bat')
        self.assertEqual(seen["cwd"], "C:\\ComfyUI")
        flags = seen["creationflags"]
        self.assertTrue(flags & subprocess.DETACHED_PROCESS,
                        "不脱离控制台就会在主人桌面上留一个 pause 黑窗")
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP,
                        "不另起进程组，收工那句 taskkill /T 会顺着进程树把炉子一起带走")
        for k in ("stdin", "stdout", "stderr"):
            self.assertEqual(seen[k], subprocess.DEVNULL, k)
        self.assertTrue(seen["close_fds"])

    def test_spawn_takes_both_shapes_the_launcher_can_return(self):
        """vbs 那一路给列表、bat 那一路给字符串——两种都得原样进 Popen，spawn 不许自己重拼。"""
        seen = []
        real_popen = subprocess.Popen
        subprocess.Popen = lambda a, **kw: (seen.append(a), "<FakeProc>")[1]
        try:
            self._real_spawn(["wscript.exe", "//B", "C:\\x.vbs"], None)
            self._real_spawn('cmd.exe /c start "" /b C:\\x.bat', "C:\\")
        finally:
            subprocess.Popen = real_popen
        self.assertEqual(seen, [["wscript.exe", "//B", "C:\\x.vbs"],
                                'cmd.exe /c start "" /b C:\\x.bat'])

    def test_start_hands_the_launcher_output_to_spawn_unchanged(self):
        cfg = self.cfg_with(vbs=False)
        furnace.start(FakeProbe([False, True]), "http://127.0.0.1:8188", cfg=cfg,
                      timeout=1, interval=0.01)
        self.assertEqual(self.fake.n, 1)
        argv, cwd = self.fake.calls[0]
        self.assertEqual(argv, furnace.launcher(cfg)[1])   # 一字不改地转交，中途不重拼一遍
        self.assertIsInstance(argv, str)                   # 重拼成列表就把 start 的空标题转义坏了
        self.assertEqual(cwd, str(Path(cfg["bat"]).parent))


# ------------------------------------------------------------------ E start 四路＋锁
class StartTests(FurnaceBase):
    """卡面 §3「start 四路」的单元版，外加裁定 5 的 in-flight 锁。"""

    def start(self, probe, cfg=None, timeout=1.0, interval=0.01, url="http://127.0.0.1:8188"):
        return furnace.start(probe, url, cfg=cfg if cfg is not None else self.cfg_with(),
                             timeout=timeout, interval=interval)

    def test_path1_already_up_spawns_nothing(self):
        r = self.start(FakeProbe([True]))
        self.assertEqual((r["ok"], r["already"]), (True, True))
        self.assertEqual(self.fake.n, 0)                       # 已经开着绝不二次拉起
        self.assertEqual(r["port"], 8188)
        self.assertIn("已经开着", r["human"])

    def test_path2_comes_up_after_two_polls(self):
        p = FakeProbe([False, False, True])
        r = self.start(p)
        self.assertTrue(r["ok"])
        self.assertNotIn("already", r)
        self.assertEqual(r["kind"], "vbs")
        self.assertEqual(self.fake.n, 1)
        self.assertIn("起好了", r["human"])
        self.assertIn("8188", r["human"])
        self.assertEqual(p.n, 3)                               # 开头一次＋轮询两次
        self.assertFalse(furnace._IN_FLIGHT)                   # 起来了要把锁放掉

    def test_path3_timeout_reports_ok_false_and_warns_against_a_second_click(self):
        """裁定 5 后半句：超时不许「误报之后放行双开」——人话必须拦住主人再点一次。"""
        r = self.start(FakeProbe([False]), timeout=0.1, interval=0.02)
        self.assertFalse(r["ok"])
        self.assertNotIn("busy", r)
        self.assertEqual(self.fake.n, 1)                       # 只点了一次火，没有偷偷重试
        self.assertIn("别急着再点一次", r["human"])
        self.assertIn("双开", r["human"])
        self.assertFalse(furnace._IN_FLIGHT)                   # 锁放掉，否则按钮这辈子就死了

    def test_path4_no_launcher_file_spawns_nothing(self):
        r = self.start(FakeProbe([False]), cfg=self.cfg_with(vbs=False, bat=False))
        self.assertFalse(r["ok"])
        self.assertEqual(r["kind"], "none")
        self.assertEqual(self.fake.n, 0)
        self.assertIn("启动文件", r["human"])
        self.assertIn("双击炉子", r["human"])
        self.assertFalse(furnace._IN_FLIGHT)

    def test_spawn_oserror_becomes_human_not_a_500(self):
        self.fake.err = OSError(2, "系统找不到指定的文件", "wscript.exe")
        r = self.start(FakeProbe([False]))
        self.assertFalse(r["ok"])
        self.assertIn("没点着", r["human"])
        self.assertIn("系统找不到指定的文件", r["human"])
        self.assertFalse(furnace._IN_FLIGHT)

    def test_in_flight_lock_blocks_a_second_start(self):
        furnace._IN_FLIGHT = True
        r = self.start(FakeProbe([False]))
        self.assertFalse(r["ok"])
        self.assertTrue(r["busy"])
        self.assertEqual(self.fake.n, 0)                       # 绝不二次拉起
        self.assertIn("正在起炉子", r["human"])
        self.assertIn("别再点了", r["human"])
        self.assertTrue(furnace._IN_FLIGHT)                    # 别人的锁不许被我清掉

    def test_busy_message_reports_the_timeout_actually_in_use(self):
        furnace._IN_FLIGHT = True
        self.assertIn("3 分钟", self.start(FakeProbe([False]), timeout=180.0)["human"])
        self.assertIn("1 分钟", self.start(FakeProbe([False]), timeout=90.0)["human"])
        self.assertIn("1 分钟", self.start(FakeProbe([False]), timeout=0.3)["human"])   # 不说「0 分钟」

    def test_in_flight_lock_holds_across_threads(self):
        """真并发：一个线程在轮询里挂着，另一个线程点第二下必须被 busy 挡回去。"""
        gate, entered = threading.Event(), threading.Event()
        cfg = self.cfg_with()
        out = {}

        def worker():
            out["r"] = furnace.start(GateProbe(gate, entered), "http://127.0.0.1:8188",
                                     cfg=cfg, timeout=8, interval=0.02)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        try:
            self.assertTrue(entered.wait(8), "轮询那一发没走到")
            self.assertTrue(furnace._IN_FLIGHT)
            r2 = self.start(FakeProbe([False]), cfg=cfg, timeout=0.1, interval=0.02)
            self.assertTrue(r2["busy"])
            self.assertEqual(self.fake.n, 1)                   # 全场只点了一次火
        finally:
            gate.set()
            t.join(15)
        self.assertFalse(out["r"]["ok"])                       # 闸门放开后是超时那一支
        self.assertFalse(furnace._IN_FLIGHT)

    def test_probe_exception_counts_as_dead_and_never_escapes(self):
        """_safe_probe 的靶心：ComfyClient.alive() 只接 ConnectError，ReadTimeout 会漏出来。"""
        self.assertFalse(furnace._safe_probe(BoomProbe(httpx.ReadTimeout("还在装模型"))))
        boom = BoomProbe(httpx.ReadTimeout("还在装模型"))
        r = self.start(boom, cfg=self.cfg_with(vbs=False, bat=False))
        self.assertFalse(r["ok"])                              # 没抛＝没 500
        self.assertEqual(boom.n, 1)                            # 只在开头那句「已经开着吗」上探了一次
        self.assertEqual(self.fake.n, 0)

    def test_safe_probe_swallows_any_exception(self):
        for exc in (RuntimeError("x"), ValueError("y"), KeyError("n"),
                    httpx.ConnectError("z"), httpx.ReadTimeout("t"), OSError(13, "拒绝访问")):
            self.assertFalse(furnace._safe_probe(BoomProbe(exc)), repr(exc))

    def test_safe_probe_coerces_to_bool(self):
        self.assertIs(furnace._safe_probe(lambda: "yes"), True)
        self.assertIs(furnace._safe_probe(lambda: 0), False)
        self.assertIs(furnace._safe_probe(lambda: None), False)

    def test_port_comes_from_the_url(self):
        cfg = self.cfg_with(vbs=False, bat=False)
        for url, want in (("http://127.0.0.1:8188", 8188), ("http://127.0.0.1:9999", 9999),
                          ("http://127.0.0.1", furnace.FALLBACK_PORT),
                          ("http://127.0.0.1:不是端口", furnace.FALLBACK_PORT),
                          ("", furnace.FALLBACK_PORT)):
            r = furnace.start(FakeProbe([True]), url, cfg=cfg, timeout=0.1, interval=0.01)
            self.assertEqual(r["port"], want, url)

    def test_cfg_is_read_from_disk_when_not_given(self):
        vbs = self.fake_file("盘上的.vbs")
        self.write_settings({"comfy": {"vbs": vbs, "bat": str(self.tmp / "没有.bat")}})
        r = furnace.start(FakeProbe([False, True]), "http://127.0.0.1:8188",
                          timeout=1, interval=0.01)
        self.assertTrue(r["ok"])
        self.assertEqual(self.fake.argvs()[0][-1], vbs)        # 用的是盘上那条，不是 DEFAULTS

    def test_non_dict_cfg_falls_back_to_disk(self):
        self.write_settings({"comfy": {"vbs": str(self.tmp / "没有.vbs"),
                                       "bat": str(self.tmp / "没有.bat")}})
        for bad in ("不是字典", 123, None, []):
            r = furnace.start(FakeProbe([True]), "http://127.0.0.1:8188",
                              cfg=bad, timeout=0.1, interval=0.01)
            self.assertTrue(r["ok"], repr(bad))
            self.assertEqual(r["already"], True, repr(bad))
            self.assertEqual(self.fake.n, 0, repr(bad))


# ------------------------------------------------------------------ F 自动拉起（裁定 4）
class AutoStartTests(FurnaceBase):
    """三道闸：环境开关 → 每进程一次 → auto_start 是 True。任何一道不过都零调用。"""

    URL = "http://127.0.0.1:8188"

    def test_env_gate_wins_before_anything_else(self):
        """测试默认设着 HUAFANG_SKIP_AUTOSTART=1：哪怕开关是 True、炉子是死的，也一次都不点。"""
        self.cfg_on_disk(auto_start=True)
        self.assertFalse(furnace.auto_start_once(FakeProbe([False]), self.URL))
        self.assertEqual(self.fake.n, 0)
        # 关键：环境闸在「每进程一次」那道旗**之前**，所以跑单测不会把真跑那一次给消耗掉
        self.assertFalse(furnace._AUTO_TRIED)

    def test_auto_start_false_means_zero_calls(self):
        """卡面 §3 明写的一条：auto_start=false 时启动零调用。"""
        self.allow_autostart()
        self.cfg_on_disk(auto_start=False)
        self.assertFalse(furnace.auto_start_once(FakeProbe([False]), self.URL))
        self.assertEqual(self.fake.n, 0)
        self.assertTrue(furnace._AUTO_TRIED)

    def test_missing_key_means_zero_calls(self):
        """D102：缺键＝false，绝不因为「没写」就自作主张起炉子。"""
        self.allow_autostart()
        for obj in ({}, {"comfy": {}}, {"comfy": {"auto_start": "yes"}}, {"comfy": "true"}):
            furnace._AUTO_TRIED = False
            self.write_settings(obj)
            self.assertFalse(furnace.auto_start_once(FakeProbe([False]), self.URL), obj)
            self.assertEqual(self.fake.n, 0, obj)

    def test_corrupt_settings_means_zero_calls(self):
        self.allow_autostart()
        ST.SETTINGS_FILE.write_text("{半截", encoding="utf-8")
        self.assertFalse(furnace.auto_start_once(FakeProbe([False]), self.URL))
        self.assertEqual(self.fake.n, 0)

    def test_already_up_means_zero_calls_but_consumes_the_flag(self):
        self.allow_autostart()
        self.cfg_on_disk(auto_start=True)
        self.assertFalse(furnace.auto_start_once(FakeProbe([True]), self.URL))
        self.assertEqual(self.fake.n, 0)
        self.assertTrue(furnace._AUTO_TRIED)

    def test_once_per_process_flag_blocks_the_second_call(self):
        self.allow_autostart()
        cfg = self.cfg_on_disk(auto_start=True)
        self.assertTrue(furnace.auto_start_once(FakeProbe([False, False, True]), self.URL))
        self.join_auto()
        self.assertEqual(self.fake.n, 1)
        self.assertEqual(self.fake.argvs()[0][-1], cfg["vbs"])
        # 第二次：旗已经立了，连探活都不该再走一遍
        p = FakeProbe([False])
        self.assertFalse(furnace.auto_start_once(p, self.URL))
        self.join_auto()
        self.assertEqual(self.fake.n, 1)                       # 卡面：每进程最多一次，失败不重试
        self.assertEqual(p.n, 0)

    def test_autostart_runs_in_a_background_daemon_thread(self):
        """绝不拖住 uvicorn 启动：炉子没起来那三分钟里，主人打开页面得有人应。"""
        self.allow_autostart()
        self.cfg_on_disk(auto_start=True)
        gate, entered = threading.Event(), threading.Event()
        # auto_start_once 立刻返回＝没在等探活；真等的那一下发生在后台线程里
        self.assertTrue(furnace.auto_start_once(GateProbe(gate, entered), self.URL))
        t = next((x for x in threading.enumerate() if x.name == AUTO_THREAD), None)
        self.assertIsNotNone(t)
        self.assertTrue(t.daemon)
        self.assertTrue(entered.wait(8))
        gate.set()
        self.join_auto()
        self.assertEqual(self.fake.n, 1)

    def test_auto_worker_swallows_exceptions(self):
        """线程里漏出去的异常没人接，会把 traceback 打到主人控制台上——必须自己咽下去。"""
        def boom(*a, **k):
            raise RuntimeError("炸了")

        furnace.start = boom
        try:
            furnace._auto_worker(FakeProbe([False]), self.URL)   # 不抛＝通过
        finally:
            furnace.start = self._real_start

    def test_auto_worker_reports_both_outcomes_without_raising(self):
        cfg = self.cfg_with(vbs=False, bat=False)
        self.write_settings({"comfy": cfg})
        furnace.POLL_INTERVAL = 0.01
        furnace._auto_worker(FakeProbe([False]), self.URL)       # 起不来那一支
        self.assertEqual(self.fake.n, 0)
        furnace._auto_worker(FakeProbe([True]), self.URL)        # 已经开着那一支
        self.assertEqual(self.fake.n, 0)


# ------------------------------------------------------------------ 源码结构（裁定 4＋不新增依赖）
class SourceStructureTests(unittest.TestCase):
    """读源码本身来钉几条卡面红线——这些靠运行时测试证明不了。"""

    @staticmethod
    def calls_to(node, name):
        return [n for n in ast.walk(node)
                if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(name)]

    def src(self, name):
        return (ROOT / "app" / name).read_text(encoding="utf-8")

    def test_autostart_is_not_called_at_module_level(self):
        """裁定 4「禁止模块级」：import app.main 就跑的话，每跑一次全量单测都点一次炉子。"""
        tree = ast.parse(self.src("main.py"))
        top = []
        for st in tree.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            top += self.calls_to(st, "auto_start_once")
        self.assertEqual(top, [], "auto_start_once 不许出现在模块级语句里")

    def test_autostart_lives_in_the_lifespan_hook(self):
        tree = ast.parse(self.src("main.py"))
        fn = next((s for s in tree.body
                   if isinstance(s, ast.AsyncFunctionDef) and s.name == "_lifespan"), None)
        self.assertIsNotNone(fn, "自动拉起该挂在 FastAPI 的 lifespan 里")
        self.assertEqual(len(self.calls_to(fn, "auto_start_once")), 1)
        wires = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and ast.unparse(n.func).endswith("FastAPI")
                 and any(kw.arg == "lifespan" and ast.unparse(kw.value) == "_lifespan"
                         for kw in n.keywords)]
        self.assertEqual(len(wires), 1, "app = FastAPI(...) 必须带 lifespan=_lifespan")

    def test_no_endpoint_takes_a_path_parameter_for_the_furnace(self):
        """卡面硬红线「不接受任何用户输入路径」的静态版：两个端点都不许有路径形参。"""
        tree = ast.parse(self.src("main.py"))
        seen = set()
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                   and n.name in ("furnace_start", "furnace_auto_save")]:
            seen.add(fn.name)
            names = [a.arg for a in fn.args.args]
            for banned in ("vbs", "bat", "path", "cmd", "argv", "exe"):
                self.assertNotIn(banned, names, fn.name)
            if fn.name == "furnace_start":
                self.assertEqual(names, [], "起炉子端点一个参数都不该收")
        self.assertEqual(seen, {"furnace_start", "furnace_auto_save"})

    def test_main_never_reads_the_comfy_block_bare(self):
        """裁定 3：不许混——main 里一律经宽容读，没有裸取 SETTINGS["comfy"]。

        扫 AST 而不是扫字符串：bootstrap 那段注释里正好写着「裸取 SETTINGS["comfy"] 会 KeyError」，
        拿字符串比对会被自己的注释绊倒。
        """
        tree = ast.parse(self.src("main.py"))
        bare = [n for n in ast.walk(tree)
                if isinstance(n, ast.Subscript) and ast.unparse(n.value) == "SETTINGS"
                and isinstance(n.slice, ast.Constant) and n.slice.value == "comfy"]
        self.assertEqual(bare, [], 'SETTINGS["comfy"] 存盘时可能是半截块，裸取会 KeyError')
        subs = {n.slice.value for n in ast.walk(tree)
                if isinstance(n, ast.Subscript) and ast.unparse(n.value) == "SETTINGS"
                and isinstance(n.slice, ast.Constant)}
        self.assertNotIn("comfy", subs)
        self.assertIn("comfy_url", subs)                       # 既有那一条照旧
        self.assertIn("ST.comfy_settings()", self.src("main.py"))

    def test_furnace_module_adds_no_dependency(self):
        """卡面硬红线「不新增依赖」：furnace 只许 import 标准库与本仓库。"""
        mods = set()
        for n in ast.walk(ast.parse(self.src("furnace.py"))):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and not n.level:
                mods.add((n.module or "").split(".")[0])
        third = {m for m in mods if m and m not in sys.stdlib_module_names}
        self.assertEqual(third, set(), f"furnace 引入了第三方依赖：{third}")

    def test_furnace_never_inspects_processes_or_training(self):
        """卡面硬红线：不检测进程、不读训练日志。判活只用只读 HTTP（与 gpu.py 同一数据源）。

        只扫代码里真会出现的调用名；`comfyui_server.log` 不在此列——那是启动器自己写的日志，
        裁定 7 认它是既成行为，furnace 的 docstring 提一句是记账，不是去读它。
        """
        src = self.src("furnace.py")
        for banned in ("tasklist", "psutil", "wmic", "Get-Process", "Win32_Process",
                       ".pid", "poll()", "training", "train_log"):
            self.assertNotIn(banned, src, f"furnace 里不许出现 {banned}")

    def test_launcher_paths_only_come_from_local_settings(self):
        """公开副本默认不带本机启动器；furnace／main 不许自行藏一条回退路径。"""
        for name in ("furnace.py", "main.py", "gpu.py", "comfy.py"):
            src = self.src(name)
            self.assertNotIn("start_comfyui_hidden", src, name)
            self.assertNotIn("run_nvidia_gpu", src, name)
        self.assertEqual(ST.DEFAULTS["comfy"], {"auto_start": False, "auto_free": False,
                                               "idle_minutes": 15, "vbs": "", "bat": ""})


# ------------------------------------------------------------------ G 端点
class EndpointTests(FurnaceBase):
    """/api/furnace/start 与 /api/furnace/auto：守卫照旧、四路全 200、坏值 400、不漏路径。"""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        cls.TestClient = TestClient
        cls._token_file = ST.TOKEN_FILE
        cls._token_before = (ST.TOKEN_FILE.read_text(encoding="utf-8")
                             if ST.TOKEN_FILE.exists() else None)
        from app import main
        cls.main = main
        cls.http = TestClient(main.app, base_url="http://127.0.0.1:8788",
                              headers={"Authorization": "Bearer " + main.TOKEN})

    @classmethod
    def tearDownClass(cls):
        if cls._token_before is not None:
            cls._token_file.write_text(cls._token_before, encoding="utf-8")

    def setUp(self):
        super().setUp()
        self._client = self.main.CLIENT
        self._url = self.main.SETTINGS["comfy_url"]
        self.main.SETTINGS["comfy_url"] = "http://127.0.0.1:8188"
        self.alive = FakeAlive([False])
        self.main.CLIENT = self.alive

    def tearDown(self):
        self.main.CLIENT = self._client
        self.main.SETTINGS["comfy_url"] = self._url
        super().tearDown()

    def set_alive(self, script):
        self.alive = FakeAlive(script)
        self.main.CLIENT = self.alive

    def post(self, url, payload=None, token=None, origin=None):
        headers = {"Authorization": "Bearer " + (token or self.main.TOKEN)}
        if origin:
            headers["Origin"] = origin
        return self.http.post(url, json=payload, headers=headers)

    def get(self, url):
        return self.http.get(url, headers={"Authorization": "Bearer " + self.main.TOKEN})

    def lifespan_client(self):
        """进 with 才跑 lifespan——裸用 TestClient（本仓库其余九个端点文件都是裸用）根本不跑。"""
        return self.TestClient(self.main.app, base_url="http://127.0.0.1:8788",
                              headers={"Authorization": "Bearer " + self.main.TOKEN})

    # ---- 守卫
    def test_guard_needs_a_token(self):
        for url in ("/api/furnace/start", "/api/furnace/auto"):
            self.assertEqual(self.post(url, {}, token="wrong").status_code, 401, url)

    def test_guard_rejects_a_bad_origin(self):
        for url in ("/api/furnace/start", "/api/furnace/auto"):
            self.assertEqual(self.post(url, {}, origin="http://evil.example").status_code, 403, url)

    # ---- start 四路（卡面 §3）
    def test_start_path1_already_up(self):
        self.set_alive([True])
        r = self.post("/api/furnace/start")
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual((b["ok"], b["already"]), (True, True))
        self.assertEqual(self.fake.n, 0)
        self.assertIn("已经开着", b["human"])

    def test_start_path2_comes_up(self):
        self.cfg_on_disk()
        self.set_alive([False, False, True])
        r = self.post("/api/furnace/start")
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertTrue(b["ok"])
        self.assertEqual(b["kind"], "vbs")
        self.assertEqual(b["port"], 8188)
        self.assertEqual(self.fake.n, 1)

    def test_start_path3_timeout_is_200_not_500(self):
        self.cfg_on_disk()
        self.set_alive([False])
        furnace.POLL_TIMEOUT = 0.1
        furnace.POLL_INTERVAL = 0.02
        r = self.post("/api/furnace/start")
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertFalse(b["ok"])
        self.assertIn("别急着再点一次", b["human"])
        self.assertEqual(self.fake.n, 1)

    def test_start_path4_no_launcher_file(self):
        self.cfg_on_disk(vbs=False, bat=False)
        self.set_alive([False])
        r = self.post("/api/furnace/start")
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertFalse(b["ok"])
        self.assertEqual(b["kind"], "none")
        self.assertEqual(self.fake.n, 0)
        self.assertIn("启动文件", b["human"])

    def test_start_busy_while_in_flight(self):
        self.cfg_on_disk()
        self.set_alive([False])
        furnace._IN_FLIGHT = True
        r = self.post("/api/furnace/start")
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertTrue(b["busy"])
        self.assertFalse(b["ok"])
        self.assertEqual(self.fake.n, 0)

    def test_start_probe_explosion_is_still_200(self):
        """真炉子冷启动那几秒 httpx 会抛 ReadTimeout：端点不许把它变成 500。"""
        self.cfg_on_disk(vbs=False, bat=False)

        class Boom:
            def alive(self):
                raise httpx.ReadTimeout("还在装模型")

        self.main.CLIENT = Boom()
        r = self.post("/api/furnace/start")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ok"])

    def test_start_ignores_any_path_sent_in_the_body(self):
        """卡面硬红线：端点不收路径。塞一身体 hostile 路径也只走 settings 里那两条。"""
        cfg = self.cfg_on_disk()
        self.set_alive([False, True])
        evil = self.fake_file("evil.bat")
        r = self.post("/api/furnace/start",
                      {"vbs": evil, "bat": evil, "cmd": evil, "path": evil, "dir": evil,
                       "argv": ["cmd.exe", "/c", evil], "auto_start": True})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self.fake.n, 1)
        self.assertEqual(self.fake.argvs()[0][-1], cfg["vbs"])
        self.assertNotIn(evil, json.dumps(self.fake.calls))

    def test_start_never_returns_the_launcher_paths(self):
        """路径是后端的私事：人话与字段里都不该出现本机路径。"""
        cfg = self.cfg_on_disk()
        self.set_alive([False, True])
        for body in (self.post("/api/furnace/start").json(),
                     self.post("/api/furnace/start", {"vbs": "C:\\x.vbs"}).json()):
            blob = json.dumps(body, ensure_ascii=False)
            self.assertNotIn(cfg["vbs"], blob)
            self.assertNotIn(cfg["bat"], blob)
            self.assertNotIn("\\\\", blob)

    # ---- auto 开关
    def test_auto_saves_true_and_false_and_takes_effect_at_once(self):
        self.write_settings({"ui": {"blur": 10}})
        r = self.post("/api/furnace/auto", {"auto_start": True})
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertIs(b["auto_start"], True)
        self.assertIn("8G 显存", b["human"])
        self.assertIs(ST.comfy_settings()["auto_start"], True)     # 裁定 6：不用重启
        self.assertEqual(self.get("/api/bootstrap").json()["comfy"], {"auto_start": True})

        r = self.post("/api/furnace/auto", {"auto_start": False})
        self.assertIs(r.json()["auto_start"], False)
        self.assertIn("绝不偷偷点", r.json()["human"])
        self.assertIs(ST.comfy_settings()["auto_start"], False)
        self.assertEqual(self.get("/api/bootstrap").json()["comfy"], {"auto_start": False})

    def test_auto_preserves_the_owner_ui_block(self):
        self.write_settings({"ui": {"bg": "off", "density": "loose", "blur": 10},
                             "output": {"dir": "C:\\画集"}})
        self.assertEqual(self.post("/api/furnace/auto", {"auto_start": True}).status_code, 200)
        got = json.loads(self.raw())
        self.assertEqual(got["ui"], {"bg": "off", "density": "loose", "blur": 10})
        self.assertEqual(got["output"], {"dir": "C:\\画集"})
        self.assertEqual(got["comfy"], {"auto_start": True})

    def test_auto_rejects_bad_values_with_400_and_writes_nothing(self):
        self.write_settings({"ui": {"blur": 10}})
        before = self.raw()
        for bad in ({"auto_start": "yes"}, {"auto_start": 1}, {"auto_start": None},
                    {"auto_start": []}, {}, {"auto_start": {"on": True}}, None,
                    {"vbs": "C:\\evil.bat"}, {"bat": "C:\\evil.bat"}):
            r = self.post("/api/furnace/auto", bad)
            self.assertEqual(r.status_code, 400, repr(bad))
            d = r.json()["detail"]
            self.assertEqual(d["code"], "bad_auto_start", repr(bad))
            self.assertIn("开关", d["human"], repr(bad))
        self.assertEqual(self.raw(), before)                       # 一个字都没写
        self.assertNotIn("evil", self.raw())

    def test_auto_ignores_extra_keys_and_never_persists_a_path(self):
        """开关合法就 200（多余键忽略，与 /api/ui 同口径），但塞进来的路径一个字都不许落盘。"""
        self.write_settings({"ui": {"blur": 10}})
        r = self.post("/api/furnace/auto",
                      {"auto_start": True, "vbs": "C:\\evil.bat", "bat": "C:\\evil2.bat",
                       "cmd": "C:\\evil3.exe"})
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.json()["auto_start"], True)
        got = json.loads(self.raw())
        self.assertEqual(got["comfy"], {"auto_start": True})         # 只有这一个键
        self.assertNotIn("evil", self.raw())
        self.assertEqual(got["ui"], {"blur": 10})
        # 而且下一次起炉子用的还是 settings 里那两条本机常量，不是刚塞进来的
        cfg = ST.comfy_settings()
        self.assertEqual((cfg["vbs"], cfg["bat"]), (VBS_DEFAULT, BAT_DEFAULT))

    def test_auto_save_failure_is_400_not_500(self):
        real = ST.save_comfy_auto

        def boom(on):
            raise OSError(13, "拒绝访问", "settings.json")

        ST.save_comfy_auto = boom
        try:
            r = self.post("/api/furnace/auto", {"auto_start": True})
        finally:
            ST.save_comfy_auto = real
        self.assertEqual(r.status_code, 400)
        d = r.json()["detail"]
        self.assertEqual(d["code"], "save_failed")
        self.assertIn("存不进磁盘", d["human"])

    # ---- bootstrap
    def test_bootstrap_exposes_only_the_switch(self):
        cfg = self.cfg_on_disk()
        body = self.get("/api/bootstrap").json()
        self.assertEqual(body["comfy"], {"auto_start": False})     # 裁定 3：只吐 auto_start
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(cfg["vbs"], blob)                         # 两条可执行路径不出后端
        self.assertNotIn(cfg["bat"], blob)
        self.assertNotIn("假启动器", blob)

    def test_bootstrap_survives_a_half_written_comfy_block(self):
        """主人勾过一次开关之后，盘上的 comfy 块只剩一个键——bootstrap 不许因此 500。"""
        self.write_settings({"comfy": {"auto_start": True}})
        r = self.get("/api/bootstrap")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["comfy"], {"auto_start": True})

    def test_bootstrap_reads_a_corrupt_settings_file_without_500(self):
        ST.SETTINGS_FILE.write_text("{半截", encoding="utf-8")
        r = self.get("/api/bootstrap")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["comfy"], {"auto_start": False})

    def test_node_ids_never_leave_the_backend(self):
        """卡面 §3 最后一条（D83）：T011 这三个载荷里不许出现节点号。"""
        self.cfg_on_disk()
        self.set_alive([False, True])
        payloads = [self.get("/api/bootstrap").json(),
                    self.post("/api/furnace/start").json(),
                    self.post("/api/furnace/auto", {"auto_start": False}).json()]
        ids = set()
        for p in self.main.PROFILES.values():
            prof = p["profile"]
            for key in ("model_slots", "lora_slots", "save_slots", "image_slots"):
                for slot in prof.get(key) or []:
                    ids.add(str(slot["node"]))
            for st in prof.get("stages") or []:
                refs = list((st.get("params") or {}).values()) + list((st.get("links") or {}).values())
                ids |= {str(r["node"]) for r in refs}
        self.assertTrue(ids, "工作流该有节点号可查，否则这条测试是空的")
        for body in payloads:
            blob = json.dumps(body, ensure_ascii=False)
            self.assertNotIn('"node"', blob)
            for nid in ids:
                self.assertNotIn(f'"node": "{nid}"', blob)
                self.assertNotIn(f'"node":"{nid}"', blob)
        self.assertEqual(sorted(payloads[0]["comfy"]), ["auto_start"])

    # ---- 裁定 4：lifespan 真跑起来是什么样
    def test_lifespan_with_switch_off_spawns_nothing(self):
        self.cfg_on_disk(auto_start=False)
        self.set_alive([False])
        self.allow_autostart()
        furnace._AUTO_TRIED = False
        with self.lifespan_client():
            pass
        self.join_auto()
        self.assertEqual(self.fake.n, 0)
        self.assertTrue(furnace._AUTO_TRIED)                       # 试过了，只是没起

    def test_lifespan_with_switch_on_starts_the_furnace_once(self):
        """验收 D 的单元版：auto_start=true 时画舫一启动就自己去点一次炉子（且只一次）。"""
        cfg = self.cfg_on_disk(auto_start=True)
        self.set_alive([False, False, True])
        self.allow_autostart()
        furnace._AUTO_TRIED = False
        with self.lifespan_client():
            self.join_auto()
            self.assertEqual(self.fake.n, 1)
            self.assertEqual(self.fake.argvs()[0][-1], cfg["vbs"])
        self.join_auto()
        self.assertEqual(self.fake.n, 1)
        with self.lifespan_client():                               # 同一进程再起一次
            pass
        self.join_auto()
        self.assertEqual(self.fake.n, 1)                           # 绝不第二次点火

    def test_lifespan_under_the_env_gate_spawns_nothing(self):
        """HUAFANG_SKIP_AUTOSTART=1 时，哪怕开关是 True、炉子是死的也一次不点（单测的命门）。"""
        self.cfg_on_disk(auto_start=True)
        self.set_alive([False])
        furnace._AUTO_TRIED = False
        self.assertEqual(os.environ.get("HUAFANG_SKIP_AUTOSTART"), "1")
        with self.lifespan_client():
            pass
        self.join_auto()
        self.assertEqual(self.fake.n, 0)
        self.assertFalse(furnace._AUTO_TRIED)

    def test_the_env_gate_is_closed_by_the_test_package_not_just_by_this_file(self):
        """闸挂在 `tests/__init__.py` 上，不是只挂在本文件的基类里（裁定 4）。

        基类那道只管得住 T011 自己的用例；下一个工人若在别的测试文件里也写 `with TestClient(…)`，
        而主人盘上勾着自动拉起，那一跑全量单测就会去点真炉子（8G 显存、和训练抢卡）。
        """
        import importlib

        import tests as pkg

        saved = os.environ.pop("HUAFANG_SKIP_AUTOSTART")
        try:
            self.assertNotIn("HUAFANG_SKIP_AUTOSTART", os.environ)
            importlib.reload(pkg)                     # 重放一遍包级 __init__
            self.assertEqual(os.environ.get("HUAFANG_SKIP_AUTOSTART"), "1",
                             "闸没在包级合上：别的测试文件用 with TestClient 就会真起炉子")
        finally:
            os.environ["HUAFANG_SKIP_AUTOSTART"] = saved
            importlib.reload(pkg)


# ------------------------------------------------------------------ H 前端锚点与文案
class FrontEndTests(unittest.TestCase):
    """卡面 §2：两个新锚点＋既有 ID/class 与中文文案不许动（T007-BA 红线）。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    def test_the_two_new_anchors_exist_exactly_once(self):
        for anchor in ('id="furnace-btn"', 'id="comfy-auto"'):
            self.assertEqual(self.html.count(anchor), 1, anchor)

    def test_button_text_and_title_are_chinese(self):
        self.assertIn("起炉子</button>", self.html)
        self.assertIn("绝不偷偷起", self.html)
        self.assertIn("8G 显存", self.html)

    def test_checkbox_label_is_the_cards_verbatim_wording(self):
        self.assertIn("开机自动拉起炉子（吃8G显存，默认关）", self.html)

    def test_checkbox_is_an_unchecked_checkbox(self):
        i = self.html.index('id="comfy-auto"')
        tag = self.html[self.html.rindex("<input", 0, i):self.html.index(">", i)]
        self.assertIn('type="checkbox"', tag)
        self.assertNotIn("checked", tag)                      # D102：默认关，HTML 里就不该预勾

    def test_js_binds_both_anchors(self):
        self.assertIn('$("furnace-btn").onclick = startFurnace', self.js)
        self.assertIn('$("comfy-auto").onchange = saveComfyAuto', self.js)

    def test_js_posts_to_the_two_new_endpoints(self):
        self.assertIn('"/api/furnace/start"', self.js)
        self.assertIn('"/api/furnace/auto"', self.js)

    def test_js_restores_the_switch_from_bootstrap(self):
        self.assertIn('$("comfy-auto").checked = !!(BOOT.comfy || {}).auto_start', self.js)

    def test_js_greys_the_button_out_while_busy_or_alive(self):
        self.assertIn("syncFurnaceUi(BOOT.comfy_alive)", self.js)
        self.assertIn('btn.disabled = busy || !!alive', self.js)
        self.assertIn('btn.dataset.busy = "1"', self.js)
        self.assertIn('if (btn.disabled) return false', self.js)     # 防连点

    def test_failed_save_rolls_the_checkbox_back(self):
        """存不进盘就把勾退回来：界面不许显示一个盘上并不存在的状态。"""
        self.assertIn("box.checked = !want", self.js)

    def test_token_never_goes_into_a_url_and_t011_js_keeps_off_localstorage(self):
        """D135：token 只走 Authorization 头。"""
        self.assertNotIn("token=", self.js)
        self.assertNotIn("token=", self.html)
        block = self.js[self.js.index("/* ---- T011 起炉子"):]
        self.assertNotIn("localStorage", block)

    def test_existing_anchors_are_untouched(self):
        """T007-BA 红线：既有 ID 一个都不许少（挑顶栏与设置区那几个盯）。"""
        for anchor in ('id="comfy-state"', 'id="gpu-pill"', 'id="ui-blur"', 'id="ui-density"',
                       'id="ui-bg"', 'id="ui-message"', 'id="go"', 'id="status"'):
            self.assertEqual(self.html.count(anchor), 1, anchor)

    def test_style_css_needed_no_change(self):
        """置灰靠既有全局规则，不是新加的 CSS——本卡没动 style.css。"""
        self.assertIn("button:disabled", self.css)
        self.assertNotIn("furnace-btn", self.css)
        self.assertNotIn("comfy-auto", self.css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
