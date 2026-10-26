"use strict";
/* Oracle, the AI financial assistant: streaming chat shared by the side dock and the full page. */

const TOOL_LABELS = {
  get_balances: "Checked your balances",
  spending_summary: "Looked at your spending",
  search_transactions: "Searched transactions",
  budget_status: "Checked your budgets",
  recurring_payments: "Found recurring payments",
  cash_forecast: "Projected this month",
  list_insights: "Read your insights",
  fraud_activity: "Reviewed fraud screening",
  list_cards: "Checked your cards",
  savings_goals: "Checked your goals",
  financial_health: "Checked your health score",
  explain_transaction: "Inspected a transaction",
  propose_freeze_card: "Suggested freezing a card",
  propose_budget: "Suggested a budget",
  propose_transfer: "Suggested a transfer",
};
const SUGGESTIONS = [
  "How much did I spend on dining this month?",
  "Is there anything suspicious on my cards?",
  "What subscriptions am I paying for?",
  "How can I save more?",
];

function engineLabel(engine) {
  if (!engine || engine === "offline") return "Answered by the offline engine: instant and rule-based, no model involved.";
  return `Answered by ${engine.replace(/^ollama:/, "")}, running locally on this machine. Your data never left it.`;
}

/* Minimal, safe Markdown: paragraphs, bullet lists, **bold**, `code`. Builds DOM, never HTML. */
function inline(text) {
  const out = [];
  const re = /\*\*(.+?)\*\*|`([^`]+)`/g;
  let i = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > i) out.push(text.slice(i, m.index));
    out.push(m[1] !== undefined ? h("strong", {}, m[1]) : h("code", {}, m[2]));
    i = m.index + m[0].length;
  }
  if (i < text.length) out.push(text.slice(i));
  return out;
}
function renderMarkdown(text) {
  const nodes = [];
  let list = null, para = [];
  const flush = () => { if (para.length) { nodes.push(h("p", {}, ...inline(para.join(" ")))); para = []; } };
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const bullet = line.match(/^(?:[-*•]|\d+[.)]|✅|⚠️)\s+(.*)$/u);
    if (bullet) { flush(); if (!list) { list = h("ul"); nodes.push(list); } list.append(h("li", {}, ...inline(bullet[1]))); continue; }
    list = null;
    if (!line) { flush(); continue; }
    const heading = line.match(/^#{1,4}\s+(.*)$/);
    if (heading) { flush(); nodes.push(h("p", {}, h("strong", {}, heading[1]))); continue; }
    para.push(line);
  }
  flush();
  return nodes;
}

async function runAction(action, btn) {
  btn.disabled = true;
  try {
    if (action.type === "freeze_card") await api.send("POST", `/api/fintech/cards/${action.card_id}/status`, { status: "frozen" });
    else if (action.type === "set_budget") await api.send("PUT", `/api/fintech/budgets/${action.category}`, { limit_minor: action.limit_minor });
    else if (action.type === "transfer") {
      const out = await api.send("POST", "/api/fintech/payments", { from_account_id: action.from_account_id, to_account_id: action.to_account_id,
        amount_minor: action.amount_minor, memo: action.memo || "Suggested by Oracle", idempotency_key: "ai-" + crypto.randomUUID() });
      if (out.status === "failed") throw new Error(out.failure_reason || "Transfer failed");
    } else throw new Error("Unknown action");
    btn.replaceWith(h("span", { class: "chip pos" }, "Done: " + action.label));
    toast("Done: " + action.label);
    refreshView();
  } catch (e) { btn.disabled = false; toast(e.message, "error"); }
}

function createChat({ dock = false, onConversation = null } = {}) {
  const state = { conversationId: null, busy: false, controller: null, engine: prefs.get("chat-engine", "auto") };
  const orb = h("span", { class: "orb", "aria-hidden": "true" });
  const log = h("div", { class: "chat-log", "aria-live": "polite" });
  const input = h("textarea", { rows: 1, placeholder: "Ask about your money…", "aria-label": "Message Oracle", maxlength: 2000 });
  const send = h("button", { type: "submit", class: "send", "aria-label": "Send" }, icon("send"));
  const statusText = h("span", {});
  const welcome = () => h("div", { class: "welcome" }, h("h3", {}, "Ask anything about your money."),
    h("p", {}, "Oracle reads your accounts, cards and budgets to answer. It can suggest actions, and you confirm each one."));
  const suggestions = h("div", { class: "suggest" }, ...SUGGESTIONS.map(q => h("button", { type: "button", onclick: () => ask(q) }, q)));
  const engineSeg = segmented([["auto", "Local AI"], ["offline", "Instant"]], state.engine, (v) => { state.engine = v; prefs.set("chat-engine", v); }, "Answer engine");
  const form = h("form", { class: "composer", onsubmit: (e) => { e.preventDefault(); ask(input.value); } }, input, send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(input.value); } });
  input.addEventListener("input", () => { input.style.height = "auto"; input.style.height = Math.min(140, input.scrollHeight) + "px"; });
  const newChat = h("button", { type: "button", class: "btn small ghost", onclick: () => reset() }, "New chat");
  const closeBtn = dock ? h("button", { type: "button", class: "icon-btn", "aria-label": "Close assistant", onclick: () => assistant.closeDock() }, icon("close")) : null;
  const root = h("div", { class: "chat" },
    h("div", { class: "chat-head" }, orb, h("div", { style: { flex: 1, minWidth: 0 } }, h("h2", {}, "Oracle"), h("div", { class: "tiny muted" }, statusText)), newChat, closeBtn),
    log, h("div", { class: "chat-foot" }, suggestions, form, h("div", { class: "chat-opts" }, engineSeg, h("span", {}, "Suggestions need your confirmation."))));

  function paintStatus(st) {
    statusText.textContent = st && st.engine !== "offline" ? `Local model ${st.model}` : "Offline engine";
  }
  function reset() {
    if (state.controller) state.controller.abort();
    state.conversationId = null; log.replaceChildren(welcome()); suggestions.hidden = false; input.focus();
  }
  function scroll() { log.scrollTop = log.scrollHeight; }
  function assistantBubble() {
    const metaRow = h("div", { class: "meta" }), body = h("div", { class: "body" }, h("span", { class: "typing", "aria-label": "Thinking" }, h("i"), h("i"), h("i")));
    const actions = h("div", { class: "actions" }), foot = h("div", { class: "engine" });
    const el = h("div", { class: "msg assistant" }, metaRow, body, actions, foot);
    return { el, metaRow, body, actions, foot };
  }
  function renderActions(container, list, live = true) {
    container.replaceChildren(...list.map(a => {
      if (!live) return h("span", { class: "chip" }, "Suggested: " + a.label);
      const b = h("button", { type: "button", class: "btn small primary" }, a.label);
      b.addEventListener("click", () => runAction(a, b));
      return b;
    }));
  }
  async function ask(text) {
    text = String(text || "").trim();
    if (!text || state.busy) return;
    state.busy = true; send.disabled = true; input.value = ""; input.style.height = "auto"; suggestions.hidden = true;
    log.querySelector(".welcome")?.remove();
    log.append(h("div", { class: "msg user" }, text));
    const bubble = assistantBubble();
    log.append(bubble.el); scroll();
    orb.classList.add("thinking");
    let raw = "", pending = false;
    const paint = () => { pending = false; bubble.body.replaceChildren(...renderMarkdown(raw)); scroll(); };
    state.controller = new AbortController();
    try {
      await api.stream("/api/ai/chat", { message: text, conversation_id: state.conversationId, engine: state.engine }, (e) => {
        if (e.type === "meta") { state.conversationId = e.conversation_id; }
        else if (e.type === "tool") { bubble.metaRow.append(h("span", { class: "tool-chip" }, TOOL_LABELS[e.name] || title(e.name))); scroll(); }
        else if (e.type === "token") { raw += e.text; if (!pending) { pending = true; requestAnimationFrame(paint); } }
        else if (e.type === "reset") { raw = ""; }
        else if (e.type === "notice") { bubble.metaRow.append(h("div", { class: "notice" }, e.text)); }
        else if (e.type === "actions") { renderActions(bubble.actions, e.actions); }
        else if (e.type === "done") { bubble.foot.textContent = engineLabel(e.engine); if (onConversation) onConversation(e.conversation_id); }
      }, state.controller.signal);
      paint();
    } catch (e) {
      if (e.name !== "AbortError") bubble.body.replaceChildren(h("p", { class: "form-error" }, "Oracle couldn't answer: " + e.message));
    } finally {
      state.busy = false; send.disabled = false; orb.classList.remove("thinking"); state.controller = null; scroll();
    }
  }
  async function load(id) {
    const conv = await api.get(`/api/ai/conversations/${encodeURIComponent(id)}`);
    state.conversationId = conv.id; suggestions.hidden = true;
    log.replaceChildren(...conv.messages.map(m => {
      if (m.role === "user") return h("div", { class: "msg user" }, m.content);
      const b = assistantBubble();
      b.body.replaceChildren(...renderMarkdown(m.content));
      for (const t of (m.meta.tools || [])) b.metaRow.append(h("span", { class: "tool-chip" }, TOOL_LABELS[t.name] || title(t.name)));
      renderActions(b.actions, m.meta.actions || [], false);
      b.foot.textContent = engineLabel(m.meta.engine);
      return b.el;
    }));
    scroll();
  }
  reset();
  return { root, ask, load, reset, paintStatus, focus: () => input.focus() };
}

const assistant = (() => {
  const dockEl = document.getElementById("assistant-dock");
  let chat = null, status = null;
  function ensure() {
    if (!chat) { chat = createChat({ dock: true }); dockEl.replaceChildren(chat.root); chat.paintStatus(status); }
    return chat;
  }
  return {
    get isOpen() { return dockEl.classList.contains("open"); },
    openDock(question) {
      if (location.hash.startsWith("#/assistant")) { if (question && window.pageChat) window.pageChat.ask(question); return; }
      const c = ensure(); dockEl.hidden = false;
      requestAnimationFrame(() => dockEl.classList.add("open"));
      setTimeout(() => (question ? c.ask(question) : c.focus()), 60);
    },
    closeDock() { dockEl.classList.remove("open"); setTimeout(() => { if (!dockEl.classList.contains("open")) dockEl.hidden = true; }, 330); },
    toggle() { this.isOpen ? this.closeDock() : this.openDock(); },
    setStatus(st) { status = st; if (chat) chat.paintStatus(st); if (window.pageChat) window.pageChat.paintStatus(st); },
  };
})();

async function viewAssistant(param) {
  const list = h("div", { class: "conv-list" });
  const chat = createChat({ onConversation: () => paintList() });
  window.pageChat = chat;
  chat.paintStatus(window.aiStatus);
  async function paintList() {
    const convs = await api.get("/api/ai/conversations").catch(() => []);
    list.replaceChildren(h("button", { type: "button", onclick: () => { chat.reset(); go("#/assistant"); } }, "+ New conversation"),
      ...convs.map(c => h("button", { type: "button", "aria-current": String(c.id === param), title: c.title,
        onclick: () => go(`#/assistant/${c.id}`) }, c.title)));
  }
  paintList();
  if (param) chat.load(param).catch(() => toast("Conversation not found", "error"));
  const st = window.aiStatus;
  return h("div", { class: "page" },
    pageHead("Assistant", st && st.engine !== "offline"
      ? `Oracle runs ${st.model} on this machine through Ollama. Questions and answers stay local.`
      : "The local model isn't running, so the instant offline engine answers. Start Ollama to use the local AI model."),
    h("div", { class: "assistant-page" },
      h("section", { class: "panel convs" }, h("div", { class: "panel-head", style: { padding: "16px 16px 0" } }, h("h2", {}, "Conversations")), list),
      h("section", { class: "panel" }, chat.root)));
}
