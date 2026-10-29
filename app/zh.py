"""画舫 T004：中文辅助——本地词表补全 ＋ 本机 Ollama 整句翻译（只用 CPU，D99）。
词表由本地配置提供、只读；Ollama 停机只报人话，绝不拦住出图。"""
import json
import re
from pathlib import Path

import httpx

from . import settings as ST

# settings.json 的 zh 块缺字段就用这些
DEFAULTS = {
    "enabled": True,
    "ollama_url": "http://127.0.0.1:11434",
    "model": "qwen3.5:9b",
    "timeout_sec": 20,
    "table": ST.DEFAULTS["zh"]["table"],
}
MAX_ITEMS = 100          # 补全一次最多回多少条
MAX_TEXT = 5000          # 一次最多翻多少字
PROBE_TIMEOUT = 3        # 探活要快，别让首屏等

PROMPT = """你是 Danbooru/Anima 提示词翻译器。把下面这段中文翻成简洁的英文提示词。
规则：
1. 只输出英文译文，不要解释、不要加引号、不要 markdown
2. 如果是人物动作或场景描述，用自然的英文短语
3. 如果是单个名词，用 Danbooru 风格的英文标签
4. 不要添加原文没有的元素

中文：{text}
英文："""

# 中文片段：起头必须是汉字，后面可以带中文标点与空格；英文 tag／@画师天然不在里面（雷区 3）
_CN_SEG = re.compile(r"[\u4e00-\u9fff][\u4e00-\u9fff，。、；：！？…—\s]*")
# 中文残留检测比片段宽：连全角标点、扩展区一起算，宁可多剥（雷区 2）
_CJK_ANY = re.compile(
    r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*$", re.M)
_EMPH = re.compile(r"\*\*|__|~~|`")
_LABEL = re.compile(r"^\s*(英文|译文|翻译|标签|English|Output|Result|Tags?)\s*[:：]\s*", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•·]|\d+[.)、])\s+")
_QUOTES = "\"'“”‘’「」『』"
_JSONISH = re.compile(r"[{}]")
_EMPTY_PAREN = re.compile(r"\(\s*\)|\[\s*\]")
_HAS_ALNUM = re.compile(r"[A-Za-z0-9]")
# 中文标点是分隔符，删掉会把两个英文 tag 粘成一个，所以先换成逗号
_CJK_SEP = re.compile(r"[，、；：。！？…—～]")


class ZhError(Exception):
    """中文辅助的失败一律说人话；code 给前端/日志认。"""

    def __init__(self, code: str, human: str, detail: str = ""):
        super().__init__(human)
        self.code = code
        self.human = human
        self.detail = detail


def has_cjk(text: str) -> bool:
    return bool(_CJK_ANY.search(text or ""))


def _clamp_limit(limit, default: int = 20) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, MAX_ITEMS))


def _to_sec(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return DEFAULTS["timeout_sec"]
    if n <= 0:
        return DEFAULTS["timeout_sec"]
    return int(n) if n.is_integer() else n


def _tidy(text: str) -> str:
    """收敛空白与逗号：`a , ,  b` → `a, b`，首尾逗号去掉。"""
    s = re.sub(r"\s+", " ", text or "").strip()
    s = re.sub(r"\s*,\s*", ", ", s)
    s = re.sub(r"(,\s*){2,}", ", ", s)
    return s.strip(" ,")


def _drop_cjk(text: str) -> str:
    s = _CJK_SEP.sub(", ", text or "")
    s = _CJK_ANY.sub("", s)
    return _EMPTY_PAREN.sub("", s)


def _from_json(text: str):
    """模型偶尔回一坨 JSON：能解析就只取里面的字符串值（雷区 6）。"""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, dict):
        vals = [v for v in data.values() if isinstance(v, str)]
    elif isinstance(data, list):
        vals = [v for v in data if isinstance(v, str)]
    elif isinstance(data, str):
        vals = [data]
    else:
        return None
    return ", ".join(vals) if vals else ""


def clean_output(raw: str) -> str:
    """把模型的输出洗成能直接进正词框的英文标签：
    去围栏/粗体/项目符号/「英文：」标签/包裹引号，丢掉整段中文解释与行内中文残留。"""
    text = _EMPH.sub("", _FENCE.sub("", (raw or "").strip()))
    if not text:
        return ""
    parsed = _from_json(text)
    if parsed is not None:
        text = parsed
    lines = []
    for line in text.splitlines():
        s = _BULLET.sub("", _LABEL.sub("", line.strip())).strip().strip(_QUOTES).strip()
        s = _drop_cjk(s)
        # 洗完只剩标点＝这行本来就是中文解释，整行丢掉
        if _HAS_ALNUM.search(s):
            lines.append(s.strip(" ,"))
    return _tidy(_JSONISH.sub(" ", ", ".join(lines)))


class ZhTable:
    """词表懒加载＋进程内缓存：18 MB 只在第一次查的时候读一遍（雷区 1）。
    表在画舫外且只读——读不到就当空表，绝不改写、绝不崩。"""

    def __init__(self, path):
        self.path = Path(path)
        self._pairs = None      # None＝还没读盘；读过就是 [(中文, 英文tag)]

    def size(self) -> int:
        return len(self._pairs or [])

    def _load(self) -> list:
        if self._pairs is not None:
            return self._pairs
        pairs, seen = [], set()
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        if isinstance(rows, list):
            for r in rows:
                # 真表是 7 列 [en, 分类, 热度, 中文名, 中文别名, 英文别名, 中文说明]；缺列的行跳过
                if not isinstance(r, list) or len(r) < 4:
                    continue
                en, zh = r[0], r[3]
                if not isinstance(en, str) or not en.strip() or not isinstance(zh, str):
                    continue
                aliases = r[4] if len(r) > 4 and isinstance(r[4], str) else ""
                for cand in [zh] + aliases.split("|"):
                    cand = cand.strip()
                    if not cand or (cand, en) in seen:
                        continue
                    seen.add((cand, en))
                    pairs.append((cand, en))
        self._pairs = pairs     # 表本身按热度降序，扫描顺序＝候选排序，不用再排
        return pairs

    def complete(self, prefix, limit=20) -> list:
        """中文前缀优先、子串兜底；空查询返回空。"""
        q = prefix.strip() if isinstance(prefix, str) else ""
        if not q:
            return []
        limit = _clamp_limit(limit)
        head, tail = [], []
        for zh, en in self._load():
            if zh.startswith(q):
                head.append({"zh": zh, "en": en})
                if len(head) >= limit:
                    break
            elif q in zh and len(tail) < limit:
                tail.append({"zh": zh, "en": en})
        return (head + tail)[:limit]


def _default_post(url: str, payload: dict, timeout):
    return httpx.post(url, json=payload, timeout=timeout, trust_env=False)


def _default_probe(url: str, timeout) -> bool:
    return httpx.get(url + "/api/tags", timeout=timeout, trust_env=False).status_code == 200


class ZhService:
    """补全走本地词表，翻译走本机 Ollama；出网只有 `_post`／`_probe` 两处，测试用假的顶掉。"""

    def __init__(self, cfg=None, poster=None, prober=None):
        cfg = dict(DEFAULTS, **(cfg if isinstance(cfg, dict) else {}))
        self.enabled = bool(cfg.get("enabled", True))
        self.url = str(cfg.get("ollama_url") or DEFAULTS["ollama_url"]).rstrip("/")
        self.model = str(cfg.get("model") or DEFAULTS["model"])
        self.timeout = _to_sec(cfg.get("timeout_sec"))
        self.table = ZhTable(cfg.get("table") or DEFAULTS["table"])
        self._post = poster or _default_post
        self._probe = prober or _default_probe

    def complete(self, prefix, limit=20) -> list:
        return self.table.complete(prefix, limit)

    def segments(self, text: str) -> list:
        """摘出要送进模型的中文片段（去重保序）；触发词与英文 tag 根本不在里面。"""
        out = []
        for m in _CN_SEG.finditer(text or ""):
            s = m.group().strip()
            if s and s not in out:
                out.append(s)
        return out

    def status(self) -> dict:
        """给前端置灰用；探针自己炸了也只当「不在」，不把异常抛到界面。"""
        alive = False
        if self.enabled:
            try:
                alive = bool(self._probe(self.url, PROBE_TIMEOUT))
            except Exception:
                alive = False
        return {"ollama": alive, "model": self.model, "timeout": self.timeout}

    def translate(self, text: str, timeout=None) -> dict:
        text = text if isinstance(text, str) else ""
        segs = self.segments(text)
        if not segs:
            return {"text": text, "changed": False}
        if not self.enabled:
            raise ZhError("zh_disabled", "中文翻译在设置里关掉了（settings.json 的 zh.enabled）")
        secs = timeout if timeout is not None else self.timeout
        out = text
        for s in segs:
            out = out.replace(s, self._segment(s, secs))
        # 兜底再洗一遍：模型把中文回显进来也不许留（雷区 2／D99 中文不进最终 prompt）
        out = _tidy(_drop_cjk(out))
        return {"text": out, "changed": out != text}

    def _segment(self, seg: str, timeout) -> str:
        payload = {
            "model": self.model,
            "prompt": PROMPT.format(text=seg),
            "stream": False,
            "think": False,
            "keep_alive": "60s",   # 连翻多段不用反复加载模型，1 分钟后自己释放
            "options": {"temperature": 0, "num_predict": 256, "num_gpu": 0},  # 只用 CPU，不碰显卡
        }
        try:
            r = self._post(self.url + "/api/generate", payload, timeout)
        except httpx.TimeoutException as e:
            raise ZhError("ollama_timeout",
                          f"翻译超时了：{timeout:g} 秒还没回话。模型在 CPU 上跑，长句可以把 "
                          "settings.json 里的 zh.timeout_sec 调大", str(e)) from e
        except httpx.HTTPError as e:
            raise ZhError("ollama_down",
                          "连不上本机翻译模型（Ollama）；它没启动也不影响出图", str(e)) from e
        body = getattr(r, "text", "") or ""
        if getattr(r, "status_code", 0) != 200:
            if "not found" in body.lower():
                raise ZhError("model_missing",
                              f"本机没有这个翻译模型（{self.model}）；先用 ollama pull 拉下来，"
                              "或把 settings.json 里的 zh.model 改成已有的模型", body[:500])
            raise ZhError("ollama_http",
                          f"本机翻译模型回了 {r.status_code}，原文在「高级详情」里", body[:500])
        try:
            data = r.json()
        except ValueError as e:
            raise ZhError("ollama_bad_json", "本机翻译模型回的不是 JSON，没法用", body[:500]) from e
        en = clean_output(str((data or {}).get("response") or ""))
        if not en:
            raise ZhError("empty_result", f"「{seg[:20]}」没翻出来：模型回了空或全是解释")
        return en
