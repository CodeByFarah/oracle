"use strict";
/* Banking views. Every figure comes from /api/fintech; amounts are integer minor units. */

const FIN = "/api/fintech";

/* ===================== shared pieces ===================== */
function txnSub(t) {
  const parts = [CAT(t.category).label];
  parts.push(timeShort(t.occurred_at));
  if (t.city || (t.country && t.country !== "US")) parts.push([t.city, t.country].filter(Boolean).join(", "));
  return parts.join(", ");
}
function txnAmount(t) {
  const cls = t.status === "declined" ? "declined" : t.amount_minor > 0 ? "credit" : "";
  return h("span", { class: "amount " + cls }, money(t.amount_minor, t.currency, { sign: t.amount_minor > 0 }));
}
function txnRow(t) {
  const flags = [];
  if (t.status === "declined") flags.push(h("span", { class: "chip neg" }, "Declined"));
  else if ((t.fraud_score || 0) >= 0.5) flags.push(h("span", { class: "chip warn" }, "Unusual"));
  if (t.original_currency) flags.push(h("span", { class: "chip" }, money(t.original_amount_minor, t.original_currency)));
  return h("button", { type: "button", class: "row", onclick: () => openTxn(t.id) },
    glyph(t.merchant_name, CAT(t.category).color),
    h("div", { class: "row-main" }, h("div", { class: "row-title" }, t.merchant_name),
      h("div", { class: "row-sub" }, t.status === "declined" && t.decline_reason ? "Declined: " + t.decline_reason : txnSub(t))),
    h("div", { class: "row-end" }, txnAmount(t), flags.length ? h("div", { class: "chips", style: { justifyContent: "flex-end", marginTop: "3px" } }, ...flags) : null));
}
function txnList(items, { grouped = true } = {}) {
  if (!items.length) return h("p", { class: "muted" }, "No transactions.");
  const out = [];
  let day = null;
  for (const t of items) {
    const d = new Date(t.occurred_at).toDateString();
    if (grouped && d !== day) { day = d; out.push(h("div", { class: "day-head" }, dateLong(t.occurred_at))); }
    out.push(txnRow(t));
  }
  return h("div", { class: "list" }, ...out);
}
function accountGlyph(a) {
  const color = { checking: "#14161b", savings: "#0f7b57", wallet: "#2d5bd7", credit: "#ff5f00" }[a.kind] || "#7c8391";
  return h("span", { class: "glyph", style: { background: color }, "aria-hidden": "true" }, a.currency === "USD" ? a.name[0] : a.currency.slice(0, 1) === "E" ? "€" : a.currency === "GBP" ? "£" : a.name[0]);
}
function accountRow(a, base) {
  return h("a", { class: "row", href: `#/accounts/${a.id}` }, accountGlyph(a),
    h("div", { class: "row-main" }, h("div", { class: "row-title" }, a.name),
      h("div", { class: "row-sub" }, `${title(a.kind)} ${a.number_masked}${a.apy_bps ? `, ${(a.apy_bps / 100).toFixed(2)}% APY` : ""}`)),
    h("div", { class: "row-end" }, h("div", { class: "amount" }, money(a.balance_minor, a.currency)),
      a.currency !== base ? h("div", { class: "tiny muted" }, "≈ " + money(a.balance_base_minor, base)) : a.kind === "credit"
        ? h("div", { class: "tiny muted" }, money(a.available_minor, a.currency) + " available") : null));
}
function insightRow(i) {
  return h("div", { class: "insight" }, h("span", { class: "sev " + i.severity, "aria-label": i.severity }),
    h("div", {}, h("h3", {}, i.title), h("p", {}, i.body)),
    i.action ? h("a", { class: "btn small", href: i.action.route }, i.action.label) : null);
}
async function resolveAlert(a, resolution) {
  const out = await attempt(() => api.send("POST", `${FIN}/alerts/${a.id}/resolve`, { resolution }));
  toast(out.actions && out.actions.length ? "Reported. We " + out.actions.join(" and ") + "." : resolution === "genuine" ? "Thanks, marked as yours." : "Dismissed.");
  refreshView();
}
function alertCard(a) {
  const fraud = a.kind === "fraud" && a.subject_type === "transaction";
  return h("article", { class: "alert-card " + a.severity },
    h("div", { class: "chips" }, h("span", { class: "chip " + (SEVERITY_CHIP[a.severity] || "") }, title(a.kind)), h("span", { class: "tiny muted" }, relTime(a.created_at))),
    h("h3", {}, a.title), h("p", {}, a.body),
    h("div", { class: "alert-actions" },
      fraud ? button("It was me", () => resolveAlert(a, "genuine"), "small") : null,
      fraud ? button("Not me: freeze card", () => resolveAlert(a, "fraud"), "small danger") : null,
      fraud ? button("View purchase", () => openTxn(a.subject_id), "small ghost") : button("Dismiss", () => resolveAlert(a, "dismissed"), "small")));
}
async function setCardStatus(card, status) {
  const label = { frozen: "Card frozen", active: "Card unfrozen", cancelled: "Card cancelled" }[status];
  await attempt(() => api.send("POST", `${FIN}/cards/${card}/status`, { status }), label);
}

/* ===================== transaction drawer ===================== */
async function openTxn(id) {
  const body = h("div", {}, h("div", { class: "sk", style: { height: "120px" } }));
  drawer.open("Transaction", body);
  let t, alerts;
  try {
    await meta();
    [t, alerts] = await Promise.all([api.get(`${FIN}/transactions/${encodeURIComponent(id)}`), api.get(`${FIN}/alerts?status=open`)]);
  } catch (e) { body.replaceChildren(empty("Transaction not found.", e.message)); return; }
  const alert = alerts.find(a => a.subject_id === t.id);
  const statusChip = t.status === "declined" ? h("span", { class: "chip neg" }, "Declined")
    : (t.fraud_score || 0) >= 0.5 ? h("span", { class: "chip warn" }, "Approved, flagged") : h("span", { class: "chip pos" }, title(t.status));
  const category = select("category", META.categories.map(c => [c.key, c.label]), t.category);
  category.addEventListener("change", async () => { await attempt(() => api.send("PATCH", `${FIN}/transactions/${t.id}`, { category: category.value }), "Category updated"); });
  const note = h("textarea", { class: "input", rows: 2, maxlength: 280, placeholder: "Add a note" }, t.note || "");
  const explainOut = h("div");
  const explainBtn = button([icon("assistant"), "Explain with Oracle"], async () => {
    explainBtn.disabled = true;
    explainOut.replaceChildren(h("div", { class: "briefing" }, h("span", { class: "typing" }, h("i"), h("i"), h("i"))));
    try {
      const r = await api.send("POST", `/api/ai/transactions/${t.id}/explain`);
      explainOut.replaceChildren(h("div", { class: "briefing" }, h("p", {}, r.text), h("p", { class: "tiny muted", style: { marginTop: "8px" } }, engineLabel(r.engine))));
    } catch (e) { explainOut.replaceChildren(h("p", { class: "form-error" }, e.message)); }
    finally { explainBtn.disabled = false; }
  }, "small");
  const signals = (t.fraud_signals || []);
  body.replaceChildren(h("div", {}, // h() drops null children; replaceChildren would print "null"
    h("div", { style: { display: "flex", gap: "14px", alignItems: "center" } }, glyph(t.merchant_name, CAT(t.category).color),
      h("div", {}, h("div", { style: { fontWeight: 600, fontSize: "17px" } }, t.merchant_name), h("div", { class: "small muted" }, dateLong(t.occurred_at) + ", " + timeShort(t.occurred_at)))),
    h("div", { class: "big-amount" + (t.status === "declined" ? " muted" : t.amount_minor > 0 ? " pos" : "") }, money(t.amount_minor, t.currency, { sign: t.amount_minor > 0 })),
    h("div", { class: "chips" }, statusChip, t.synthetic ? h("span", { class: "chip" }, "Synthetic") : null,
      t.original_currency ? h("span", { class: "chip" }, `Charged ${money(t.original_amount_minor, t.original_currency)}`) : null),
    t.decline_reason ? h("p", { class: "neg", style: { marginTop: "12px" } }, "Declined: " + t.decline_reason) : null,
    alert ? h("div", { class: "mt" }, alertCard(alert)) : null,
    h("dl", { class: "facts" },
      ...[["Account", t.account_name], ["Card", t.card ? `${t.card.nickname} •• ${t.card.last4}` : "–"],
        ["Channel", title(t.channel)], ["Location", [t.city, t.country].filter(Boolean).join(", ") || "–"],
        ["Balance after", t.balance_after_minor === null ? "–" : money(t.balance_after_minor, t.currency)], ["Reference", t.id]]
        .map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", { class: k === "Reference" ? "mono" : "" }, v)))),
    field("Category", category, t.category_source === "ai" ? "Categorised by Oracle AI" : t.category_source === "user" ? "Set by you" : null),
    h("div", { class: "field mt" }, h("span", {}, "Note"), note,
      h("div", {}, button("Save note", async () => { await attempt(() => api.send("PATCH", `${FIN}/transactions/${t.id}`, { note: note.value }), "Note saved"); }, "small"))),
    t.kind === "card_purchase" ? h("div", {},
      h("h3", { class: "sub-head" }, "Fraud screening"),
      h("div", { class: "score-dial" }, h("b", {}, isNum(t.fraud_score) ? Math.round(t.fraud_score * 100) : "–"),
        h("span", { class: "muted" }, `risk out of 100. Flag at 50, decline at 75.`)),
      signals.length ? h("div", { class: "mt" }, ...signals.map(sg => h("div", { class: "signal" },
        h("div", {}, h("b", {}, sg.label), h("span", {}, sg.detail)), meter(sg.contribution / 0.6, sg.contribution > 0.25 ? "neg" : "warn"))))
        : h("p", { class: "muted" }, "Nothing unusual compared with your history."),
      h("div", { class: "mt" }, explainBtn), explainOut) : null,
    t.merchant_history && t.merchant_history.length ? h("div", {}, h("h3", { class: "sub-head" }, `Earlier at ${t.merchant_name}`), txnList(t.merchant_history, { grouped: false })) : null));
}

/* ===================== dialogs ===================== */
async function moveMoneyDialog(prefill = {}) {
  const accounts = await api.get(`${FIN}/accounts`);
  const opts = accounts.filter(a => a.status === "open").map(a => [a.id, `${a.name} · ${money(a.balance_minor, a.currency)}`]);
  const from = select("from", opts, prefill.from || accounts.find(a => a.kind === "checking")?.id, { required: true });
  const to = select("to", opts, prefill.to || accounts.find(a => a.kind === "savings")?.id, { required: true });
  const amount = moneyField("Amount", "amount", "USD", { required: true, value: prefill.amount ?? "" });
  const memo = h("input", { class: "input", name: "memo", maxlength: 140, value: prefill.memo || "" });
  const fx = h("p", { class: "small muted", style: { margin: 0 } });
  const updateFx = () => {
    const a = accounts.find(x => x.id === from.value), b = accounts.find(x => x.id === to.value);
    fx.textContent = a && b && a.currency !== b.currency ? `Converted from ${a.currency} to ${b.currency} at the sandbox reference rate.` : "";
  };
  from.addEventListener("change", updateFx); to.addEventListener("change", updateFx); updateFx();
  openDialog({
    heading: "Move money", lede: "Between your own accounts. Settles instantly.", submitLabel: "Move money",
    body: [field("From", from), field("To", to), amount, fx, field("Memo", memo)],
    async onSubmit(data) {
      const src = accounts.find(a => a.id === data.get("from"));
      if (data.get("from") === data.get("to")) throw new Error("Choose two different accounts.");
      const out = await api.send("POST", `${FIN}/payments`, {
        from_account_id: data.get("from"), to_account_id: data.get("to"),
        amount_minor: toMinor(data.get("amount"), src.currency), memo: data.get("memo") || "",
        idempotency_key: "ui-" + crypto.randomUUID(),
      });
      if (out.status === "failed") throw new Error(out.failure_reason || "The transfer failed.");
      toast(`Moved ${money(out.amount_minor, out.currency)} to ${out.to_name}`);
      refreshView();
    },
  });
}

/* ===================== Home ===================== */
function greeting(name) {
  const hr = new Date().getHours();
  return `${hr < 12 ? "Good morning" : hr < 18 ? "Good afternoon" : "Good evening"}, ${String(name).split(" ")[0]}.`;
}
function bigMoney(minor, currency) {
  const text = money(minor, currency), m = text.match(/^(.*?)([.,]\d{2})$/);
  return m ? [m[1], h("span", { class: "cents" }, m[2])] : [text];
}
function wallet(cards) {
  const list = cards.slice(0, 4), n = list.length;
  return h("div", { class: "wallet", "aria-label": "Your cards" }, ...list.map((c, i) => {
    const k = i - (n - 1) / 2;
    return h("a", { class: "slot", href: `#/cards/${c.id}`, "aria-label": `Open ${c.nickname}`,
      style: { "--rot": `${k * 7}deg`, "--dx": `${k * 44}px`, "--dy": `${Math.abs(k) * 10}px`, zIndex: String(i + 1), animationDelay: `${i * 80}ms` } },
      plastic(c, { small: true }));
  }));
}
async function viewHome() {
  await meta();
  const o = await api.get(`${FIN}/overview`);
  const cur = o.base_currency, m = o.month, firstName = o.customer.display_name;
  const brief = h("div");
  const briefBtn = button([icon("assistant"), "Summarise my month"], async () => {
    briefBtn.disabled = true;
    brief.replaceChildren(h("div", { class: "briefing" }, h("p", { class: "muted" }, "Oracle is reading your accounts…"), h("span", { class: "typing" }, h("i"), h("i"), h("i"))));
    try {
      const r = await api.send("POST", "/api/ai/briefing");
      brief.replaceChildren(h("div", { class: "briefing" }, h("p", {}, r.text), h("p", { class: "tiny muted", style: { marginTop: "8px" } }, engineLabel(r.engine))));
    } catch (e) { brief.replaceChildren(h("p", { class: "form-error" }, e.message)); }
    finally { briefBtn.disabled = false; }
  });
  const urgent = o.alerts.filter(a => a.severity !== "info").slice(0, 3);
  const daily = o.daily.slice(-30).map(d => ({ label: String(Number(d.date.slice(8))), values: [d.spent_minor], partial: false }));
  const health = o.health;
  return h("div", { class: "page" },
    h("section", { class: "hero" },
      h("div", {},
        h("p", { class: "hero-greet" }, greeting(firstName)),
        h("p", { class: "hero-label" }, "Net worth across all accounts"),
        h("div", { class: "hero-figure" }, ...bigMoney(o.totals.net_worth_minor, cur)),
        h("dl", { class: "hero-facts" },
          h("div", {}, h("dt", {}, "Spent this month"), h("dd", {}, money(m.spent_to_date_minor, cur))),
          h("div", {}, h("dt", {}, "On pace for"), h("dd", {}, money(m.projected_minor, cur, { whole: true }))),
          h("div", {}, h("dt", {}, "Came in"), h("dd", { class: "pos" }, money(m.income_minor, cur)))),
        h("div", { class: "quick" },
          button([icon("payments"), "Move money"], () => moveMoneyDialog(), "primary"),
          button([icon("assistant"), "Ask Oracle"], () => assistant.openDock(), ""),
          h("a", { class: "btn", href: "#/cards" }, icon("cards"), "Card controls"))),
      wallet(o.cards)),
    urgent.length ? h("section", { class: "grid cols-3", style: { marginBottom: "20px" } }, ...urgent.map(alertCard)) : null,
    h("section", { style: { display: "flex", gap: "14px", alignItems: "center", flexWrap: "wrap", marginBottom: "20px" } }, briefBtn, brief),
    h("div", { class: "grid cols-7-5" },
      h("div", { class: "stack" },
        panel("Spending, last 30 days", `${money(m.spent_to_date_minor, cur)} this month so far. By this day last month: ${money(m.last_month_to_date_minor, cur)}.`,
          barChart(daily, [{ name: "Spent", color: "var(--ink)" }], { height: 190, format: (v) => money(v, cur, { compact: true }), label: "Daily spending" }),
          h("div", { class: "legend mt" }, ...o.categories.slice(0, 5).map(c => h("div", { class: "legend-row" }, h("i", { style: { background: c.color } }), c.label,
            h("span", { class: "muted small" }, pct(c.share)), h("span", { class: "num" }, money(c.spent_minor, cur)))))),
        panel("Recent activity", null, txnList(o.recent, { grouped: false }), h("div", { class: "mt" }, h("a", { class: "link", href: "#/activity" }, "All transactions")))),
      h("div", { class: "stack" },
        panel("Financial health", "How your saving, cash buffer, budgets, credit and security add up.",
          h("div", { class: "ring-wrap" },
            h("div", { style: { position: "relative", width: "132px", height: "132px" } }, ring(health.score / 100),
              h("div", { style: { position: "absolute", inset: 0, display: "grid", placeItems: "center", textAlign: "center" } },
                h("div", {}, h("div", { class: "ring-label" }, String(health.score)), h("div", { class: "tiny muted" }, health.grade)))),
            h("div", { class: "list" }, ...health.components.map(c => h("div", { style: { padding: "5px 0" } },
              h("div", { style: { display: "flex", justifyContent: "space-between", fontSize: "13.5px" } }, h("span", {}, c.name), h("span", { class: "num muted" }, c.score)),
              meter(c.score / 100, c.score >= 70 ? "pos" : c.score >= 45 ? "warn" : "neg")))))),
        panel("Accounts", null, h("div", { class: "list" }, ...o.accounts.map(a => accountRow(a, cur)))),
        panel("Worth knowing", null, h("div", {}, ...o.insights.map(insightRow)),
          h("div", { class: "mt" }, h("a", { class: "link", href: "#/insights" }, "All insights"))))));
}

/* ===================== Accounts ===================== */
async function viewAccounts(param) {
  await meta();
  if (param) return viewAccount(param);
  const [accounts, o] = await Promise.all([api.get(`${FIN}/accounts`), api.get(`${FIN}/overview`)]);
  const cur = o.base_currency, t = o.totals;
  return h("div", { class: "page" },
    pageHead("Accounts", "Everyday banking, savings, multi-currency wallets and credit, in one place.", button([icon("payments"), "Move money"], () => moveMoneyDialog(), "primary")),
    h("section", { class: "panel" }, h("dl", { class: "stat-row" },
      h("div", { class: "stat" }, h("dt", {}, "Net worth"), h("dd", {}, money(t.net_worth_minor, cur))),
      h("div", { class: "stat" }, h("dt", {}, "Cash and savings"), h("dd", {}, money(t.liquid_minor, cur))),
      h("div", { class: "stat" }, h("dt", {}, "Credit used"), h("dd", {}, money(t.credit_used_minor, cur), h("small", {}, "of " + money(t.credit_limit_minor, cur, { whole: true })))))),
    h("div", { class: "grid cols-2 mt" },
      panel("Banking", null, h("div", { class: "list" }, ...accounts.filter(a => a.kind !== "wallet").map(a => accountRow(a, cur)))),
      panel("Currency wallets", "Converted at sandbox reference rates, not market quotes.", h("div", { class: "list" }, ...accounts.filter(a => a.kind === "wallet").map(a => accountRow(a, cur))))));
}
async function viewAccount(id) {
  const a = await api.get(`${FIN}/accounts/${encodeURIComponent(id)}`);
  const points = a.history.map(p => ({ x: dateShort(p.date + "T12:00:00"), y: toMajor(p.balance_minor, a.currency) }));
  return h("div", { class: "page" },
    h("p", { style: { margin: "14px 0 0" } }, h("a", { class: "link", href: "#/accounts" }, "Accounts")),
    pageHead(a.name, `${title(a.kind)} account ${a.number_masked} in ${a.currency}`, button([icon("payments"), "Move money"], () => moveMoneyDialog({ from: a.id }), "primary")),
    h("section", { class: "panel" },
      h("dl", { class: "stat-row" },
        h("div", { class: "stat" }, h("dt", {}, "Balance"), h("dd", {}, money(a.balance_minor, a.currency))),
        h("div", { class: "stat" }, h("dt", {}, "Available"), h("dd", {}, money(a.available_minor, a.currency))),
        a.apy_bps ? h("div", { class: "stat" }, h("dt", {}, "Interest"), h("dd", {}, (a.apy_bps / 100).toFixed(2) + "%", h("small", {}, "APY"))) : null,
        a.credit_limit_minor ? h("div", { class: "stat" }, h("dt", {}, "Credit limit"), h("dd", {}, money(a.credit_limit_minor, a.currency))) : null),
      h("div", { class: "mt" }, areaChart(points, { height: 240, color: a.kind === "credit" ? "#ff5f00" : "var(--ink)",
        format: (v) => money(toMinor(v, a.currency), a.currency, { compact: true }), label: "Balance over 90 days" }))),
    panel("Recent transactions", null, txnList(a.recent)));
}

/* ===================== Cards ===================== */
async function issueCardDialog() {
  const [accounts, m] = await Promise.all([api.get(`${FIN}/accounts`), meta()]);
  const eligible = accounts.filter(a => ["checking", "credit", "wallet"].includes(a.kind) && a.status === "open");
  openDialog({
    heading: "New virtual card", lede: "Ready to use online right away. Issued by the sandbox issuer, so it can't be charged in the real world.",
    submitLabel: "Create card",
    body: [
      field("Name", h("input", { class: "input", name: "nickname", required: true, maxlength: 40, placeholder: "e.g. Subscriptions" })),
      field("Pays from", select("account_id", eligible.map(a => [a.id, `${a.name} (${a.currency})`]), eligible[0]?.id)),
      field("Type", select("form", [["virtual", "Virtual card"], ["single_use", "Single-use card (expires in a month)"]], "virtual")),
      field("Design", select("design", m.card_designs.map(d => [d, title(d)]), "aurora")),
      moneyField("Limit per purchase (optional)", "limit", "USD"),
    ],
    async onSubmit(data) {
      const card = await api.send("POST", `${FIN}/cards`, {
        account_id: data.get("account_id"), nickname: data.get("nickname"), form: data.get("form"), design: data.get("design"),
        limit_single_minor: data.get("limit") ? toMinor(data.get("limit")) : null,
      });
      toast(`Created ${card.nickname} •• ${card.last4}`);
      go(`#/cards/${card.id}`);
    },
  });
}
async function viewCards(param) {
  await meta();
  const cards = await api.get(`${FIN}/cards`);
  const live = cards.filter(c => c.status !== "cancelled");
  if (!live.length) return h("div", { class: "page" }, pageHead("Cards", null, button([icon("plus"), "New virtual card"], issueCardDialog, "primary")), empty("No cards.", "Create a virtual card to start."));
  const card = cards.find(c => c.id === param) || live[0];
  const recent = await api.get(`${FIN}/transactions?card_id=${encodeURIComponent(card.id)}&limit=8`);
  const locked = card.status === "cancelled";
  const patch = async (body, msg) => { await attempt(() => api.send("PATCH", `${FIN}/cards/${card.id}`, body), msg); };
  const face = plastic(card);
  const controls = [
    ["online", "Online payments", "Shopping on websites and in apps"],
    ["contactless", "Contactless", "Tap to pay in stores"],
    ["international", "Payments abroad", "Purchases in other countries"],
    ["atm", "Cash machines", "Withdrawals at ATMs"],
  ];
  const limitInput = (key, label) => moneyField(label, key, card.currency, { value: card.limits[key] ? toMajor(card.limits[key], card.currency) : "", placeholder: "No limit", min: "1" });
  const limitsForm = h("form", { class: "form", onsubmit: async (e) => {
    e.preventDefault();
    const d = new FormData(e.target), val = (k) => d.get(k) ? toMinor(d.get(k), card.currency) : null;
    await patch({ single_minor: val("single_minor"), daily_minor: val("daily_minor"), monthly_minor: val("monthly_minor") }, "Limits saved");
  } },
    h("div", { class: "limit-grid" }, limitInput("single_minor", "Per purchase"), limitInput("daily_minor", "Per day"), limitInput("monthly_minor", "Per month")),
    h("div", {}, h("button", { type: "submit", class: "btn small", disabled: locked }, "Save limits")));
  const blocked = new Set(card.controls.blocked_categories);
  const chips = h("div", { class: "chips" }, ...META.spending_categories.map(k => {
    const b = h("button", { type: "button", class: "chip toggle-chip", "aria-pressed": String(blocked.has(k)), disabled: locked }, CAT(k).label);
    b.addEventListener("click", async () => {
      blocked.has(k) ? blocked.delete(k) : blocked.add(k);
      b.setAttribute("aria-pressed", String(blocked.has(k)));
      await patch({ blocked_categories: [...blocked] }, blocked.has(k) ? `${CAT(k).label} blocked` : `${CAT(k).label} allowed`);
    });
    return b;
  }));
  const freezeBar = h("div", { class: "freeze-bar" + (card.status === "frozen" ? " on" : "") },
    h("div", { style: { flex: 1 } }, h("b", {}, card.status === "frozen" ? "This card is frozen" : "Freeze card"),
      h("div", { class: "small muted" }, card.status === "frozen" ? "New purchases are declined until you unfreeze it." : "Stops new purchases instantly. Unfreeze any time.")),
    toggle(card.status === "frozen", async (on) => {
      await setCardStatus(card.id, on ? "frozen" : "active");
      face.classList.toggle("frozen", on); face.classList.toggle("active", !on);
      setTimeout(refreshView, 450); // after the frost transition, sync the strip and history
    }, "Freeze card", locked));
  return h("div", { class: "page" },
    pageHead("Cards", "Freeze a card, switch payment types on or off, set limits and block categories. Changes apply to the next purchase.",
      button([icon("plus"), "New virtual card"], issueCardDialog, "primary")),
    h("div", { class: "card-strip", role: "list" }, ...cards.map(c => h("a", { class: "pick", href: `#/cards/${c.id}`, role: "listitem",
      "aria-current": String(c.id === card.id), "aria-label": `${c.nickname} ending ${c.last4}` }, plastic(c, { small: true })))),
    h("section", { class: "card-stage" },
      h("div", {}, h("div", { class: "tilt" }, face),
        h("dl", { class: "facts", style: { maxWidth: "380px" } },
          ...[["Pays from", card.account_name], ["Spent this month", money(card.spent_this_month_minor, card.currency)],
            ["Type", `${title(card.form)} ${card.product}`], ["Issued by", card.issuer]].map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, v))))),
      h("div", { class: "stack" },
        panel(card.nickname, `Card ending ${card.last4}${locked ? ", cancelled" : ""}`,
          freezeBar,
          h("div", { class: "mt" }, ...controls.map(([key, label, hint]) => h("div", { class: "control-row" },
            h("div", {}, h("b", {}, label), h("span", {}, hint)),
            toggle(card.controls[key], (on) => patch({ [key]: on }, `${label} ${on ? "on" : "off"}`), label, locked))))),
        panel("Spending limits", "Purchases above a limit are declined.", limitsForm),
        panel("Blocked categories", "Purchases in a blocked category are always declined on this card.", chips),
        panel("Recent on this card", null, txnList(recent.items, { grouped: false }),
          !locked ? h("div", { class: "mt", style: { display: "flex", justifyContent: "flex-end" } },
            button("Cancel this card", () => openDialog({ heading: `Cancel ${card.nickname}?`, lede: "A cancelled card can't be reactivated. You can create a new one at any time.",
              submitLabel: "Cancel card", danger: true, body: [], async onSubmit() { await setCardStatus(card.id, "cancelled"); refreshView(); } }), "small danger")) : null))));
}

/* ===================== Activity ===================== */
async function viewActivity(_param, query) {
  await meta();
  const state = { q: query.get("q") || "", category: query.get("category") || "", status: query.get("status") || "all", offset: 0 };
  const list = h("div", {}, h("div", { class: "sk", style: { height: "300px" } }));
  const count = h("span", { class: "small muted" });
  const more = button("Show more", () => load(true), "small");
  let items = [];
  async function load(append = false) {
    state.offset = append ? state.offset + 50 : 0;
    const p = new URLSearchParams({ limit: 50, offset: state.offset });
    if (state.q) p.set("q", state.q);
    if (state.category) p.set("category", state.category);
    if (state.status === "flagged") p.set("flagged", "true"); else if (state.status !== "all") p.set("status", state.status);
    try {
      const r = await api.get(`${FIN}/transactions?${p}`);
      items = append ? items.concat(r.items) : r.items;
      count.textContent = `${r.total.toLocaleString()} transactions`;
      list.replaceChildren(items.length ? txnList(items) : empty("Nothing matches.", "Try another search or clear the filters."));
      more.hidden = items.length >= r.total;
    } catch (e) { list.replaceChildren(empty("Couldn't load transactions.", e.message)); }
  }
  const search = h("input", { class: "input", type: "search", placeholder: "Search merchants, cities or notes", value: state.q, "aria-label": "Search transactions", id: "activity-search", style: { maxWidth: "360px" } });
  search.addEventListener("input", debounce(() => { state.q = search.value.trim(); load(); }, 220));
  const cat = select("category", [["", "All categories"], ...META.categories.map(c => [c.key, c.label])], state.category, { "aria-label": "Category", style: { width: "auto" } });
  cat.addEventListener("change", () => { state.category = cat.value; load(); });
  load();
  return h("div", { class: "page" },
    pageHead("Activity", "Every card purchase, transfer and payment, including declined attempts and why they were declined."),
    h("div", { style: { display: "flex", gap: "10px", flexWrap: "wrap", alignItems: "center", marginBottom: "16px" } }, search, cat,
      segmented([["all", "All"], ["posted", "Posted"], ["declined", "Declined"], ["flagged", "Flagged"]], state.status, (v) => { state.status = v; load(); }, "Status"), count),
    h("section", { class: "panel" }, list, h("div", { class: "mt" }, more)));
}

/* ===================== Payments ===================== */
async function addPayeeDialog() {
  await meta();
  openDialog({
    heading: "Add a payee", lede: "Only the last four digits of the account number are kept.", submitLabel: "Add payee",
    body: [
      field("Name", h("input", { class: "input", name: "name", required: true, maxlength: 80 })),
      h("div", { class: "form-row" },
        field("Type", select("kind", [["person", "Person"], ["business", "Business"], ["biller", "Bill"]], "person")),
        field("Category", select("category", META.categories.map(c => [c.key, c.label]), "transfers"))),
      field("Account number", h("input", { class: "input", name: "account_number", required: true, minlength: 4, maxlength: 34, inputmode: "numeric", autocomplete: "off" })),
      field("Bank", h("input", { class: "input", name: "bank_name", value: "Sandbox Bank", maxlength: 80 })),
    ],
    async onSubmit(d) {
      const p = await api.send("POST", `${FIN}/payees`, Object.fromEntries(d));
      toast(`Added ${p.name}`); refreshView();
    },
  });
}
async function viewPayments() {
  await meta();
  const [accounts, payees, payments] = await Promise.all([api.get(`${FIN}/accounts`), api.get(`${FIN}/payees`), api.get(`${FIN}/payments`)]);
  const open = accounts.filter(a => a.status === "open");
  const state = { mode: "transfer", when: "now" };
  const from = select("from", open.map(a => [a.id, `${a.name} · ${money(a.balance_minor, a.currency)}`]), open.find(a => a.kind === "checking")?.id);
  const to = h("select", { class: "input", name: "to" });
  const amount = h("input", { class: "input", name: "amount", type: "number", step: "0.01", min: "0.01", required: true, inputmode: "decimal" });
  const memo = h("input", { class: "input", name: "memo", maxlength: 140, placeholder: "What's it for?" });
  const date = h("input", { class: "input", name: "date", type: "date", min: new Date().toISOString().slice(0, 10) });
  const repeat = select("repeat", [["none", "Doesn't repeat"], ["weekly", "Every week"], ["monthly", "Every month"]], "none");
  const preview = h("p", { class: "small muted", style: { margin: 0 } });
  const submit = h("button", { type: "submit", class: "btn primary" }, "Send money");
  const error = h("p", { class: "form-error", role: "alert" });
  const sched = h("div", { hidden: true, class: "form-row" }, field("Date", date), field("Repeat", repeat));
  function paintTo() {
    const opts = state.mode === "transfer" ? open.filter(a => a.id !== from.value).map(a => [a.id, `${a.name} (${a.currency})`])
      : payees.map(p => [p.id, `${p.name} ${p.account_masked}`]);
    to.replaceChildren(...opts.map(([v, t]) => h("option", { value: v }, t)));
    if (state.mode === "transfer") { const sav = open.find(a => a.kind === "savings" && a.id !== from.value); if (sav) to.value = sav.id; }
    paintPreview();
  }
  function paintPreview() {
    const a = open.find(x => x.id === from.value), b = open.find(x => x.id === to.value);
    const amt = Number(amount.value || 0);
    const parts = [];
    if (a && b && a.currency !== b.currency && amt > 0) {
      const rate = META.reference_fx_usd[a.currency] / META.reference_fx_usd[b.currency];
      parts.push(`They receive about ${money(toMinor(amt * rate, b.currency), b.currency)} at the sandbox reference rate ${rate.toFixed(4)}.`);
    }
    if (state.mode === "pay") parts.push("Pays through the sandbox instant rail; no money leaves this ledger.");
    preview.textContent = parts.join(" ");
    submit.textContent = state.when === "later" ? "Schedule payment" : state.mode === "transfer" ? "Move money" : "Send money";
  }
  from.addEventListener("change", paintTo); to.addEventListener("change", paintPreview); amount.addEventListener("input", paintPreview);
  const form = h("form", { class: "form", onsubmit: async (e) => {
    e.preventDefault(); error.textContent = ""; submit.disabled = true;
    const src = open.find(a => a.id === from.value);
    try {
      const body = { from_account_id: from.value, amount_minor: toMinor(amount.value, src.currency), memo: memo.value, idempotency_key: "ui-" + crypto.randomUUID() };
      body[state.mode === "transfer" ? "to_account_id" : "payee_id"] = to.value;
      if (state.when === "later") {
        if (!date.value) throw new Error("Pick a date.");
        body.scheduled_for = new Date(date.value + "T09:00:00").toISOString();
        body.recurrence = repeat.value;
      }
      const out = await api.send("POST", `${FIN}/payments`, body);
      if (out.status === "failed") throw new Error(out.failure_reason || "Payment failed.");
      toast(out.status === "scheduled" && out.executions === 0 ? `Scheduled ${money(out.amount_minor, out.currency)} to ${out.to_name}` : `Sent ${money(out.amount_minor, out.currency)} to ${out.to_name}`);
      refreshView();
    } catch (err) { error.textContent = err.message; }
    finally { submit.disabled = false; }
  } },
    segmented([["transfer", "Between my accounts"], ["pay", "Pay someone"]], state.mode, (v) => { state.mode = v; paintTo(); }, "Payment type"),
    field("From", from), field("To", to),
    h("label", { class: "field" }, h("span", {}, "Amount"), h("div", { class: "money-input" }, h("span", { class: "cur" }, "$"), amount)),
    preview, field("Memo", memo),
    segmented([["now", "Send now"], ["later", "Schedule"]], state.when, (v) => { state.when = v; sched.hidden = v !== "later"; paintPreview(); }, "When"),
    sched, error, h("div", {}, submit));
  paintTo();
  const scheduled = payments.filter(p => p.status === "scheduled");
  return h("div", { class: "page" },
    pageHead("Payments", "Move money between your accounts and wallets, pay people and bills, and manage what's scheduled."),
    h("div", { class: "grid cols-7-5" },
      panel("Move money", null, form),
      h("div", { class: "stack" },
        panel("Scheduled", scheduled.length ? `${scheduled.length} upcoming` : "Nothing scheduled", h("div", { class: "list" }, ...scheduled.map(p => h("div", { class: "row" },
          glyph(p.to_name, "#2d5bd7"),
          h("div", { class: "row-main" }, h("div", { class: "row-title" }, p.to_name), h("div", { class: "row-sub" }, `${p.recurrence === "none" ? "Once" : title(p.recurrence)}, next ${dateShort(p.scheduled_for)}`)),
          h("div", { class: "row-end" }, h("div", { class: "amount" }, money(p.amount_minor, p.currency)),
            h("button", { type: "button", class: "link small", onclick: async () => { await attempt(() => api.send("POST", `${FIN}/payments/${p.id}/cancel`), "Payment cancelled"); refreshView(); } }, "Cancel")))))),
        panel("Payees", null, h("div", { class: "list" }, ...payees.map(p => h("div", { class: "row" }, glyph(p.name, CAT(p.category).color),
          h("div", { class: "row-main" }, h("div", { class: "row-title" }, p.name), h("div", { class: "row-sub" }, `${title(p.kind)} · ${p.bank_name} ${p.account_masked}`)), h("span")))),
          h("div", { class: "mt" }, button([icon("plus"), "Add payee"], addPayeeDialog, "small"))))),
    panel("History", null, dataTable({ name: "payments", rows: payments, columns: [
      { key: "created_at", label: "Created", value: p => p.created_at, render: p => dateShort(p.created_at) },
      { key: "to_name", label: "To" }, { key: "from_name", label: "From" },
      { key: "amount_minor", label: "Amount", num: true, render: p => money(p.amount_minor, p.currency) },
      { key: "recurrence", label: "Repeats", value: p => p.recurrence === "none" ? "No" : title(p.recurrence) },
      { key: "rail", label: "Rail", value: p => words(p.rail) },
      { key: "status", label: "Status", render: p => h("span", { class: "chip " + ({ completed: "pos", failed: "neg", scheduled: "info" }[p.status] || "") }, title(p.status)) },
    ] })));
}

/* ===================== Insights ===================== */
async function viewInsights() {
  await meta();
  const [a, insights] = await Promise.all([api.get(`${FIN}/analytics?months=6`), api.get(`${FIN}/insights`)]);
  const cur = a.base_currency, f = a.forecast;
  const last = Object.fromEntries(a.last_month.map(r => [r.category, r.spent_minor]));
  const total = a.this_month.reduce((t, r) => t + Math.max(0, r.spent_minor), 0);
  const subs = a.recurring;
  return h("div", { class: "page" },
    pageHead("Insights", "Where your money goes, what's coming up, and what Oracle noticed."),
    h("section", { class: "panel" }, h("dl", { class: "stat-row" },
      h("div", { class: "stat" }, h("dt", {}, "Spent so far"), h("dd", {}, money(f.spent_to_date_minor, cur), h("small", {}, `day ${f.day} of ${f.days_in_month}`))),
      h("div", { class: "stat" }, h("dt", {}, "Projected for the month"), h("dd", {}, money(f.projected_minor, cur, { whole: true }))),
      h("div", { class: "stat" }, h("dt", {}, "Last month"), h("dd", {}, money(f.last_month_minor, cur, { whole: true }))),
      h("div", { class: "stat" }, h("dt", {}, "Recurring each month"), h("dd", {}, money(subs.reduce((t, r) => t + r.monthly_cost_minor, 0), cur, { whole: true }))))),
    h("div", { class: "grid cols-2 mt" },
      panel("This month by category", "Compared with the whole of last month.",
        h("div", { class: "donut-wrap" }, donut(a.this_month.map(r => ({ label: r.label, color: r.color, value: r.spent_minor })), money(total, cur, { whole: true }), "spent"),
          h("div", { class: "legend" }, ...a.this_month.slice(0, 8).map(r => {
            const prev = last[r.category] || 0, delta = prev ? r.spent_minor / prev - 1 : null;
            return h("a", { class: "legend-row", href: `#/activity?category=${r.category}`, style: { textDecoration: "none" } }, h("i", { style: { background: r.color } }), r.label,
              h("span", { class: "small " + (delta > 0.1 ? "neg" : delta < -0.1 ? "pos" : "muted") }, delta === null ? "new" : (delta > 0 ? "+" : "") + pct(delta)),
              h("span", { class: "num" }, money(r.spent_minor, cur)));
          })))),
      panel("Money in and out", "Income in green, spending in dark. The lighter pair is this month so far.",
        barChart(a.cashflow.map(m => ({ label: m.label, values: [m.income_minor, m.spending_minor], partial: m.partial })),
          [{ name: "In", color: "var(--pos)" }, { name: "Out", color: "var(--ink)" }], { height: 240, format: (v) => money(v, cur, { compact: true }), label: "Income and spending by month" }))),
    h("div", { class: "grid cols-7-5 mt" },
      panel("Subscriptions and bills", "Detected from regular charges at the same merchant.", h("div", { class: "list" }, ...subs.map(r => h("div", { class: "row" },
        glyph(r.merchant, r.color),
        h("div", { class: "row-main" }, h("div", { class: "row-title" }, r.merchant), h("div", { class: "row-sub" }, `${title(r.cadence)}, next around ${dateShort(r.next_expected)}`)),
        h("div", { class: "row-end" }, h("div", { class: "amount" }, money(r.amount_minor, cur)),
          r.fixed_price && r.price_change_pct >= 5 ? h("span", { class: "chip warn" }, `Up ${Math.round(r.price_change_pct)}%`) : null))))),
      panel("Top merchants", "Last three months.", h("div", { class: "list" }, ...a.merchants.map(r => h("a", { class: "row", href: `#/activity?q=${encodeURIComponent(r.merchant)}` },
        glyph(r.merchant, r.color), h("div", { class: "row-main" }, h("div", { class: "row-title" }, r.merchant), h("div", { class: "row-sub" }, `${r.count} purchases`)),
        h("span", { class: "amount" }, money(r.spent_minor, cur))))))),
    panel("Everything Oracle noticed", `${insights.length} insights, most important first.`, h("div", { class: "mt" }, ...insights.map(insightRow))));
}

/* ===================== Budgets and goals ===================== */
async function budgetDialog(existing, cur) {
  await meta();
  const used = new Set((await api.get(`${FIN}/budgets`)).map(b => b.category));
  const cats = existing ? [[existing.category, existing.label]] : META.spending_categories.filter(k => !used.has(k)).map(k => [k, CAT(k).label]);
  openDialog({
    heading: existing ? `${existing.label} budget` : "New budget", lede: "A monthly limit. Oracle warns you at 80% and when you go over.",
    submitLabel: "Save budget",
    body: [field("Category", select("category", cats, cats[0]?.[0], { disabled: !!existing })),
      moneyField("Monthly limit", "limit", cur, { required: true, value: existing ? toMajor(existing.limit_minor, cur) : "" })],
    async onSubmit(d) {
      const category = existing ? existing.category : d.get("category");
      await api.send("PUT", `${FIN}/budgets/${category}`, { limit_minor: toMinor(d.get("limit"), cur) });
      toast("Budget saved"); refreshView();
    },
  });
}
async function viewBudgets() {
  await meta();
  const [budgets, goals, accounts] = await Promise.all([api.get(`${FIN}/budgets`), api.get(`${FIN}/goals`), api.get(`${FIN}/accounts`)]);
  const cur = "USD";
  const STATUS = { on_track: ["On track", "pos"], at_risk: ["On pace to go over", "warn"], over: ["Over budget", "neg"] };
  const contribute = (g) => openDialog({
    heading: `Add to ${g.name}`, lede: `${money(g.saved_minor, g.currency)} saved of ${money(g.target_minor, g.currency)}.`, submitLabel: "Add money",
    body: [field("From", select("from", accounts.filter(a => a.kind === "checking" || a.kind === "savings").map(a => [a.id, `${a.name} · ${money(a.balance_minor, a.currency)}`]))),
      moneyField("Amount", "amount", g.currency, { required: true })],
    async onSubmit(d) {
      await api.send("POST", `${FIN}/goals/${g.id}/contribute`, { from_account_id: d.get("from"), amount_minor: toMinor(d.get("amount"), g.currency) });
      toast(`Added to ${g.name}`); refreshView();
    },
  });
  const newGoal = () => openDialog({
    heading: "New savings goal", lede: "Money you add moves into your savings account.", submitLabel: "Create goal",
    body: [field("Name", h("input", { class: "input", name: "name", required: true, maxlength: 60 })), moneyField("Target", "target", cur, { required: true }),
      field("By (optional)", h("input", { class: "input", name: "deadline", type: "date" }))],
    async onSubmit(d) {
      await api.send("POST", `${FIN}/goals`, { name: d.get("name"), target_minor: toMinor(d.get("target"), cur), deadline: d.get("deadline") ? new Date(d.get("deadline") + "T12:00:00").toISOString() : null });
      toast("Goal created"); refreshView();
    },
  });
  return h("div", { class: "page" },
    pageHead("Budgets & goals", "Monthly limits by category, and what you're saving toward.",
      button([icon("plus"), "New budget"], () => budgetDialog(null, cur)), button([icon("plus"), "New goal"], newGoal, "primary")),
    panel("This month's budgets", "The tick on each bar is where you'll land at your current pace.", h("div", { class: "list" }, ...budgets.map(b => {
      const [label, kind] = STATUS[b.status];
      return h("div", { style: { padding: "14px 0", borderBottom: "1px solid var(--line)" } },
        h("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", gap: "12px", marginBottom: "8px", flexWrap: "wrap" } },
          h("div", { style: { display: "flex", alignItems: "center", gap: "10px" } }, h("i", { style: { width: "10px", height: "10px", borderRadius: "3px", background: b.color } }), h("b", {}, b.label), h("span", { class: "chip " + kind }, label)),
          h("div", { style: { display: "flex", alignItems: "center", gap: "12px" } },
            h("span", { class: "num" }, h("b", {}, money(b.spent_minor, cur)), h("span", { class: "muted" }, " of " + money(b.limit_minor, cur, { whole: true }))),
            button("Edit", () => budgetDialog(b, cur), "small ghost"),
            button("Remove", async () => { await attempt(() => api.send("DELETE", `${FIN}/budgets/${b.category}`), "Budget removed"); refreshView(); }, "small ghost"))),
        meter(b.used, kind, b.projected_minor / b.limit_minor),
        h("div", { class: "small muted", style: { marginTop: "6px" } }, b.remaining_minor >= 0 ? `${money(b.remaining_minor, cur)} left` : `${money(-b.remaining_minor, cur)} over`, `, heading for ${money(b.projected_minor, cur, { whole: true })}`));
    }))),
    h("h2", { class: "section-title", style: { margin: "30px 0 14px" } }, "Savings goals"),
    h("div", { class: "grid cols-3" }, ...goals.map(g => panel(null, null,
      h("div", { style: { display: "flex", gap: "16px", alignItems: "center" } },
        h("div", { style: { position: "relative", width: "84px", height: "84px", flex: "none" } }, ring(g.progress, 84, "url(#warm)", 9),
          h("b", { style: { position: "absolute", inset: 0, display: "grid", placeItems: "center", font: "650 18px var(--display)" } }, pct(g.progress))),
        h("div", { style: { minWidth: 0 } }, h("h3", { style: { margin: "0 0 2px", font: "620 17px var(--display)" } }, g.name),
          h("div", { class: "num" }, money(g.saved_minor, g.currency), h("span", { class: "muted" }, " of " + money(g.target_minor, g.currency, { whole: true }))),
          h("div", { class: "small muted" }, g.completed_at ? "Reached" : g.deadline ? "By " + dateShort(g.deadline) : "No deadline"))),
      h("div", { class: "mt" }, button("Add money", () => contribute(g), "small"))))));
}

/* ===================== Security ===================== */
function checkList(checks) {
  return h("div", { class: "checks" }, ...checks.map((c, i) => h("div", { class: "check" + (c.passed ? "" : " fail"), style: { animationDelay: `${i * 90}ms` } },
    h("span", { class: "mark", "aria-hidden": "true" }, c.passed ? "✓" : "✕"), h("span", {}, title(c.name)), h("span", { class: "small muted" }, c.detail))));
}
async function viewSecurity() {
  await meta();
  const [sec, alerts] = await Promise.all([api.get(`${FIN}/security`), api.get(`${FIN}/alerts?status=open`)]);
  const st = sec.stats, cur = "USD";
  const lab = h("div", { class: "mt" });
  const simulate = async (scenario) => {
    lab.replaceChildren(h("div", { class: "sk", style: { height: "140px" } }));
    try {
      const r = await api.send("POST", `${FIN}/sandbox/simulate`, { scenario });
      const d = r.decision, t = r.transaction;
      lab.replaceChildren(
        h("div", { style: { display: "flex", justifyContent: "space-between", gap: "12px", flexWrap: "wrap", alignItems: "center" } },
          h("div", {}, h("b", {}, t.merchant_name), h("span", { class: "muted" }, ` ${[t.city, t.country].filter(Boolean).join(", ")}, ${words(t.channel)}`)),
          h("div", { style: { display: "flex", gap: "10px", alignItems: "center" } }, h("span", { class: "amount" }, money(-t.amount_minor, t.currency)),
            h("span", { class: "chip " + (d.approved ? (d.review_required ? "warn" : "pos") : "neg") }, d.approved ? (d.review_required ? "Approved, flagged" : "Approved") : "Declined"))),
        h("p", { class: "small muted" }, `Risk ${Math.round(d.fraud.score * 100)} out of 100${d.decline_reason ? `. Declined: ${d.decline_reason}` : ""}.`),
        checkList(d.checks),
        h("div", { class: "mt" }, button("Open transaction", () => openTxn(t.id), "small")));
      api.invalidate();
    } catch (e) { lab.replaceChildren(h("p", { class: "form-error" }, e.message)); }
  };
  const actorClass = (a) => a === "system" ? "sys" : a.includes("ai") ? "ai" : "";
  return h("div", { class: "page" },
    pageHead("Security", "Every card purchase is screened against your own history before it's approved. Review what was flagged, and control your cards."),
    h("section", { class: "panel" }, h("dl", { class: "stat-row" },
      h("div", { class: "stat" }, h("dt", {}, "Purchases screened"), h("dd", {}, st.authorizations_30d, h("small", {}, "30 days"))),
      h("div", { class: "stat" }, h("dt", {}, "Blocked as fraud"), h("dd", { class: st.declined_fraud_30d ? "neg" : "" }, st.declined_fraud_30d)),
      h("div", { class: "stat" }, h("dt", {}, "Flagged for review"), h("dd", {}, st.flagged_30d)),
      h("div", { class: "stat" }, h("dt", {}, "Typical risk"), h("dd", {}, Math.round(st.mean_score_30d * 100), h("small", {}, "out of 100"))))),
    alerts.length ? h("div", { class: "mt" }, h("h2", { class: "section-title", style: { marginBottom: "12px" } }, "Needs your attention"),
      h("div", { class: "grid cols-3" }, ...alerts.map(alertCard))) : null,
    h("div", { class: "grid cols-7-5 mt" },
      h("div", { class: "stack" },
        panel("Try the fraud screen", "Run a sandbox purchase through authorization and watch every check.",
          h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap" } },
            button([icon("bolt"), "Everyday purchase"], () => simulate("normal")), button([icon("risk"), "Suspicious purchase"], () => simulate("suspicious"), "danger")), lab),
        panel("Flagged and declined", "Purchases that scored 50 or more in the last 30 days.", txnList(sec.flagged, { grouped: false }))),
      h("div", { class: "stack" },
        panel("Cards", null, h("div", { class: "list" }, ...sec.cards.map(c => h("div", { class: "control-row" },
          h("div", {}, h("b", {}, `${c.nickname} •• ${c.last4}`), h("span", {}, c.status === "frozen" ? "Frozen" : `${c.controls.online ? "Online on" : "Online off"}, ${c.controls.international ? "abroad on" : "abroad off"}`)),
          toggle(c.status === "frozen", (on) => setCardStatus(c.id, on ? "frozen" : "active"), `Freeze ${c.nickname}`))))),
        panel("Ledger integrity", "Each balance must equal its opening balance plus every posted transaction.", h("div", { class: "list" }, ...sec.ledger.map(r => h("div", { class: "control-row" },
          h("div", {}, h("b", {}, r.name), h("span", {}, money(r.balance_minor, r.currency))), h("span", { class: "chip " + (r.balanced ? "pos" : "neg") }, r.balanced ? "Reconciled" : "Mismatch"))))),
        panel("Providers", "Regulated services run in sandbox mode.", h("div", { class: "list" }, ...sec.providers.map(p => h("div", { class: "control-row" },
          h("div", {}, h("b", {}, words(p.name)), h("span", {}, p.detail)), h("span", { class: "chip info" }, "Sandbox"))))))),
    panel("Security log", "Append-only record of card, payment and alert changes.", h("div", { class: "timeline" }, ...sec.events.map(e => h("div", { class: "event " + actorClass(e.actor) },
      h("time", { datetime: e.occurred_at }, relTime(e.occurred_at)), h("i"),
      h("div", {}, h("b", {}, title(e.action.replace(".", " "))), h("span", { class: "muted" }, ` by ${e.actor}`)))))));
}
