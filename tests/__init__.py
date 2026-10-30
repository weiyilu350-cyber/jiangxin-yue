"""画舫单元测试：python -m unittest discover -s tests -t .。"""
import atexit
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from app import settings as ST

# T011 裁定 4 的那道闸，在整个测试包里默认合上（用 setdefault，外面显式给的值优先）。
#
# 只写在 T011 自己的测试基类里是不够的：`app.main` 的自动拉起挂在 lifespan 上，而 lifespan
# 只在 `with TestClient(...)` 里才跑。今天只有 T011 的用例这么写，可下一个工人一旦在别的
# 测试文件里也用 `with`，而主人盘上又勾着「开机自动拉起炉子」，那一跑全量单测就会去点一次
# 真炉子——8G 显存，还和训练抢卡。放在包级＝这件事从此靠构造保证，不靠人记得。
os.environ.setdefault("HUAFANG_SKIP_AUTOSTART", "1")
# T012 同样在包级关闸，避免其它端点用例进入 lifespan 后碰到真炉子的 /free。
os.environ.setdefault("HUAFANG_SKIP_IDLE_FREE", "1")

# 公开副本不带运行数据；导入 main 生成的 token 与配置也只落在测试临时目录。
_runtime = TemporaryDirectory(prefix="huafang-tests-")
atexit.register(_runtime.cleanup)
ST.DATA = Path(_runtime.name)
ST.SETTINGS_FILE = ST.DATA / "settings.json"
ST.PRESETS_FILE = ST.DATA / "presets.json"
ST.TOKEN_FILE = ST.DATA / "token.txt"
ST.TRIGGER_OVERRIDES_FILE = ST.DATA / "trigger_overrides.json"

# 端点单测使用假炉子，但启动时的契约检查仍会核模型文件是否存在。
# 明列测试夹具，避免把「本机正好装了这些模型」误当成单测前提；这些头部不能用于推理。
_models = ST.DATA / "models"
_model_files = {
    "diffusion_models": ("anima-base-v1.0.safetensors", "anima-aesthetic-v1.1.safetensors",
                         "anima-turbo-v1.1.safetensors", "chosenIrisesMix_v20Anima.safetensors"),
    "text_encoders": ("qwen_3_06b_base.safetensors",),
    "vae": ("qwen_image_vae.safetensors",),
    "loras": ("sample_style.safetensors",),
}
for _role, _names in _model_files.items():
    (_models / _role).mkdir(parents=True)
    for _name in _names:
        (_models / _role / _name).write_bytes((2).to_bytes(8, "little") + b"{}")
_output = ST.DATA / "output"
_output.mkdir()
ST.SETTINGS_FILE.write_text(json.dumps({
    "models_dir": str(_models), "output_dir": str(_output),
    "trigger_map": str(ST.DATA / "trigger_map.json"),
    "zh": {"table": str(ST.DATA / "zh_tags.json")},
}), encoding="utf-8")
