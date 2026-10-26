"use strict";
/* Research workspace: the original ORACLE research dashboard views, restyled. Read-only. */

function tile(k, v, sub, frac) {
  return h("div", { class: "tile" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v), h("div", { class: "s" }, sub),
    isNum(frac) ? meter(frac) : null);
}
function header(eyebrow, heading, lede) {
  return [eyebrow ? h("p", { class: "eyebrow" }, eyebrow) : null, h("h1", {}, heading), lede ? h("p", { class: "lede" }, lede) : null];
}
const band = (cls, ...kids) => h("div", { class: "band " + cls }, h("div", { class: "wrap" }, ...kids));
const card = (...kids) => h("div", { class: "card" }, ...kids);
const researchCtx = {};

/* Largest-Triangle-Three-Buckets downsampling: keeps the visual shape of a long series. */
function lttb(points, threshold) {
  const n = points.length;
  if (threshold >= n || threshold < 3) return points;
  const out = [points[0]], every = (n - 2) / (threshold - 2);
  let a = 0;
  for (let i = 0; i < threshold - 2; i++) {
    const start = Math.floor((i + 1) * every) + 1, end = Math.min(Math.floor((i + 2) * every) + 1, n);
    let ax = 0, ay = 0;
    for (let j = start; j < end; j++) { ax += points[j][0]; ay += points[j][1]; }
    ax /= (end - start); ay /= (end - start);
    const rs = Math.floor(i * every) + 1, re = Math.floor((i + 1) * every) + 1;
    let best = -1, pick = rs;
    for (let j = rs; j < re; j++) {
      const area = Math.abs((points[a][0] - ax) * (points[j][1] - points[a][1]) - (points[a][0] - points[j][0]) * (ay - points[a][1]));
      if (area > best) { best = area; pick = j; }
    }
    out.push(points[pick]); a = pick;
  }
  out.push(points[n - 1]);
  return out;
}
function nearest(xs, x) {
  let lo = 0, hi = xs.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (xs[mid] < x) lo = mid; else hi = mid; }
  return x - xs[lo] < xs[hi] - x ? lo : hi;
}

/**
 * Interactive price chart: close line with gradient fill, volume histogram and a crosshair
 * tooltip. The x axis is trading time (bar index), so nights, weekends and holidays are not
 * drawn as flat lines. Redraws on resize; downsampled to about one point per pixel of width.
 */
function priceChart(series, { interval, onHover }) {
  const host = h("div", { class: "price", role: "img", "aria-label": `Price chart of ${series.length} bars` });
  const tip = h("div", { class: "tip" });
  let animated = false;
  function draw() {
    if (!host.isConnected) return;
    const W = Math.max(320, host.clientWidth), H = host.clientHeight || 360;
    const padL = 4, padR = 58, padT = 12, padB = 26, volH = 54, gap = 10;
    const plotW = W - padL - padR, priceH = H - padT - padB - volH - gap;
    const pts = lttb(series.map((b, i) => [i, b[4], b]), Math.max(60, Math.floor(plotW)));
    const last = series.length - 1, xs = pts.map(p => p[0]), span = series[last][0] - series[0][0];
    let lo = Infinity, hi = -Infinity, vmax = 0;
    for (const p of pts) { lo = Math.min(lo, p[2][3]); hi = Math.max(hi, p[2][2]); vmax = Math.max(vmax, p[2][5]); }
    const padY = (hi - lo) * 0.08 || 1, ticks = niceTicks(lo - padY, hi + padY, 5);
    const y0 = Math.min(ticks[0], lo - padY), y1 = Math.max(ticks[ticks.length - 1], hi + padY);
    const X = (i) => padL + (last === 0 ? plotW / 2 : (i / last) * plotW);
    const Y = (v) => padT + (1 - (v - y0) / (y1 - y0)) * priceH;
    const up = series[series.length - 1][4] >= series[0][4];
    const hex = up ? "#1a8e3a" : "#e30000";
    const line = pts.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join("");
    const base = (padT + priceH).toFixed(1);
    const area = `${line}L${X(last).toFixed(1)},${base}L${X(0).toFixed(1)},${base}Z`;
    const gid = "g" + Math.random().toString(36).slice(2, 8);
    const volTop = padT + priceH + gap, bw = Math.max(1, plotW / pts.length - 0.6);
    const daily = interval.endsWith("d") || span > 20 * 86400000;
    const xTickCount = Math.max(2, Math.floor(plotW / 130));
    const xTicks = Array.from({ length: xTickCount }, (_, i) => Math.round((last * (i + 0.5)) / xTickCount));
    const xLabel = (t) => new Date(t).toLocaleString(undefined, daily
      ? { month: "short", day: "numeric", year: span > 300 * 86400000 ? "2-digit" : undefined }
      : { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    const visible = ticks.filter(v => v >= y0 && v <= y1);
    const xline = s("line", { class: "xline", y1: padT, y2: volTop + volH, visibility: "hidden" });
    const xdot = s("circle", { class: "xdot", r: 4.5, fill: hex, visibility: "hidden" });
    const overlay = s("rect", { x: padL, y: 0, width: plotW, height: H, fill: "transparent" });
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}` },
      s("defs", {}, s("linearGradient", { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 },
        s("stop", { offset: "0", "stop-color": hex, "stop-opacity": ".18" }), s("stop", { offset: "1", "stop-color": hex, "stop-opacity": "0" }))),
      s("g", { class: "grid" }, ...visible.map(v => s("line", { x1: padL, x2: padL + plotW, y1: Y(v).toFixed(0), y2: Y(v).toFixed(0) }))),
      s("g", { class: "axis" },
        ...visible.map(v => s("text", { x: W - padR + 10, y: Y(v) + 4 }, fmt(v, 2))),
        ...xTicks.map(i => s("text", { x: X(i), y: H - 6, "text-anchor": "middle" }, xLabel(series[i][0])))),
      s("g", { class: "vol" }, ...pts.map(p => {
        const hgt = vmax ? (p[2][5] / vmax) * volH : 0;
        return s("rect", { x: (X(p[0]) - bw / 2).toFixed(1), y: (volTop + volH - hgt).toFixed(1), width: bw.toFixed(1), height: hgt.toFixed(1) });
      })),
      s("path", { d: area, fill: `url(#${gid})` }),
      s("path", { class: animated ? null : "line", pathLength: 1, d: line, fill: "none", stroke: hex, "stroke-width": 1.8, "stroke-linejoin": "round", "stroke-linecap": "round" }),
      xline, xdot, overlay);
    animated = true;
    function move(ev) {
      const box = svg.getBoundingClientRect();
      const px = (ev.clientX - box.left) * (W / box.width);
      const i = nearest(xs, clamp((px - padL) / plotW, 0, 1) * last);
      const b = pts[i][2], x = X(pts[i][0]);
      for (const [k, v] of [["x1", x], ["x2", x], ["visibility", "visible"]]) xline.setAttribute(k, v);
      for (const [k, v] of [["cx", x], ["cy", Y(pts[i][1])], ["visibility", "visible"]]) xdot.setAttribute(k, v);
      tip.replaceChildren(h("b", {}, new Date(b[0]).toLocaleString(undefined, daily ? { dateStyle: "medium" } : { dateStyle: "medium", timeStyle: "short" })),
        ...[["Open", b[1]], ["High", b[2]], ["Low", b[3]], ["Close", b[4]]].map(([k, v]) => h("div", { class: "row" }, h("span", { class: "lbl" }, k), h("span", {}, fmt(v, 2)))),
        h("div", { class: "row" }, h("span", { class: "lbl" }, "Volume"), h("span", {}, Number(b[5]).toLocaleString())));
      const left = (x / W) * box.width;
      tip.style.left = (left > box.width * 0.6 ? left - tip.offsetWidth - 14 : left + 14) + "px";
      tip.style.opacity = "1";
      if (onHover) onHover(b);
    }
    function leave() {
      xline.setAttribute("visibility", "hidden"); xdot.setAttribute("visibility", "hidden"); tip.style.opacity = "0";
      if (onHover) onHover(null);
    }
    overlay.addEventListener("pointermove", move);
    overlay.addEventListener("pointerdown", move);
    overlay.addEventListener("pointerleave", leave);
    host.replaceChildren(svg, tip);
  }
  // Observing fires once the host is laid out, then on every width change.
  const ro = new ResizeObserver(debounce(draw, 50));
  ro.observe(host);
  host.dispose = () => ro.disconnect();
  return host;
}

/* Correlation matrix from significant undirected links; pairs that failed the threshold stay blank. */
function heatmap(edges) {
  const corr = edges.filter(e => e.direction === "undirected");
  const names = [...new Set(corr.flatMap(e => [e.source, e.target]))].sort();
  if (names.length < 2) return null;
  const key = (a, b) => [a, b].sort().join("|");
  const m = new Map(corr.map(e => [key(e.source, e.target), e]));
  const n = names.length, cell = 48, lab = 48, W = lab + n * cell, H = lab + n * cell;
  const color = (v) => {
    const a = clamp(Math.abs(v), 0, 1), [r, g, b] = v >= 0 ? [0, 113, 227] : [217, 48, 37];
    const mix = (c) => Math.round(242 + (c - 242) * a);
    return `rgb(${mix(r)},${mix(g)},${mix(b)})`;
  };
  const cells = [];
  names.forEach((a, i) => names.forEach((b, j) => {
    const e = i === j ? null : m.get(key(a, b));
    const v = i === j ? 1 : e ? e.strength : null;
    const rect = s("rect", { class: "cell", x: lab + j * cell, y: lab + i * cell, width: cell, height: cell, rx: 6, fill: v === null ? "#ececf0" : color(v) });
    rect.append(s("title", {}, i === j ? a : e ? `${a} ↔ ${b}: r = ${fmt(e.strength)}, p = ${sci(e.p_value)}, stability ${pct(e.stability)}` : `${a} ↔ ${b}: not significant`));
    cells.push(rect);
    if (v !== null && i !== j) cells.push(s("text", { class: Math.abs(v) > 0.55 ? "on" : null, x: lab + j * cell + cell / 2, y: lab + i * cell + cell / 2 + 4, "text-anchor": "middle", "pointer-events": "none" }, fmt(v, 2)));
  }));
  return h("div", { class: "heat" },
    s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Correlation matrix of assets" },
      ...names.map((a, i) => s("text", { x: lab - 8, y: lab + i * cell + cell / 2 + 4, "text-anchor": "end" }, a)),
      ...names.map((a, j) => s("text", { x: lab + j * cell + cell / 2, y: lab - 10, "text-anchor": "middle" }, a)),
      ...cells),
    h("div", { class: "scale" }, h("span", {}, "−1"), h("i"), h("span", {}, "+1")),
    h("p", { class: "fine", style: "margin:8px 0 0" }, "Grey cells did not pass the significance threshold."));
}

/* p-value histogram against the flat line expected if no effect exists anywhere. */
function pHistogram(pvalues, bins = 20) {
  const counts = new Array(bins).fill(0);
  for (const p of pvalues) counts[Math.min(bins - 1, Math.floor(clamp(p, 0, 1) * bins))]++;
  const expect = pvalues.length / bins, max = Math.max(expect * 1.5, ...counts);
  const W = 640, H = 220, padL = 34, padB = 26, padT = 8, bw = (W - padL) / bins;
  const Y = (c) => padT + (1 - c / max) * (H - padT - padB);
  return h("div", { class: "hist" },
    s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Histogram of p-values" },
      ...counts.map((c, i) => {
        const r = s("rect", { class: "bin" + (i === 0 ? " sig" : ""), x: padL + i * bw + 1, y: Y(c), width: bw - 2, height: Math.max(0, H - padB - Y(c)), rx: 2 });
        r.append(s("title", {}, `p from ${(i / bins).toFixed(2)} to ${((i + 1) / bins).toFixed(2)}: ${c} tests`));
        return r;
      }),
      s("line", { class: "expect", x1: padL, x2: W, y1: Y(expect), y2: Y(expect) }),
      s("text", { class: "halo", x: padL + 6, y: Y(expect) - 6 }, "expected if nothing is real"),
      ...[0, 0.25, 0.5, 0.75, 1].map(v => s("text", { x: padL + v * (W - padL), y: H - 6, "text-anchor": v === 0 ? "start" : v === 1 ? "end" : "middle" }, v.toFixed(2))),
      s("text", { x: padL - 6, y: Y(max) + 10, "text-anchor": "end" }, String(Math.round(max))),
      s("text", { x: padL - 6, y: H - padB, "text-anchor": "end" }, "0")));
}

/* ===================== Views ===================== */
const STATUS = { candidate: "Candidate", validated_oos: "Validated out of sample", failed_oos: "Failed out of sample",
  survived_adversarial: "Passed adversarial checks", rejected_adversarial: "Rejected by adversarial checks", paper_trading: "Paper trading", retired: "Retired" };
const statusClass = (st) => /reject|fail|retired/.test(st) ? "bad" : /surviv|validat|paper/.test(st) ? "good" : "";
const describeCandidate = (c) => (c.conditions || []).map(k => [k.variable, k.side].filter(Boolean).map(words).join(" ")).join(" and ") + ` · ${c.horizon} bars`;
const label = (text) => h("p", { class: "label" }, text);

async function viewOverview(ctx) {
  const [w, symbols] = await Promise.all([api.get("/api/world"), api.get("/api/symbols").catch(() => [])]);
  const snap = w.snapshot, regime = snap.market_regime;
  const edges = [...(snap.cross_asset_signals || [])].sort((a, b) => Math.abs(b.strength) - Math.abs(a.strength));
  const anomalies = snap.anomalies || [];
  const alt = (() => { let i = 0; return () => (i++ % 2 ? "alt" : ""); })();
  alt(); // the hero band is white
  const nodes = [
    band("", ...header("Market reading · " + new Date(snap.as_of).toLocaleString(),
      regime ? title(regime.label) + "." : "Regime not yet estimated.",
      `Volatility is ${words(snap.volatility_state)}. Liquidity is ${words(snap.liquidity_state)}. ` +
      (regime ? `The model expects this regime to last about ${fmt(regime.expected_duration_bars, 0)} more bars.` : "")),
      h("div", { class: "tiles" },
        tile("Regime probability", regime ? pct(regime.probability) : "–", regime ? `Regime ${regime.regime_id + 1} of ${regime.n_regimes}` : "Not fitted", regime && regime.probability),
        tile("Volatility", pct(snap.volatility_percentile), "Percentile of its own history", snap.volatility_percentile),
        tile("Liquidity", pct(snap.liquidity_percentile), "Percentile of its own history", snap.liquidity_percentile),
        tile("Confidence", pct(snap.confidence), "How much to trust this reading", snap.confidence)),
      h("p", { class: "note" }, "These are estimates from past data. They describe the current state of the market; they are not price forecasts.")),
  ];
  if (symbols.length) nodes.push(band(alt(), h("h2", {}, "Prices."),
    h("p", { class: "sub" }, symbols.some(x => x.synthetic) ? "Synthetic bars, generated for testing. Not market data." : "Unadjusted bars from the current dataset version."),
    priceCard(symbols, ctx)));
  const heat = heatmap(edges);
  nodes.push(band(alt(), h("h2", {}, "How assets move together."),
    h("p", { class: "sub" }, `${edges.length} links passed the multiple-testing threshold. Association is not causation.`),
    edges.length ? h("div", { class: "grid2" },
      card(h("h3", {}, "Correlation matrix"), h("p", { class: "fine", style: "margin:0 0 14px" }, "Hover a cell for details."),
        heat || h("p", { class: "muted" }, "Not enough pairs to draw a matrix.")),
      card(h("h3", {}, "Strongest links"), h("p", { class: "fine", style: "margin:0 0 14px" }, "Select a column to sort."),
        dataTable({ name: "cross-asset-links", rows: edges, initialSort: { i: 1, dir: -1 }, columns: [
          { key: "pair", label: "Pair", value: e => e.source + (e.direction === "undirected" ? " ↔ " : " → ") + e.target },
          { key: "strength", label: "Strength", num: true, value: e => e.strength,
            render: e => h("span", {}, h("span", { class: "strength" }, h("i", { style: `width:${Math.min(100, Math.abs(e.strength) * 100).toFixed(0)}%` })), fmt(e.strength)) },
          { key: "p_value", label: "p", num: true, render: e => sci(e.p_value) },
        ] })))
      : card(h("p", { class: "muted", style: "text-align:center" }, "No link between assets was strong enough to report."))));
  const vars = Object.entries((snap.state && snap.state.variables) || {}).map(([k, v]) => ({ k, v, st: (snap.state.epistemic_status || {})[k] }));
  nodes.push(band(alt(),
    h("h2", {}, anomalies.length ? `${anomalies.length} anomalies flagged.` : "No anomalies."),
    h("p", { class: "sub" }, "Unusual readings, measured against each variable's own history."),
    anomalies.length ? card(dataTable({ name: "anomalies", rows: anomalies, initialSort: { i: 2, dir: -1 }, columns: [
      { key: "variable", label: "Variable", value: a => words(a.variable) },
      { key: "timestamp", label: "When", render: a => new Date(a.timestamp).toLocaleString() },
      { key: "robust_z", label: "Robust z", num: true, render: a => fmt(a.robust_z) },
      { key: "rarity", label: "Rarity", num: true, render: a => pct(a.rarity) },
    ] })) : null,
    card(h("h3", {}, "All readings"), h("p", { class: "fine", style: "margin:0 0 14px" }, "Each variable is either observed directly or estimated by a model."),
      dataTable({ name: "market-state", rows: vars, columns: [
        { key: "k", label: "Variable", value: r => words(r.k) },
        { key: "v", label: "Value", num: true, render: r => fmt(r.v, 5) },
        { key: "st", label: "Status", value: r => words(r.st) },
      ] }),
      h("details", {}, h("summary", {}, "Read the full summary"), h("pre", {}, w.text)))));
  return nodes;
}

function priceCard(symbols, ctx) {
  const intervals = [...new Set(symbols.map(x => x.interval))];
  const state = { interval: prefs.get("interval", intervals[0]), symbol: prefs.get("symbol", symbols[0].symbol), range: prefs.get("range", "max") };
  if (!intervals.includes(state.interval)) state.interval = intervals[0];
  const symbolsFor = () => symbols.filter(x => x.interval === state.interval).map(x => x.symbol);
  if (!symbolsFor().includes(state.symbol)) state.symbol = symbolsFor()[0];
  const ranges = () => state.interval.endsWith("d") ? [["21", "1M"], ["126", "6M"], ["252", "1Y"], ["max", "Max"]] : [["390", "1D"], ["1950", "1W"], ["max", "Max"]];
  const quote = h("div", { class: "quote" }), controls = h("div", { class: "controls" });
  const body = h("div", {}, h("div", { class: "sk", style: "height:360px" }));
  let chart = null, seq = 0;
  function paintQuote(bars, hover) {
    const first = bars[0][4], b = hover || bars[bars.length - 1], chg = b[4] - first;
    quote.replaceChildren(h("span", { class: "sym" }, state.symbol), h("span", { class: "px" }, fmt(b[4], 2)),
      h("span", { class: "chg " + (chg >= 0 ? "good" : "bad") }, `${signed(chg)} (${signed((chg / first) * 100)}%)`),
      h("span", { class: "fine" }, hover ? new Date(b[0]).toLocaleString() : "over the selected range"));
  }
  async function load() {
    const my = ++seq, limit = state.range === "max" ? 5000 : Number(state.range);
    try {
      const r = await api.get(`/api/bars?symbol=${encodeURIComponent(state.symbol)}&interval=${encodeURIComponent(state.interval)}&limit=${limit}`);
      if (my !== seq) return; // a newer selection superseded this request
      if (chart) chart.dispose();
      chart = priceChart(r.bars, { interval: state.interval, onHover: (b) => paintQuote(r.bars, b) });
      paintQuote(r.bars, null);
      body.replaceChildren(chart, h("p", { class: "fine", style: "margin:10px 0 0" },
        `${r.bars.length.toLocaleString()} bars · ${state.interval} · dataset ${short(r.dataset_version)}${r.synthetic ? " · synthetic" : ""}`));
    } catch (e) {
      if (my === seq) body.replaceChildren(h("p", { class: "muted" }, "Could not load bars: " + e.message));
    }
  }
  function paintControls() {
    const rs = ranges();
    if (!rs.some(r => r[0] === state.range)) state.range = "max";
    controls.replaceChildren(...[
      segmented(symbolsFor().map(x => [x, x]), state.symbol, (v) => { state.symbol = v; prefs.set("symbol", v); load(); }, "Symbol"),
      intervals.length > 1 ? segmented(intervals.map(x => [x, x]), state.interval, (v) => {
        state.interval = v; prefs.set("interval", v);
        if (!symbolsFor().includes(state.symbol)) state.symbol = symbolsFor()[0];
        paintControls(); load(); }, "Interval") : null,
      segmented(rs, state.range, (v) => { state.range = v; prefs.set("range", v); load(); }, "Range")].filter(Boolean));
  }
  ctx.selectSymbol = (sym) => { if (symbolsFor().includes(sym)) { state.symbol = sym; prefs.set("symbol", sym); paintControls(); load(); } };
  paintControls(); load();
  return card(h("div", { class: "card-head" }, quote, controls), body);
}

function hypothesisCard(hy) {
  const adv = hy.adversarial, pred = hy.prediction || {};
  return card(
    h("div", { class: "card-head" },
      h("div", { style: "flex:1;min-width:260px" }, h("p", { class: "fine", style: "margin:0 0 6px" }, hy.hypothesis_id + " · Discovery score " + (adv && adv.score ? fmt(adv.score.score) : "not scored")),
        h("h3", {}, hy.statement)),
      h("span", { class: "badge " + statusClass(String(hy.status)) }, STATUS[hy.status] || title(hy.status))),
    h("h4", {}, "Prediction"),
    h("p", {}, `${pred.direction === "negative" ? "A decline" : "A rise"} of about ${fmt(Math.abs(Number(pred.expected_effect_bps)))} basis points over ${pred.horizon_bars ?? "–"} bars, under the conditions it was found in.`),
    h("h4", {}, "Evidence"),
    dataTable({ name: hy.hypothesis_id + "-evidence", rows: hy.observations || [], columns: [
      { key: "split", label: "Split", value: o => title(o.split) },
      { key: "n_events", label: "Events", num: true, render: o => Number(o.n_events).toLocaleString() },
      { key: "effect_bps", label: "Effect (bps)", num: true, render: o => fmt(o.effect_bps) },
      { key: "t_stat", label: "t", num: true, render: o => fmt(o.t_stat) },
      { key: "p_value", label: "p", num: true, render: o => sci(o.p_value) },
      { key: "p_adjusted", label: "Adjusted p", num: true, render: o => isNum(o.p_adjusted) ? fmt(o.p_adjusted, 4) : "–" },
      { key: "cost_adjusted_effect_bps", label: "After costs", num: true, render: o => fmt(o.cost_adjusted_effect_bps) },
    ] }),
    h("h4", {}, "Possible mechanisms (untested)"),
    h("ul", { class: "plain" }, ...(hy.mechanisms || []).map(m => h("li", {}, title(m.name) + ": " + m.explanation))),
    h("h4", {}, "Adversarial checks"),
    adv ? dataTable({ name: hy.hypothesis_id + "-checks", rows: adv.checks || [], columns: [
      { key: "name", label: "Check", value: c => title(c.name) },
      { key: "status", label: "Result", render: c => h("span", { class: c.status === "passed" ? "good" : c.status === "failed" ? "bad" : "muted" }, title(c.status)) },
      { key: "detail", label: "Detail" },
    ] }) : h("p", { class: "muted" }, "Not yet tested."),
    h("h4", {}, "Invalidated if"),
    h("ul", { class: "plain" }, ...(hy.invalidation || []).map(i => h("li", {}, i.rule))),
    h("p", { class: "fine", style: "margin-top:20px" }, `Protocol ${hy.protocol_hash} · Data ${short(hy.dataset_version)} · Features ${short(hy.feature_set_id)}`));
}
async function viewDiscoveries() {
  const items = await api.get("/api/hypotheses");
  if (!items.length) return [band("", ...header("Discoveries", "No discoveries yet.",
    "No pattern has survived the first statistical test. On synthetic data that is the correct answer: there is nothing real to find."))];
  const isStanding = (x) => statusClass(String(x.status)) !== "bad";
  const standing = items.filter(isStanding).length;
  const list = h("div");
  const paint = (f) => list.replaceChildren(...items.filter(x => f === "all" || (f === "standing") === isStanding(x)).map(hypothesisCard));
  const filter = prefs.get("hyp-filter", "all");
  paint(filter);
  return [
    band("", ...header("Discoveries", `${standing} of ${items.length} still standing.`,
      "Every hypothesis is listed, including the ones that failed, with its evidence and the tests it faced.")),
    band("alt", h("div", { class: "center-row" },
      segmented([["all", `All ${items.length}`], ["standing", `Standing ${standing}`], ["failed", `Failed ${items.length - standing}`]], filter,
        (v) => { prefs.set("hyp-filter", v); paint(v); }, "Filter hypotheses")), list),
  ];
}

async function viewResearch() {
  const [loops, journal] = await Promise.all([api.get("/api/loops"), api.get("/api/journal?limit=500")]);
  const tests = loops.reduce((t, l) => t + l.tests_run, 0), validated = loops.reduce((t, l) => t + l.validated_hypotheses, 0);
  const search = h("input", { class: "search", type: "search", name: "q", placeholder: "Search the research journal", "aria-label": "Search the research journal" });
  const list = card();
  const render = (entries, q) => list.replaceChildren(...(entries.length ? entries.map(e => h("div", { class: "entry" },
    h("h3", {}, ...highlight(e.question || "–", q)), h("p", {}, ...highlight(e.result || "", q)), h("p", {}, ...highlight(e.interpretation || "", q)),
    h("p", { class: "fine" }, "Method: " + (e.method || "–")), h("p", { class: "fine" }, "Next: " + (e.next_experiment || "–"))))
    : [h("p", { class: "muted", style: "text-align:center;margin:0" }, "No entries match.")]));
  render(journal, "");
  search.addEventListener("input", debounce(async () => {
    const q = search.value.trim();
    try { render(await api.get("/api/journal?limit=500&q=" + encodeURIComponent(q)), q); } catch (_) { /* keep the last view */ }
  }, 200));
  return [
    band("", ...header("Research", `${tests.toLocaleString()} tests. ${validated} validated.`,
      "Every experiment is registered before it runs, and every result is recorded, including failures."),
      h("div", { class: "tiles" }, tile("Cycles", loops.length, "Research cycles run"), tile("Tests", tests.toLocaleString(), "Statistical tests"),
        tile("Validated", validated, "Passed out of sample"), tile("Validation rate", tests ? pct(validated / tests, 1) : "–", "Validated per test"))),
    band("alt", h("h2", {}, "Cycles."),
      loops.length ? card(dataTable({ name: "research-cycles", rows: loops, initialSort: { i: 0, dir: -1 }, columns: [
        { key: "name", label: "Cycle", render: l => h("span", { class: "mono" }, l.name) },
        { key: "experiments", label: "Experiments", num: true },
        { key: "tests_run", label: "Tests", num: true },
        { key: "validated_hypotheses", label: "Validated", num: true },
        { key: "adversarial_survivors", label: "Survived checks", num: true },
        { key: "notes", label: "Notes", value: l => (l.notes || []).join(" · ") },
      ] })) : card(h("p", { class: "muted" }, "No cycles yet."))),
    band("", h("h2", {}, "Journal."), h("p", { class: "sub" }, "What was asked, what was found, and what comes next. Press / to search."), search, list),
  ];
}

async function viewStrategies() {
  const items = await api.get("/api/strategies");
  if (!items.length) return [band("", ...header("Strategies", "No strategies yet.", "A strategy is created only after a hypothesis passes every adversarial check."))];
  return [band("", ...header("Strategies", "Paper trading only.", "Strategies are promoted no further than paper trading."),
    h("div", { style: "margin-top:48px" }, card(dataTable({ name: "strategies", rows: items, columns: [
      { key: "strategy_id", label: "Strategy" }, { key: "hypothesis_id", label: "Hypothesis" },
      { key: "stage", label: "Stage", value: x => title(x.stage) }, { key: "lifecycle", label: "Lifecycle", value: x => title((x.lifecycle || {}).state) },
      { key: "paper_fills", label: "Fills", num: true }, { key: "paper_rejections", label: "Rejected", num: true },
      { key: "realized", label: "Realized (bps)", num: true, value: x => x.feedback?.overall?.mean_realized_bps ?? null, render: x => fmt(x.feedback?.overall?.mean_realized_bps) },
      { key: "reasons", label: "Reasons", value: x => ((x.lifecycle || {}).reasons || []).join("; ") },
    ] }))))];
}
async function viewPortfolio() {
  const items = await api.get("/api/strategies");
  const rows = items.flatMap(x => ((x.feedback || {}).diagnosis || []).map(d => ({ strategy: x.strategy_id, d })));
  if (!rows.length) return [band("", ...header("Portfolio", "No positions.", "No paper trades have been placed. No real money is ever traded."))];
  return [band("", ...header("Portfolio", "Simulated positions.", "Paper money only."),
    h("div", { style: "margin-top:48px" }, card(dataTable({ name: "paper-feedback", rows, columns: [{ key: "strategy", label: "Strategy" }, { key: "d", label: "Diagnosis" }] }))))];
}
async function viewRisk() {
  const [r, hl] = await Promise.all([api.get("/api/risk"), api.get("/api/health")]);
  const limits = Object.entries(r.limits).filter(([, v]) => typeof v !== "object" || v === null).map(([k, v]) => ({ k, v }));
  return [
    band("", ...header("Risk", r.live_trading_enabled ? "Live trading is enabled." : "Live trading is off.",
      "Every order passes validation, the kill switch and the risk engine before it can reach a broker. No real broker is connected."),
      h("div", { class: "tiles" },
        tile("Live trading", r.live_trading_enabled ? "On" : "Off", "Disabled by default"),
        tile("Kill switch", r.kill_switch_engaged ? "Engaged" : "Off", "Stops all order flow"),
        tile("Real broker", "None", "Orders cannot reach a market"),
        tile("Environment", title(hl.environment), "Current configuration"))),
    band("alt", h("h2", {}, "Limits."), h("p", { class: "sub" }, "Any order that breaks one of these is rejected."),
      card(dataTable({ name: "risk-limits", rows: limits, columns: [
        { key: "k", label: "Limit", value: x => title(x.k) },
        { key: "v", label: "Value", num: true, value: x => x.v, render: x => typeof x.v === "number" ? fmt(x.v, 3) : String(x.v) },
      ] }))),
  ];
}
async function viewData() {
  const [items, hl, symbols] = await Promise.all([api.get("/api/data?quality=true"), api.get("/api/health"), api.get("/api/symbols").catch(() => [])]);
  const unchecked = items.filter(d => d.quality && d.quality.unchecked).flatMap(d => Object.entries(d.quality.unchecked).map(([k, v]) => ({ d: d.dataset, k, v })));
  const real = hl.providers.some(p => p.enabled && !p.synthetic);
  const errors = (d) => d.quality ? (d.quality.issues || []).filter(i => i.severity === "error").length : null;
  const alt = (() => { let i = 1; return () => (i++ % 2 ? "alt" : ""); })();
  return [
    band("", ...header("Data", real ? "Real market data connected." : "Synthetic data only.",
      "Every dataset is fingerprinted, versioned and checked for quality before research can use it."),
      h("div", { style: "margin-top:48px" }, card(dataTable({ name: "datasets", rows: items, columns: [
        { key: "dataset", label: "Dataset", value: d => title(d.dataset) },
        { key: "version", label: "Version", render: d => d.present ? h("span", { class: "mono" }, short(d.version)) : "–" },
        { key: "rows", label: "Rows", num: true, render: d => d.present ? Number(d.rows).toLocaleString() : "–" },
        { key: "partitions", label: "Files", num: true, render: d => d.present ? d.partitions : "–" },
        { key: "integrity", label: "Integrity", value: d => !d.present ? "not loaded" : d.integrity_problems.length ? "problems" : "verified",
          render: d => !d.present ? h("span", { class: "muted" }, "Not loaded") : d.integrity_problems.length ? h("span", { class: "bad" }, d.integrity_problems.length + " problems") : h("span", { class: "good" }, "Verified") },
        { key: "quality", label: "Quality", value: errors,
          render: d => errors(d) === null ? "–" : h("span", { class: errors(d) ? "bad" : "good" }, errors(d) ? errors(d) + " errors" : "Passed") },
      ] })))),
    symbols.length ? band(alt(), h("h2", {}, "Coverage."), h("p", { class: "sub" }, "Bars available per symbol in the current version."),
      card(dataTable({ name: "coverage", rows: symbols, columns: [
        { key: "symbol", label: "Symbol" }, { key: "interval", label: "Interval" },
        { key: "bars", label: "Bars", num: true, render: x => x.bars.toLocaleString() },
        { key: "first", label: "From", render: x => new Date(x.first).toLocaleString() },
        { key: "last", label: "To", render: x => new Date(x.last).toLocaleString() },
        { key: "synthetic", label: "Kind", value: x => x.synthetic ? "Synthetic" : "Market data" },
      ] }))) : null,
    band(alt(), h("h2", {}, "Sources."), h("p", { class: "sub" }, "Free sources only. Paid sources are optional and switched off."),
      card(dataTable({ name: "providers", rows: hl.providers, columns: [
        { key: "name", label: "Provider" },
        { key: "enabled", label: "Status", value: p => p.enabled ? "On" : "Off", render: p => h("span", { class: p.enabled ? "good" : "muted" }, p.enabled ? "On" : "Off") },
        { key: "cost", label: "Cost", value: p => p.requires_payment ? "Paid, optional" : "Free" },
        { key: "type", label: "Type", value: p => p.synthetic ? "Synthetic" : "Real data" },
        { key: "reason", label: "Note", value: p => p.reason || "" },
      ] }))),
    unchecked.length ? band(alt(), h("h2", {}, "Not yet checked."), h("p", { class: "sub" }, "Checks that need a data source ORACLE does not have yet."),
      card(dataTable({ name: "unchecked", rows: unchecked, columns: [
        { key: "d", label: "Dataset", value: x => title(x.d) }, { key: "k", label: "Check", value: x => title(x.k) }, { key: "v", label: "Reason" },
      ] }))) : null,
  ];
}
async function viewExperiments() {
  const items = await api.get("/api/experiments");
  if (!items.length) return [band("", ...header("Experiments", "No experiments yet.", "Run a research cycle to register the first one."))];
  const sum = (k) => items.reduce((t, e) => t + e[k], 0);
  return [band("", ...header("Experiments", "Registered before they run.",
      "Each experiment's plan is fixed and fingerprinted in advance, so results cannot be tuned after the fact. Select one to inspect it."),
    h("div", { class: "tiles" }, tile("Experiments", items.length, "Pre-registered protocols"), tile("Tests", sum("n_tests").toLocaleString(), "Hypothesis tests in the ledger"),
      tile("Passed FDR", sum("discovery_survivors"), "Discovery-stage survivors"), tile("Validated", sum("validated"), "Confirmed out of sample"))),
    band("alt", card(dataTable({ name: "experiments", rows: items, onRowClick: (e) => go(`#/experiments/${e.protocol_hash}`), columns: [
      { key: "protocol_hash", label: "Protocol", render: e => h("span", { class: "mono" }, e.protocol_hash) },
      { key: "name", label: "Name" },
      { key: "horizons", label: "Horizons", value: e => (e.horizons || []).join(", ") },
      { key: "n_tests", label: "Tests", num: true },
      { key: "discovery_survivors", label: "Passed FDR", num: true },
      { key: "validated", label: "Validated", num: true },
    ] })))];
}


/* Experiment detail, deep-linkable as #/experiments/<protocol_hash>. */
async function openExperiment(protocol) {
  const body = h("div", {}, h("div", { class: "sk", style: { height: "40px", width: "60%", marginBottom: "20px" } }), h("div", { class: "sk", style: { height: "220px" } }));
  drawer.open("Experiment", h("div", { class: "research" }, body), { wide: true,
    close: () => { if (location.hash.startsWith("#/experiments/")) history.replaceState(null, "", "#/experiments"); } });
  try {
    const d = await api.get(`/api/experiments/${encodeURIComponent(protocol)}?limit=1000`);
      const pv = d.top_tests.map(t => t.p_value).filter(isNum);
      const below = pv.filter(p => p < 0.05).length;
      const validatedIds = new Set(d.validated || []);
      body.replaceChildren(
        h("p", { class: "eyebrow" }, "Experiment"),
        h("h2", { id: "drawer-title" }, d.protocol.name || protocol),
        h("div", { class: "facts" },
          ...[["Tests", d.n_tests], ["Too few events", d.insufficient_events], ["Horizons", (d.protocol.horizons || []).join(", ")], ["Validated", (d.validated || []).length]]
            .map(([k, v]) => h("div", {}, h("div", { class: "k" }, k), h("div", { class: "v" }, String(v))))),
        label("Distribution of p-values"),
        pv.length ? pHistogram(pv) : h("p", { class: "muted" }, "No test had enough events."),
        h("p", { class: "fine" }, `${below} of ${pv.length} tests (${pct(pv.length ? below / pv.length : null, 1)}) have p < 0.05, where about 5% would by chance alone. ` +
          "A flat histogram means the search found nothing beyond noise. A spike at the left edge suggests real structure, which must then survive validation."),
        label("Tests"),
        dataTable({ name: protocol + "-tests", rows: d.top_tests, initialSort: { i: 4, dir: 1 }, columns: [
          { key: "candidate", label: "Condition", value: t => describeCandidate(t.candidate),
            render: t => h("span", {}, describeCandidate(t.candidate), validatedIds.has(t.test_id) ? h("span", { class: "badge good", style: "margin-left:8px" }, "Validated") : null) },
          { key: "n_events", label: "Events", num: true },
          { key: "effect", label: "Effect (bps)", num: true, value: t => t.effect * 1e4, render: t => fmt(t.effect * 1e4) },
          { key: "t_stat", label: "t", num: true, render: t => fmt(t.t_stat) },
          { key: "p_value", label: "p", num: true, render: t => sci(t.p_value) },
          { key: "p_adj", label: "Adjusted p", num: true, value: t => d.p_adjusted[t.test_id] ?? null, render: t => isNum(d.p_adjusted[t.test_id]) ? fmt(d.p_adjusted[t.test_id], 4) : "–" },
          { key: "cost_adjusted_effect_bps", label: "After costs", num: true, render: t => fmt(t.cost_adjusted_effect_bps) },
        ] }));
  } catch (e) {
    body.replaceChildren(empty("Experiment not found.", e.message));
  }
}
