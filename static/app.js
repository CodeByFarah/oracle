"use strict";
/* Shell: navigation, router, command palette, alerts, theme and keyboard shortcuts. */

const ROUTES = [
  { id: "home", label: "Home", group: "Money", icon: "home", view: viewHome },
  { id: "accounts", label: "Accounts", group: "Money", icon: "accounts", view: viewAccounts },
  { id: "cards", label: "Cards", group: "Money", icon: "cards", view: viewCards },
  { id: "activity", label: "Activity", group: "Money", icon: "activity", view: viewActivity },
  { id: "payments", label: "Payments", group: "Money", icon: "payments", view: viewPayments },
  { id: "insights", label: "Insights", group: "Money", icon: "insights", view: viewInsights },
  { id: "budgets", label: "Budgets & goals", group: "Money", icon: "budgets", view: viewBudgets },
  { id: "security", label: "Security", group: "Money", icon: "security", view: viewSecurity, badge: true },
  { id: "assistant", label: "Assistant", group: "Intelligence", icon: "assistant", view: viewAssistant, ai: true },
  { id: "market", label: "Market", group: "Research", icon: "market", view: viewOverview, research: true },
  { id: "discoveries", label: "Discoveries", group: "Research", icon: "discoveries", view: viewDiscoveries, research: true },
  { id: "research", label: "Research lab", group: "Research", icon: "research", view: viewResearch, research: true },
  { id: "strategies", label: "Strategies", group: "Research", icon: "strategies", view: viewStrategies, research: true },
  { id: "portfolio", label: "Paper portfolio", group: "Research", icon: "portfolio", view: viewPortfolio, research: true },
  { id: "risk", label: "Risk", group: "Research", icon: "risk", view: viewRisk, research: true },
  { id: "data", label: "Data", group: "Research", icon: "data", view: viewData, research: true },
  { id: "experiments", label: "Experiments", group: "Research", icon: "experiments", view: viewExperiments, research: true },
];

/* ===================== navigation ===================== */
const rail = document.getElementById("rail");
const navRoot = document.getElementById("nav");
for (const group of [...new Set(ROUTES.map(r => r.group))]) {
  navRoot.append(h("div", { class: "nav-group" }, h("div", { class: "nav-title" }, group),
    ...ROUTES.filter(r => r.group === group).map(r => h("a", { class: "nav-link" + (r.ai ? " ai" : ""), href: "#/" + r.id, "data-route": r.id },
      icon(r.icon), h("span", {}, r.label), r.badge ? h("span", { class: "count", hidden: true, "data-badge": "alerts" }) : null))));
}
navRoot.addEventListener("click", (e) => { if (e.target.closest("a")) rail.classList.remove("open"); });
document.getElementById("menu").addEventListener("click", () => rail.classList.toggle("open"));

/* ===================== router ===================== */
let renderedKey = null, renderSeq = 0;
function parseHash() {
  const [path, qs] = location.hash.replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean);
  const r = ROUTES.find(x => x.id === parts[0]) || ROUTES[0];
  return { route: r, param: parts[1] ? decodeURIComponent(parts[1]) : null, query: new URLSearchParams(qs || "") };
}
function go(hash) { if (location.hash !== hash) location.hash = hash; else route({ force: true }); }
async function route({ force = false } = {}) {
  const { route: r, param, query } = parseHash();
  for (const a of navRoot.querySelectorAll("a")) {
    if (a.dataset.route === r.id) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  document.title = `${r.label} · ORACLE`;
  prefs.set("last-route", r.id);
  // Experiments open in a drawer over the list; the list itself does not re-render.
  if (r.id === "experiments" && param) { openExperiment(param); if (renderedKey === "experiments" && !force) return; }
  else if (drawer.isOpen && !force) drawer.close();
  const key = r.id === "experiments" ? "experiments" : location.hash;
  if (!force && renderedKey === key) return;
  const main = document.getElementById("main"), my = ++renderSeq, y = scrollY;
  if (!force) { main.replaceChildren(skeletonPage()); scrollTo({ top: 0 }); }
  try {
    let node = await r.view(r.research ? researchCtx : r.id === "experiments" ? null : param, query);
    if (my !== renderSeq) return;
    if (Array.isArray(node)) node = h("div", { class: "page research" }, ...node.filter(Boolean));
    main.replaceChildren(node);
    if (force) scrollTo({ top: y });
  } catch (e) {
    if (my !== renderSeq) return;
    main.replaceChildren(h("div", { class: "page" }, e.status === 404
      ? (r.research ? empty("Nothing to show yet.", "Load data and run a research cycle first:", "oracle loop run-once") : empty("Not found.", e.message))
      : empty("Something went wrong.", e.message)));
  }
  renderedKey = key;
}
function refreshView() { api.invalidate(); loadBadges(); return route({ force: true }); }

/* ===================== top bar: AI status, alerts, theme ===================== */
const pill = document.getElementById("engine");
async function loadAiStatus(refresh = false) {
  try {
    const st = await fetch("/api/ai/status" + (refresh ? "?refresh=true" : "")).then(r => r.json());
    window.aiStatus = st;
    const local = st.engine !== "offline";
    pill.classList.toggle("local", local);
    pill.replaceChildren(h("span", { class: "dot" }), h("span", { class: "label" }, local ? `Local AI · ${st.model.split(":")[0]}` : "Offline AI"));
    pill.title = local ? `${st.model} via Ollama on this machine` : st.detail;
    assistant.setStatus(st);
  } catch (_) { pill.replaceChildren(h("span", { class: "dot" }), h("span", { class: "label" }, "AI unavailable")); }
}
pill.addEventListener("click", () => assistant.toggle());

const bell = document.getElementById("bell"), pop = document.getElementById("alerts-pop");
let openAlerts = [];
async function loadBadges() {
  try { openAlerts = await api.get("/api/fintech/alerts?status=open"); } catch (_) { openAlerts = []; }
  const n = openAlerts.filter(a => a.severity !== "info").length;
  for (const b of document.querySelectorAll("[data-badge=alerts]")) { b.hidden = !n; b.textContent = String(n); }
  bell.querySelector(".badge-dot").hidden = !n;
  bell.setAttribute("aria-label", n ? `${n} alerts need attention` : "Alerts");
}
bell.addEventListener("click", (e) => {
  e.stopPropagation();
  if (!pop.hidden) { pop.hidden = true; return; }
  pop.replaceChildren(h("div", { class: "panel-head", style: { marginTop: "6px" } }, h("h2", {}, "Alerts")),
    openAlerts.length ? h("div", { class: "list" }, ...openAlerts.slice(0, 8).map(a => h("button", { type: "button", class: "row",
      onclick: () => { pop.hidden = true; if (a.subject_type === "transaction") openTxn(a.subject_id); else go("#/security"); } },
      h("span", { class: "sev-dot", style: { width: "9px", height: "9px", borderRadius: "50%", background: `var(--${a.severity === "critical" ? "neg" : a.severity === "warning" ? "warn" : "info"})` } }),
      h("div", { class: "row-main" }, h("div", { class: "row-title", style: { whiteSpace: "normal" } }, a.title), h("div", { class: "row-sub" }, relTime(a.created_at))), h("span"))))
      : h("p", { class: "muted" }, "You're all caught up."),
    h("p", {}, h("a", { class: "link", href: "#/security", onclick: () => { pop.hidden = true; } }, "Open Security")));
  pop.hidden = false;
});
document.addEventListener("click", (e) => { if (!pop.hidden && !pop.contains(e.target)) pop.hidden = true; });

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
}
applyTheme(prefs.get("theme", null));
function toggleTheme() {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const next = dark ? "light" : "dark";
  prefs.set("theme", next); applyTheme(next);
}
document.getElementById("theme").addEventListener("click", toggleTheme);
document.getElementById("refresh").addEventListener("click", async (e) => {
  const btn = e.currentTarget; btn.classList.add("spin");
  try { await Promise.all([refreshView(), loadAiStatus(true)]); toast("Up to date"); } finally { btn.classList.remove("spin"); }
});

async function loadIdentity() {
  try {
    const c = await api.get("/api/fintech/overview").then(o => o.customer);
    document.getElementById("avatar").textContent = initials(c.display_name);
    document.getElementById("avatar").title = `${c.display_name} (synthetic demo customer)`;
  } catch (_) { /* ledger unavailable: leave the placeholder */ }
}

/* ===================== command palette ===================== */
const palette = (() => {
  const root = document.getElementById("palette"), input = document.getElementById("palette-input"), list = document.getElementById("palette-list");
  let items = [], shown = [], active = 0, returnFocus = null;
  const base = () => [
    ...ROUTES.map(r => ({ label: r.label, group: r.group, run: () => go("#/" + r.id) })),
    { label: "Move money", group: "Action", run: () => moveMoneyDialog() },
    { label: "New virtual card", group: "Action", run: () => { go("#/cards"); setTimeout(issueCardDialog, 300); } },
    { label: "Ask Oracle", group: "AI", run: () => assistant.openDock() },
    { label: "Try a suspicious purchase", group: "Security", run: () => go("#/security") },
    { label: "Switch light or dark theme", group: "Action", run: toggleTheme },
    { label: "Refresh", group: "Action", run: () => refreshView() },
  ];
  function score(text, q) {
    const t = text.toLowerCase(); let ti = 0, sc = 0, run = 0;
    for (const ch of q.toLowerCase()) {
      if (ch === " ") continue;
      const j = t.indexOf(ch, ti);
      if (j === -1) return -1;
      run = j === ti ? run + 1 : 0;
      sc += 1 + run * 2 + (j === 0 || t[j - 1] === " " ? 3 : 0);
      ti = j + 1;
    }
    return sc - t.length * 0.01;
  }
  async function gather() {
    const out = base();
    const [cards, accounts, sym, exps] = await Promise.all([
      api.get("/api/fintech/cards").catch(() => []), api.get("/api/fintech/accounts").catch(() => []),
      api.get("/api/symbols").catch(() => []), api.get("/api/experiments").catch(() => [])]);
    for (const c of cards) out.push({ label: `${c.nickname} •• ${c.last4}`, group: "Card", run: () => go(`#/cards/${c.id}`) });
    for (const a of accounts) out.push({ label: a.name, group: "Account", run: () => go(`#/accounts/${a.id}`) });
    for (const x of [...new Set(sym.map(y => y.symbol))]) out.push({ label: `Chart ${x}`, group: "Research", run: () => { prefs.set("symbol", x); go("#/market"); } });
    for (const e of exps) out.push({ label: e.name, group: "Experiment", run: () => go(`#/experiments/${e.protocol_hash}`) });
    return out;
  }
  function paint() {
    const q = input.value.trim();
    const dynamic = q ? [
      { label: `Ask Oracle: “${q}”`, group: "AI", run: () => assistant.openDock(q) },
      { label: `Search transactions for “${q}”`, group: "Activity", run: () => go(`#/activity?q=${encodeURIComponent(q)}`) },
    ] : [];
    const ranked = q ? items.map(i => [score(i.label + " " + i.group, q), i]).filter(([sc]) => sc >= 0).sort((a, b) => b[0] - a[0]).map(([, i]) => i) : items;
    shown = [...ranked.slice(0, 40), ...dynamic];
    active = clamp(active, 0, Math.max(0, shown.length - 1));
    list.replaceChildren(...shown.map((it, i) => h("li", { role: "option", id: "opt-" + i, "aria-selected": String(i === active),
      onmousemove: () => { if (active !== i) { active = i; paint(); } }, onclick: () => choose(i) },
      h("span", {}, ...highlight(it.label, q)), h("span", { class: "grp" }, it.group))));
    input.setAttribute("aria-activedescendant", shown.length ? "opt-" + active : "");
    list.children[active]?.scrollIntoView({ block: "nearest" });
  }
  function choose(i) { const it = shown[i]; if (!it) return; close(false); it.run(); }
  async function open() {
    returnFocus = document.activeElement;
    root.classList.add("open"); input.value = ""; active = 0;
    items = base(); paint(); input.focus();
    const all = await gather();
    if (root.classList.contains("open")) { items = all; paint(); }
  }
  function close(restore = true) { root.classList.remove("open"); if (restore && returnFocus && returnFocus.isConnected) returnFocus.focus(); }
  input.addEventListener("input", () => { active = 0; paint(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(shown.length - 1, active + 1); paint(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(0, active - 1); paint(); }
    else if (e.key === "Enter") { e.preventDefault(); choose(active); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  root.addEventListener("click", (e) => { if (e.target === root) close(); });
  return { open, close, get isOpen() { return root.classList.contains("open"); } };
})();
document.getElementById("open-palette").addEventListener("click", () => palette.open());

/* ===================== keyboard ===================== */
addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); palette.isOpen ? palette.close() : palette.open(); return; }
  if (mod && e.key.toLowerCase() === "j") { e.preventDefault(); assistant.toggle(); return; }
  if (e.key === "Escape") {
    if (palette.isOpen) palette.close(); else if (drawer.isOpen) drawer.close(); else if (assistant.isOpen) assistant.closeDock();
    return;
  }
  if (typing || mod || e.altKey || palette.isOpen || document.querySelector("dialog[open]")) return;
  if (e.key === "/") {
    const box = document.getElementById("activity-search") || document.querySelector(".research .search");
    e.preventDefault();
    if (box) box.focus(); else palette.open();
  } else if (e.key === "r" || e.key === "R") document.getElementById("refresh").click();
});

/* ===================== start ===================== */
addEventListener("hashchange", () => route());
document.addEventListener("oracle:changed", () => loadBadges());
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") loadBadges(); });
setInterval(() => { if (document.visibilityState === "visible") loadBadges(); }, 60000);
if (!location.hash) history.replaceState(null, "", "#/" + prefs.get("last-route", "home"));
route();
loadBadges();
loadIdentity();
loadAiStatus();
