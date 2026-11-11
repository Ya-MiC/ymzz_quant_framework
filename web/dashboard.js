const $ = (id) => document.getElementById(id);

function text(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return Number.isFinite(value) ? value.toLocaleString("zh-Hant", { maximumFractionDigits: 6 }) : "—";
  return String(value);
}

function money(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("zh-Hant", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

async function getJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok || body.ok === false) {
    const message = body.message_zh || body.status || "讀取失敗";
    const error = new Error(message);
    error.body = body;
    throw error;
  }
  return body;
}

function draw(canvas, points) {
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#fffdf8";
  ctx.fillRect(0, 0, width, height);
  const values = (points || []).map((row) => Number(row.equity)).filter((n) => Number.isFinite(n));
  if (values.length < 2) {
    ctx.fillStyle = "#5e564c";
    ctx.font = "14px serif";
    ctx.fillText("尚無權益曲線", 16, 28);
    return;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  ctx.strokeStyle = "#8a6a2f";
  ctx.lineWidth = 2;
  ctx.beginPath();
  values.forEach((value, i) => {
    const x = 8 + (i / (values.length - 1)) * (width - 16);
    const y = height - 8 - ((value - min) / span) * (height - 16);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function livePanel(body) {
  const status = $("live-status");
  if (!body.configured || body.status === "not configured") {
    status.textContent = "not configured　尚未設定 API 金鑰，實盤面板不連線。";
    $("live-body").innerHTML = "";
    $("v-live").textContent = "not configured";
    draw($("live-equity"), []);
    return;
  }
  status.textContent = body.ok ? body.message_zh : (body.message_zh || "讀取失敗");
  status.className = body.ok ? "status" : "status error";
  const pos = body.position || {};
  const rows = [
    ["權益 equity", money(body.equity)],
    ["未實現 unrealized", money(body.unrealized_pnl)],
    ["已實現 realized", money(body.realized_pnl)],
    ["部位 XBT", text(pos.qty)],
    ["止損 SL", text(pos.strategy_stop_loss ?? pos.stop_loss)],
    ["止盈 TP", text(pos.strategy_take_profit ?? pos.take_profit)],
  ];
  const balances = (body.balances || []).map((row) => `${row.asset} ${row.amount}`).join(" · ") || "—";
  const orders = (body.open_orders || []).map((row) => `${row.ordertype || ""} ${row.side || ""} ${row.price ?? ""}`).join(" · ") || "無";
  const trades = (body.trades || []).slice(0, 5).map((row) => `${row.side || ""} ${row.vol || ""} @ ${row.price || ""}`).join(" · ") || "無";
  $("live-body").innerHTML = `<div class="kv">${rows.map(([k, v]) => `<span>${k}</span><strong>${v}</strong>`).join("")}</div><p class="note">餘額 ${balances}</p><p class="note">未結委託 ${orders}</p><p class="note">近期成交 ${trades}</p><p class="note">${body.realized_note_zh || ""}</p>`;
  $("v-live").textContent = body.ok ? money(body.equity) : "error";
  draw($("live-equity"), body.equity_curve || []);
}

function paperPanel(body) {
  const pos = body.position || {};
  const used = body.daily_loss_used;
  const limit = body.daily_loss_limit;
  const breaker = used === null || used === undefined ? "—" : `${(used * 100).toFixed(2)}% / ${limit ? (limit * 100).toFixed(0) : "15"}%`;
  $("paper-status").textContent = body.configured ? "紙上狀態來自 runner 寫下的 JSON 與決策紀錄。" : "還沒有紙上狀態。先跑 python -m ymzz.cli run --mode paper。";
  const rows = [
    ["權益 equity", money(body.equity)],
    ["回撤 drawdown", body.drawdown === null || body.drawdown === undefined ? "—" : `${(body.drawdown * 100).toFixed(2)}%`],
    ["距 15% 熔斷", breaker],
    ["熔斷 halted", body.halted ? "是" : "否"],
    ["部位 qty", text(pos.qty)],
    ["未實現", money(pos.unrealized_pnl)],
    ["已實現", money(pos.realized_pnl)],
    ["止損 SL", text(pos.stop_loss)],
    ["止盈 TP", text(pos.take_profit)],
  ];
  $("paper-body").innerHTML = `<div class="kv">${rows.map(([k, v]) => `<span>${k}</span><strong>${v}</strong>`).join("")}</div>`;
  $("v-paper").textContent = body.equity === null || body.equity === undefined ? "尚無" : money(body.equity);
  draw($("paper-equity"), body.equity_curve || []);
  const tbody = $("paper-log").querySelector("tbody");
  tbody.innerHTML = "";
  (body.decisions || []).forEach((row) => {
    const tr = document.createElement("tr");
    [row.time_kst, row.action, row.rsi, row.dif, row.dea, row.reason].forEach((cell) => {
      const td = document.createElement("td");
      td.textContent = text(cell);
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

function forecastPanel(body) {
  const box = $("forecast-body");
  if (!body.forecast) {
    $("forecast-status").textContent = body.message_zh || "還沒有預測。";
    box.innerHTML = "";
    return;
  }
  const f = body.forecast;
  $("forecast-status").textContent = "數字來自最新已收盤 K 線。回測在下一根開盤成交；紙上 runner 在看到這根收盤時處理。";
  const rows = [
    ["RSI(14)", text(f.rsi14)],
    ["MACD DIF", text(f.dif)],
    ["MACD DEA", text(f.dea)],
    ["DIF−DEA", text(f.distance_dif_minus_dea)],
    ["位置", f.macd_side_zh],
    ["下一根", f.next_candle_zh],
  ];
  box.innerHTML = rows.map(([k, v]) => `<span>${k}</span><strong>${v}</strong>`).join("");
}

function solPanel(body) {
  const wallets = body.wallets || [];
  $("v-sol").textContent = String(wallets.length);
  if (!wallets.length && !(body.errors || []).length) {
    $("sol-body").innerHTML = "<p class='note'>觀察名單是空的。把公開地址貼在上面，或在 .env 設 SOL_WATCH_ADDRESSES。</p>";
    return;
  }
  const html = wallets.map((row) => {
    const tokens = (row.tokens || []).map((token) => `${(token.mint || "").slice(0, 4)}… ${token.amount}`).join(" · ") || "無 SPL";
    return `<p><span class="pill">${row.address}</span> SOL ${text(row.sol)} · ${tokens}</p>`;
  }).join("");
  const errs = (body.errors || []).map((row) => `<p class="down-text">${row.address}: ${row.message_zh}</p>`).join("");
  $("sol-body").innerHTML = html + errs + `<p class="note">${body.note_zh || ""}</p>`;
}

function statusPanel(body) {
  const beat = body.heartbeat;
  const box = $("status-body");
  if (!beat) {
    box.innerHTML = `<span>心跳</span><strong>${body.message_zh}</strong>`;
    return;
  }
  const rows = [
    ["最後心跳", beat.beat_kst || beat.beat_utc],
    ["下一輪心跳", beat.next_heartbeat_utc],
    ["下一根日線檢查", beat.next_candle_check_utc],
    ["模式 mode", beat.mode],
    ["上次動作", beat.last_action],
    ["同一根重跑", beat.idempotent ? "是" : "否"],
    ["錯誤", beat.last_error || "無"],
  ];
  box.innerHTML = rows.map(([k, v]) => `<span>${k}</span><strong>${text(v)}</strong>`).join("");
}

async function refresh() {
  const jobs = [
    getJson("/api/dashboard/live").then(livePanel).catch((error) => { $("live-status").textContent = error.message; }),
    getJson("/api/dashboard/paper").then(paperPanel).catch((error) => { $("paper-status").textContent = error.message; }),
    getJson("/api/dashboard/forecast").then(forecastPanel).catch((error) => { $("forecast-status").textContent = error.message; }),
    getJson("/api/dashboard/solana").then(solPanel).catch((error) => { $("sol-body").textContent = error.message; }),
    getJson("/api/dashboard/status").then(statusPanel).catch((error) => { $("status-body").textContent = error.message; }),
  ];
  await Promise.all(jobs);
  $("refresh").textContent = "剛剛更新 · 15 秒後再讀";
}

$("bt-run").addEventListener("click", async () => {
  const form = new FormData($("bt-form"));
  const payload = Object.fromEntries(form.entries());
  ["capital", "size", "sl", "tp", "fee_bp", "slip_bp", "daily_loss", "rsi", "rsi_low", "rsi_high", "rsi_exit"].forEach((key) => {
    if (payload[key] !== "") payload[key] = Number(payload[key]);
  });
  if (!payload.end) delete payload.end;
  $("bt-out").textContent = "回測進行中…";
  try {
    const body = await getJson("/api/dashboard/backtest", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const metrics = body.metrics || {};
    $("bt-out").textContent = `期末 ${money(metrics.final_equity)} · 報酬 ${text(metrics.total_return)} · 回撤 ${text(metrics.max_drawdown)} · 筆數 ${text(metrics.round_trips)}。${body.note_zh}`;
  } catch (error) {
    $("bt-out").textContent = error.message;
  }
});

$("watch-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const address = $("watch-address").value.trim();
  try {
    await getJson("/api/dashboard/wallets", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ address }),
    });
    $("watch-address").value = "";
    const body = await getJson("/api/dashboard/solana");
    solPanel(body);
  } catch (error) {
    $("sol-body").textContent = error.message;
  }
});

refresh();
setInterval(refresh, 15000);
