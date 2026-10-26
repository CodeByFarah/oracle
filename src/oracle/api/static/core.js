"use strict";
/*
 * ORACLE client runtime: DOM helpers, API client, formatting, shared components and charts.
 * No build step and no dependencies. Rendering only creates elements and text nodes:
 * nothing returned by the API is ever parsed as markup.
 */

/* ===================== DOM ===================== */
const SVGNS = "http://www.w3.org/2000/svg";
function build(ns, tag, attrs, kids) {
  const node = ns ? document.createElementNS(ns, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class" && !ns) node.className = v;
    else if (k === "style" && typeof v === "object") {
      // Custom properties (--x) are only settable through setProperty.
      for (const [prop, val] of Object.entries(v)) {
        if (prop.startsWith("--")) node.style.setProperty(prop, val); else node.style[prop] = val;
      }
    }
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const h = (tag, attrs = {}, ...kids) => build(null, tag, attrs, kids);
const s = (tag, attrs = {}, ...kids) => build(SVGNS, tag, attrs, kids);

/* Line icons, drawn on a 24px grid. */
const ICONS = {
  home: "M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  accounts: "M3 9h18M5 9v9m4.5-9v9m5-9v9M19 9v9M2 21h20M12 2.5 21 7H3z",
  cards: "M2.5 6.5a2 2 0 0 1 2-2h15a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-15a2 2 0 0 1-2-2zM2.5 9.5h19M6 15h4",
  activity: "M4 6h16M4 12h10M4 18h7m9-4-3 4-2-2",
  payments: "M7 7h13l-3-3m3 3-3 3M17 17H4l3 3m-3-3 3-3",
  insights: "M4 20V10m6 10V4m6 16v-7m4 7H2",
  budgets: "M12 3a9 9 0 1 0 9 9h-9zM15 2.6A9 9 0 0 1 21.4 9H15z",
  security: "M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6zm-3 9 2 2 4-4",
  assistant: "M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z",
  market: "M3 17l5-6 4 3 6-8 3 3",
  discoveries: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zm9 16-4-4",
  research: "M6 3h9l4 4v14H6zM14 3v5h5M9 13h7M9 17h5",
  strategies: "M4 19h16M7 15l3-4 3 2 4-6",
  portfolio: "M3 8h18v12H3zM8 8V5h8v3",
  risk: "M12 3 2 20h20zM12 10v4m0 3v.5",
  data: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zm0 0v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  experiments: "M9 3h6M10 3v6L4.5 19a1.5 1.5 0 0 0 1.3 2h12.4a1.5 1.5 0 0 0 1.3-2L14 9V3",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zm9 16-4-4",
  bell: "M6 16V11a6 6 0 1 1 12 0v5l2 2H4zm4 4h4",
  refresh: "M20 12a8 8 0 1 1-2.3-5.7M20 4v4h-4",
  moon: "M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z",
  menu: "M4 7h16M4 12h16M4 17h16",
  close: "M6 6l12 12M18 6 6 18",
  send: "M4 12l16-8-6 16-2-7z",
  plus: "M12 5v14M5 12h14",
  snow: "M12 2v20M4 6l16 12M20 6 4 18",
  bolt: "M13 2 4 14h7l-1 8 9-12h-7z",
  arrow: "M5 12h14m-5-5 5 5-5 5",
};
function icon(name) {
  return s("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, s("path", { d: ICONS[name] || ICONS.home }));
}

/* ===================== Formatting ===================== */
const isNum = (x) => x !== null && x !== undefined && x !== "" && Number.isFinite(Number(x));
const fmt = (x, d = 2) => isNum(x) ? Number(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) : "–";
const sci = (x) => isNum(x) ? Number(x).toExponential(2) : "–";
const pct = (x, d = 0) => isNum(x) ? (100 * x).toFixed(d) + "%" : "–";
const signed = (x, d = 2) => isNum(x) ? (x > 0 ? "+" : "") + fmt(x, d) : "–";
const words = (v) => String(v ?? "").replace(/_/g, " ");
const title = (v) => words(v).replace(/^./, c => c.toUpperCase());
const short = (v, n = 23) => v ? String(v).slice(0, n) : "–";
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const EXP = { JPY: 0 };
const fmtCache = new Map();
function money(minor, currency = "USD", { sign = false, compact = false, whole = false } = {}) {
  if (!isNum(minor)) return "–";
  const exp = EXP[currency] ?? 2, value = Number(minor) / 10 ** exp;
  const key = `${currency}|${compact}|${whole}`;
  if (!fmtCache.has(key)) {
    fmtCache.set(key, new Intl.NumberFormat(undefined, {
      style: "currency", currency, notation: compact ? "compact" : "standard",
      minimumFractionDigits: compact || whole ? 0 : exp, maximumFractionDigits: compact ? 1 : whole ? 0 : exp,
    }));
  }
  const text = fmtCache.get(key).format(Math.abs(value));
  return (value < 0 ? "−" : sign && value > 0 ? "+" : "") + text;
}
const toMinor = (major, currency = "USD") => Math.round(Number(major) * 10 ** (EXP[currency] ?? 2));
const toMajor = (minor, currency = "USD") => Number(minor) / 10 ** (EXP[currency] ?? 2);
function relTime(iso) {
  const ms = typeof iso === "number" ? iso : Date.parse(iso);
  const sec = Math.round((Date.now() - ms) / 1000);
  if (sec < 45) return "just now";
  if (sec < 3600) return Math.round(sec / 60) + " min ago";
  if (sec < 86400) return Math.round(sec / 3600) + " h ago";
  const days = Math.round(sec / 86400);
  return days === 1 ? "yesterday" : days < 7 ? days + " days ago" : new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const dateLong = (iso) => new Date(iso).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
const dateShort = (iso) => new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
const timeShort = (iso) => new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
const prefs = {
  get(k, d) { try { const v = localStorage.getItem("oracle:" + k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem("oracle:" + k, JSON.stringify(v)); } catch (_) { /* storage unavailable */ } },
};
function initials(name) { return String(name || "?").split(/\s+/).map(w => w[0]).join("").slice(0, 2).toUpperCase(); }

/* ===================== API ===================== */
/* GETs share in-flight requests and are cached briefly; any write clears the cache. */
const api = (() => {
  const cache = new Map(), TTL = 15000;
  function errorFrom(status, body, fallback) {
    let detail = body && body.detail;
    if (Array.isArray(detail)) detail = detail.map(d => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    const err = new Error(typeof detail === "string" && detail ? detail : fallback);
    err.status = status;
    return err;
  }
  async function get(path) {
    const hit = cache.get(path);
    if (hit && Date.now() - hit.at < TTL) return hit.promise;
    const promise = fetch(path, { headers: { Accept: "application/json" } }).then(async r => {
      if (!r.ok) throw errorFrom(r.status, await r.json().catch(() => ({})), r.statusText);
      return r.json();
    });
    cache.set(path, { at: Date.now(), promise });
    promise.catch(() => { if (cache.get(path)?.promise === promise) cache.delete(path); });
    return promise;
  }
  async function send(method, path, body) {
    const r = await fetch(path, {
      method,
      headers: { Accept: "application/json", "Content-Type": "application/json", "X-Oracle-Client": "dashboard" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    cache.clear();
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw errorFrom(r.status, data, r.statusText);
    document.dispatchEvent(new CustomEvent("oracle:changed", { detail: { method, path } }));
    return data;
  }
  /* POST that streams NDJSON events to `onEvent`. Resolves when the stream ends. */
  async function stream(path, body, onEvent, signal) {
    const r = await fetch(path, {
      method: "POST", signal,
      headers: { "Content-Type": "application/json", "X-Oracle-Client": "dashboard" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw errorFrom(r.status, await r.json().catch(() => ({})), r.statusText);
    const reader = r.body.getReader(), decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (line) onEvent(JSON.parse(line));
      }
    }
    cache.clear();
  }
  return { get, send, stream, invalidate() { cache.clear(); } };
})();

/* ===================== Feedback ===================== */
function toast(text, kind = "") {
  const t = h("div", { class: "toast " + kind, role: kind === "error" ? "alert" : "status" }, text);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), 3600);
}
async function attempt(fn, ok) {
  try { const out = await fn(); if (ok) toast(ok); return out; }
  catch (e) { toast(e.message || String(e), "error"); throw e; }
}
function highlight(text, query) {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [text];
  const lower = text.toLowerCase(), out = [];
  let i = 0;
  while (i < text.length) {
    let hit = null;
    for (const w of terms) { const j = lower.indexOf(w, i); if (j !== -1 && (hit === null || j < hit[0])) hit = [j, w.length]; }
    if (!hit) { out.push(text.slice(i)); break; }
    if (hit[0] > i) out.push(text.slice(i, hit[0]));
    out.push(h("mark", {}, text.slice(hit[0], hit[0] + hit[1])));
    i = hit[0] + hit[1];
  }
  return out;
}

/* ===================== Components ===================== */
function pageHead(heading, lede, ...actions) {
  return h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, heading), lede ? h("p", {}, lede) : null),
    actions.length ? h("div", { class: "page-actions" }, ...actions) : null);
}
function panel(heading, sub, ...kids) {
  return h("section", { class: "panel" },
    heading ? h("div", { class: "panel-head" }, h("h2", {}, heading), sub ? h("p", {}, sub) : null) : null, ...kids);
}
function empty(heading, text, cmd) {
  return h("div", { class: "empty" }, h("h2", {}, heading), h("p", {}, text, cmd ? " " : null, cmd ? h("code", {}, cmd) : null));
}
function skeletonPage() {
  return h("div", {}, h("div", { class: "sk", style: { height: "40px", width: "260px", margin: "16px 0 26px" } }),
    h("div", { class: "grid cols-2" }, h("div", { class: "sk", style: { height: "220px" } }), h("div", { class: "sk", style: { height: "220px" } })),
    h("div", { class: "sk", style: { height: "260px", marginTop: "20px" } }));
}
function button(label, onclick, cls = "", attrs = {}) {
  return h("button", { type: "button", class: "btn " + cls, onclick, ...attrs }, label);
}
function segmented(options, value, onChange, label) {
  const root = h("div", { class: "seg", role: "group", "aria-label": label });
  for (const [v, text] of options) {
    const b = h("button", { type: "button", "aria-pressed": String(v === value) }, text);
    b.addEventListener("click", () => {
      for (const x of root.children) x.setAttribute("aria-pressed", String(x === b));
      onChange(v);
    });
    root.append(b);
  }
  return root;
}
function toggle(checked, onChange, label, disabled = false) {
  const el = h("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(checked), "aria-label": label, disabled });
  el.addEventListener("click", async () => {
    const next = el.getAttribute("aria-checked") !== "true";
    el.setAttribute("aria-checked", String(next));
    try { await onChange(next); } catch (_) { el.setAttribute("aria-checked", String(!next)); }
  });
  return el;
}
function meter(frac, kind = "", markAt = null) {
  const bar = h("i");
  requestAnimationFrame(() => setTimeout(() => { bar.style.width = (clamp(frac, 0, 1) * 100).toFixed(1) + "%"; }, 30));
  return h("div", { class: "meter " + kind, role: "img", "aria-label": pct(frac) },
    bar, isNum(markAt) ? h("span", { class: "mark", style: { left: (clamp(markAt, 0, 1) * 100).toFixed(1) + "%" } }) : null);
}
function glyph(name, color) {
  return h("span", { class: "glyph", style: { background: color || "#7c8391" }, "aria-hidden": "true" }, (name || "?").replace(/^(To|From) /, "").trim()[0] || "?");
}
const SEVERITY_CHIP = { critical: "neg", warning: "warn", info: "info", positive: "pos" };

/* Payment card. Shows only the last four digits; the network mark is ORACLE's own. */
function plastic(card, { small = false } = {}) {
  const el = h("div", { class: `plastic ${card.design} ${card.status}`, role: "img",
    "aria-label": `${card.nickname}, card ending ${card.last4}, ${card.status}` },
    h("div", { class: "pl-top" }, h("span", { class: "pl-name" }, card.nickname), h("span", { class: "pl-sandbox" }, "SANDBOX")),
    h("div", { class: "pl-mid" }, h("span", { class: "chip-art" }),
      s("svg", { class: "contactless", viewBox: "0 0 24 24" }, s("path", { d: "M8 7a8 8 0 0 1 0 10M12 4.5a12 12 0 0 1 0 15M16 2a16 16 0 0 1 0 20" }))),
    h("div", { class: "pl-number" }, "•••• •••• •••• " + card.last4),
    h("div", { class: "pl-bottom" },
      h("div", {}, h("div", { class: "pl-holder" }, card.cardholder_name), h("div", { class: "pl-exp" }, `${card.product} · ${card.expiry}`)),
      h("span", { class: "pl-mark" }, s("svg", { viewBox: "0 0 32 32" },
        s("circle", { cx: 16, cy: 16, r: 11, fill: "none", stroke: "currentColor", "stroke-width": 3 }),
        s("circle", { cx: 16, cy: 16, r: 4, fill: "currentColor" })), "ORACLE")),
    h("div", { class: "frost" }, h("span", {}, card.status === "cancelled" ? "Cancelled" : "Frozen")));
  if (!small) {
    // Light follows the pointer across the card face.
    el.addEventListener("pointermove", (e) => {
      const r = el.getBoundingClientRect(), x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
      el.style.setProperty("--gx", (x * 100).toFixed(0) + "%"); el.style.setProperty("--gy", (y * 100).toFixed(0) + "%");
      if (!matchMedia("(prefers-reduced-motion: reduce)").matches)
        el.style.transform = `rotateY(${((x - 0.5) * 10).toFixed(2)}deg) rotateX(${((0.5 - y) * 8).toFixed(2)}deg)`;
    });
    el.addEventListener("pointerleave", () => { el.style.transform = ""; });
  }
  return el;
}

/* ===================== Dialog and drawer ===================== */
/* Modal form. `fields(form)` returns nodes; `submit(data)` may throw to show an error. */
function openDialog({ heading, lede, body, submitLabel = "Save", onSubmit, danger = false }) {
  const error = h("p", { class: "form-error", role: "alert" });
  const submit = h("button", { type: "submit", class: "btn " + (danger ? "danger" : "primary") }, submitLabel);
  const form = h("form", { method: "dialog", class: "form" }, body);
  const dlg = h("dialog", { "aria-labelledby": "dlg-title" },
    h("div", { class: "dialog-head" }, h("h2", { id: "dlg-title" }, heading), lede ? h("p", {}, lede) : null),
    h("div", { class: "dialog-body" }, form, error),
    h("div", { class: "dialog-foot" }, h("button", { type: "button", class: "btn ghost", onclick: () => dlg.close() }, "Cancel"), submit));
  submit.setAttribute("form", "");
  submit.addEventListener("click", (e) => { e.preventDefault(); form.requestSubmit(); });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!form.reportValidity()) return;
    error.textContent = ""; submit.disabled = true;
    try { await onSubmit(new FormData(form)); dlg.close(); }
    catch (err) { error.textContent = err.message || String(err); }
    finally { submit.disabled = false; }
  });
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}
function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", {}, label), input, hint ? h("small", {}, hint) : null);
}
function moneyField(label, name, currency = "USD", attrs = {}) {
  const sym = (0).toLocaleString(undefined, { style: "currency", currency, maximumFractionDigits: 0 }).replace(/[\d\s.,]/g, "");
  return field(label, h("div", { class: "money-input" }, h("span", { class: "cur" }, sym),
    h("input", { class: "input", name, type: "number", inputmode: "decimal", step: "0.01", min: "0.01", ...attrs })));
}
function select(name, options, value, attrs = {}) {
  return h("select", { class: "input", name, ...attrs }, ...options.map(([v, text]) => h("option", { value: v, selected: v === value }, text)));
}

const drawer = (() => {
  const el = document.getElementById("drawer"), scrim = document.getElementById("scrim");
  let lastFocus = null, onClose = null, isOpen = false;
  function close() {
    if (!isOpen) return;
    isOpen = false;
    el.classList.remove("open"); scrim.classList.remove("open");
    setTimeout(() => { if (!isOpen) { el.hidden = true; scrim.hidden = true; } }, 320);
    const cb = onClose; onClose = null;
    if (cb) cb();
    if (lastFocus && lastFocus.isConnected) lastFocus.focus();
  }
  function open(heading, body, { wide = false, close: cb = null } = {}) {
    if (!isOpen) lastFocus = document.activeElement;
    onClose = cb; isOpen = true;
    el.hidden = false; scrim.hidden = false;
    el.classList.toggle("wide", wide);
    const closeBtn = h("button", { type: "button", class: "icon-btn", "aria-label": "Close", onclick: close }, icon("close"));
    el.replaceChildren(h("div", { class: "drawer-head" }, h("h2", { id: "drawer-title" }, heading), closeBtn), h("div", { class: "drawer-body" }, body));
    requestAnimationFrame(() => { el.classList.add("open"); scrim.classList.add("open"); });
    closeBtn.focus();
  }
  scrim.addEventListener("click", close);
  el.addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const f = [...el.querySelectorAll("button, a[href], input, select, textarea, [tabindex='0']")].filter(x => !x.disabled);
    if (!f.length) return;
    if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
    else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
  });
  return { open, close, get isOpen() { return isOpen; } };
})();

/* ===================== Charts ===================== */
function niceTicks(min, max, count = 4) {
  if (min === max) { min -= 1; max += 1; }
  const step0 = (max - min) / count, mag = 10 ** Math.floor(Math.log10(step0)), r = step0 / mag;
  const step = (r < 1.5 ? 1 : r < 3 ? 2 : r < 7 ? 5 : 10) * mag;
  const ticks = [];
  for (let v = Math.floor(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}
function tooltipFor(host) {
  const tip = host.querySelector(":scope > .tip") || h("div", { class: "tip" });
  return {
    el: tip,
    show(x, y, nodes) {
      tip.replaceChildren(...nodes.filter(n => n !== null && n !== undefined && n !== false));
      const w = host.clientWidth, left = x > w * 0.6 ? x - tip.offsetWidth - 12 : x + 12;
      tip.style.left = left + "px"; tip.style.top = Math.max(0, y - 10) + "px"; tip.style.opacity = "1";
    },
    hide() { tip.style.opacity = "0"; },
  };
}
/* Responsive SVG: re-renders `draw(width)` whenever the host width changes. */
function responsive(cls, label, draw, height) {
  const host = h("div", { class: "chart " + (cls || ""), role: "img", "aria-label": label, style: { height: height + "px" } });
  let lastW = 0;
  const ro = new ResizeObserver(debounce(() => {
    if (!host.isConnected) { ro.disconnect(); return; }
    const w = Math.round(host.clientWidth);
    if (w && w !== lastW) { lastW = w; draw(host, w); }
  }, 30));
  ro.observe(host);
  return host;
}

/* Area/line chart. points: [{x: label, t: ms|null, y: number}], compare: optional second series. */
function areaChart(points, { height = 220, color = "var(--ink)", format = (v) => String(v), compare = null, compareLabel = "", label = "Chart" } = {}) {
  return responsive("", label, (host, W) => {
    const H = height, padL = 4, padR = 56, padT = 10, padB = 24, plotW = W - padL - padR, plotH = H - padT - padB;
    const all = points.map(p => p.y).concat(compare ? compare.map(p => p.y) : []);
    const lo = Math.min(0, ...all), hi = Math.max(...all, 1);
    const ticks = niceTicks(lo, hi, 4), y0 = Math.min(ticks[0], lo), y1 = Math.max(ticks[ticks.length - 1], hi);
    const n = points.length - 1 || 1;
    const X = (i) => padL + (i / n) * plotW, Y = (v) => padT + (1 - (v - y0) / (y1 - y0)) * plotH;
    const path = (ps) => ps.map((p, i) => (i ? "L" : "M") + X(i).toFixed(1) + "," + Y(p.y).toFixed(1)).join("");
    const gid = "g" + Math.random().toString(36).slice(2, 8);
    const line = path(points);
    const area = `${line}L${X(points.length - 1).toFixed(1)},${Y(y0).toFixed(1)}L${X(0).toFixed(1)},${Y(y0).toFixed(1)}Z`;
    const every = Math.max(1, Math.ceil(points.length / Math.max(2, Math.floor(plotW / 90))));
    const hoverLine = s("line", { class: "hover-line", y1: padT, y2: padT + plotH, visibility: "hidden" });
    const dot = s("circle", { r: 4.5, fill: color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" });
    const overlay = s("rect", { x: padL, y: 0, width: plotW, height: H, fill: "transparent" });
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H },
      s("defs", {}, s("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 },
        s("stop", { offset: "0", "stop-color": color, "stop-opacity": ".16" }), s("stop", { offset: "1", "stop-color": color, "stop-opacity": "0" }))),
      ...ticks.filter(v => v >= y0 && v <= y1).map(v => [
        s("line", { class: "gridline", x1: padL, x2: padL + plotW, y1: Y(v).toFixed(0), y2: Y(v).toFixed(0) }),
        s("text", { x: W - padR + 10, y: Y(v) + 4 }, format(v))]),
      ...points.map((p, i) => i % every === 0 ? s("text", { x: X(i), y: H - 6, "text-anchor": i === 0 ? "start" : "middle" }, p.x) : null),
      compare ? s("path", { d: path(compare), fill: "none", stroke: "var(--ink-3)", "stroke-width": 1.5, "stroke-dasharray": "4 4" }) : null,
      s("path", { d: area, fill: `url(#${gid})` }),
      s("path", { d: line, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }),
      hoverLine, dot, overlay);
    const tip = tooltipFor(host);
    overlay.addEventListener("pointermove", (ev) => {
      const box = svg.getBoundingClientRect(), px = (ev.clientX - box.left) * (W / box.width);
      const i = clamp(Math.round(((px - padL) / plotW) * n), 0, points.length - 1);
      for (const el of [hoverLine]) { el.setAttribute("x1", X(i)); el.setAttribute("x2", X(i)); el.setAttribute("visibility", "visible"); }
      dot.setAttribute("cx", X(i)); dot.setAttribute("cy", Y(points[i].y)); dot.setAttribute("visibility", "visible");
      tip.show((X(i) / W) * box.width, (Y(points[i].y) / H) * box.height, [h("b", {}, points[i].x), format(points[i].y),
        compare && compare[i] ? h("div", { class: "muted" }, `${compareLabel}: ${format(compare[i].y)}`) : null]);
    });
    overlay.addEventListener("pointerleave", () => { hoverLine.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hide(); });
    host.replaceChildren(svg, tip.el);
  }, height);
}

/* Grouped bars. groups: [{label, values: [n, ...], partial}], series: [{name, color}] */
function barChart(groups, series, { height = 220, format = (v) => String(v), label = "Bar chart" } = {}) {
  return responsive("", label, (host, W) => {
    const H = height, padL = 4, padR = 56, padT = 10, padB = 24, plotW = W - padL - padR, plotH = H - padT - padB;
    const hi = Math.max(1, ...groups.flatMap(g => g.values));
    const ticks = niceTicks(0, hi, 4), y1 = Math.max(ticks[ticks.length - 1], hi);
    const Y = (v) => padT + (1 - v / y1) * plotH;
    const gw = plotW / groups.length, bw = Math.min(26, (gw * 0.64) / series.length);
    const tip = tooltipFor(host);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H },
      ...ticks.map(v => [s("line", { class: "gridline", x1: padL, x2: padL + plotW, y1: Y(v).toFixed(0), y2: Y(v).toFixed(0) }),
        s("text", { x: W - padR + 10, y: Y(v) + 4 }, format(v))]),
      ...groups.map((g, gi) => {
        const cx = padL + gi * gw + gw / 2, x0 = cx - (bw * series.length) / 2;
        const bars = g.values.map((v, si) => s("rect", { x: x0 + si * bw + 1, y: Y(v), width: bw - 2, height: Math.max(0, padT + plotH - Y(v)), rx: 3,
          fill: series[si].color, opacity: g.partial ? 0.55 : 1 }));
        const hit = s("rect", { x: padL + gi * gw, y: 0, width: gw, height: H, fill: "transparent" });
        hit.addEventListener("pointermove", (ev) => {
          const box = svg.getBoundingClientRect();
          tip.show(((cx) / W) * box.width, ev.clientY - box.top, [h("b", {}, g.label + (g.partial ? " (so far)" : "")),
            ...series.map((sr, si) => h("div", {}, `${sr.name}: ${format(g.values[si])}`))]);
        });
        hit.addEventListener("pointerleave", () => tip.hide());
        return [...bars, s("text", { x: cx, y: H - 6, "text-anchor": "middle" }, g.label), hit];
      }));
    host.replaceChildren(svg, tip.el);
  }, height);
}

/* Donut of category shares, with the total in the middle. rows: [{label, color, value}] */
function donut(rows, centerTop, centerBottom, size = 200) {
  const total = rows.reduce((t, r) => t + Math.max(0, r.value), 0) || 1;
  const R = size / 2, r = R - 18, C = 2 * Math.PI * r;
  let offset = 0;
  const arcs = rows.filter(x => x.value > 0).map((row) => {
    const len = (row.value / total) * C;
    const arc = s("circle", { cx: R, cy: R, r, fill: "none", stroke: row.color, "stroke-width": 22,
      "stroke-dasharray": `${Math.max(0, len - 2)} ${C}`, "stroke-dashoffset": -offset, transform: `rotate(-90 ${R} ${R})` },
      s("title", {}, `${row.label}: ${pct(row.value / total)}`));
    offset += len;
    return arc;
  });
  return s("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: "img", "aria-label": "Share by category" },
    s("circle", { cx: R, cy: R, r, fill: "none", stroke: "var(--sunken)", "stroke-width": 22 }), ...arcs,
    s("text", { x: R, y: R - 2, "text-anchor": "middle", style: "font: 650 22px var(--display); fill: var(--ink)" }, centerTop),
    s("text", { x: R, y: R + 18, "text-anchor": "middle", style: "font: 12px var(--text); fill: var(--ink-3)" }, centerBottom));
}

/* Progress ring (health score, goals). */
function ring(frac, size = 132, color = "url(#warm)", stroke = 12) {
  const R = size / 2, r = R - stroke / 2 - 2, C = 2 * Math.PI * r;
  const arc = s("circle", { cx: R, cy: R, r, fill: "none", stroke: color, "stroke-width": stroke, "stroke-linecap": "round",
    "stroke-dasharray": `0 ${C}`, transform: `rotate(-90 ${R} ${R})`, style: "transition: stroke-dasharray 1s cubic-bezier(.2,.7,.2,1)" });
  requestAnimationFrame(() => setTimeout(() => arc.setAttribute("stroke-dasharray", `${clamp(frac, 0, 1) * C} ${C}`), 40));
  return s("svg", { viewBox: `0 0 ${size} ${size}`, width: size, height: size, "aria-hidden": "true" },
    s("defs", {}, s("linearGradient", { id: "warm", x1: 0, y1: 0, x2: 1, y2: 1 },
      s("stop", { offset: "0", "stop-color": "#eb001b" }), s("stop", { offset: ".55", "stop-color": "#ff5f00" }), s("stop", { offset: "1", "stop-color": "#f79e1b" }))),
    s("circle", { cx: R, cy: R, r, fill: "none", stroke: "var(--sunken)", "stroke-width": stroke }), arc);
}

/* ===================== Table ===================== */
/* Sortable table with CSV export. columns: [{ key, label, num?, value?(row), render?(row) }] */
function dataTable({ columns, rows, name, onRowClick, initialSort, caption }) {
  let sort = initialSort || null;
  const value = (c, r) => c.value ? c.value(r) : r[c.key];
  const tbody = h("tbody");
  const ths = columns.map((c, i) => h("th", { scope: "col", class: c.num ? "num" : null },
    h("button", { type: "button", onclick: () => { sort = sort && sort.i === i ? { i, dir: -sort.dir } : { i, dir: c.num ? -1 : 1 }; paint(); } }, c.label)));
  const missing = (x) => x === null || x === undefined || (typeof x === "number" && Number.isNaN(x));
  function paint() {
    const list = [...rows];
    if (sort) {
      const c = columns[sort.i];
      list.sort((a, b) => {
        const x = value(c, a), y = value(c, b);
        if (missing(x) || missing(y)) return missing(x) - missing(y);
        const d = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
        return d * sort.dir;
      });
    }
    ths.forEach((th, i) => th.setAttribute("aria-sort", sort && sort.i === i ? (sort.dir > 0 ? "ascending" : "descending") : "none"));
    tbody.replaceChildren(...list.map(r => h("tr", onRowClick ? { class: "click", tabindex: "0", onclick: () => onRowClick(r),
        onkeydown: (e) => { if (e.key === "Enter") onRowClick(r); } } : {},
      ...columns.map(c => h("td", { class: c.num ? "num" : null }, c.render ? c.render(r) : (value(c, r) ?? "–"))))));
  }
  function exportCsv() {
    const esc = (v) => { const t = v === null || v === undefined ? "" : String(v); return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t; };
    const lines = [columns.map(c => esc(c.label)).join(","), ...rows.map(r => columns.map(c => esc(value(c, r))).join(","))];
    const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    const a = h("a", { href: url, download: (name || "oracle") + ".csv" });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Exported ${rows.length} rows`);
  }
  paint();
  return h("div", {},
    h("div", { class: "tbl-bar" }, h("span", {}, caption || `${rows.length} ${rows.length === 1 ? "row" : "rows"}`),
      rows.length ? h("button", { type: "button", class: "link", onclick: exportCsv }, "Export CSV") : null),
    h("div", { class: "tbl-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...ths)), tbody)));
}

/* Shared, lazily loaded reference data (categories, card designs). */
let META = null;
async function meta() {
  if (!META) META = await api.get("/api/fintech/meta");
  return META;
}
const CAT = (key) => (META && META.categories.find(c => c.key === key)) || { key, label: title(key), color: "#9aa3b2" };
