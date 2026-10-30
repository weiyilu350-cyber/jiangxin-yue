/* 画舫前端：只认角色/槽位，不碰节点 ID。 */
const TOKEN = window.HUAFANG_TOKEN;
const H = { Authorization: "Bearer " + TOKEN };
const $ = (id) => document.getElementById(id);
const SAMPLERS = ["euler", "euler_ancestral", "heun", "dpmpp_2m", "dpmpp_2m_sde", "dpmpp_3m_sde", "uni_pc", "ddim", "res_multistep"];
const SCHEDULERS = ["simple", "karras", "exponential", "sgm_uniform", "beta", "normal"];
const PARAM_FIELDS = ["width", "height", "steps", "cfg", "seed", "sampler_name", "scheduler"];
// 张数（T009）是「界面当次值」，故意不进 PARAM_FIELDS：存/载配方都不带它（D136 裁定）
const COUNT_MIN = 1;
const COUNT_MAX = 8;
// C 卡跨页交接：只放参数，token 一个字不放（D135）；用掉就擦，刷新页面不会又跑一遍
const COMPARE_KEY = "huafang.compare";
const REFILL_KEY = "huafang.refill";
let BOOT = null;
let PRESETS = [];
let IDLE_FREE = { auto_free: false, idle_minutes: 15 };

function clampCount(v) {
  const n = Math.trunc(Number(v));
  return Number.isFinite(n) ? Math.min(COUNT_MAX, Math.max(COUNT_MIN, n)) : COUNT_MIN;
}

async function boot() {
  const r = await fetch("/api/bootstrap", { headers: H });
  BOOT = await r.json();
  const pill = $("comfy-state");
  pill.textContent = BOOT.comfy_alive ? "ComfyUI 在线" : "ComfyUI 未启动";
  pill.className = "pill " + (BOOT.comfy_alive ? "ok" : "bad");
  syncFurnaceUi(BOOT.comfy_alive);                            // T011：在线就把「起炉子」置灰
  $("comfy-auto").checked = !!(BOOT.comfy || {}).auto_start;   // T011：开关照盘上的值复原（后端已兜坏值）
  IDLE_FREE = { ...(BOOT.idle_free || { auto_free: false, idle_minutes: 15 }) };
  fillIdleFree();

  const ps = $("profile");
  ps.innerHTML = "";
  BOOT.profiles.forEach((p) => {
    ps.appendChild(new Option(p.label + (p.errors.length ? "（契约失败！）" : ""), p.id));
  });
  if (BOOT.settings.default_profile) ps.value = BOOT.settings.default_profile;

  const un = $("unet");
  un.innerHTML = "";
  BOOT.models.unet.forEach((n) => un.appendChild(new Option(n, n)));
  if (BOOT.models.unet.includes(BOOT.settings.default_unet)) un.value = BOOT.settings.default_unet;

  renderProfile();
  await loadPresets();
  $("output-dir").value = (BOOT.output || {}).dir || "";   // T010：把已存的成品目录填回文本框
  await applyUiBoot();                                     // T007：背景／密度照设置复原（坏值后端已兜默认）
}

function renderLoras() {
  const p = BOOT.profiles.find((x) => x.id === $("profile").value);
  const box = $("loras");
  box.innerHTML = "";
  for (let i = 0; i < p.lora_slots; i++) {
    const row = document.createElement("div");
    row.className = "row";
    const sel = document.createElement("select");
    sel.id = "lora" + i;
    sel.appendChild(new Option("（空槽）", ""));
    BOOT.models.lora.forEach((n) => sel.appendChild(new Option(n, n)));
    const st = document.createElement("input");
    st.type = "number"; st.step = "0.05"; st.min = "0"; st.max = "2";
    st.value = "0.65"; st.id = "lorastr" + i; st.className = "narrow";
    row.append("笔 " + (i + 1) + " ", sel, st);
    // 笔槽标所属段（B 卡 §4）：单段工作流不挂徽章，界面与 A 阶段一模一样
    const cov = (p.lora_stages || [])[i] || [];
    if ((p.stages || []).length > 1) {
      const tag = document.createElement("span");
      tag.className = "pill stage-tag";
      tag.textContent = cov.length === p.stages.length ? "全部段"
        : cov.map((sid) => stageNo(p.stages.findIndex((x) => x.id === sid))).join("、");
      tag.title = "这支笔只在标出的段里生效";
      row.append(" ", tag);
    }
    box.appendChild(row);
  }
  $("steps").value = p.defaults.steps;
  $("cfg").value = p.defaults.cfg;
  $("width").value = p.defaults.width;
  $("height").value = p.defaults.height;
  $("sampler_name").value = p.defaults.sampler_name;
  $("scheduler").value = p.defaults.scheduler;
  syncSizePreset();   // 换工作流会带出新的宽高默认值，下拉得跟着走
}

function currentProfile() {
  return BOOT.profiles.find((x) => x.id === $("profile").value);
}

/* ---- B 卡 §4 段卡：只画段2+（上面那排全局框＝段1 卡，主人裁定 2026-10-28 不许动） ---- */
const CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"];
// 段卡上画什么控件。不在这张表里的参数＝界面上不出现，照工作流原值走（导入器的词表就这几个）
const STAGE_FIELDS = {
  seed: { label: "种子", type: "number" },
  steps: { label: "步数", type: "number", min: 1 },
  cfg: { label: "CFG", type: "number", step: 0.1 },
  sampler_name: { label: "采样器", type: "select", options: SAMPLERS },
  scheduler: { label: "调度", type: "select", options: SCHEDULERS },
  denoise: { label: "重绘幅度", type: "number", step: 0.05, min: 0, max: 1 },
};
// 「同步到全部」只碰这几项（裁定第2款）：尺寸归全局那排、种子与重绘幅度是每段自己的手感
const SYNC_FIELDS = ["steps", "cfg", "sampler_name", "scheduler"];

function stageNo(i) {
  return "第" + (CN_NUM[i] || i + 1) + "段";
}

function stageTitle(i, label) {
  const no = stageNo(i);
  // 导入器给的 label 本来就带段号（「第二段·精修放大」），段卡自己会报段号，别念两遍
  const rest = label.startsWith(no) ? label.slice(no.length).replace(/^[·\s]+/, "") : label;
  return rest ? no + " · " + rest : no;
}

function stageFieldId(stageId, key) {
  return "sg-" + stageId + "-" + key;
}

function renderStageCards() {
  const p = currentProfile();
  const box = $("stage-cards");
  box.replaceChildren();
  const stages = p.stages || [];
  if (stages.length < 2) return;   // 没有段2＝一个卡片都不画，单段工作流界面与 A 阶段逐字相同

  const bar = document.createElement("div");
  bar.className = "row stage-bar";
  const sync = document.createElement("button");
  sync.type = "button"; sync.id = "sync-stages"; sync.className = "mini";
  sync.textContent = "同步到全部";
  sync.title = "把第一段的步数／CFG／采样器／调度刷进每一段；尺寸、种子、重绘幅度不动";
  sync.onclick = syncStages;
  const note = document.createElement("span");
  note.className = "hint";
  note.textContent = "宽／高＝最终成品尺寸；每段该多大由工作流自己按比例算，所以段卡上不放宽高。";
  const msg = document.createElement("span");
  msg.id = "stage-msg";
  msg.className = "stage-msg";
  msg.setAttribute("role", "status");
  bar.append(sync, note, msg);
  box.appendChild(bar);

  stages.slice(1).forEach((st, k) => {
    const card = document.createElement("div");
    card.className = "stage-card";
    const h = document.createElement("h3");
    h.textContent = stageTitle(k + 1, st.label);
    card.appendChild(h);
    if (!(st.pen_roles || []).length) {
      const w = document.createElement("div");
      w.className = "warn";
      w.textContent = "此段无笔：上面选的笔不对这一段生效";
      card.appendChild(w);
    }
    const row = document.createElement("div");
    row.className = "row params";
    (st.params || []).forEach((key) => {
      const spec = STAGE_FIELDS[key];
      if (!spec) return;
      const want = (st.defaults || {})[key];
      const lab = document.createElement("label");
      lab.textContent = spec.label + " ";
      let el;
      if (spec.type === "select") {
        el = document.createElement("select");
        const opts = spec.options.slice();
        // 工作流里的值不在清单上也得保住：悄悄换成第一项＝界面所见与炉子所用岔开
        if (want !== undefined && !opts.includes(want)) opts.push(want);
        opts.forEach((v) => el.appendChild(new Option(v, v)));
        if (want !== undefined) el.value = want;
      } else {
        el = document.createElement("input");
        el.type = "number";
        if (spec.step !== undefined) el.step = spec.step;
        if (spec.min !== undefined) el.min = spec.min;
        if (spec.max !== undefined) el.max = spec.max;
        if (want !== undefined) el.value = want;
      }
      el.id = stageFieldId(st.id, key);
      lab.appendChild(el);
      row.appendChild(lab);
    });
    card.appendChild(row);
    box.appendChild(card);
  });
}

function syncStages() {
  const p = currentProfile();
  let hit = 0;
  (p.stages || []).slice(1).forEach((st) => {
    SYNC_FIELDS.forEach((key) => {
      const to = $(stageFieldId(st.id, key));
      const from = $(key);   // 全局那排＝第一段的值
      if (!to || !from) return;
      to.value = from.value;
      hit++;
    });
  });
  const msg = $("stage-msg");
  msg.textContent = hit
    ? "已把第一段的值刷进各段（共 " + hit + " 项）；尺寸、种子、重绘幅度没动"
    : "这几段没有可同步的参数";
}

function renderProfileWarn() {
  const el = $("profile-warn");
  const ws = currentProfile().warnings || [];
  el.textContent = ws.join("；");
  el.hidden = !ws.length;   // D76：warning 不拦出图，但必须让主人看见
}

function renderProfile() {
  renderLoras();
  renderStageCards();
  renderProfileWarn();
}

/* 段2+ 的逐段参数只在出图时拼：collectUI() 一行不动，所以存／载配方的口径与 A 阶段完全一致
   （配方不带段参数——B 卡的已知限制，交差里写明）。单段工作流不发这个键，请求体逐字不变。 */
function collectStageParams() {
  const stages = (currentProfile().stages || []).slice(1);
  const out = {};
  stages.forEach((st) => {
    const one = {};
    (st.params || []).forEach((key) => {
      const el = $(stageFieldId(st.id, key));
      if (!el) return;
      one[key] = el.tagName === "SELECT" ? el.value : +el.value;
    });
    if (Object.keys(one).length) out[st.id] = one;
  });
  return Object.keys(out).length ? out : null;
}

/* ---- T008 宽高快捷预设（沿用早期版本的操作习惯，D136 之一） ---- */
// 清单逐字沿用早期版本的 SIZE_PRESETS；要加改尺寸就动这里，不做配置文件（D96：看得见的下拉比配置页好用）
const SIZE_PRESETS = [
  ["竖 768×1024", 768, 1024],
  ["竖 832×1216", 832, 1216],
  ["竖 1024×1536", 1024, 1536],
  ["横 1024×576", 1024, 576],
  ["横 1280×720", 1280, 720],
  ["横 1920×1080", 1920, 1080],
  ["方 1024×1024", 1024, 1024],
  ["小 512×512", 512, 512],
];
const SIZE_CUSTOM = "自定义";
let SIZE_LOCK = false;   // 自己填值时别回写下拉（早期版本 _size_lock 同款，免得填宽的瞬间闪一下「自定义」）

function sizeLabel(w, h) {
  const hit = SIZE_PRESETS.find(([, pw, ph]) => pw === w && ph === h);
  return hit ? hit[0] : SIZE_CUSTOM;   // 大尺寸照填不拦：清单外的数字一律算自定义，前端不做「这个不能选」的阉割
}

function syncSizePreset() {
  $("size-preset").value = sizeLabel(+$("width").value, +$("height").value);
}

function fillSize(id, n) {
  const el = $(id);
  el.value = n;
  el.dispatchEvent(new Event("input", { bubbles: true }));   // 沿用现有监听口径，回执链路不动（卡 §2）
}

function onSizeInput() {
  if (!SIZE_LOCK) syncSizePreset();
}

function applySizePreset() {
  const sel = $("size-preset");
  if (sel.value === SIZE_CUSTOM) {
    $("width").focus();   // 早期版本 on_size_preset 同款：选「自定义」＝把光标送到宽度框等主人自己填
    return;
  }
  const hit = SIZE_PRESETS.find(([label]) => label === sel.value);
  if (!hit) return;
  SIZE_LOCK = true;
  fillSize("width", hit[1]);
  fillSize("height", hit[2]);
  SIZE_LOCK = false;
  syncSizePreset();
}

function collectUI() {
  const p = currentProfile();
  const loras = [];
  for (let i = 0; i < p.lora_slots; i++) {
    const name = $("lora" + i).value;
    const strength = parseFloat($("lorastr" + i).value || "0");
    loras.push(name ? { name, strength } : null);
  }
  const params = {};
  PARAM_FIELDS.forEach((k) => { params[k] = +$(k).value; });
  params.sampler_name = $("sampler_name").value;
  params.scheduler = $("scheduler").value;
  return {
    unet: $("unet").value,
    loras,
    positive: $("positive").value,
    negative: $("negative").value,
    params,
  };
}

function sayPreset(msg, bad) {
  const el = $("preset-warn");
  el.textContent = msg || "";
  el.hidden = !msg;
  el.className = "warn" + (msg ? (bad ? " bad" : " ok") : "");
}

async function loadPresets(keepId) {
  const pid = $("profile").value;
  if (!pid) return;
  const url = "/api/presets?profile_id=" + encodeURIComponent(pid) +
    ($("show-all").checked ? "&all=1" : "");
  const r = await fetch(url, { headers: H });
  if (!r.ok) {
    sayPreset("预设列表取不到（" + r.status + "）", true);
    return;
  }
  PRESETS = (await r.json()).presets;
  const sel = $("preset");
  sel.innerHTML = "";
  PRESETS.forEach((p) => {
    const o = new Option(p.name + (p.usable ? "" : "（不兼容）"), p.id);
    if (!p.usable) {
      o.className = "unusable";
      o.title = p.warnings.join("；");
    }
    sel.appendChild(o);
  });
  const want = keepId || (PRESETS[0] && PRESETS[0].id);
  if (want && PRESETS.some((p) => p.id === want)) sel.value = want;
  presetChanged();
}

function presetChanged() {
  const p = PRESETS.find((x) => x.id === $("preset").value);
  $("delete-preset").disabled = !p || p.builtin;
  $("delete-preset").title = p && p.builtin ? "内置的默认配方不能删" : "删除所选预设前会再次确认";
  if (p && !p.usable) sayPreset(p.warnings.join("；") + "（要整套载入，先换个笔槽够多的工作流）", true);
  else sayPreset("");
}

async function deletePreset() {
  const p = PRESETS.find((x) => x.id === $("preset").value);
  if (!p) {
    sayPreset("先选一支配方", true);
    return;
  }
  if (p.builtin) {
    sayPreset("内置的「默认」配方不能删", true);
    return;
  }
  // 二次确认：删配方会连带删掉它在各工作流下存的差异，不能一键就没
  const sure = "删除配方「" + p.name + "」？\n它在各工作流下存的差异也会一起删掉，这一步不能撤销。";
  if (!window.confirm(sure)) return;
  try {
    const r = await fetch("/api/presets/" + encodeURIComponent(p.id),
                          { method: "DELETE", headers: H });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(human(body) || "删除失败（" + r.status + "）");
    await loadPresets(); // 列表立刻刷新，不留已删的那一支（雷区 7）
    sayPreset("已删除「" + p.name + "」");
  } catch (e) {
    sayPreset(e.message, true);
  }
}

/* ---- T003 模型中心：只读扫描 ＋ 触发词人工覆盖 ---- */
const MODEL_ROLES = [["lora", "笔"], ["unet", "底模"], ["clip", "文本编码器（TE）"], ["vae", "VAE"]];

function human(body) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  return (d && d.human) || "";
}

function sayModel(msg, bad) {
  const el = $("models-message");
  el.textContent = msg || "";
  el.className = "model-msg" + (msg ? (bad ? " bad" : " ok") : "");
}

function mb(bytes) {
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function sourceOf(t) {
  return t.override !== null ? "覆盖" : (t.table !== null ? "表" : "元数据");
}

function plainRow(item) {
  const row = document.createElement("div");
  row.className = "model-row";
  const name = document.createElement("span");
  name.className = "model-name";
  name.textContent = item.name;
  name.title = item.name;
  const size = document.createElement("span");
  size.className = "model-size";
  size.textContent = mb(item.size);
  row.append(name, size);
  return row;
}

function penRow(item) {
  const row = document.createElement("div");
  row.className = "model-row";
  const name = document.createElement("span");
  name.className = "model-name";
  name.textContent = item.name;
  name.title = item.name;
  const size = document.createElement("span");
  size.className = "model-size";
  size.textContent = mb(item.size);
  const source = document.createElement("span");
  source.className = "pill model-source";
  const shown = document.createElement("span");
  shown.className = "model-effective";
  const input = document.createElement("input");
  input.type = "text";
  input.maxLength = 512;
  input.placeholder = "空串＝这支笔不用触发词";
  input.setAttribute("aria-label", item.name + " 的触发词");
  const save = document.createElement("button");
  save.type = "button"; save.className = "mini"; save.textContent = "保存";
  const clear = document.createElement("button");
  clear.type = "button"; clear.className = "mini"; clear.textContent = "清除";
  clear.title = "清掉人工覆盖，回到表值或元数据";

  const layer = (v) => (v === null ? "（无）" : (v || "（空串＝明确无）"));
  const paint = (t) => {
    source.textContent = sourceOf(t);
    // 元数据层的空串只表示「读不到」，跟表/覆盖里的空串（明确无）不是一回事
    source.title = "表：" + layer(t.table) + "｜元数据：" + (t.meta || "（读不到）")
                 + "｜覆盖：" + layer(t.override);
    shown.textContent = t.effective || "（无触发词）";
    shown.className = "model-effective" + (t.effective ? "" : " none");
    input.value = t.effective || "";
  };
  paint(item.trigger);

  const send = async (remove) => {
    save.disabled = clear.disabled = true;
    try {
      // 清除只在 query 里带笔名；token 一律走 Authorization 头（D135）
      const r = remove
        ? await fetch("/api/models/trigger?name=" + encodeURIComponent(item.name),
                      { method: "DELETE", headers: H })
        : await fetch("/api/models/trigger", {
            method: "POST",
            headers: { ...H, "Content-Type": "application/json" },
            body: JSON.stringify({ name: item.name, trigger: input.value }),
          });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(human(body) || "改触发词失败（" + r.status + "）");
      paint(body);
      sayModel(remove
        ? "已清除覆盖：" + item.name + " 回到「" + sourceOf(body) + "」"
        : "已存覆盖：" + item.name + " → " + (body.effective || "（不用触发词）"));
    } catch (e) {
      sayModel(e.message, true);
    } finally {
      save.disabled = clear.disabled = false;
    }
  };
  save.onclick = () => send(false);
  clear.onclick = () => send(true);
  input.onkeydown = (e) => { if (e.key === "Enter") send(false); };
  row.append(name, size, source, shown, input, save, clear);
  return row;
}

function renderModels(data) {
  const box = $("models-list");
  box.replaceChildren();
  MODEL_ROLES.forEach(([role, label]) => {
    const items = data[role] || [];
    const head = document.createElement("h3");
    head.textContent = label + "（" + items.length + "）";
    box.appendChild(head);
    items.forEach((it) => box.appendChild(role === "lora" ? penRow(it) : plainRow(it)));
  });
}

async function loadModels() {
  sayModel("扫描中…");
  try {
    const r = await fetch("/api/models", { headers: H });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(human(data) || "模型列表取不到（" + r.status + "）");
    renderModels(data);
    sayModel("扫描完成；模型文件只读");
  } catch (e) {
    sayModel(e.message, true);
  }
}

/* ---- T004 中文辅助：本地词表补全 ＋ 本机 Ollama 整句翻译（D99） ---- */
// 与后端 app/zh.py 的 _CN_SEG 同口径：中文标点算在片段里，整句一起翻才像话
const ZH_SEG = /[\u4e00-\u9fff][\u4e00-\u9fff\uff0c\u3002\u3001\uff1b\uff1a\uff01\uff1f\u2026\u2014\s]*/g;
let ZH_OK = false;   // Ollama 在不在；不在只置灰「整句翻译」，出图与补全都不受影响

function sayZh(msg, kind) {
  const el = $("zh-message");
  el.textContent = msg || "";
  el.className = "zh-msg" + (msg && kind ? " " + kind : "");
}

function hideZhPop() {
  const pop = $("zh-pop");
  pop.replaceChildren();
  pop.hidden = true;
}

function zhQuery() {
  // 选中的文字优先；没选中就取光标所在的中文片段；再没有就取框里第一段中文
  const ta = $("positive");
  const from = ta.selectionStart || 0;
  const to = ta.selectionEnd || 0;
  const sel = ta.value.slice(from, to).trim();
  if (sel) return { q: sel, from: from, to: to, text: ta.value };
  ZH_SEG.lastIndex = 0;
  let first = null;
  let m;
  while ((m = ZH_SEG.exec(ta.value)) !== null) {
    const q = m[0].trim();
    if (!q) continue;
    const hit = { q: q, from: m.index, to: m.index + m[0].length, text: ta.value };
    if (!first) first = hit;
    if (m.index <= from && from <= m.index + m[0].length) return hit;
  }
  return first || { q: "", from: 0, to: 0, text: ta.value };
}

function applyZh(pick, en) {
  const ta = $("positive");
  let from = pick.from;
  let to = pick.to;
  if (ta.value !== pick.text) {
    // 列表弹出后又改过字：按文字找，别拿旧坐标乱切
    const i = ta.value.indexOf(pick.q);
    if (i < 0) {
      sayZh("框里已经没有「" + pick.q + "」了，重新点一次补全", "bad");
      return;
    }
    from = i;
    to = i + pick.q.length;
  }
  ta.value = ta.value.slice(0, from) + en + ta.value.slice(to);
  ta.dispatchEvent(new Event("input", { bubbles: true }));
  ta.setSelectionRange(from + en.length, from + en.length);
  sayZh("已换上：" + pick.q + " → " + en + "（没有自动出图）", "ok");
}

function showZhPop(items, pick) {
  const pop = $("zh-pop");
  pop.replaceChildren();
  items.forEach((it) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "zh-item";
    const zh = document.createElement("span");
    zh.className = "zh-item-zh";
    zh.textContent = it.zh;
    const en = document.createElement("span");
    en.className = "zh-item-en";
    en.textContent = it.en;
    b.append(zh, en);
    b.title = "把「" + pick.q + "」换成 " + it.en;
    b.onclick = () => { applyZh(pick, it.en); hideZhPop(); };
    pop.appendChild(b);
  });
  pop.hidden = false;
}

async function zhComplete() {
  hideZhPop();
  const pick = zhQuery();
  if (!pick.q) {
    sayZh("正词框里没有中文：先写点中文，或选中要查的那几个字", "bad");
    return;
  }
  sayZh("查「" + pick.q + "」…");
  try {
    // 词表在本地，只读；q 走 query 但 token 一律走 Authorization 头（D135）
    const r = await fetch("/api/zh/complete?q=" + encodeURIComponent(pick.q), { headers: H });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(human(body) || "词表查不动（" + r.status + "）");
    const items = body.items || [];
    if (!items.length) {
      sayZh("词表里没有「" + pick.q + "」的候选；选中更短的词再试", "bad");
      return;
    }
    showZhPop(items, pick);
    sayZh("找到 " + items.length + " 条候选，点一条就换进去", "ok");
  } catch (e) {
    sayZh(e.message, "bad");
  }
}

async function zhTranslate() {
  hideZhPop();
  const ta = $("positive");
  const text = ta.value;
  if (!text.trim()) {
    sayZh("正词框是空的，没什么可翻的", "bad");
    return;
  }
  const btn = $("zh-translate");
  btn.disabled = true;
  sayZh("翻译中…（本机 CPU 跑，长句要等一会儿）");
  try {
    const r = await fetch("/api/zh/translate", {
      method: "POST",
      headers: { ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(human(body) || "翻译接口出错了（" + r.status + "）");
    // 后端失败也回 200＋ok=false：只报人话，不拦住出图（雷区 4）
    if (!body.ok) throw new Error((body.error && body.error.human) || "翻译没成");
    if (!body.changed) {
      sayZh("框里没有中文，一个字没动", "ok");
      return;
    }
    ta.value = body.text;   // 只回填，不自动出图（卡 §4）
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    sayZh("已翻译：中文换成了英文标签，英文与触发词没动（没有自动出图）", "ok");
  } catch (e) {
    sayZh(e.message, "bad");
  } finally {
    btn.disabled = !ZH_OK;
  }
}

async function loadZhStatus() {
  try {
    const r = await fetch("/api/zh/status", { headers: H });
    const s = await r.json().catch(() => ({}));
    ZH_OK = !!(r.ok && s.ollama);
  } catch (e) {
    ZH_OK = false;
  }
  const btn = $("zh-translate");
  btn.disabled = !ZH_OK;
  btn.title = ZH_OK
    ? "整句里的中文翻成英文标签；英文 tag 与触发词不动（本机模型，只用 CPU）"
    : "本机翻译模型（Ollama）不在，先置灰；出图和中文补全都不受影响";
  if (!ZH_OK) sayZh("本机翻译模型（Ollama）不在，「整句翻译」先置灰；中文补全走本地词表，照常能用", "warn");
}

function setSelect(el, value) {
  const has = [...el.options].some((o) => o.value === value);
  if (has) el.value = value;
  return has;
}

function applyPayload(p) {
  const misses = [];
  // params 模式只碰提示词与参数，绝不动底模和笔（雷区 4）
  if (p.mode === "full") {
    if (p.unet && !setSelect($("unet"), p.unet)) misses.push("底模 " + p.unet);
    const pens = p.loras || [];
    for (let i = 0; i < currentProfile().lora_slots; i++) {
      const pen = pens[i];
      if (pen && pen.name) {
        if (setSelect($("lora" + i), pen.name)) $("lorastr" + i).value = pen.strength;
        else misses.push("笔 " + pen.name);
      } else {
        $("lora" + i).value = ""; // 空槽：留工作流默认笔名，强度由后端归零（D65）
      }
    }
  }
  $("positive").value = p.positive || "";
  $("negative").value = p.negative || "";
  const prm = p.params || {};
  PARAM_FIELDS.forEach((k) => { if (k in prm) $(k).value = prm[k]; });
  syncSizePreset();   // 载配方也会改宽高，下拉同样要跟着走
  return misses;
}

async function loadPreset(mode) {
  const rid = $("preset").value;
  if (!rid) {
    sayPreset("先选一支配方", true);
    return;
  }
  const r = await fetch("/api/presets/" + encodeURIComponent(rid) + "/load", {
    method: "POST",
    headers: { ...H, "Content-Type": "application/json" },
    body: JSON.stringify({ profile_id: $("profile").value, mode }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    sayPreset((j.detail && j.detail.human) || "载入失败（" + r.status + "）", true);
    return;
  }
  const misses = applyPayload(j);
  if (misses.length) sayPreset("载入了，但这些现在不在模型目录里，没动：" + misses.join("、"), true);
  else sayPreset(mode === "params" ? "只换了提示词与参数，笔没动" : "已整套载入");
}

function openSaveBox() {
  $("preset-name").value = "";
  $("save-box").hidden = false;
  $("preset-name").focus();
}

function closeSaveBox() {
  $("save-box").hidden = true;
  sayPreset("");
}

async function savePreset() {
  const name = $("preset-name").value;
  if (!name.trim()) {
    sayPreset("预设得有个名字", true);
    return;
  }
  const s = collectUI();
  const r = await fetch("/api/presets", {
    method: "POST",
    headers: { ...H, "Content-Type": "application/json" },
    body: JSON.stringify({ profile_id: $("profile").value, name, ...s }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    sayPreset((j.detail && j.detail.human) || "存预设失败（" + r.status + "）", true);
    return;
  }
  closeSaveBox();
  await loadPresets(j.id);
  sayPreset("已存为「" + j.name + "」");
}

async function generate() {
  const s = collectUI();
  const count = clampCount($("count").value);
  $("count").value = count;   // 界面所见＝炉子所用：夹取后的值立刻回填（与其余参数同纪律）
  const req = {
    profile_id: $("profile").value,
    unet: s.unet,
    loras: s.loras,
    positive: s.positive,
    negative: s.negative,
    count,
    ...s.params,
  };
  const sp = collectStageParams();
  if (sp) req.stage_params = sp;   // 段2+ 才有；单段工作流的请求体与 A 阶段逐字相同
  $("status").textContent = "提交中…";
  const r = await fetch("/api/generate", {
    method: "POST",
    headers: { ...H, "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  const j = await r.json();
  if (j.status === "error") {
    $("status").textContent = j.error.human;
    $("status").className = "pill bad";
    $("raw").textContent = JSON.stringify(j.error, null, 2);
    return;
  }
  poll(j.job_id, count);
  loadJobs().catch(() => {});   // 新任务立刻上卡片（此时多半是排队中／出图中）
}

async function poll(jobId, want) {
  $("status").textContent = "出图中…";
  $("status").className = "pill";
  let fails = 0;
  let short = 0;
  for (;;) {
    await new Promise((res) => setTimeout(res, 1500));
    let j = null;
    try {
      const r = await fetch("/api/job/" + jobId, { headers: H });
      if (r.ok) j = await r.json();
    } catch (e) { /* 网络抖动或服务重启，下面计数重试 */ }
    if (!j) {
      if (++fails > 20) {
        $("status").textContent = "轮询失败太多次：画舫服务可能重启过，请刷新重试";
        $("status").className = "pill bad";
        return;
      }
      continue;
    }
    fails = 0;
    updateCard(jobId, j.status);   // 排队中 → 出图中 → 完成，卡片跟着走（不重拉列表）
    if (j.status === "done") {
      // 张数 >1：等整批出齐再上墙，中途不摆半墙（卡 §2）；等不到就按现状显示，绝不卡死在这
      if (want > 1 && (j.images || []).length < want && short++ < 4) continue;
      return showDone(j);
    }
    if (j.status === "error") {
      $("status").textContent = j.error.human;
      $("status").className = "pill bad";
      $("raw").textContent = j.error.detail || "";
      return;
    }
  }
}

/* ---- T005 队列对账与结果墙恢复 ---- */
const BLOBS = new Map();   // "filename|subfolder" → blob URL 的 promise（雷区 3：同一张图只请求一次）
const JOB_STATE = { pending: "待提交", queued: "排队中", running: "出图中", done: "完成 ✓", error: "失败" };

function sayJobs(msg, kind) {
  const el = $("jobs-message");
  el.textContent = msg || "";
  el.className = "jobs-msg" + (msg && kind ? " " + kind : "");
}

function blobUrl(im) {
  const key = im.filename + "|" + (im.subfolder || "");
  if (!BLOBS.has(key)) {
    // <img> 带不了 Authorization 头，走 fetch 拿 blob（D135：token 不进 URL）
    const p = fetch("/api/view?filename=" + encodeURIComponent(im.filename) +
        "&subfolder=" + encodeURIComponent(im.subfolder), { headers: H })
      .then((r) => { if (!r.ok) throw new Error("取不到图片（" + r.status + "）"); return r.blob(); })
      .then((b) => URL.createObjectURL(b));
    BLOBS.set(key, p);
    p.catch(() => BLOBS.delete(key));  // 失败不缓存（下次还能重试），顺便吃掉 rejection 免得上控制台
  }
  return BLOBS.get(key);
}

function imageView(im, cls) {
  const img = document.createElement("img");
  if (cls) img.className = cls;
  img.alt = im.filename;
  blobUrl(im).then((u) => { img.src = u; }, (e) => {
    img.className = (cls ? cls + " " : "") + "bad";
    img.title = e.message || "图取不到";
  });
  return img;
}

function stateLine(el, status) {
  el.replaceChildren();
  if (status === "running") {
    const s = document.createElement("span");
    s.className = "spin";
    el.append(s, " 出图中");
    return;
  }
  el.textContent = JOB_STATE[status] || status;
}

function jobCard(j) {
  const card = document.createElement("div");
  card.className = "job-card " + j.status;
  card.tabIndex = 0;
  card.dataset.jobId = j.job_id;
  card.title = j.readonly
    ? "从炉子历史恢复的只读记录（本次会话没有它的回执）"
    : "点一下重新加载这条任务的回执详情";
  const state = document.createElement("div");
  state.className = "job-state";
  stateLine(state, j.status);
  if (j.status === "error" && j.human) state.textContent = j.human;   // 红字人话（卡 §3）
  card.appendChild(state);
  const sum = document.createElement("div");
  sum.className = "job-sum";
  sum.textContent = [j.created_at || "", j.receipt_summary || ""].filter(Boolean).join(" · ");
  card.appendChild(sum);
  if (j.status === "done") (j.images || []).forEach((im) => card.appendChild(imageView(im, "thumb")));
  if (j.copy_error) {
    // T010：复制失败只是少了一份副本，原件还在炉子里——黄字说清楚，不改成失败
    const copy = document.createElement("div");
    copy.className = "job-copy";
    copy.textContent = j.copy_error;
    card.appendChild(copy);
  }
  const open = () => openJob(j.job_id);
  card.onclick = open;
  card.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  return card;
}

function renderJobs(list) {
  const box = $("jobs");
  box.replaceChildren();
  (list || []).forEach((j) => box.appendChild(jobCard(j)));
}

function updateCard(jobId, status) {
  // 轮询时只改那一张卡的状态字，不重拉列表（省得每 1.5 秒打一次 /queue ＋ /history）
  const card = [...$("jobs").children].find((c) => c.dataset.jobId === jobId);
  if (!card || card.classList.contains(status)) return;
  card.className = "job-card " + status;
  const state = card.querySelector(".job-state");
  if (state) stateLine(state, status);
}

async function loadJobs(autoOpen) {
  let body = null;
  try {
    const r = await fetch("/api/jobs", { headers: H });
    body = await r.json().catch(() => null);
    if (!r.ok) body = { ok: false, error: { human: "任务列表取不到（" + r.status + "）" } };
  } catch (e) {
    body = { ok: false, error: { human: "画舫连不上炉子，先确认 ComfyUI 开着" } };
  }
  if (!body || body.ok === false) {
    sayJobs((body && body.error && body.error.human) || "任务列表取不到", "bad");  // 不清空结果墙、不白屏（卡 D）
    return;
  }
  const c = body.comfy || {};
  const bad = c.history_ok === false;
  sayJobs(bad ? "历史读取异常：结果墙可能不完整" : "", bad ? "warn" : "");
  renderJobs(body.jobs);
  if (autoOpen) {
    const done = (body.jobs || []).filter((j) => j.status === "done");
    // 刷新页面后把最近一条跑完的重新摆上结果墙（A 项）；墙上已经有图就不抢
    if (done.length && !$("wall").children.length) openJob(done[done.length - 1].job_id);
  }
}

function markSelected(jobId) {
  [...$("jobs").children].forEach((c) => c.classList.toggle("selected", c.dataset.jobId === jobId));
}

async function openJob(jobId) {
  let j = null;
  try {
    const r = await fetch("/api/job/" + encodeURIComponent(jobId), { headers: H });
    if (r.ok) j = await r.json();
  } catch (e) { /* 炉子或服务抖动，下面给人话 */ }
  markSelected(jobId);
  if (!j) {
    sayJobs("这条任务的详情取不到（画舫可能重启过）", "bad");
    return;
  }
  renderJobDetail(j);
}

function renderReceipt(j) {
  const tb = $("receipt").tBodies[0];
  tb.replaceChildren();
  (j.receipt || []).forEach((it) => {
    const tr = document.createElement("tr");
    for (let k = 0; k < 4; k++) tr.appendChild(document.createElement("td"));
    tr.children[0].textContent = it.key;
    tr.children[1].textContent = JSON.stringify(it.requested);
    tr.children[2].textContent = JSON.stringify(it.actual);
    tr.children[3].textContent = it.ok ? "✓" : "✗";
    tr.children[3].className = it.ok ? "ok" : "bad";
    tb.appendChild(tr);
  });
  if (!j.receipt || !j.receipt.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.textContent = j.status === "error"
      ? ((j.error && j.error.human) || "出图失败")
      : "这条是从炉子历史恢复的只读记录：本次会话没有它的回执，画舫不编造（雷区 4）";
    tr.appendChild(td);
    tb.appendChild(tr);
  }
}

function renderJobDetail(j) {
  const wall = $("wall");
  wall.replaceChildren();
  (j.images || []).forEach((im) => wall.appendChild(imageView(im)));
  sayCopy(j.copy_error);   // T010：墙上这句黄字只报「复制没成」，done 与回执都不受影响
  renderReceipt(j);
  $("raw").textContent = JSON.stringify(
    { request: j.request, positive_final: j.positive_final, images: j.images || [] }, null, 2);
}

function showDone(j) {
  $("status").textContent = "完成 ✓";
  $("status").className = "pill ok";
  renderJobDetail(j);
  if (j.request && j.request.seed !== undefined) $("seed").value = j.request.seed; // 把真种子回填，方便复现
  if (j.request && j.request.count !== undefined) $("count").value = j.request.count; // 张数回填夹取后的值（T009）
  loadJobs().catch(() => {});   // 任务卡片跟着刷新；只重画列表，绝不重新提交（雷区 2）
}

/* ---- C 卡：出图台 ↔ 图库／对比板 的两趟交接（都只搬参数，不搬 token） ---- */
// 「当前参数为基准」＝把此刻界面上这一整套（含笔与强度）交给对比板当模式 A 的 base。
function gotoCompare() {
  const ui = collectUI();
  localStorage.setItem(COMPARE_KEY, JSON.stringify({
    mode: "variants",
    base: {
      profile_id: $("profile").value, unet: ui.unet, loras: ui.loras,
      positive: ui.positive, negative: ui.negative, params: ui.params,
    },
  }));
  location.href = "/compare";
}

// 图库里的「按此参数回到出图台」：**只回填界面，绝不自动出图**（卡 §3）。
function consumeRefill() {
  let p = null;
  try {
    p = JSON.parse(localStorage.getItem(REFILL_KEY) || "null");
    localStorage.removeItem(REFILL_KEY);
  } catch (e) { p = null; }
  if (!p) return;
  if (p.profile_id && !setSelect($("profile"), p.profile_id)) {
    sayPreset("这张图的工作流现在不在列表里（" + p.profile_id + "），参数没动", true);
    return;
  }
  renderProfile();
  loadPresets();
  const misses = applyPayload(p);
  sayPreset(misses.length
    ? "参数已从图库填回来（没有自动出图），但这些现在不在模型目录里，没动：" + misses.join("、")
    : "参数已从图库填回来，没有自动出图", misses.length > 0);
}

/* ---- T006 显卡忙／显存紧只读提示：15s 一轮，标签页不可见暂停，失败静默隐藏 ---- */
const GPU_POLL_MS = 15000;
function showGpuPill(body) {
  const pill = $("gpu-pill");
  if (!body || body.ok !== true || body.level === "ok" || body.level === "none") {
    pill.hidden = true;            // ok／探不到／炉子不在：整个不渲染，首屏干净（宁缺毋滥报）
    return;
  }
  if (body.level === "tight") {
    pill.className = "pill warn bad";
    pill.textContent = "显存紧张（剩 " + body.mem_free_mb + " MB），出图可能变慢或失败";
  } else {
    pill.className = "pill warn";
    const n = (body.queue || {}).pending || 0;
    pill.textContent = n > 0 ? "显卡忙：前面还有 " + n + " 张在排队" : "显卡忙：正在出图";
  }
  pill.hidden = false;
}
async function pollGpu() {
  let body = null;
  try {
    const r = await fetch("/api/gpu", { headers: H });
    body = r.ok ? await r.json().catch(() => null) : null;
  } catch (e) {
    body = null;                   // 雷区 3：请求失败静默隐藏，绝不往控制台留 error
  }
  showGpuPill(body);
}
function startGpuPoll() {
  pollGpu();
  setInterval(() => { if (!document.hidden) pollGpu(); }, GPU_POLL_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollGpu(); });
}

/* ---- T010 成品落地：输出目录 ＋ 打开成品文件夹 ---- */
function sayOutput(msg, kind) {
  const el = $("output-message");
  el.textContent = msg || "";
  el.className = "zh-msg" + (msg && kind ? " " + kind : "");
}

function sayCopy(msg) {
  const el = $("copy-message");
  el.textContent = msg || "";
  el.hidden = !msg;
}

async function saveOutputDir() {
  const btn = $("save-output-dir");
  btn.disabled = true;
  sayOutput("存中…");
  try {
    const r = await fetch("/api/output-dir", {
      method: "POST",
      headers: { ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ dir: $("output-dir").value }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(human(body) || "存不下来（" + r.status + "）");
    $("output-dir").value = body.dir || "";   // 回填后端规范化后的值，界面所见＝设置所存
    sayOutput(body.human || "已存", "ok");
  } catch (e) {
    sayOutput(e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

async function revealOutput() {
  const btn = $("reveal-output");
  btn.disabled = true;
  try {
    const r = await fetch("/api/reveal", {
      method: "POST",
      headers: { ...H, "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(human(body) || "打不开（" + r.status + "）");
    sayOutput(body.human || "已打开", "ok");
  } catch (e) {
    sayOutput(e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

/* ---- T007 BA 视觉（D35–D38）：可换背景＋密度；只动外观，功能语义一行没碰 ---- */
// 设置只认背景文件名、两档密度和整数模糊值；文件路径不交给用户输入。
let UI = { bg: "off", density: "loose", blur: 10 };
const DARK_LUM = 127.5;   // 16×16 平均亮度低于此值＝深色图，面板翻深色、文字提亮（卡 §2）
let UI_FILES = [];
let UI_SAVED = { ...UI };
let UI_SAVE_QUEUE = Promise.resolve();
let UI_BLUR_TIMER;
let UI_APPLY_SEQ = 0;

function sayUi(msg, kind) {
  const el = $("ui-message");
  el.textContent = msg || "";
  el.className = "ui-msg" + (msg && kind ? " " + kind : "");
}

function fillUiSelects() {
  const bg = $("ui-bg");
  bg.replaceChildren(new Option("关闭背景", "off"));   // 「关闭背景」永远在第一项、永远可用（卡 §2）
  UI_FILES.forEach((f) => bg.appendChild(new Option(f, f)));
  bg.appendChild(new Option("＋ 导入自己的图…", "__upload__"));
  bg.value = UI.bg;
  $("ui-density").value = UI.density;
}

/* 16×16 采样平均亮度（0–255）：浅图 → 白遮罩＋文字压深；深图 → 深遮罩＋文字提亮。
   读不出（图坏了／画布被污染／请求失败）→ 回 null，调用方按浅图兜底，控制台一个字不留（卡 §2）。 */
function sampleTone(url) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      try {
        const cv = document.createElement("canvas");
        cv.width = 16;
        cv.height = 16;
        const ctx = cv.getContext("2d");
        ctx.drawImage(img, 0, 0, 16, 16);
        const d = ctx.getImageData(0, 0, 16, 16).data;
        let sum = 0;
        let alpha = 0;
        for (let i = 0; i < d.length; i += 4) {
          const a = d[i + 3] / 255;
          sum += (0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]) * a;
          alpha += a;   // 透明像素不算进平均，免得整张图被透明区拖成「深色」
        }
        resolve(alpha > 0 ? sum / alpha : null);
      } catch (e) {
        resolve(null);
      }
    };
    img.onerror = () => resolve(null);
    img.src = url;
  });
}

async function applyUi() {
  const seq = ++UI_APPLY_SEQ;
  const body = document.body;
  body.dataset.density = UI.density;
  previewBlur(UI.blur);
  const on = UI.bg !== "off" && UI_FILES.indexOf(UI.bg) >= 0;
  $("ui-blur").disabled = !on;
  body.dataset.bgOn = on ? "1" : "0";
  const layer = $("bg-layer");
  if (!on) {
    body.dataset.bgTone = "light";   // 关掉背景＝纯底色主题（浅色板）
    layer.style.backgroundImage = "";
    return;
  }
  const url = "/static/bg/" + encodeURIComponent(UI.bg);
  layer.style.backgroundImage = 'url("' + url + '")';
  const lum = await sampleTone(url);
  if (seq === UI_APPLY_SEQ) body.dataset.bgTone = lum !== null && lum < DARK_LUM ? "dark" : "light";
}

function applyUiBoot() {
  const u = BOOT.ui || {};
  UI_FILES = Array.isArray(BOOT.bg_files) ? BOOT.bg_files : [];
  // 后端读侧已经兜过一遍，这里再夹一次：界面上绝不出现设置外的值
  UI = {
    bg: typeof u.bg === "string" && (u.bg === "off" || UI_FILES.indexOf(u.bg) >= 0) ? u.bg : "off",
    density: u.density === "compact" ? "compact" : "loose",
    blur: Number.isInteger(u.blur) && u.blur >= 0 && u.blur <= 40 ? u.blur : 10,
  };
  UI_SAVED = { ...UI };
  fillUiSelects();
  return applyUi();
}

function saveUi(next) {
  UI = { ...UI, ...next };
  applyUi();
  // 排队写补丁：快速交替调密度和背景，不让较晚回来的旧请求覆盖新选择。
  const pending = UI_SAVE_QUEUE.then(() => persistUi(next));
  UI_SAVE_QUEUE = pending.catch(() => false);
  return pending;
}

async function persistUi(next) {
  try {
    const r = await fetch("/api/ui", {
      method: "POST",
      headers: { ...H, "Content-Type": "application/json" },
      body: JSON.stringify(next),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(human(body) || "存不下来（" + r.status + "）");
    UI_SAVED = { ...body.ui };
    sayUi("界面设置已记住", "ok");
    return true;
  } catch (e) {
    UI = { ...UI_SAVED };
    await applyUi();
    fillUiSelects();
    sayUi(e.message, "bad");
    return false;
  }
}

function previewBlur(value) {
  $("ui-blur").value = value;
  $("ui-blur-value").textContent = "模糊 " + value + "px";
  document.documentElement.style.setProperty("--bg-blur", value + "px");
}

function saveBlur() {
  clearTimeout(UI_BLUR_TIMER);
  const blur = Number($("ui-blur").value);
  UI_BLUR_TIMER = setTimeout(() => saveUi({ blur }), 250);
}

async function importBackground() {
  const input = $("ui-bg-file");
  const file = input.files[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file);
  try {
    const r = await fetch("/api/bg/upload", { method: "POST", headers: H, body: form });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(human(body) || "背景图导入失败，请重新选择");
    const boot = await fetch("/api/bootstrap", { headers: H });
    if (!boot.ok) throw new Error("背景列表刷新失败，请重新选择");
    UI_FILES = (await boot.json()).bg_files;
    if (await saveUi({ bg: body.filename })) sayUi("背景图已导入并记住", "ok");
  } catch (e) {
    sayUi(e.message, "bad");
  } finally {
    fillUiSelects();
    input.value = "";
  }
}

/* ---- T011 起炉子：按钮点了才起，画舫绝不偷偷点（D102）；自动拉起开关默认关 ---- */

// 炉子在线时按钮**置灰而不是隐藏**：隐藏会让顶栏宽度跳一下，主人也看不出这里本来有个按钮。
// busy 记在 dataset 上，是为了 syncFurnaceUi 被别处调用时不会把「正在起」这个状态抹掉。
function syncFurnaceUi(alive) {
  const btn = $("furnace-btn");
  const busy = btn.dataset.busy === "1";
  btn.disabled = busy || !!alive;
  btn.textContent = busy ? "正在起炉子…" : "起炉子";
  btn.title = alive ? "炉子已经在线，不用起"
    : "点一下才起，画舫绝不偷偷起；起炉子会吃掉约 8G 显存（和训练抢卡），用完记得关";
}

// 只写「在线」那一半：boot() 里那两行是既有代码，不动它（卡外不改）。
function markFurnaceUp() {
  const pill = $("comfy-state");
  pill.textContent = "ComfyUI 在线";
  pill.className = "pill ok";
}

async function startFurnace() {
  const btn = $("furnace-btn");
  if (btn.disabled) return false;          // 防连点：进行中／已在线都不许再发一发
  btn.dataset.busy = "1";
  syncFurnaceUi(false);
  sayUi("正在起炉子，最多等 3 分钟…");
  let alive = false;
  try {
    // 后端是阻塞轮询到 8188 真应答才回 ok:true，所以这里的绿是真绿，不是「点过了就算」
    const r = await fetch("/api/furnace/start", { method: "POST", headers: H });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(human(body) || "起炉子失败（" + r.status + "）");
    alive = !!body.ok;
    sayUi(body.human || (alive ? "炉子起好了" : "没能起炉子"), alive ? "ok" : "bad");
    if (alive) markFurnaceUp();
    return alive;
  } catch (e) {
    sayUi(e.message, "bad");
    return false;
  } finally {
    delete btn.dataset.busy;
    syncFurnaceUi(alive);
  }
}

async function saveComfyAuto() {
  const box = $("comfy-auto");
  const want = box.checked;
  box.disabled = true;
  try {
    const r = await fetch("/api/furnace/auto", {
      method: "POST",
      headers: { ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ auto_start: want }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(human(body) || "存不下来（" + r.status + "）");
    sayUi(body.human || "已记住", "ok");
    return true;
  } catch (e) {
    box.checked = !want;      // 存不进盘就把勾退回来：界面不许显示一个盘上并不存在的状态
    sayUi(e.message, "bad");
    return false;
  } finally {
    box.disabled = false;
  }
}

/* ---- T012 空闲收火：状态失败静默，设置失败回退，不打断出图 ---- */
function fillIdleFree() {
  $("comfy-auto-free").checked = IDLE_FREE.auto_free;
  $("comfy-idle-minutes").value = IDLE_FREE.idle_minutes;
}

async function saveIdleFree() {
  const box = $("comfy-auto-free");
  const input = $("comfy-idle-minutes");
  const minutes = Number(input.value);
  if (!input.value.trim() || !Number.isInteger(minutes)) {
    fillIdleFree();
    sayUi("空闲时间得是整数分钟（5–120 分钟）");
    return false;
  }
  box.disabled = input.disabled = true;
  try {
    const r = await fetch("/api/furnace/idle-free", {
      method: "POST", headers: { ...H, "Content-Type": "application/json" },
      body: JSON.stringify({ auto_free: box.checked, idle_minutes: minutes }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok || body.ok !== true) throw new Error(human(body) || body.human || "空闲收火设置没能记住");
    IDLE_FREE = { auto_free: body.auto_free, idle_minutes: body.idle_minutes };
    fillIdleFree();
    sayUi(body.human || "已记住", "ok");
    return true;
  } catch (e) {
    fillIdleFree();
    sayUi(e.message);
    return false;
  } finally {
    box.disabled = input.disabled = false;
  }
}

async function pollIdleFree() {
  try {
    const r = await fetch("/api/furnace/idle-free", { headers: H });
    if (!r.ok) return;
    const body = await r.json();
    const pill = $("comfy-state");
    pill.textContent = !body.alive ? "ComfyUI 未启动"
      : body.standby ? "待命（模型已卸）" : "ComfyUI 在线";
    pill.className = "pill " + (body.alive ? "ok" : "bad");
    syncFurnaceUi(body.alive);
  } catch (e) { /* 状态只是提示，失败静默留待下轮，不弹红、不写控制台。 */ }
}

function startIdleFreePoll() {
  pollIdleFree();
  setInterval(() => { if (!document.hidden) pollIdleFree(); }, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollIdleFree(); });
}

window.onload = () => {
  SAMPLERS.forEach((s) => $("sampler_name").appendChild(new Option(s, s)));
  SCHEDULERS.forEach((s) => $("scheduler").appendChild(new Option(s, s)));
  // 尺寸下拉得在 boot 之前填好：boot 里的 renderLoras 会按 profile 默认宽高回写下拉（T008）
  SIZE_PRESETS.forEach(([label]) => $("size-preset").appendChild(new Option(label, label)));
  $("size-preset").appendChild(new Option(SIZE_CUSTOM, SIZE_CUSTOM));
  syncSizePreset();
  boot().then(() => {
    $("profile").onchange = () => { renderProfile(); loadPresets(); };
    $("size-preset").onchange = applySizePreset;
    $("width").oninput = onSizeInput;
    $("height").oninput = onSizeInput;
    $("go").onclick = generate;
    $("show-all").onchange = () => loadPresets();
    $("preset").onchange = presetChanged;
    $("load-full").onclick = () => loadPreset("full");
    $("load-params").onclick = () => loadPreset("params");
    $("save-preset").onclick = openSaveBox;
    $("save-confirm").onclick = savePreset;
    $("save-cancel").onclick = closeSaveBox;
    $("delete-preset").onclick = deletePreset;
    $("refresh-models").onclick = loadModels;
    $("models-panel").ontoggle = () => { if ($("models-panel").open) loadModels(); };
    $("preset-name").onkeydown = (e) => { if (e.key === "Enter") savePreset(); };
    $("zh-complete").onclick = zhComplete;
    $("zh-translate").onclick = zhTranslate;
    $("positive").onkeydown = (e) => { if (e.key === "Escape") hideZhPop(); };
    $("save-output-dir").onclick = saveOutputDir;
    $("reveal-output").onclick = revealOutput;
    $("nav-compare").onclick = (e) => { e.preventDefault(); gotoCompare(); };
    $("output-dir").onkeydown = (e) => { if (e.key === "Enter") saveOutputDir(); };
    $("ui-bg").onchange = () => {
      const value = $("ui-bg").value;
      if (value === "__upload__") {
        $("ui-bg").value = UI.bg;
        $("ui-bg-file").click();
      } else saveUi({ bg: value });
    };
    $("ui-bg-file").onchange = importBackground;
    $("ui-blur").oninput = () => previewBlur(Number($("ui-blur").value));
    $("ui-blur").onchange = saveBlur;
    $("ui-density").onchange = () => saveUi({ density: $("ui-density").value });
    $("furnace-btn").onclick = startFurnace;      // T011
    $("comfy-auto").onchange = saveComfyAuto;     // T011
    $("comfy-auto-free").onchange = saveIdleFree;
    $("comfy-idle-minutes").onchange = saveIdleFree;
    loadZhStatus();
    consumeRefill();                   // C 卡：从图库「按此参数回到出图台」跳回来时把参数填上（不自动出图）
    startGpuPoll();                // T006：加载查一次，其后 15s 一轮（不可见暂停）
    startIdleFreePoll();
    loadJobs(true).catch(() => {});   // T005：刷新页面就把结果墙恢复回来（只读恢复，不重新提交）
  });
};
