"""触发词：人工覆盖 → 查表 → safetensors 元数据 → 留空（D20），并按槽位顺序插到 B 位（D115）。

B 位规则沿用早期版本（trigger 插入位置一致）：跳过开头的质量词/人数词，插在它们之后。
覆盖表只影响「查到什么值」，不影响 `compose` 的清理与插入口径（T003）。
"""
import json
import struct
from pathlib import Path

from . import models

QUALITY_WORDS = {
    "newest", "recent", "old", "early", "mid", "safe", "explicit", "questionable", "sensitive",
    "masterpiece", "best quality", "good quality", "normal quality", "worst quality", "low quality",
    "highres", "absurdres", "high quality", "score_1", "score_2", "score_3", "score_4",
    "score_5", "score_6", "score_7", "score_8", "score_9",
}
COUNT_WORDS = {
    "1girl", "2girls", "3girls", "4girls", "5girls", "6+girls", "multiple girls", "many girls",
    "1boy", "2boys", "multiple boys", "solo", "solo focus", "no humans", "animal focus", "other focus",
}
# 元数据兜底时不当触发词看的通用标签
GENERIC_TAGS = QUALITY_WORDS | COUNT_WORDS | {
    "multiple boys", "2boys", "3boys", "boy", "girl", "male", "female", "breasts", "hair",
    "long hair", "short hair", "blonde hair", "black hair", "brown hair", "eyes", "smile",
    "school uniform", "outdoors", "indoors", "standing", "sitting", "looking at viewer",
}
EXPLICIT_TRIGGER_KEYS = ("modelspec.trigger", "ss_trigger_words", "trigger", "trigger_words")
_MAX_HEADER = 50 * 1024 * 1024


def _norm(tok: str) -> str:
    """归一：小写 + 下划线当空格 + 折叠空白。'Blue_Archive' 与 'blue  archive' 视为同一 token。"""
    return " ".join((tok or "").lower().replace("_", " ").split())


def _is_head_word(part: str) -> bool:
    q = (part or "").strip().lower()
    if not q:
        return False
    if q in QUALITY_WORDS or q in COUNT_WORDS:
        return True
    return q.startswith("year ")


def _split_tags(trigger: str) -> list:
    """一支笔的触发词可能是 'blue_archive, game_cg, halo'，拆成有序 token（按归一去重，保留原写法）。"""
    out, seen = [], set()
    for tok in (trigger or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        n = _norm(tok)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(tok)
    return out


def _metadata_trigger(path: Path) -> str:
    """读 safetensors 头部元数据猜触发词；读不到/看不懂一律返回空串，不抛。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(8)
            if len(raw) < 8:
                return ""
            (size,) = struct.unpack("<Q", raw)
            if size <= 0 or size > _MAX_HEADER:
                return ""
            header = json.loads(fh.read(size))
    except Exception:
        return ""
    meta = header.get("__metadata__") if isinstance(header, dict) else None
    if not isinstance(meta, dict):
        return ""
    for key in EXPLICIT_TRIGGER_KEYS:
        val = meta.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # 退路：ss_tag_frequency 里出现最多的非通用标签；并列时取与文件名同源的那个
    freq_raw = meta.get("ss_tag_frequency")
    if not isinstance(freq_raw, str):
        return ""
    try:
        freq = json.loads(freq_raw)
    except Exception:
        return ""
    totals: dict = {}
    for per_dir in freq.values() if isinstance(freq, dict) else []:
        if not isinstance(per_dir, dict):
            continue
        for tag, n in per_dir.items():
            if not isinstance(tag, str) or not isinstance(n, (int, float)):
                continue
            if tag.strip().lower() in GENERIC_TAGS:
                continue
            totals[tag.strip()] = totals.get(tag.strip(), 0) + n
    if not totals:
        return ""
    stem = {t for t in path.stem.lower().replace("_", " ").replace("-", " ").split() if len(t) > 2}
    best = max(totals.values())
    tied = [t for t, n in totals.items() if n == best]
    for tag in tied:
        if stem & set(tag.lower().replace("_", " ").replace("-", " ").split()):
            return tag
    return min(tied, key=len)


class TriggerResolver:
    """笔名 → 触发词，三层：人工覆盖 > 表 > safetensors 元数据（D20）。

    表是权威源（空串＝这支笔明确无触发词），元数据只兜新笔；
    人工覆盖（T003）压在最上面，覆盖成空串同样表示「明确无」，不兜元数据。
    不传 override_path 就只剩后两层，行为与 T001 完全一致（回归保护）。
    """

    def __init__(self, table_path, lora_dir, override_path=None):
        self.table_path = Path(table_path)
        self.lora_dir = Path(lora_dir)
        self.override_path = Path(override_path) if override_path is not None else None
        self._table = None
        self._meta_cache: dict = {}

    @property
    def table(self) -> dict:
        if self._table is None:
            self._table = self._read_table()
        return self._table

    def _read_table(self) -> dict:
        try:
            data = json.loads(self.table_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        out = {}
        for k, v in data.items() if isinstance(data, dict) else []:
            if isinstance(k, str) and isinstance(v, str):
                out[k] = v.strip()
                out[k + ".safetensors"] = v.strip()
        return out

    def _table_value(self, name: str):
        """表里查这支笔：全名 → 裸名 → 短键当关键词兜底；没命中返回 None（区别于命中的空串）。"""
        table = self.table
        bare = name[:-len(".safetensors")] if name.lower().endswith(".safetensors") else name
        if name in table:
            return table[name]
        if bare in table:
            return table[bare]
        low = name.lower()
        # 表里的短键（'pattern_'/'example_'/'sample_style'…）当关键词兜底；长键优先，避免 example-pen 抢走 example-pen_v4.0
        for key in sorted(table, key=len, reverse=True):
            if len(key) >= 2 and key.lower() in low:
                return table[key]
        return None

    def _meta_value(self, name: str) -> str:
        """元数据兜底值，按笔名缓存：一次扫描里同一支笔只读一次头。"""
        if name not in self._meta_cache:
            self._meta_cache[name] = _metadata_trigger(self.lora_dir / name)
        return self._meta_cache[name]

    def _override_value(self, name: str):
        """人工覆盖值；没接覆盖表或没这条返回 None。每次重读：界面存完立刻生效，不用重启。"""
        if self.override_path is None:
            return None
        return models.load_overrides(self.override_path).get(models.normalize_name(name))

    def details(self, lora_name: str) -> dict:
        """三层各是什么 ＋ 最终生效值，给模型面板显示。"""
        name = (lora_name or "").strip()
        override = self._override_value(name)
        table = self._table_value(name) if name else None
        meta = self._meta_value(name) if name else ""
        effective = override if override is not None else (table if table is not None else meta)
        return {"table": table, "meta": meta, "override": override,
                "effective": effective or ""}

    def lookup(self, lora_name: str) -> str:
        """短路版 details：命中上层就不再往下读（口径与 details 的 effective 一致，有测试盯着）。"""
        name = (lora_name or "").strip()
        if not name:
            return ""
        override = self._override_value(name)
        if override is not None:
            return override
        table = self._table_value(name)
        if table is not None:
            return table
        return self._meta_value(name)

    def for_loras(self, loras: list) -> list:
        """按槽位顺序取每支已选笔的触发词；空槽与无触发词的笔跳过，顺序不变。"""
        out = []
        for entry in loras or []:
            if not entry or not entry.get("name"):
                continue
            trig = self.lookup(entry["name"])
            if trig:
                out.append(trig)
        return out


def compose(positive: str, triggers: list) -> str:
    """把本次所选笔的触发词按槽位顺序插到 B 位，并去重。

    清理范围＝「本次所选笔」的触发词 token（归一后比对：小写、下划线当空格、折叠空白）；
    未选笔的触发词、普通提示词、笔名都不动。无触发词可插时（含全空触发词的笔，D20）正词逐字不变。
    """
    tokens, seen = [], set()
    for trig in triggers or []:
        for tok in _split_tags(trig):
            n = _norm(tok)
            if n and n not in seen:
                seen.add(n)
                tokens.append(tok)
    if not tokens:
        return positive or ""
    kept = [p.strip() for p in (positive or "").split(",")]
    kept = [p for p in kept if p and _norm(p) not in seen]
    idx = 0
    while idx < len(kept) and _is_head_word(kept[idx]):
        idx += 1
    return ", ".join(kept[:idx] + tokens + kept[idx:])
