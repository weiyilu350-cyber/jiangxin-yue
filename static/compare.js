/* 对比板（C 卡 §4）：模式 A 同 seed 出变体＋拼板导出，模式 B 从图库挑图并排＋同步缩放。
   模式 A **不另开出图路**：POST /api/compare 由后端复用 generate N 次，这里只轮询 /api/job。
   拼板 PNG 在浏览器里用 canvas 拼、走浏览器下载存——画舫没有也不许加图像依赖，
   后端只索引不碰图片字节（卡面禁止栏）。 */
const TOKEN = window.HUAFANG_TOKEN;
const H = { Authorization: "Bearer " + TOKEN };
const $ = (id) => document.getElementById(id);
const COMPARE_KEY = "huafang.compare";
const SAMPLERS = ["euler", "euler_ancestral", "heun", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "uni_pc", "ddim", "res_multistep"];
const SCHEDULERS = ["simple", "karras", "exponential", "sgm_uniform", "beta", "normal"];
const CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八"];
const MIN_N = 2, MAX_N = 8;
// 变体里不许改的三项（后端也拦，前端先不给填的地方）：模式 A 比的就是同 seed、同工作流、同张数下的参数差
const VARIANT_FIELDS = [["steps", "步数", "number", ""], ["cfg", "CFG", "number", "0.1"],
                        ["width", "宽", "number", ""], ["height", "高", "number", ""],
                        ["sampler_name", "采样器", "select", SAMPLERS],
                        ["scheduler", "调度", "select", SCHEDULERS]];
const POLL_MS = 1500;

let BOOT = null;
let BASE = null;                 // 出图台交接过来的基准参数（含笔）；没有就用界面上的默认值
let MODE = "a";
let JOBS = [];                   // 模式 A：[{variant, job_id, status, receipt, images, seed}]
let ITEMS = [];                  // 模式 B：图库记录
const BLOBS = new Map();
const VIEW = { z: 1, x: 0, y: 0 };

function human(body) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  return (d && d.human) || "";
}

function say(msg, kind) {
  const el = $("cmp-message");
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
    p.catch(() => BLOBS.delete(key));
  }
  return BLOBS.get(key);
}

/* ---- 格子 ---- */
function cell(caption) {
  const box = document.createElement("div");
  box.className = "cmp-cell";
  const shot = document.createElement("div");
  shot.className = "cmp-shot";
  const img = document.createElement("img");
  img.alt = caption;
  shot.appendChild(img);
  const cap = document.createElement("div");
  cap.className = "cmp-cap";
  box.append(shot, cap);
  attachZoom(shot, img);
  return { box, shot, img, cap };
}

function fillImage(img, im, onLoaded) {
  blobUrl(im).then((u) => {
    img.src = u;
    img.onload = () => { if (onLoaded) onLoaded(u); };
  }, (e) => {
    img.alt = "图取不到：" + (e.message || "");
    img.title = img.alt;
  });
}

function textCell(text) {
  const box = document.createElement("div");
  box.className = "cmp-cell";
  const p = document.createElement("p");
  p.className = "hint";
  p.textContent = text;
  const cap = document.createElement("div");
  cap.className = "cmp-cap";
  box.append(p, cap);
  return box;
}

function variantLabel(v, i) {
  const bits = [];
  VARIANT_FIELDS.forEach(([key, label]) => {
    if (v && v[key] !== undefined && v[key] !== null && v[key] !== "") bits.push(label + " " + v[key]);
  });
  return "变体" + (CN_NUM[i] || i + 1) + (bits.length ? "：" + bits.join(" · ") : "：与基准同");
}

/* ---- 模式 A ---- */
function baseParams() {
  const num = (id, fallback) => {
    const v = $(id).value;
    return v === "" ? fallback : +v;
  };
  return {
    profile_id: $("a-profile").value,
    unet: $("a-unet").value || undefined,
    loras: (BASE && BASE.loras) || [],
    positive: $("a-positive").value,
    negative: $("a-negative").value,
    width: num("a-width", 832), height: num("a-height", 1216),
    steps: num("a-steps", 30), cfg: num("a-cfg", 4.0),
    seed: $("a-seed").value === "" ? -1 : +$("a-seed").value,
    sampler_name: $("a-sampler").value, scheduler: $("a-scheduler").value,
  };
}

function addVariantRow(values) {
  const rows = $("variants");
  if (rows.children.length >= MAX_N) { say("一次最多 " + MAX_N + " 个变体", "bad"); return; }
  const row = document.createElement("div");
  row.className = "variant-row";
  const tag = document.createElement("span");
  tag.className = "tag";
  row.appendChild(tag);
  VARIANT_FIELDS.forEach(([key, label, kind, opts]) => {
    const lab = document.createElement("label");
    lab.append(label + " ");
    let el;
    if (kind === "select") {
      el = document.createElement("select");
      el.appendChild(new Option("跟基准", ""));
      opts.forEach((o) => el.appendChild(new Option(o, o)));
    } else {
      el = document.createElement("input");
      el.type = "number";
      el.placeholder = "跟基准";
      if (opts) el.step = opts;
    }
    el.dataset.key = key;
    if (values && values[key] !== undefined && values[key] !== null) el.value = values[key];
    lab.appendChild(el);
    row.appendChild(lab);
  });
  rows.appendChild(row);
  renumber();
}

function renumber() {
  [...$("variants").children].forEach((r, i) => {
    r.querySelector(".tag").textContent = "变体" + (CN_NUM[i] || i + 1);
  });
  $("variant-count").textContent = $("variants").children.length + " 个变体（对比要 " +
    MIN_N + "–" + MAX_N + " 个）";
  $("run-a").disabled = $("variants").children.length < MIN_N;
  $("drop-variant").disabled = $("variants").children.length <= 1;
}

function readVariants() {
  return [...$("variants").children].map((row) => {
    const v = {};
    [...row.querySelectorAll("[data-key]")].forEach((el) => {
      if (el.value === "" || el.value === null) return;      // 留空＝跟基准，压根不发这个键
      v[el.dataset.key] = el.type === "number" ? +el.value : el.value;
    });
    return v;
  });
}

async function runA() {
  const variants = readVariants();
  if (variants.length < MIN_N || variants.length > MAX_N) {
    say("一次比 " + MIN_N + "–" + MAX_N + " 个变体，现在给了 " + variants.length + " 个", "bad");
    return;
  }
  const btn = $("run-a");
  btn.disabled = true;
  say("提交中…");
  let body;
  try {
    body = await api("/api/compare", {
      method: "POST",
      body: JSON.stringify({ mode: "variants", base: baseParams(), variants }),
    });
  } catch (e) {
    say(e.message, "bad");
    btn.disabled = false;
    return;
  }
  $("seed-show").hidden = false;
  $("seed-show").className = "pill ok";
  $("seed-show").textContent = "共用 seed " + body.seed;
  $("a-seed").value = body.seed;                 // 回填真 seed，方便主人自己复现
  JOBS = body.jobs.map((j) => ({ ...j, receipt: null, images: [], status: j.status || "running" }));
  say("已提交 " + JOBS.length + " 个变体，同一个 seed：" + body.seed, "ok");
  renderBoardA();
  pollA(btn);
}

async function pollA(btn) {
  const terminal = (s) => s === "done" || s === "error";
  for (let round = 0; round < 400; round++) {
    const waiting = JOBS.filter((j) => !terminal(j.status));
    if (!waiting.length) break;
    await new Promise((r) => setTimeout(r, POLL_MS));
    await Promise.all(waiting.map(async (j) => {
      let d;
      try {
        d = await api("/api/job/" + encodeURIComponent(j.job_id));
      } catch (e) { return; }               // 轮询中途抖一下不整场失败，下一轮再问
      j.status = d.status;
      j.receipt = d.receipt;
      j.images = d.images || [];
      j.error = d.error;
      j.positive_final = d.positive_final;
    }));
    renderBoardA();
  }
  if (btn) btn.disabled = false;
  const bad = JOBS.filter((j) => j.status !== "done");
  const wrong = JOBS.filter((j) => (j.receipt || []).some((it) => !it.ok));
  if (bad.length) say(bad.length + " 个变体没出完：" + (bad[0].error && bad[0].error.human || bad[0].status), "bad");
  else if (wrong.length) say("出完了，但有 " + wrong.length + " 条回执对不上（看下面的 ✗）", "bad");
  else say("全部出完，" + JOBS.length + " 条回执逐项 ✓，seed 全同", "ok");
  $("export-row").hidden = !JOBS.every((j) => j.status === "done" && j.images.length);
}

function renderBoardA() {
  const board = $("board");
  board.replaceChildren();
  $("board-title").textContent = "对比 · 模式 A（同 seed 出变体）";
  if (!JOBS.length) {
    board.appendChild(textCell("还没有变体：上面填两行不同的参数，点「同 seed 出这几个变体」。"));
    renderReceiptsA();
    return;
  }
  JOBS.forEach((j, i) => {
    const label = variantLabel(j.variant, i);
    const c = cell(label);
    c.box.dataset.jobId = j.job_id;
    const seedRow = (j.receipt || []).find((it) => it.key === "seed");
    c.cap.replaceChildren();
    const b = document.createElement("b");
    b.textContent = label;
    c.cap.appendChild(b);
    c.cap.append(" · " + (j.status === "done" ? "完成 ✓" : j.status === "error"
      ? "失败：" + ((j.error && j.error.human) || "未知原因")
      : j.status === "running" ? "出图中…" : j.status));
    if (seedRow) c.cap.append(" · seed " + seedRow.actual);
    const im = (j.images || [])[0];
    if (im) fillImage(c.img, im, () => { if (allDone()) enableExport(); });
    else if (j.status === "error") c.img.alt = "没出图";
    else c.img.alt = "还在出图…";
    board.appendChild(c.box);
  });
  renderReceiptsA();
}

function allDone() {
  return JOBS.length && JOBS.every((j) => j.status === "done" && (j.images || []).length);
}

function renderReceiptsA() {
  const thead = $("receipts").tHead.rows[0];
  thead.replaceChildren(...["变体", "项", "界面要的", "炉子收到的", ""].map((t) => {
    const th = document.createElement("th");
    th.textContent = t;
    return th;
  }));
  const tb = $("receipts").tBodies[0];
  tb.replaceChildren();
  JOBS.forEach((j, i) => {
    const label = "变体" + (CN_NUM[i] || i + 1);
    (j.receipt || []).forEach((it) => {
      const tr = document.createElement("tr");
      const cells = [label, it.key, JSON.stringify(it.requested), JSON.stringify(it.actual),
                     it.ok ? "✓" : "✗"];
      cells.forEach((txt, k) => {
        const td = document.createElement("td");
        td.textContent = txt;
        if (k === 4) td.className = it.ok ? "ok" : "bad";
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    if (!j.receipt) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.textContent = label + "：" + (j.status === "error"
        ? ((j.error && j.error.human) || "出图失败") : "还没跑完，回执稍后自己出现");
      tr.appendChild(td);
      tb.appendChild(tr);
    }
  });
}

/* ---- 拼板导出（浏览器里拼，走浏览器下载） ---- */
function enableExport() {
  $("export-row").hidden = false;
}

async function exportMontage() {
  const done = JOBS.filter((j) => j.status === "done" && (j.images || []).length);
  if (done.length < MIN_N) { say("至少要有 " + MIN_N + " 张出完的图才能拼板", "bad"); return; }
  const board = $("board");
  const loaded = await Promise.all(done.map((j) => {
    const el = board.querySelector('.cmp-cell[data-job-id="' + j.job_id + '"] img');
    if (!el || !el.src) return null;
    return new Promise((res) => {
      const img = new Image();
      img.onload = () => res({ job: j, img });
      img.onerror = () => res(null);
      img.src = el.src;
    });
  }));
  const cells = loaded.filter(Boolean);
  if (cells.length < MIN_N) { say("图还没加载完，等一下再导出", "bad"); return; }
  const cols = Math.ceil(Math.sqrt(cells.length));
  const rows = Math.ceil(cells.length / cols);
  const cw = Math.max(...cells.map((c) => c.img.naturalWidth));
  const ch = Math.max(...cells.map((c) => c.img.naturalHeight));
  const cap = 34;
  const cv = document.createElement("canvas");
  cv.width = cw * cols;
  cv.height = (ch + cap) * rows;
  const ctx = cv.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, cv.width, cv.height);
  ctx.font = "16px 'Microsoft YaHei', sans-serif";
  ctx.textBaseline = "middle";
  cells.forEach((c, i) => {
    const x = (i % cols) * cw, y = Math.floor(i / cols) * (ch + cap);
    const s = Math.min(cw / c.img.naturalWidth, ch / c.img.naturalHeight);
    const w = c.img.naturalWidth * s, h = c.img.naturalHeight * s;
    ctx.drawImage(c.img, x + (cw - w) / 2, y + (ch - h) / 2, w, h);
    const seedRow = (c.job.receipt || []).find((it) => it.key === "seed");
    ctx.fillStyle = "#22324a";
    ctx.fillText(variantLabel(c.job.variant, JOBS.indexOf(c.job)) + " · seed " +
      (seedRow ? seedRow.actual : "?"), x + 8, y + ch + cap / 2);
  });
  cv.toBlob((blob) => {
    if (!blob) { say("拼板导出失败（浏览器不给 canvas 转 PNG）", "bad"); return; }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const seedRow = (cells[0].job.receipt || []).find((it) => it.key === "seed");
    a.download = "对比拼板_seed" + (seedRow ? seedRow.actual : "") + ".png";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    $("export-message").textContent = "拼板 " + cv.width + "×" + cv.height + "，" + cells.length +
      " 格，已交给浏览器下载（画舫不往你的目录里写文件）";
    say("拼板已导出", "ok");
  }, "image/png");
}

/* ---- 模式 B ---- */
async function loadPick(ids) {
  let body;
  try {
    body = await api("/api/compare", { method: "POST", body: JSON.stringify({ mode: "pick", ids }) });
  } catch (e) {
    say(e.message, "bad");
    $("board").replaceChildren(textCell("挑图没成：" + e.message));
    return;
  }
  ITEMS = body.items || [];
  renderBoardB();
  say("并排看 " + ITEMS.length + " 张（滚轮缩放、按住拖动；同步缩放开着时几张一起动）", "ok");
}

function renderBoardB() {
  const board = $("board");
  board.replaceChildren();
  $("board-title").textContent = "对比 · 模式 B（从图库挑图）";
  $("export-row").hidden = true;
  if (!ITEMS.length) {
    board.appendChild(textCell("还没有挑图：去图库勾 2–8 张，点「对比这几张」。"));
    renderTableB([]);
    return;
  }
  ITEMS.forEach((it, i) => {
    const label = (i + 1) + " · " + it.filename;
    if (!it.servable) {
      const box = textCell("这张在画舫够不着的目录里，看不到图：\n" + (it.path || it.filename));
      box.querySelector(".cmp-cap").textContent = label;
      board.appendChild(box);
      return;
    }
    const c = cell(label);
    c.box.dataset.imageId = String(it.id);
    fillImage(c.img, it);
    c.img.alt = label;
    c.cap.replaceChildren();
    const b = document.createElement("b");
    b.textContent = label;
    c.cap.appendChild(b);
    c.cap.append(" · " + [it.workflow_id || "认不出工作流",
      it.width && it.height ? it.width + "×" + it.height : "",
      it.seed === null || it.seed === undefined ? "" : "seed " + it.seed,
      it.steps ? "步数 " + it.steps : "", it.cfg ? "CFG " + it.cfg : "",
      it.fav ? "★ 收藏" : "", it.rating ? "评 " + it.rating + " 星" : ""]
      .filter(Boolean).join(" · "));
    board.appendChild(c.box);
  });
  renderTableB(ITEMS);
  VIEW.z = 1; VIEW.x = 0; VIEW.y = 0;
  applyView();
}

const B_ROWS = [
  ["工作流", (r) => r.workflow_id], ["底模", (r) => r.unet], ["笔", (r) => (r.pens || []).filter((p) => p && p.name)
    .map((p) => p.name + "（" + p.strength + "）").join("、")],
  ["尺寸", (r) => (r.width && r.height ? r.width + "×" + r.height : "")],
  ["步数", (r) => r.steps], ["CFG", (r) => r.cfg], ["种子", (r) => r.seed],
  ["采样器", (r) => r.sampler_name], ["调度", (r) => r.scheduler],
  ["正词", (r) => r.positive], ["负词", (r) => r.negative],
  ["出图时刻", (r) => (r.created || "").replace("T", " ")],
  ["收藏", (r) => (r.fav ? "★" : "—")], ["评分", (r) => (r.rating ? "★".repeat(r.rating) : "—")],
  ["标签", (r) => (r.tags || []).join("、")],
];

function renderTableB(items) {
  const thead = $("receipts").tHead.rows[0];
  thead.replaceChildren(...["项"].concat(items.map((_, i) => "图 " + (i + 1))).map((t) => {
    const th = document.createElement("th");
    th.textContent = t;
    return th;
  }));
  const tb = $("receipts").tBodies[0];
  tb.replaceChildren();
  if (!items.length) return;
  B_ROWS.forEach(([label, get]) => {
    const vals = items.map((it) => {
      const v = get(it);
      return v === null || v === undefined || v === "" ? "—" : String(v);
    });
    const tr = document.createElement("tr");
    if (new Set(vals).size > 1) tr.className = "diff";     // 不一样的那几项才是要看的，标出来
    const td0 = document.createElement("td");
    td0.textContent = label;
    tr.appendChild(td0);
    vals.forEach((v) => {
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
}

/* ---- 同步缩放／平移 ---- */
function viewOf(img) {
  if ($("sync-zoom").checked) return VIEW;
  if (!img.dataset.view) img.dataset.view = JSON.stringify({ z: 1, x: 0, y: 0 });
  return JSON.parse(img.dataset.view);
}

function setView(img, v) {
  if ($("sync-zoom").checked) { VIEW.z = v.z; VIEW.x = v.x; VIEW.y = v.y; applyView(); return; }
  img.dataset.view = JSON.stringify(v);
  paint(img, v);
}

function paint(img, v) {
  img.style.transform = "translate(" + v.x + "px," + v.y + "px) scale(" + v.z + ")";
}

function applyView() {
  $("board").querySelectorAll(".cmp-shot img").forEach((img) => {
    img.dataset.view = JSON.stringify({ ...VIEW });
    paint(img, VIEW);
  });
}

function bump(img, factor) {
  const v = viewOf(img);
  v.z = Math.min(12, Math.max(0.2, v.z * factor));
  setView(img, v);
}

function attachZoom(shot, img) {
  shot.addEventListener("wheel", (e) => {
    e.preventDefault();
    bump(img, e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
  let drag = null;
  shot.addEventListener("pointerdown", (e) => {
    drag = { x: e.clientX, y: e.clientY, v: { ...viewOf(img) } };
    shot.setPointerCapture(e.pointerId);
    shot.style.cursor = "grabbing";
  });
  shot.addEventListener("pointermove", (e) => {
    if (!drag) return;
    setView(img, { z: drag.v.z, x: drag.v.x + (e.clientX - drag.x), y: drag.v.y + (e.clientY - drag.y) });
  });
  const end = (e) => {
    drag = null;
    shot.style.cursor = "grab";
    if (e.pointerId !== undefined && shot.hasPointerCapture(e.pointerId)) shot.releasePointerCapture(e.pointerId);
  };
  shot.addEventListener("pointerup", end);
  shot.addEventListener("pointercancel", end);
}

function zoomAll(factor) {
  VIEW.z = Math.min(12, Math.max(0.2, VIEW.z * factor));
  applyView();
}

function resetZoom() {
  VIEW.z = 1; VIEW.x = 0; VIEW.y = 0;
  $("board").querySelectorAll(".cmp-shot img").forEach((img) => { delete img.dataset.view; paint(img, VIEW); });
}

/* ---- 起步 ---- */
function setMode(m) {
  MODE = m;
  $("pane-a").hidden = m !== "a";
  $("pane-b").hidden = m !== "b";
  $("tab-a").className = "mini" + (m === "a" ? " on" : "");
  $("tab-b").className = "mini" + (m === "b" ? " on" : "");
  if (m === "a") renderBoardA();
  else renderBoardB();
}

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

function fillBase(b) {
  if (!b) return;
  if (b.profile_id) $("a-profile").value = b.profile_id;
  if (b.unet) $("a-unet").value = b.unet;
  $("a-positive").value = b.positive || "";
  $("a-negative").value = b.negative || "";
  const prm = b.params || b;
  ["width", "height", "steps", "cfg", "seed"].forEach((k) => {
    if (prm[k] !== undefined && prm[k] !== null && $("a-" + k)) $("a-" + k).value = prm[k];
  });
  if (prm.sampler_name) $("a-sampler").value = prm.sampler_name;
  if (prm.scheduler) $("a-scheduler").value = prm.scheduler;
  const pens = (b.loras || []).filter((p) => p && p.name);
  say(pens.length
    ? "基准参数已从出图台带过来，笔：" + pens.map((p) => p.name + "（" + p.strength + "）").join("、")
    : "基准参数已从出图台带过来（没带笔）", "ok");
}

async function boot() {
  let handoff = null;
  try {
    handoff = JSON.parse(localStorage.getItem(COMPARE_KEY) || "null");
    localStorage.removeItem(COMPARE_KEY);          // 用掉就擦：刷新页面不该又跑一遍
  } catch (e) { handoff = null; }
  try {
    BOOT = await api("/api/bootstrap");
  } catch (e) {
    $("cmp-state").textContent = "画舫没回话";
    $("cmp-state").className = "pill bad";
    say(e.message, "bad");
    return;
  }
  const pill = $("cmp-state");
  pill.textContent = BOOT.comfy_alive ? "ComfyUI 在线" : "ComfyUI 未启动";
  pill.className = "pill " + (BOOT.comfy_alive ? "ok" : "bad");
  applyDensity((BOOT.ui || {}).density);

  const ps = $("a-profile");
  (BOOT.profiles || []).forEach((p) => ps.appendChild(new Option(p.label || p.id, p.id)));
  if ((BOOT.settings || {}).default_profile) ps.value = BOOT.settings.default_profile;
  const un = $("a-unet");
  ((BOOT.models || {}).unet || []).forEach((n) => un.appendChild(new Option(n, n)));
  if ((BOOT.settings || {}).default_unet) un.value = BOOT.settings.default_unet;
  SAMPLERS.forEach((s) => $("a-sampler").appendChild(new Option(s, s)));
  SCHEDULERS.forEach((s) => $("a-scheduler").appendChild(new Option(s, s)));

  if (handoff && handoff.mode === "variants" && handoff.base) {
    BASE = handoff.base;
    fillBase(BASE);
    addVariantRow({ steps: 20 });      // 带过来了就顺手摆两行，主人不必先点两次「加一个变体」
    addVariantRow({ steps: 40 });
    setMode("a");
    return;
  }
  addVariantRow({ steps: 20 });
  addVariantRow({ steps: 40 });
  renumber();
  if (handoff && handoff.mode === "pick" && Array.isArray(handoff.ids)) {
    setMode("b");
    await loadPick(handoff.ids);
    return;
  }
  setMode("a");
  $("board").replaceChildren(textCell("还没有可比的东西：模式 A 填两个变体点「同 seed 出这几个变体」，或去图库勾几张图。"));
}

window.onload = () => {
  $("tab-a").onclick = () => setMode("a");
  $("tab-b").onclick = () => setMode("b");
  $("add-variant").onclick = () => { addVariantRow(); renumber(); };
  $("drop-variant").onclick = () => {
    const rows = $("variants");
    if (rows.children.length > 1) rows.lastElementChild.remove();
    renumber();
  };
  $("run-a").onclick = runA;
  $("export").onclick = exportMontage;
  $("layout-side").onclick = () => { $("board").className = "cmp-grid side"; };
  $("layout-tiles").onclick = () => { $("board").className = "cmp-grid tiles"; };
  $("sync-zoom").onchange = () => { if ($("sync-zoom").checked) applyView(); };
  $("zoom-in").onclick = () => zoomAll(1.25);
  $("zoom-out").onclick = () => zoomAll(1 / 1.25);
  $("zoom-reset").onclick = resetZoom;
  $("ui-density").onchange = () => saveDensity($("ui-density").value);
  boot();
};
