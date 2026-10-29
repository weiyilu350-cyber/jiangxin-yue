/* 图库页（C 卡 §2／§3）。
   三条规矩，与出图台一字不差：
   ① 图片字节只经既有 `/api/view`，用 fetch 拿 blob（`<img>` 带不了 Authorization 头，token 绝不进 URL，D135）；
   ② 界面上不出现节点 ID，只说人话字段；
   ③ 认不出的东西不编——回填报告照后端给的数字念，取不到字节的图给一句说明而不是一个坏图框。 */
const TOKEN = window.HUAFANG_TOKEN;
const H = { Authorization: "Bearer " + TOKEN };
const $ = (id) => document.getElementById(id);
const REFILL_KEY = "huafang.refill";     // 跨页交接只放参数，token 一个字不放
const COMPARE_KEY = "huafang.compare";
const PICK_MIN = 2, PICK_MAX = 8;
const RATING_MAX = 5;
const TAG_MAX = 32;
const FAV_TITLE = ["点一下收藏（只写索引库，图片文件一个字节都不动）", "已收藏：点一下取消"];

let BOOT = null;
let PAGE = { total: 0, page: 1, pages: 0, per_page: 60, items: [] };
let STATS = { total: 0, fav: 0, intermediate: 0 };
const PICKED = new Set();
let CURRENT = null;                      // 弹层里那张图的完整记录（PATCH 回来后就地更新）
const BLOBS = new Map();                 // "filename|subfolder" → blob URL 的 promise：同一张图只请求一次

function human(body) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  return (d && d.human) || "";
}

function say(msg, kind) {
  const el = $("gal-message");
  el.textContent = msg || "";
  el.className = "ui-msg" + (msg && kind ? " " + kind : "");
}

async function api(path, opts) {
  const r = await fetch(path, { headers: opts && opts.body ? { ...H, "Content-Type": "application/json" } : H, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(human(body) || "画舫回了一句看不懂的话（" + r.status + "）");
  return body;
}

function blobUrl(im) {
  const key = im.filename + "|" + (im.subfolder || "");
  if (!BLOBS.has(key)) {
    const p = fetch("/api/view?filename=" + encodeURIComponent(im.filename) +
        "&subfolder=" + encodeURIComponent(im.subfolder || "") +
        "&type=" + encodeURIComponent(im.type || "output"), { headers: H })
      .then((r) => { if (!r.ok) throw new Error("取不到图片（" + r.status + "）"); return r.blob(); })
      .then((b) => URL.createObjectURL(b));
    BLOBS.set(key, p);
    p.catch(() => BLOBS.delete(key));    // 失败不缓存（下次还能重试），顺便吃掉 rejection 免得上控制台
  }
  return BLOBS.get(key);
}

/* 取不到字节的图（附加目录里的：`/api/view` 是炉子的代理，够不着）不给坏图框，
   给一块写着路径的灰卡——参数照旧能看、能筛、能收藏，只是缩略图没有。 */
function thumbOf(it, cls) {
  if (!it.servable) {
    const box = document.createElement("div");
    box.className = "gal-nofile";
    box.textContent = "这张在画舫够不着的目录里，只能看参数：\n" + (it.path || it.filename);
    box.title = "缩略图取不到：/api/view 只代理炉子的输出目录";
    return box;
  }
  const img = document.createElement("img");
  img.className = cls;
  img.alt = it.filename;
  img.loading = "lazy";
  blobUrl(it).then((u) => { img.src = u; }, (e) => {
    img.className = cls + " bad";
    img.alt = "缩略图取不到：" + (e.message || "");
    img.title = img.alt;
  });
  return img;
}

function pensText(pens) {
  return (pens || []).filter((p) => p && p.name)
    .map((p) => p.name + "（" + (p.strength === null || p.strength === undefined ? "?" : p.strength) + "）")
    .join("、") || "（无笔）";
}

/* ---- 列表 ---- */
function filters() {
  const f = {
    q: $("f-q").value.trim(), workflow: $("f-workflow").value, unet: $("f-unet").value,
    pen: $("f-pen").value.trim(), tag: $("f-tag").value, rating_min: $("f-rating").value,
    source: $("f-source").value, date_from: $("f-from").value, date_to: $("f-to").value,
    w_min: $("f-wmin").value, w_max: $("f-wmax").value,
    h_min: $("f-hmin").value, h_max: $("f-hmax").value, per_page: $("f-per").value,
  };
  if ($("f-fav").checked) f.fav_only = "1";
  if ($("f-mid").checked) f.show_intermediate = "1";
  const q = new URLSearchParams();
  Object.keys(f).forEach((k) => { if (f[k] !== "" && f[k] !== null && f[k] !== undefined) q.set(k, f[k]); });
  return q;
}

function card(it) {
  const card = document.createElement("div");
  card.className = "gal-card" + (PICKED.has(it.id) ? " picked" : "");
  card.dataset.id = String(it.id);

  const pick = document.createElement("button");
  pick.type = "button";
  pick.className = "gal-pick" + (PICKED.has(it.id) ? " on" : "");
  pick.textContent = PICKED.has(it.id) ? "✓" : "○";
  pick.title = "勾上＝加入对比（2–8 张）";
  pick.onclick = (e) => { e.stopPropagation(); togglePick(it.id); };
  card.appendChild(pick);

  const fav = document.createElement("button");
  fav.type = "button";
  fav.className = "gal-fav" + (it.fav ? " on" : "");
  fav.textContent = it.fav ? "★" : "☆";
  fav.title = FAV_TITLE[it.fav ? 1 : 0];
  fav.onclick = (e) => { e.stopPropagation(); fav.disabled = true; toggleFav(it.id, !it.fav); };
  card.appendChild(fav);

  const view = thumbOf(it, "thumb");
  view.onclick = () => openLb(it.id);
  card.appendChild(view);

  const badges = document.createElement("div");
  badges.className = "gal-badges";
  if (it.rating) {
    const r = document.createElement("span");
    r.className = "gal-badge";
    r.textContent = "★".repeat(it.rating);
    badges.appendChild(r);
  }
  if (it.kind === "intermediate") {
    const m = document.createElement("span");
    m.className = "gal-badge mid";
    m.textContent = "中间图";
    badges.appendChild(m);
  }
  if (!it.servable) {
    const o = document.createElement("span");
    o.className = "gal-badge out";
    o.textContent = "外部目录";
    badges.appendChild(o);
  }
  (it.tags || []).slice(0, 3).forEach((t) => {
    const s = document.createElement("span");
    s.className = "gal-badge";
    s.textContent = t;
    badges.appendChild(s);
  });
  if (badges.children.length) card.appendChild(badges);

  const name = document.createElement("div");
  name.className = "gal-name";
  name.textContent = it.filename;
  name.title = it.subfolder ? it.subfolder + "/" + it.filename : it.filename;
  card.appendChild(name);

  const meta = document.createElement("div");
  meta.className = "gal-meta";
  meta.textContent = [it.workflow_id || "认不出工作流",
    it.width && it.height ? it.width + "×" + it.height : "",
    it.seed === null || it.seed === undefined ? "" : "seed " + it.seed,
    (it.created || "").replace("T", " ")].filter(Boolean).join(" · ");
  card.appendChild(meta);
  return card;
}

function render() {
  const grid = $("grid");
  grid.replaceChildren();
  PAGE.items.forEach((it) => grid.appendChild(card(it)));
  if (!PAGE.items.length) {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = STATS.total
      ? "这个筛选条件下一张也没有；点「清空筛选」看全部。"
      : "索引库还是空的：出过图就会自动登进来，也可以点右上角「回填旧图」把以前的图扫进来。";
    grid.appendChild(p);
  }
  $("page-info").textContent = PAGE.pages
    ? "第 " + PAGE.page + " / " + PAGE.pages + " 页 · 筛出 " + PAGE.total + " 张"
    : "没有图";
  $("prev").disabled = PAGE.page <= 1;
  $("next").disabled = PAGE.page >= PAGE.pages;
  renderStats();
  renderTray();
}

async function load(page) {
  const q = filters();
  if (page) q.set("page", String(page));
  let body;
  try {
    body = await api("/api/gallery?" + q.toString());
  } catch (e) {
    say(e.message, "bad");
    return;
  }
  PAGE = body;
  STATS = body.stats || STATS;
  render();
  if (PICKED.size) say("已挑 " + PICKED.size + " 张（对比要 " + PICK_MIN + "–" + PICK_MAX + " 张）");
}

async function loadTags(keep) {
  let tags = [];
  try {
    tags = (await api("/api/gallery/tags")).tags || [];
  } catch (e) { return; }                 // 标签下拉填不上不影响看图，不值得报一句红字
  const sel = $("f-tag");
  sel.replaceChildren(new Option("全部", ""));
  tags.forEach((t) => sel.appendChild(new Option(t.tag + "（" + t.count + "）", t.tag)));
  if (keep && tags.some((t) => t.tag === keep)) sel.value = keep;
}

/* ---- 收藏／评分／标签：操作即时落库，刷新保持 ---- */
async function patch(id, body) {
  const rec = await api("/api/gallery/" + encodeURIComponent(id), {
    method: "PATCH", body: JSON.stringify(body),
  });
  const i = PAGE.items.findIndex((x) => x.id === rec.id);
  if (i >= 0) PAGE.items[i] = rec;         // 就地换：不重拉列表，免得主人正看着的网格跳一下
  if (CURRENT && CURRENT.id === rec.id) CURRENT = rec;
  return rec;
}

function renderStats() {
  $("stats").textContent = "索引共 " + STATS.total + " 张 · 收藏 " + STATS.fav +
    " 张 · 中间图 " + STATS.intermediate + " 张";
}

function paintFav(rec) {
  const c = $("grid").querySelector('.gal-card[data-id="' + rec.id + '"]');
  if (c) {
    const b = c.querySelector(".gal-fav");
    b.className = "gal-fav" + (rec.fav ? " on" : "");
    b.textContent = rec.fav ? "★" : "☆";
    b.title = FAV_TITLE[rec.fav ? 1 : 0];
    b.disabled = false;
  }
  if (CURRENT && CURRENT.id === rec.id) renderLbFav();
  renderStats();
}

async function toggleFav(id, want) {
  try {
    const rec = await patch(id, { fav: want });
    STATS.fav += rec.fav ? 1 : -1;
    paintFav(rec);
    say(rec.fav ? "已收藏" : "已取消收藏", "ok");
  } catch (e) {
    say(e.message, "bad");
    paintFav({ id, fav: !want });          // 没存上就把界面拨回去，别让星星说谎
  }
}

async function setRating(n) {
  if (!CURRENT) return;
  const want = CURRENT.rating === n ? 0 : n;    // 再点同一颗星＝取消评分（0＝未评，合法值）
  try {
    CURRENT = await patch(CURRENT.id, { rating: want });
    renderStars();
    render();                                   // 角标上的星要跟着变
    say(want ? "评了 " + want + " 星" : "已取消评分", "ok");
  } catch (e) { say(e.message, "bad"); }
}

async function saveTags(tags) {
  if (!CURRENT) return;
  try {
    CURRENT = await patch(CURRENT.id, { tags });
    renderTags();
    render();
    say("标签已存（" + CURRENT.tags.length + " 个）", "ok");
    loadTags($("f-tag").value);
  } catch (e) { say(e.message, "bad"); }
}

function addTag() {
  const raw = $("lb-tag-input").value.trim();
  if (!raw || !CURRENT) return;
  const tags = CURRENT.tags || [];
  if (tags.includes(raw)) { say("这个标签已经在上面了"); return; }
  if (tags.length >= TAG_MAX) { say("一张图最多 " + TAG_MAX + " 个标签，先删几个", "bad"); return; }
  $("lb-tag-input").value = "";
  saveTags(tags.concat([raw]));
}

/* ---- 弹层 ---- */
const PARAM_ROWS = [
  ["工作流", (r) => r.workflow_id], ["底模", (r) => r.unet], ["文本编码器", (r) => r.clip],
  ["VAE", (r) => r.vae], ["笔", (r) => pensText(r.pens)],
  ["尺寸", (r) => (r.width && r.height ? r.width + " × " + r.height : "")],
  ["步数", (r) => r.steps], ["CFG", (r) => r.cfg], ["种子", (r) => r.seed],
  ["采样器", (r) => r.sampler_name], ["调度", (r) => r.scheduler],
  ["重绘幅度", (r) => r.denoise], ["张数", (r) => r.count],
  ["正词", (r) => r.positive], ["负词", (r) => r.negative],
  ["出图时刻", (r) => (r.created || "").replace("T", " ")],
  ["哪来的", (r) => ({ job: "本次出图", history: "炉子历史", disk: "磁盘回填" }[r.source] || r.source)],
  ["文件", (r) => (r.subfolder ? r.subfolder + "/" : "") + r.filename],
  ["在盘上", (r) => r.path],
];

function renderParams(rec) {
  const tb = $("lb-params");
  tb.replaceChildren();
  PARAM_ROWS.forEach(([label, get]) => {
    const v = get(rec);
    if (v === null || v === undefined || v === "") return;   // 认不出的字段不显示，也不填个假的
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    td1.textContent = label;
    const td2 = document.createElement("td");
    td2.textContent = String(v);
    tr.append(td1, td2);
    tb.appendChild(tr);
  });
}

function renderLbFav() {
  const b = $("lb-fav");
  b.textContent = CURRENT.fav ? "★ 已收藏（点一下取消）" : "☆ 收藏";
  b.className = "mini" + (CURRENT.fav ? " on" : "");
}

function renderStars() {
  const box = $("lb-stars");
  box.replaceChildren();
  for (let n = 1; n <= RATING_MAX; n++) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "lb-star" + (n <= CURRENT.rating ? " on" : "");
    b.textContent = "★";
    b.title = n <= CURRENT.rating ? "点一下取消评分" : "评 " + n + " 星";
    b.onclick = () => setRating(n);
    box.appendChild(b);
  }
}

function renderTags() {
  const box = $("lb-tags");
  box.replaceChildren();
  (CURRENT.tags || []).forEach((t) => {
    const chip = document.createElement("span");
    chip.className = "lb-tag";
    chip.append(t);
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.title = "删掉这个标签";
    x.onclick = () => saveTags((CURRENT.tags || []).filter((y) => y !== t));
    chip.appendChild(x);
    box.appendChild(chip);
  });
  if (!(CURRENT.tags || []).length) {
    const s = document.createElement("span");
    s.className = "gal-stats";
    s.textContent = "还没有标签";
    box.appendChild(s);
  }
}

async function openLb(id) {
  let rec;
  try {
    rec = await api("/api/gallery/" + encodeURIComponent(id));
  } catch (e) { say(e.message, "bad"); return; }
  CURRENT = rec;
  $("lightbox").hidden = false;
  $("lb-title").textContent = "参数 · " + rec.filename;
  $("lb-note").textContent = rec.servable
    ? "" : "这张图在画舫够不着的目录里（附加目录只读扫描进来的），所以看不到大图，参数照旧全在。";
  const view = $("lb-view");
  view.replaceChildren();
  if (rec.servable) {
    const img = document.createElement("img");
    img.alt = rec.filename;
    view.appendChild(img);
    blobUrl(rec).then((u) => { img.src = u; }, (e) => {
      view.replaceChildren();
      const p = document.createElement("p");
      p.className = "hint";
      p.textContent = "大图取不到：" + e.message;
      view.appendChild(p);
    });
  } else {
    const p = document.createElement("p");
    p.className = "hint";
    p.textContent = rec.path || rec.filename;
    view.appendChild(p);
  }
  renderLbFav();
  renderStars();
  renderTags();
  renderParams(rec);
  renderPickBtn();
  $("lb-tag-input").value = "";
  $("lb-tag-input").focus();
}

function closeLb() {
  $("lightbox").hidden = true;
  CURRENT = null;
}

/* ---- 多选 → 对比板 ---- */
function togglePick(id) {
  if (PICKED.has(id)) PICKED.delete(id);
  else if (PICKED.size >= PICK_MAX) { say("对比最多 " + PICK_MAX + " 张，先取消几张", "bad"); return; }
  else PICKED.add(id);
  const c = $("grid").querySelector('.gal-card[data-id="' + id + '"]');
  if (c) {
    c.classList.toggle("picked", PICKED.has(id));
    const b = c.querySelector(".gal-pick");
    b.className = "gal-pick" + (PICKED.has(id) ? " on" : "");
    b.textContent = PICKED.has(id) ? "✓" : "○";
  }
  renderTray();
  renderPickBtn();
}

function renderTray() {
  const n = PICKED.size;
  $("tray").hidden = !n;
  $("tray-count").textContent = "已挑 " + n + " 张（对比要 " + PICK_MIN + "–" + PICK_MAX + " 张）";
  $("go-compare").disabled = n < PICK_MIN;
}

function renderPickBtn() {
  const b = $("lb-pick");
  if (!CURRENT) return;
  const on = PICKED.has(CURRENT.id);
  b.textContent = on ? "已在对比选择里（点一下移出）" : "加入对比";
  b.disabled = !on && PICKED.size >= PICK_MAX;
}

function goCompare() {
  const ids = [...PICKED].slice(0, PICK_MAX);
  if (ids.length < PICK_MIN) { say("至少挑 " + PICK_MIN + " 张才能对比", "bad"); return; }
  localStorage.setItem(COMPARE_KEY, JSON.stringify({ mode: "pick", ids }));
  location.href = "/compare";
}

/* ---- 「按此参数回到出图台」：回填界面，不自动出图（卡 §3） ---- */
function refill() {
  if (!CURRENT) return;
  if (!CURRENT.workflow_id) { say("这张图认不出是哪个工作流出的，没法回填参数", "bad"); return; }
  const p = CURRENT;
  const prm = {};
  // 认不出的字段（None）压根不发：发过去会把出图台的输入框清空，那等于替主人做决定
  ["width", "height", "steps", "cfg", "seed", "sampler_name", "scheduler"].forEach((k) => {
    if (p[k] !== null && p[k] !== undefined && p[k] !== "") prm[k] = p[k];
  });
  localStorage.setItem(REFILL_KEY, JSON.stringify({
    profile_id: p.workflow_id, mode: "full", unet: p.unet,
    loras: (p.pens || []).map((x) => ({ name: x.name, strength: x.strength })),
    positive: p.positive || "", negative: p.negative || "", params: prm,
  }));
  location.href = "/";
}

/* ---- 回填（小唯的按钮） ---- */
async function backfill() {
  const btn = $("backfill");
  btn.disabled = true;
  say("回填中…只读扫描，图片文件一个都不动");
  try {
    const rep = await api("/api/gallery/backfill", { method: "POST", body: "{}" });
    const bits = ["新登 " + (rep.added || 0) + " 张", "已在库 " + (rep.already || 0) + " 张",
                  "跳过 " + (rep.skipped || 0) + " 张"];
    say("回填完：" + bits.join("，") + "（图片文件一个字节都没动）", "ok");
    if (rep.rebuilt) {
      // 索引库坏过要交代清楚：坏库挪去哪了、现在用的是哪个文件，绝不悄悄换个新库了事
      say("回填完，但索引库坏过已重建：坏库留在 " + rep.rebuilt.bad +
          (rep.rebuilt.moved ? "" : "（挪不动，原地留着）") + "，现在用 " + rep.rebuilt.now, "bad");
    }
    // reasons 是「原因 → 张数」的字典（后端 _tally 口径），摊成一句句人话再摆上去
    const why = Object.entries(rep.reasons || {});
    await loadTags($("f-tag").value);
    await load(1);
    if (why.length) {
      // 必须等 load(1) 画完再插：render() 会把 #grid 整个 replaceChildren 掉，先插等于白插
      const box = document.createElement("p");
      box.className = "hint";
      box.textContent = "跳过的原因：" + why.slice(0, 4)
        .map(([r, n]) => r + "（" + n + " 张）").join("；") + (why.length > 4 ? " …" : "");
      $("grid").prepend(box);
    }
  } catch (e) {
    say(e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

/* ---- 起步 ---- */
function applyDensity(d) {
  document.body.dataset.density = d === "compact" ? "compact" : "loose";
  $("ui-density").value = document.body.dataset.density;
}

async function saveDensity(d) {
  applyDensity(d);
  try {
    await api("/api/ui", { method: "POST", body: JSON.stringify({ density: d }) });
    say("密度已记住", "ok");
  } catch (e) { applyDensity((BOOT && BOOT.ui || {}).density); say(e.message, "bad"); }
}

async function boot() {
  try {
    BOOT = await api("/api/bootstrap");
  } catch (e) {
    $("gal-state").textContent = "画舫没回话";
    $("gal-state").className = "pill bad";
    say(e.message, "bad");
    return;
  }
  const pill = $("gal-state");
  pill.textContent = BOOT.comfy_alive ? "ComfyUI 在线" : "ComfyUI 未启动（回填只能扫磁盘）";
  pill.className = "pill " + (BOOT.comfy_alive ? "ok" : "bad");
  applyDensity((BOOT.ui || {}).density);

  const wf = $("f-workflow");
  (BOOT.profiles || []).forEach((p) => wf.appendChild(new Option(p.label || p.id, p.id)));
  const un = $("f-unet");
  ((BOOT.models || {}).unet || []).forEach((n) => un.appendChild(new Option(n, n)));

  await loadTags();
  await load(1);
}

function resetFilters() {
  ["f-q", "f-pen", "f-from", "f-to", "f-wmin", "f-wmax", "f-hmin", "f-hmax"].forEach((id) => { $(id).value = ""; });
  ["f-workflow", "f-unet", "f-tag", "f-source"].forEach((id) => { $(id).value = ""; });
  $("f-rating").value = "0";
  $("f-fav").checked = false;
  $("f-mid").checked = false;
  load(1);
}

window.onload = () => {
  $("apply").onclick = () => load(1);
  $("reset").onclick = resetFilters;
  $("prev").onclick = () => load(PAGE.page - 1);
  $("next").onclick = () => load(PAGE.page + 1);
  $("backfill").onclick = backfill;
  $("go-compare").onclick = goCompare;
  $("clear-pick").onclick = () => { PICKED.clear(); load(PAGE.page); };
  $("lb-close").onclick = closeLb;
  $("lb-refill").onclick = refill;
  $("lb-pick").onclick = () => { if (CURRENT) togglePick(CURRENT.id); };
  $("lb-fav").onclick = () => { if (CURRENT) toggleFav(CURRENT.id, !CURRENT.fav); };
  $("lb-tag-add").onclick = addTag;
  $("lb-tag-input").onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); addTag(); }
    if (e.key === "Escape") closeLb();
  };
  $("f-q").onkeydown = (e) => { if (e.key === "Enter") load(1); };
  $("f-pen").onkeydown = (e) => { if (e.key === "Enter") load(1); };
  ["f-workflow", "f-unet", "f-tag", "f-source", "f-rating"].forEach((id) => { $(id).onchange = () => load(1); });
  ["f-fav", "f-mid"].forEach((id) => { $(id).onchange = () => load(1); });
  $("f-per").onchange = () => load(1);
  $("ui-density").onchange = () => saveDensity($("ui-density").value);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeLb(); });
  $("lightbox").onclick = (e) => { if (e.target === $("lightbox") || e.target === $("lb-view")) closeLb(); };
  boot();
};
