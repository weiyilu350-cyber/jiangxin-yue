"""C 卡 §1/§2：图库持久索引（stdlib sqlite3，图片字节一律不入库）。

三条铁律（卡面「范围裁定」）：
1. **图文件永远不搬家、不删除、不改名**——这里只写索引；收藏／评分／标签全在索引侧，
   绝不写进图片（EXIF／边车都不写）。所有扫描都是只读。
2. **不瞎编元数据**：认不出参数来源的图跳过并计数报告（`skipped`／`reasons`），
   宁可少一行也不给主人编一个假 seed。
3. **幂等**：`(filename, subfolder, type)` 唯一；重复回填只 `INSERT OR IGNORE`，
   绝不 UPDATE——否则主人打过的星星与标签会被一次回填抹掉。

归因口径（2026-10-29 实测炉子输出目录 236 张 PNG：228 张精确归因，8 张跳过）：
- ComfyUI 的 SaveImage 会把整份 API 工作流写进 PNG 的 `prompt` tEXt 块，所以磁盘上的旧图
  也能读出真参数，不必靠文件名猜。
- `<prefix>_<序号>_.png` 去掉尾巴＝当初提交的 `filename_prefix`，据此对回具体 SaveImage 节点，
  再用 profile 的 `save_slots[].final` 判 final／intermediate（D92 同一口径）。
- 工作流与 profile 的匹配按 `{节点号: class_type}` 全等；今天有三对 profile 结构完全一样
  （anima_t2i／chosen_irises 等），再按 PNG 里那串 `huafang/<工作流号>/<时刻>` 前缀分辨。
  前缀不带工作流号的旧图（`ba-*`、`pic-*`、`restore_test` 共 8 张）认不出＝跳过，绝不猜。
"""
import json
import re
import sqlite3
import struct
from datetime import datetime
from pathlib import Path

from . import settings as ST
from . import workflow_profile as wp

# 回填一次读的炉子历史上限：T005 对账用 50 条是「刚跑完的」口径，回填要的是「所有旧账」，
# 所以另给一个更大的数；仍然只读、不改炉子任何状态。
BACKFILL_HISTORY_MAX = 500
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
# `runner.build_workflow` 给 final 保存口写的前缀根：`huafang/<workflow_id>/<时刻>`。
# 结构一样的几份 profile 靠它分辨（见 match_profile），所以这个词必须与 runner 里写的一致。
HUAFANG_PREFIX_ROOT = "huafang"
# ComfyUI 存图命名：`<filename_prefix>_<五位序号>_.png`
_COUNTER = re.compile(r"_\d+_\.(png|jpg|jpeg|webp)$", re.I)
MAX_TAGS = 32           # 一张图的标签上限：够用了，也挡住把整段提示词塞进标签
MAX_TAG_LEN = 40
RATING_MAX = 5
PER_PAGE_MAX = 200      # 一页最多这么多张，挡住 per_page=100000 把浏览器拖死


class GalleryError(Exception):
    """带人话的错：端点直接翻成 400，不吐堆栈给主人（D96）。"""

    def __init__(self, code: str, human: str):
        super().__init__(human)
        self.code = code
        self.human = human


SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT NOT NULL,
    subfolder    TEXT NOT NULL DEFAULT '',
    type         TEXT NOT NULL DEFAULT 'output',
    kind         TEXT NOT NULL DEFAULT 'final',
    source       TEXT NOT NULL DEFAULT 'job',
    servable     INTEGER NOT NULL DEFAULT 1,
    path         TEXT NOT NULL DEFAULT '',
    workflow_id  TEXT,
    seed         INTEGER,
    steps        REAL,
    cfg          REAL,
    sampler_name TEXT,
    scheduler    TEXT,
    denoise      REAL,
    width        INTEGER,
    height       INTEGER,
    count        INTEGER,
    positive     TEXT,
    negative     TEXT,
    unet         TEXT,
    clip         TEXT,
    vae          TEXT,
    pens         TEXT NOT NULL DEFAULT '[]',
    created      TEXT,
    indexed      TEXT,
    fav          INTEGER NOT NULL DEFAULT 0,
    rating       INTEGER NOT NULL DEFAULT 0,
    tags         TEXT NOT NULL DEFAULT '[]',
    UNIQUE (filename, subfolder, type)
);
CREATE INDEX IF NOT EXISTS idx_images_created ON images (created);
CREATE INDEX IF NOT EXISTS idx_images_fav     ON images (fav);
CREATE INDEX IF NOT EXISTS idx_images_wf      ON images (workflow_id);
"""

# 列表里默认不显示中间图（卡 §1）；`kind='unknown'` 是「认不出是不是成品」，
# 那就显示出来——藏起来等于替主人做决定，而且可能是他真正的成品。
HIDDEN_KINDS = ("intermediate",)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _tally(rep: dict, what) -> None:
    """把一张图的处置结果记进回填报告：`added`／`already` 各计一格，None＝已跳过、不再计。"""
    if what == "added":
        rep["added"] += 1
    elif what == "already":
        rep["already"] += 1


def _open(path: Path) -> sqlite3.Connection:
    """开一条索引库连接。

    fastapi 的同步端点跑在线程池里：连接是在别的线程建的，不关掉 `check_same_thread`
    一句 `SELECT` 就抛 ProgrammingError（实测，端点层 22 个用例全挂在这上头）。
    Python 3.12 的 sqlite3 是 serialized 模式（`threadsafety == 3`），跨线程共用一条连接安全；
    `timeout` 兜住「回填在写、前端在读」这种自己撞自己的锁。
    """
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


class Gallery:
    """一个图库索引库。db_path 可换（测试用临时库），默认 `data/gallery.sqlite`。"""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else ST.DATA / "gallery.sqlite"
        self._conn = None
        self.rebuilt = None     # 库损坏重建过＝一份交代（坏库挪去哪、现在用哪个文件），界面上要说得出来

    # ------------------------------------------------------------------ 连接
    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = None
        try:
            conn = _open(self.db_path)
            conn.executescript(SCHEMA)
        except sqlite3.DatabaseError:
            # 库坏了（半截写入／根本不是 sqlite 文件）→ 挪开重建，与 settings `_read_dict` 同哲学：
            # 坏文件不删，只让路；主人的收藏丢了要能查得到为什么。
            # Windows 上 sqlite 此刻还攥着坏文件的句柄，不先 close 就 replace 不动（WinError 32）。
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            conn = self._rebuild()
        self._conn = conn
        return conn

    def _rebuild(self) -> sqlite3.Connection:
        old = self.db_path
        bad = old.with_name(f"{old.name}.corrupt-{datetime.now():%Y%m%d%H%M%S}")
        moved = True
        try:
            old.replace(bad)
        except OSError:
            # 坏文件被别的进程攥着（杀毒／索引服务）挪不动：换个新库名接着干活，坏文件原地留着不删，
            # 绝不因为一个坏索引库把整个图库变成 500（卡面「sqlite 损坏→重建不崩」）。
            moved = False
            self.db_path = old.with_name(f"{old.stem}-{datetime.now():%Y%m%d%H%M%S}.sqlite")
        # bad 只在真挪动过时才是它；没挪动就说原路径，别报一个不存在的文件名
        self.rebuilt = {"bad": str(bad if moved else old), "moved": moved, "now": str(self.db_path)}
        conn = _open(self.db_path)
        conn.executescript(SCHEMA)
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -------------------------------------------------------------- 参数抽取
    @staticmethod
    def attrs_of(wf: dict, prof: dict) -> dict:
        """从（工作流, profile）抽出这张图的参数字面值。

        **实时登记与回填共用这一个函数**，否则「刚出的图」与「回填的旧图」字段口径会岔开，
        筛选就会漏。取不到就是 None，绝不填默认值冒充（铁律 2）。
        """
        out = {"workflow_id": prof.get("workflow_id"), "pens": [], "count": None}
        for slot in prof.get("model_slots", []):
            role = slot["role"]
            if role in ("unet", "clip", "vae"):
                out[role] = _literal(wf, slot["node"], slot["input"])
        for ls in prof.get("lora_slots", []):
            name = _literal(wf, ls["node"], ls["name_input"])
            if name is None:
                continue
            out["pens"].append({"role": ls.get("role"), "name": name,
                                "strength": _literal(wf, ls["node"], ls["strength_input"]),
                                "stages": list(ls.get("stages") or [])})
        stages = prof.get("stages", [])
        # 参数一律取**第一段**的字面值：那正是界面发出去的那套全局值，也是「按此参数回到出图台」
        # 要回填的值。段2（精修）的参数在回执里带 `stage2.` 前缀另算，不混进这一行。
        for key in ("seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"):
            val = None
            for st in stages:
                ref = st.get("params", {}).get(key)
                if not ref:
                    continue
                got = _literal(wf, ref["node"], ref["input"])
                if got is not None:
                    val = got
                    break
            out[key] = val
        for st in stages:
            src = (st.get("prompt") or {}).get("positive_source") or []
            if src and out.get("positive") is None:
                out["positive"] = _literal(wf, src[0]["node"], src[0]["input"])
            nsrc = (st.get("prompt") or {}).get("negative_source") or []
            if nsrc and out.get("negative") is None:
                out["negative"] = _literal(wf, nsrc[0]["node"], nsrc[0]["input"])
        out["width"], out["height"] = global_size(wf, prof)
        for st in stages:
            lz = st.get("latent_size")
            if lz and lz.get("batch_input"):
                n = _literal(wf, lz["node"], lz["batch_input"])
                if isinstance(n, int):
                    out["count"] = n
        out["pens"] = json.dumps(out["pens"], ensure_ascii=False)
        return out

    # ------------------------------------------------------------------ 写入
    def insert(self, row: dict) -> bool:
        """插一行；已存在（同 filename+subfolder+type）就什么都不做并返回 False。

        用 `INSERT OR IGNORE` 而不是 upsert：主人打过的 fav／rating／tags 绝不能被回填覆盖。
        """
        cols = ["filename", "subfolder", "type", "kind", "source", "servable", "path",
                "workflow_id", "seed", "steps", "cfg", "sampler_name", "scheduler", "denoise",
                "width", "height", "count", "positive", "negative", "unet", "clip", "vae",
                "pens", "created", "indexed", "fav", "rating", "tags"]
        data = {c: row.get(c) for c in cols}
        data["indexed"] = _now()
        # OR IGNORE 连 NOT NULL 违约也一并悄悄吞掉（整行消失、连个错都没有），
        # 所以这些非空列必须自己兜底，不能指望建表时的 DEFAULT。
        data["filename"] = data["filename"] or ""
        # 分隔符必须折成一种写法再入库：炉子回话里的 subfolder 是照抄画舫提交的 filename_prefix
        # （Windows 上是反斜杠），而扫盘那一路用 as_posix() 给正斜杠。不折就会同一张图登两行，
        # 主人在一张上打的收藏／评分／标签，另一张压根不知道。取图那侧两种写法炉子都认。
        data["subfolder"] = (data["subfolder"] or "").replace("\\", "/")
        data["type"] = data["type"] or "output"
        data["kind"] = data["kind"] or "final"
        data["source"] = data["source"] or "job"
        data["servable"] = 1 if data["servable"] else 0
        data["path"] = data["path"] or ""
        data["pens"] = data["pens"] or "[]"
        data["tags"] = data["tags"] or "[]"
        data["fav"] = 1 if data["fav"] else 0
        data["rating"] = _int(data["rating"], 0, 0, RATING_MAX)
        if not data["filename"]:
            return False
        conn = self._connect()
        cur = conn.execute(
            f"INSERT OR IGNORE INTO images ({','.join(cols)}) "
            f"VALUES ({','.join(':' + c for c in cols)})", data)
        conn.commit()
        return cur.rowcount > 0

    def _filename_known(self, filename: str) -> bool:
        """这个名字是不是已经登过了（不比 subfolder：复制品与原件就差在这儿）。"""
        return self._connect().execute(
            "SELECT 1 FROM images WHERE filename=? LIMIT 1", (filename,)).fetchone() is not None

    def register_job(self, job) -> int:
        """job 到 done → 每张成品入索引（卡 §1 登记时机）。

        `job.images` 已经由 D92 筛过（只有 final 口），所以这里不再判 kind。
        索引写不进去也不许把出图弄成失败：这函数吞掉库错、返回 -1，
        主人回填一次就能补上（回填幂等）。
        """
        if not job or not getattr(job, "profile", None):
            return 0
        sent = getattr(job, "sent", None) or {}
        req = getattr(job, "request", None) or {}
        try:
            attrs = self.attrs_of(sent, job.profile)
        except Exception:
            return -1
        # 界面要的值优先于工作流字面值：主人改过的 seed／尺寸才是他认的那一份。
        # 只覆盖真给了的项——界面没发的（比如跟工作流默认的 clip/vae）保持字面值，
        # 填 None 进去等于把「认不出」和「主人没填」混成一回事。
        for key in ("seed", "width", "height", "negative"):
            if req.get(key) is not None:
                attrs[key] = req[key]
        attrs["positive"] = (job.positive_final if getattr(job, "positive_final", None)
                             else req.get("positive") or attrs.get("positive"))
        n = 0
        for img in job.images or []:
            row = dict(attrs)
            row.update({"filename": img.get("filename"), "subfolder": img.get("subfolder", ""),
                        "type": img.get("type", "output"), "kind": "final", "source": "job",
                        "servable": 1, "path": "", "created": job.created})
            try:
                n += 1 if self.insert(row) else 0
            except sqlite3.Error:
                return -1
        return n

    # ------------------------------------------------------------------ 回填
    def backfill(self, client=None, profiles=None, dirs=None, furnace_root=None) -> dict:
        """只读扫描补登历史图：炉子 `/history` ＋ 图库附加目录（含炉子输出目录本身）。

        认不出的文件跳过并按原因计数（铁律 2）。返回一份能直接给小唯看的报告。
        """
        profiles = profiles or {}
        rep = {"scanned": 0, "added": 0, "already": 0, "skipped": 0, "reasons": {},
               "history_entries": 0, "dirs": []}

        def skip(why):
            rep["skipped"] += 1
            rep["reasons"][why] = rep["reasons"].get(why, 0) + 1

        if client is not None:
            try:
                hist = client.history_recent(BACKFILL_HISTORY_MAX)
            except Exception as e:              # 炉子没开／回的不是 JSON：只少一个来源，不整个失败
                skip(f"炉子历史读不出来：{e}")
                hist = {}
            for pid, entry in (hist or {}).items():
                if not isinstance(entry, dict):
                    skip("history 条目结构不对")
                    continue
                rep["history_entries"] += 1
                for what in self._backfill_entry(entry, profiles, skip):
                    rep["scanned"] += 1
                    _tally(rep, what)

        # 炉子输出目录**先扫**：它存的是原件，`/api/view` 也只认它（那个端点是代理炉子取图）。
        # 附加目录（含 T010 复制目录）里的图取不到字节，servable=0，前端只给参数与路径，
        # 不硬编一个坏图框。与已扫目录重叠的一律不再扫，否则同一张图会按不同 subfolder 登两次。
        roots, seen = [], []
        if furnace_root:
            roots.append((Path(furnace_root), 1))
            seen.append(Path(furnace_root))
        for d in (dirs or []):
            if not d:
                continue
            p = Path(d)
            if any(_overlaps(p, s) for s in seen):
                skip(f"目录与已扫目录重叠，跳过：{p}")
                continue
            seen.append(p)
            roots.append((p, 0))
        for root, servable in roots:
            rep["dirs"].append(str(root))
            if not root.exists():
                skip(f"目录不存在：{root}")
                continue
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                rep["scanned"] += 1
                if p.suffix.lower() not in IMAGE_EXTS:
                    skip("不是图片扩展名")
                    continue
                _tally(rep, self._backfill_file(p, root, profiles, skip, servable))
        return rep

    def _backfill_entry(self, entry, profiles, skip) -> list:
        """一条 history 记录 → 若干张图。返回每张的处置（`added`／`already`），认不出整条就跳过。"""
        from . import runner                     # 循环导入只在函数内发生一次，省事也不脏模块头
        wf = runner.history_workflow(entry)
        prof = match_profile(wf, profiles)
        if prof is None:
            skip("history 工作流对不上任何 profile")     # 铁律 2：认不出就不登，不给主人编参数
            return [None]
        outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
        finals = runner.final_nodes(prof)
        created = runner.history_created(entry)
        attrs = self.attrs_of(wf, prof)
        done = []
        for nid, node_out in outputs.items():
            if not isinstance(node_out, dict):
                continue
            for img in node_out.get("images") or []:
                if not isinstance(img, dict) or not img.get("filename"):
                    skip("history 里的图没有 filename")
                    continue
                row = dict(attrs)
                row.update({"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                            "type": img.get("type", "output"), "source": "history",
                            "servable": 1, "path": "", "created": created,
                            "kind": _kind_of_node(nid, finals)})
                done.append("added" if self.insert(row) else "already")
        return done

    def _backfill_file(self, p: Path, root: Path, profiles, skip, servable: int):
        """盘上一张图 → 一行索引。认不出参数来源就跳过并计数（铁律 2），返回处置结果。"""
        rel = p.relative_to(root).as_posix()
        sub, _, name = rel.rpartition("/")
        stem = _COUNTER.sub("", rel)        # 去掉 `_<序号>_.png` 尾巴＝当初提交的 filename_prefix
        if not servable and self._filename_known(name):
            # T010 复制目录里是炉子原件的副本（文件名照抄）：原件已经登过就不再登一行，
            # 否则同一张图在图库里出现两次，主人收藏了其中一个另一个还是灰的。
            return "already"
        try:
            mtime = p.stat().st_mtime
        except OSError as e:              # 主人目录里的东西读不动很正常（权限／被占用）：跳过，不整场失败
            skip(f"读不动这个文件：{e.strerror or e}")
            return None
        embedded = png_prompt(p)
        if embedded is None:
            skip("读不出图里内嵌的工作流（不是 PNG，或 PNG 里没有 prompt 块）")
            return None
        prof = match_profile(embedded, profiles)
        if prof is None:
            skip("认不出这份图是哪个工作流出的（结构对不上，或几份 profile 结构一样而前缀不带工作流号）")
            return None
        finals = {ss["node"] for ss in prof.get("save_slots", []) if ss.get("final")}
        row = dict(self.attrs_of(embedded, prof))
        row.update({"filename": name, "subfolder": sub, "type": "output", "source": "disk",
                    "servable": servable, "path": str(p),
                    "kind": _kind_of_node(prefix_node(embedded, stem), finals),
                    "created": datetime.fromtimestamp(mtime).isoformat(timespec="seconds")})
        return "added" if self.insert(row) else "already"

    # ------------------------------------------------------------------ 查询
    def query(self, f: dict) -> dict:
        sql, args = _where(f)
        per = _int(f.get("per_page"), 60, 1, PER_PAGE_MAX)
        page = max(1, _int(f.get("page"), 1, 1, 10 ** 6))
        conn = self._connect()
        total = conn.execute(f"SELECT COUNT(*) FROM images {sql}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM images {sql} ORDER BY created DESC, id DESC LIMIT ? OFFSET ?",
            args + [per, (page - 1) * per]).fetchall()
        return {"total": total, "page": page, "per_page": per,
                "pages": (total + per - 1) // per, "items": [_public(r) for r in rows]}

    def get(self, image_id) -> dict:
        row = self._connect().execute("SELECT * FROM images WHERE id=?",
                                      (_int(image_id, 0, 0, 10 ** 9),)).fetchone()
        if row is None:
            raise GalleryError("no_such_image", "图库里没有这张图（可能刚被回填重建过，刷新一下）")
        return _public(row)

    def patch(self, image_id, patch: dict) -> dict:
        """局部改 fav／rating／tags：只动给了的字段，坏值 400 人话，绝不串字段。"""
        sets, args = [], []
        if "fav" in patch:
            sets.append("fav=?")
            args.append(1 if _as_bool(patch["fav"], "收藏") else 0)
        if "rating" in patch:
            sets.append("rating=?")
            args.append(_as_rating(patch["rating"]))
        if "tags" in patch:
            sets.append("tags=?")
            args.append(json.dumps(_as_tags(patch["tags"]), ensure_ascii=False))
        if not sets:
            raise GalleryError("empty_patch", "没有要改的东西：只认 fav／rating／tags 三个字段")
        row = self.get(image_id)          # 不存在先报人话 404 语义，别 UPDATE 0 行还回成功
        args.append(row["id"])
        conn = self._connect()
        conn.execute(f"UPDATE images SET {','.join(sets)} WHERE id=?", args)
        conn.commit()
        return self.get(image_id)

    def tags(self) -> list:
        """所有标签＋各自多少张，按用量倒序（前端做标签云／下拉）。"""
        counts = {}
        for row in self._connect().execute("SELECT tags FROM images"):
            for t in _loads(row["tags"], []):
                counts[t] = counts.get(t, 0) + 1
        return [{"tag": t, "count": n} for t, n in
                sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    def stats(self) -> dict:
        conn = self._connect()
        one = conn.execute("SELECT COUNT(*) n, SUM(fav) f, SUM(kind='intermediate') i "
                           "FROM images").fetchone()
        return {"total": one["n"] or 0, "fav": one["f"] or 0, "intermediate": one["i"] or 0}


# ---------------------------------------------------------------------- 工具
def _overlaps(a, b) -> bool:
    """两条路径是不是同一条／互相包含（Windows 大小写不敏感）。

    扫重叠目录＝同一张图按不同 subfolder 登两次，而且第二次那份还取不到字节。
    """
    pa, pb = Path(a).parts, Path(b).parts
    n = min(len(pa), len(pb))
    return tuple(x.lower() for x in pa[:n]) == tuple(x.lower() for x in pb[:n])


def _literal(wf: dict, node, inp):
    """取工作流里某个节点某个输入的字面值；取不到＝None（不填默认值冒充）。"""
    ins = (wf.get(str(node)) or {}).get("inputs") or {}
    return ins.get(inp)


def global_size(wf: dict, prof: dict) -> tuple:
    """从工作流字面值反推「全局成品尺寸」。

    有 `size_out` 的段（hires 的 ImageScale）写的就是全局×1.0，直接取它；
    否则取段1 latent 的字面值除以它的倍率（段1＝全局×0.5 时就是乘回去）。
    """
    stages = prof.get("stages", [])
    for st in reversed(stages):
        so = st.get("size_out")
        if so:
            w, h = _literal(wf, so["node"], so["width_input"]), _literal(wf, so["node"], so["height_input"])
            if isinstance(w, int) and isinstance(h, int):
                return w, h
    for st in stages:
        lz = st.get("latent_size")
        if not lz:
            continue
        w, h = _literal(wf, lz["node"], lz["width_input"]), _literal(wf, lz["node"], lz["height_input"])
        if not (isinstance(w, int) and isinstance(h, int)):
            continue
        scale = lz.get("size_scale") or [1.0, 1.0]
        try:
            return int(round(w / scale[0])), int(round(h / scale[1]))
        except (TypeError, ValueError, ZeroDivisionError):
            return w, h
    return None, None


def match_profile(wf: dict, profiles: dict):
    """这份工作流是哪份 profile 出的：按 `{节点号: class_type}` 全等认。

    结构一样的两份 profile（今天 anima_t2i 与 chosen_irises、anima_dual 与 aesthetic_dual、
    anima_tri 与 aesthetic_tri 三对都是）再按 `huafang/<工作流号>/<时刻>` 前缀认一次。
    两级都认不出就返回 None＝跳过（铁律 2）。
    **不拿目录名当凭据**：参数是顺着 profile 的槽位映射从 PNG 里读的，结构对不上还硬套
    就等于用错的映射读出错号节点的值——那才是真瞎编。
    """
    if not isinstance(wf, dict) or not wf:
        return None
    shape = _shape(wf)
    hits = [pid for pid, p in profiles.items() if _shape(p.get("workflow") or {}) == shape]
    if len(hits) == 1:
        return profiles[hits[0]]["profile"]
    if not hits:
        return None
    told = prefix_profile_id(wf, profiles)
    return profiles[told]["profile"] if told in hits else None


def _shape(wf: dict) -> dict:
    return {str(k): (v or {}).get("class_type") for k, v in wf.items()}


def prefix_profile_id(wf: dict, profiles: dict):
    """从工作流自己的 SaveImage 前缀里读出工作流号。

    `runner.build_workflow` 给每个 final 保存口写死 `huafang/<workflow_id>/<时刻>`，这串
    跟着 PNG 一起落盘，所以它是出图那一刻的凭据，不是事后猜的。
    读出两个不同的号＝自相矛盾，返回 None。
    """
    told = None
    for node in (wf or {}).values():
        if (node or {}).get("class_type") != "SaveImage":
            continue
        parts = [x for x in str((node.get("inputs") or {}).get("filename_prefix") or "").split("/") if x]
        if len(parts) >= 2 and parts[0] == HUAFANG_PREFIX_ROOT and parts[1] in profiles:
            if told not in (None, parts[1]):
                return None
            told = parts[1]
    return told


def prefix_node(wf: dict, prefix: str):
    """`filename_prefix` 对回 SaveImage 节点号；对不上返回 None（kind 记 unknown）。"""
    for nid, node in (wf or {}).items():
        if (node or {}).get("class_type") != "SaveImage":
            continue
        if ((node.get("inputs") or {}).get("filename_prefix")) == prefix:
            return str(nid)
    return None


def _kind_of_node(nid, finals) -> str:
    if finals is None or nid is None:
        return "unknown"
    return "final" if str(nid) in {str(x) for x in finals} else "intermediate"


def png_prompt(path: Path):
    """读 PNG 的 `prompt` tEXt 块＝ComfyUI 存图时写进去的整份 API 工作流。

    只读、只解析（D73：工作流 JSON 当不可信数据，绝不 eval）。读不出来一律 None。
    """
    try:
        with open(path, "rb") as f:
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return None
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return None
                ln, typ = struct.unpack(">I4s", head)
                typ = typ.decode("latin1")
                data = f.read(ln)
                f.read(4)
                if len(data) < ln:
                    return None
                if typ == "tEXt":
                    key, _, val = data.partition(b"\x00")
                    if key.decode("latin1") == "prompt":
                        obj = json.loads(val.decode("utf-8"))
                        return obj if isinstance(obj, dict) else None
                if typ == "IEND":
                    return None
    except (OSError, ValueError, struct.error, UnicodeDecodeError):
        return None
    return None


def _loads(text, default):
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return default
    return obj if isinstance(obj, type(default)) else default


def _int(v, default, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


# 这两个集合用花括号写、不用元组：test_profiles 那条「不许写死节点号」的红线正则
# 会抓圆括号／方括号后紧跟的引号数字，元组写法连注释一起被误伤（D83 不松，那就换写法）。
_TRUTHY = frozenset({"1", "true"})
_FALSY = frozenset({"0", "false"})


def _as_bool(v, what) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        word = v.strip().lower()
        if word in _TRUTHY:
            return True
        if word in _FALSY:
            return False
    raise GalleryError("bad_fav", f"{what}得是 true／false（或 1／0），现在收到的是 {v!r}")


def _as_rating(v) -> int:
    if isinstance(v, bool):
        raise GalleryError("bad_rating", f"评分得是 0–{RATING_MAX} 的整数（0＝未评），不是 true/false")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise GalleryError("bad_rating", f"评分得是 0–{RATING_MAX} 的整数（0＝未评），现在收到的是 {v!r}")
    if not 0 <= n <= RATING_MAX:
        raise GalleryError("bad_rating", f"评分只能在 0–{RATING_MAX} 之间（0＝未评），现在是 {n}")
    return n


def _as_tags(v) -> list:
    if isinstance(v, str):
        v = [t for t in re.split(r"[,，]", v) if t.strip()]
    if not isinstance(v, list):
        raise GalleryError("bad_tags", "标签得是一串文字（数组，或用逗号隔开的一句话）")
    out = []
    for t in v:
        if not isinstance(t, str):
            raise GalleryError("bad_tags", f"每个标签都得是文字，{t!r} 不是")
        t = t.strip()
        if not t:
            continue
        if len(t) > MAX_TAG_LEN:
            raise GalleryError("bad_tags", f"单个标签最多 {MAX_TAG_LEN} 个字，「{t[:12]}…」太长了")
        if t not in out:
            out.append(t)
    if len(out) > MAX_TAGS:
        raise GalleryError("bad_tags", f"一张图最多 {MAX_TAGS} 个标签，现在给了 {len(out)} 个")
    return out


def _where(f: dict) -> tuple:
    """筛选矩阵 → SQL。所有值一律走参数绑定（外部输入，绝不拼字符串）。"""
    clauses, args = [], []
    kind = f.get("kind")
    if kind in ("final", "intermediate", "unknown"):
        clauses.append("kind=?")
        args.append(kind)
    elif not _as_bool_or_none(f.get("show_intermediate")):
        clauses.append(f"kind NOT IN ({','.join('?' * len(HIDDEN_KINDS))})")
        args.extend(HIDDEN_KINDS)
    for key, col in (("workflow", "workflow_id"), ("unet", "unet"), ("clip", "clip"),
                     ("vae", "vae"), ("source", "source")):
        v = f.get(key)
        if v:
            clauses.append(f"{col}=?")
            args.append(str(v))
    if _as_bool_or_none(f.get("fav_only")):
        clauses.append("fav=1")
    rmin = _int(f.get("rating_min"), 0, 0, RATING_MAX)
    if rmin:
        clauses.append("rating>=?")
        args.append(rmin)
    for key, op in (("w_min", ">="), ("w_max", "<="), ("h_min", ">="), ("h_max", "<=")):
        v = f.get(key)
        if v not in (None, ""):
            clauses.append(f"width {op} ?" if key.startswith("w") else f"height {op} ?")
            args.append(_int(v, 0, 0, 10 ** 6))
    for key, col in (("date_from", ">="), ("date_to", "<=")):
        v = f.get(key)
        if v:
            clauses.append(f"created {col} ?")
            # created 是 ISO 时刻串，按字典序比大小；「到某天为止」得含那天整天，
            # 所以补到当天最后一秒，而不是只比日期前缀（否则当天出的图全被挡在门外）。
            args.append(str(v)[:10] + ("T00:00:00" if key == "date_from" else "T23:59:59"))
    tag = f.get("tag")
    if tag:
        clauses.append("tags LIKE ?")
        args.append('%"' + str(tag).replace('"', '') + '"%')
    pen = f.get("pen")
    if pen:
        clauses.append("pens LIKE ?")
        args.append("%" + str(pen) + "%")
    q = f.get("q")
    if q:
        clauses.append("(positive LIKE ? OR negative LIKE ? OR filename LIKE ?)")
        like = "%" + str(q) + "%"
        args += [like, like, like]
    return ("WHERE " + " AND ".join(clauses) if clauses else ""), args


def _as_bool_or_none(v):
    if v in (None, ""):
        return None
    try:
        return _as_bool(v, "x")
    except GalleryError:
        return None


def _public(row) -> dict:
    """库里的行 → 前端要的形状：pens／tags 解成数组，fav 解成布尔。

    取图仍走既有 `/api/view?filename=&subfolder=&type=`（前端用 fetch＋blob，token 只在头里，
    D135），所以这里绝不拼一个带 token 的 view_url 出去。
    """
    d = dict(row)
    d["pens"] = _loads(d.get("pens"), [])
    d["tags"] = _loads(d.get("tags"), [])
    d["fav"] = bool(d.get("fav"))
    d["rating"] = d.get("rating") or 0
    return d
