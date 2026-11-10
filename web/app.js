const state = { catalog: [], strategies: [], leverage: 1, result: null };

const $ = (id) => document.getElementById(id);

function market() {
  return state.catalog.find((item) => item.id === $("market").value);
}

function fillSelect(el, values, labels) {
  el.innerHTML = "";
  values.forEach((value, i) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = labels ? labels[i] : value;
    el.appendChild(option);
  });
}

function renderLeverage() {
  const box = $("leverage");
  box.innerHTML = "";
  const allowed = market().leverage.length ? market().leverage : [1];
  if (!allowed.includes(state.leverage)) state.leverage = allowed[0];
  allowed.forEach((value) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${value}x`;
    button.setAttribute("aria-pressed", String(value === state.leverage));
    button.onclick = () => {
      state.leverage = value;
      renderLeverage();
    };
    box.appendChild(button);
  });
}

function renderParams() {
  const spec = state.strategies.find((item) => item.id === $("strategy").value);
  $("formula").textContent = `${spec.formula_zh} ${spec.source_zh}`;
  const box = $("params");
  box.innerHTML = "";
  spec.params.forEach((param) => {
    const label = document.createElement("label");
    label.textContent = param.label_zh;
    const input = document.createElement("input");
    input.type = "number";
    input.dataset.key = param.key;
    input.value = param.default;
    if (param.min != null) input.min = param.min;
    if (param.max != null) input.max = param.max;
    input.step = "any";
    label.appendChild(input);
    box.appendChild(label);
  });
}

function applyMarket() {
  const item = market();
  fillSelect($("source"), item.sources.length ? item.sources : ["—"]);
  fillSelect($("timeframe"), item.timeframes.length ? item.timeframes : ["—"]);
  $("symbol").value = item.symbols[0] || "";
  $("currency").value = item.currency || "";
  $("capital").value = item.default_capital;
  $("fee").value = item.default_fee_bps ?? 10;
  $("stamp").value = item.default_stamp_tax_bps ?? 0;
  $("market-note").textContent = item.note_zh || "";
  $("run").disabled = item.id === "stock_app";
  renderLeverage();
}

function readParams() {
  const params = {};
  document.querySelectorAll("#params input").forEach((input) => {
    params[input.dataset.key] = Number(input.value);
  });
  return params;
}

function readConfig() {
  return {
    market: $("market").value,
    source: $("source").value,
    symbol: $("symbol").value.trim(),
    timeframe: $("timeframe").value,
    start: $("start").value,
    end: $("end").value,
    strategy: $("strategy").value,
    params: readParams(),
    leverage: state.leverage,
    capital: Number($("capital").value),
    fee_bps: Number($("fee").value),
    slippage_bps: Number($("slip").value),
    impact_coef: Number($("impact").value),
    stamp_tax_bps: Number($("stamp").value),
    tp_pct: Number($("tp").value),
    sl_pct: Number($("sl").value),
    allow_short: $("short").checked,
  };
}

function applyConfig(config) {
  $("market").value = config.market;
  applyMarket();
  if (config.source) $("source").value = config.source;
  if (config.symbol) $("symbol").value = config.symbol;
  if (config.timeframe) $("timeframe").value = config.timeframe;
  if (config.start) $("start").value = config.start;
  if (config.end) $("end").value = config.end;
  if (config.strategy) $("strategy").value = config.strategy;
  renderParams();
  Object.entries(config.params || {}).forEach(([key, value]) => {
    const input = document.querySelector(`#params input[data-key="${key}"]`);
    if (input) input.value = value;
  });
  state.leverage = config.leverage ?? state.leverage;
  renderLeverage();
  if (config.capital != null) $("capital").value = config.capital;
  if (config.fee_bps != null) $("fee").value = config.fee_bps;
  if (config.slippage_bps != null) $("slip").value = config.slippage_bps;
  if (config.impact_coef != null) $("impact").value = config.impact_coef;
  if (config.stamp_tax_bps != null) $("stamp").value = config.stamp_tax_bps;
  if (config.tp_pct != null) $("tp").value = config.tp_pct;
  if (config.sl_pct != null) $("sl").value = config.sl_pct;
  $("short").checked = Boolean(config.allow_short);
}

function showError(message) {
  $("status").className = "status error";
  $("status").textContent = message;
}

async function runBacktest() {
  $("status").className = "status";
  $("status").textContent = "正在讀取公開行情。這可能要等一下，失敗會直接顯示，不會改來源。";
  const response = await fetch("/api/backtest", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(readConfig()),
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    showError(data.message_zh || "回測失敗");
    return;
  }
  state.result = data;
  renderResult(data);
}

function renderResult(data) {
  const metrics = data.metrics || {};
  const pct = (value) => (value == null ? "—" : `${(value * 100).toFixed(2)}%`);
  const num = (value) => (value == null ? "—" : Number(value).toFixed(4));
  $("status").className = "status";
  $("status").textContent = (data.warnings || []).join(" ") || "回測完成。未平倉只是市值，不是額外成交。";
  $("metrics").innerHTML = [
    ["期末權益", num(metrics.final_equity)],
    ["報酬", pct(metrics.total_return)],
    ["最大回撤", pct(metrics.max_drawdown)],
    ["手續費", num(metrics.fees_paid)],
    ["資金費", num(metrics.funding_paid)],
    ["清算次數", metrics.liquidations ?? 0],
    ["成交筆數", metrics.trade_count ?? 0],
    ["槓桿", metrics.leverage ?? "—"],
  ].map(([name, value]) => `<div><span>${name}</span><strong>${value}</strong></div>`).join("");
  drawPrice($("price"), data);
  drawEquity($("equity"), data.equity || []);
  const body = $("ledger").querySelector("tbody");
  body.innerHTML = "";
  (data.trades || []).slice(-80).forEach((row) => {
    const tr = document.createElement("tr");
    const cells = [
      String(row.time).replace("T", " ").slice(0, 16),
      row.action,
      Number(row.price).toPrecision(6),
      Number(row.qty).toPrecision(4),
      Number(row.fee).toFixed(4),
      row.reason,
      Number(row.equity_after).toFixed(4),
    ];
    tr.innerHTML = cells.map((cell) => `<td>${cell}</td>`).join("");
    body.appendChild(tr);
  });
  const prov = data.provenance || {};
  $("prov").textContent = `來源 ${prov.source_url || prov.source || ""} · 抓取 ${prov.fetched_at_kst || ""} · sha256 ${(prov.sha256 || "").slice(0, 12)} · 剔除未收盤 ${prov.dropped_unclosed ?? "—"} · ${prov.note_zh || ""}`;
}

function seriesRange(bars) {
  let lo = Infinity;
  let hi = -Infinity;
  bars.forEach((bar) => {
    lo = Math.min(lo, bar.low);
    hi = Math.max(hi, bar.high);
  });
  if (!Number.isFinite(lo) || lo === hi) return [0, 1];
  const pad = (hi - lo) * 0.06;
  return [lo - pad, hi + pad];
}

function splitIndicators(indicators, lo, hi) {
  const overlays = [];
  const oscillators = [];
  Object.entries(indicators || {}).forEach(([name, values]) => {
    if (!Array.isArray(values)) return;
    const finite = values.filter((value) => value != null && Number.isFinite(value));
    if (!finite.length) return;
    const mid = finite[Math.floor(finite.length / 2)];
    const onPrice = mid > lo * 0.25 && mid < hi * 4;
    (onPrice ? overlays : oscillators).push([name, values]);
  });
  return { overlays, oscillators };
}

function strokeSeries(ctx, values, xAt, yAt, color) {
  ctx.beginPath();
  ctx.strokeStyle = color;
  let started = false;
  values.forEach((value, i) => {
    if (value == null || !Number.isFinite(value)) return;
    const x = xAt(i);
    const y = yAt(value);
    if (!started) {
      ctx.moveTo(x, y);
      started = true;
    } else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawPrice(canvas, data) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const bars = data.bars || [];
  if (!bars.length) return;
  const [lo, hi] = seriesRange(bars);
  const { overlays, oscillators } = splitIndicators(data.indicators, lo, hi);
  const top = 8;
  const priceBottom = oscillators.length ? Math.floor(h * 0.68) : h - 8;
  const slot = w / bars.length;
  const yPrice = (value) => priceBottom - ((value - lo) / (hi - lo)) * (priceBottom - top);
  bars.forEach((bar, i) => {
    const x = i * slot + slot / 2;
    ctx.strokeStyle = bar.close >= bar.open ? "#1f6b4a" : "#8f2d2d";
    ctx.beginPath();
    ctx.moveTo(x, yPrice(bar.high));
    ctx.lineTo(x, yPrice(bar.low));
    ctx.stroke();
    const bodyTop = yPrice(Math.max(bar.open, bar.close));
    const height = Math.max(1, Math.abs(yPrice(bar.open) - yPrice(bar.close)));
    ctx.fillStyle = ctx.strokeStyle;
    ctx.fillRect(x - Math.max(1, slot * 0.3), bodyTop, Math.max(2, slot * 0.6), height);
  });
  const colors = ["#8a6a2f", "#1c4f8f", "#6b3f69", "#3d6b4f"];
  overlays.forEach(([name, values], idx) => {
    strokeSeries(ctx, values, (i) => i * slot + slot / 2, yPrice, colors[idx % colors.length]);
  });
  if (!oscillators.length) return;
  const oscTop = priceBottom + 16;
  const oscBottom = h - 6;
  let oscLo = Infinity;
  let oscHi = -Infinity;
  oscillators.forEach(([, values]) => {
    values.forEach((value) => {
      if (value == null || !Number.isFinite(value)) return;
      oscLo = Math.min(oscLo, value);
      oscHi = Math.max(oscHi, value);
    });
  });
  if (!Number.isFinite(oscLo) || oscLo === oscHi) {
    oscLo -= 1;
    oscHi += 1;
  }
  ctx.strokeStyle = "#d9d0c1";
  ctx.beginPath();
  ctx.moveTo(0, priceBottom + 8);
  ctx.lineTo(w, priceBottom + 8);
  ctx.stroke();
  oscillators.forEach(([, values], idx) => {
    strokeSeries(
      ctx,
      values,
      (i) => i * slot + slot / 2,
      (value) => oscBottom - ((value - oscLo) / (oscHi - oscLo)) * (oscBottom - oscTop),
      colors[idx % colors.length],
    );
  });
}

function drawEquity(canvas, rows) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (!rows.length) return;
  const eq = rows.map((row) => row.equity);
  const dd = rows.map((row) => row.drawdown);
  const lo = Math.min(...eq);
  const hi = Math.max(...eq);
  const span = hi - lo || 1;
  ctx.beginPath();
  ctx.strokeStyle = "#1c1915";
  eq.forEach((value, i) => {
    const x = (i / Math.max(1, eq.length - 1)) * (w - 12) + 6;
    const y = h * 0.62 - ((value - lo) / span) * (h * 0.5);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.beginPath();
  ctx.strokeStyle = "#8f2d2d";
  dd.forEach((value, i) => {
    const x = (i / Math.max(1, dd.length - 1)) * (w - 12) + 6;
    const y = h * 0.78 + value * h * 0.18;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

async function boot() {
  if (window.YMZZ_SNAPSHOT) {
    renderResult(window.YMZZ_SNAPSHOT);
    return;
  }
  const [catalog, strategies] = await Promise.all([
    fetch("/api/catalog").then((r) => r.json()),
    fetch("/api/strategies").then((r) => r.json()),
  ]);
  state.catalog = catalog.markets;
  state.strategies = strategies.strategies;
  fillSelect($("market"), state.catalog.map((item) => item.id), state.catalog.map((item) => item.label_zh));
  fillSelect($("strategy"), state.strategies.map((item) => item.id), state.strategies.map((item) => item.name_zh));
  $("strategy").value = "rsi_three";
  $("market").onchange = applyMarket;
  $("strategy").onchange = renderParams;
  applyMarket();
  renderParams();
  $("run").onclick = () => runBacktest().catch((error) => showError(String(error)));
  $("save").onclick = async () => {
    const name = window.prompt("設定名稱（英數）", "btc_rsi");
    if (!name) return;
    const response = await fetch("/api/config", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, config: readConfig() }),
    });
    const data = await response.json();
    if (!response.ok) showError(data.message_zh || "儲存失敗");
    else $("status").textContent = `已儲存 ${name}`;
  };
  $("load").onclick = async () => {
    const name = window.prompt("要載入的設定名稱", "btc_rsi");
    if (!name) return;
    const response = await fetch(`/api/config/${encodeURIComponent(name)}`);
    const data = await response.json();
    if (!response.ok) showError(data.message_zh || "載入失敗");
    else applyConfig(data.config);
  };
  $("inspect").onclick = async () => {
    $("wallet-out").textContent = "讀取公開 RPC…";
    const response = await fetch("/api/wallet/inspect", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ address: $("address").value.trim(), limit: 5 }),
    });
    const data = await response.json();
    if (!response.ok) {
      $("wallet-out").textContent = data.message_zh || "讀取失敗";
      return;
    }
    const lines = (data.caveats_zh || []).concat([JSON.stringify(data.counts, null, 2)]);
    (data.events || []).forEach((event) => lines.push(`${event.kind} ${event.note_zh}`));
    (data.errors || []).forEach((error) => lines.push(error.message_zh));
    $("wallet-out").textContent = lines.join("\n");
  };
}

boot().catch((error) => showError(String(error)));
