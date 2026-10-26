# ADR 0003: Banking domain on sandbox rails, and a local open-source AI

Status: accepted (2026-10-26)

## Context

ORACLE gains a consumer-banking workspace (accounts, wallets, cards, payments, analytics, fraud
screening) and an AI assistant, alongside the research platform. Card issuing and moving money
are regulated activities that need a licensed provider and production credentials; none is
configured. The project runs on a $0 budget (ADR 0002), and the assistant reads financial data.

## Decisions

1. **Sandbox providers only.** `oracle.fintech.providers` defines `CardIssuer` and
   `PaymentRail` interfaces with sandbox implementations. `Settings.fintech_provider` accepts
   only `"sandbox"`; adding a production adapter is a deliberate code change, not a config flag.
   Cards store a last-four and an opaque token, never a full card number. Payees keep only the
   last four digits of an account number.
2. **A real ledger, in integer minor units.** Every balance change is a posted transaction in
   the same database transaction; `reconcile` checks `opening + sum(posted) == balance`.
   Postings and payments accept idempotency keys. Declined authorizations are recorded with
   their reasons but never change a balance.
3. **Deterministic, explainable fraud screening.** Scores are computed from the customer's own
   history (robust z of amount per merchant or category, larger-than-ever, velocity of attempts,
   new country, unusual hour, high-risk MCC, card-not-present size) and combined as
   `1 - prod(1 - w*s)`. Every signal is stored with its contribution. Flagged purchases are kept
   out of the baseline until the customer confirms them, so one approved fraud cannot make the
   next look normal. Thresholds: flag at 0.50, decline at 0.75.
4. **The money path never depends on AI.** An import contract forbids `ledger`,
   `authorization`, `fraud`, `providers`, `events` and `models` from importing `oracle.ai` or
   the assistant. The assistant reads through the service and can only *propose* actions; each
   proposal is a button the customer clicks, executed by the normal endpoint and audit trail.
5. **Local open-source model via Ollama.** `oracle.ai.ollama` talks to a loopback Ollama
   server; `Settings` refuses a non-loopback URL unless `ORACLE_AI_ALLOW_REMOTE=true`. The
   default model is chosen automatically from installed small tool-calling models.
6. **Grounding over cleverness.** Small local models invent numbers when asked to fetch them.
   The assistant therefore (a) routes each question to a handful of relevant tools, (b) runs the
   main read tools *before* the model answers and gives it their results, and (c) adds
   deterministic action proposals when the model describes an action without proposing it. An
   offline intent engine answers from the same tools when no model is available or it fails.
7. **The research agent's injected-LLM seam is used, not replaced.** `oracle loop run-once
   --planner llm` injects the local model into the existing `LLMPlanner`, wrapped in a
   `FallbackPlanner` that reverts to the deterministic planner when a plan is rejected.
8. **SQLite by default, PostgreSQL supported.** The ledger defaults to a SQLite file so the
   banking app needs no setup. Migration 0004 creates the same tables on PostgreSQL (a frozen
   DDL snapshot) and makes `fin_security_events` append-only with a trigger.
9. **Write endpoints on an unauthenticated loopback server.** The server still binds loopback
   only. Every request must carry a loopback `Host` header (DNS-rebinding defence), and every
   state-changing request needs an `X-Oracle-Client` header, which a cross-site page cannot send
   without a CORS preflight that is never granted.

## Consequences

- Nothing here can move real money or issue a real card; a production integration needs a new
  adapter behind the existing interfaces, plus authentication, which this ADR does not provide.
- On a CPU-only machine a 4B model answers in roughly 20 to 60 seconds; the UI streams tokens
  and offers the instant offline engine.
- The assistant's quality is bounded by the tools: questions outside them get "I don't know"
  from the model or the help text from the offline engine.
