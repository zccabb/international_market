"use strict";
const el = (id) => document.getElementById(id);
const marketNames = Object.fromEntries(
  [...el("market").options].map((o) => [o.value, o.text]),
);
const fmt = (value) =>
  value == null
    ? "—"
    : value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
const moment = (stamp) =>
  stamp
    ? new Date(stamp * 1000).toLocaleString("zh-CN", { hour12: false })
    : "—";
let items = [],
  chosen = null,
  interval = "1m",
  candles = [],
  pendingDelete = null;
let listVersion = 0,
  chartVersion = 0;

async function api(url, options) {
  const response = await fetch(url, { cache: "no-store", ...options });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || "请求失败（" + response.status + "）");
  }
  return response.status === 204 ? null : response.json();
}
function node(tag, value, css) {
  const n = document.createElement(tag);
  if (value != null) n.textContent = value;
  if (css) n.className = css;
  return n;
}
function renderRows() {
  const visible = items.filter(
    (row) => !el("filter").value || row.market === el("filter").value,
  );
  if (!visible.some((r) => r.id === chosen)) chosen = visible[0]?.id || null;
  el("summary").textContent =
    items.length +
    " 个采集标的 · " +
    new Set(items.map((r) => r.market)).size +
    " 个市场";
  el("empty").hidden = visible.length > 0;
  el("detail").hidden = !chosen;
  const rows = visible.map((row) => {
    const tr = node("tr", null, row.id === chosen ? "selected" : "");
    const cell = node("td"),
      symbol = node("button", row.symbol, "link");
    symbol.onclick = () => {
      chosen = row.id;
      renderRows();
      refreshChart();
    };
    cell.append(
      symbol,
      node(
        "small",
        marketNames[row.market] + (row.name ? " · " + row.name : ""),
      ),
    );
    const price = node("td", fmt(row.price), "right numeric");
    price.append(node("small", row.currency));
    const change =
      row.price != null && row.previous > 0
        ? 100 * (row.price / row.previous - 1)
        : null;
    const percent =
      change == null ? "—" : (change > 0 ? "+" : "") + change.toFixed(2) + "%";
    const stamp = node("td", moment(row.asof), "numeric");
    const state = node("td", row.source || "—", row.issue ? "warn" : "");
    state.append(
      node("small", row.issue || (row.checked ? "已采集" : "等待首次采集")),
    );
    state.title = "上次采集：" + moment(row.checked);
    const remove = node("button", "移除", "remove");
    remove.setAttribute("aria-label", "移除 " + row.symbol);
    remove.onclick = () => {
      pendingDelete = row.id;
      el("confirm-text").textContent =
        "停止采集 " + row.symbol + "，并删除本地保存的该标的行情记录？";
      el("confirm").showModal();
    };
    const action = node("td");
    action.append(remove);
    tr.append(
      cell,
      price,
      node(
        "td",
        percent,
        "right numeric " + (change > 0 ? "up" : change < 0 ? "down" : ""),
      ),
      stamp,
      state,
      action,
    );
    return tr;
  });
  el("rows").replaceChildren(...rows);
}

function candleTime(row) {
  if (interval === "1d")
    return new Date(row.stamp * 1000).toISOString().slice(0, 10);
  return moment(row.stamp);
}
function inspectBar(bar) {
  el("bar-info").textContent = bar
    ? [
        candleTime(bar),
        "开 " + fmt(bar.open),
        "高 " + fmt(bar.high),
        "低 " + fmt(bar.low),
        "收 " + fmt(bar.close),
        "量 " + fmt(bar.volume),
      ].join("　")
    : "等待行情数据";
}
const canvas = el("chart");
function draw() {
  const w = canvas.clientWidth,
    h = canvas.clientHeight;
  if (!w || !h) return;
  const scale = window.devicePixelRatio || 1;
  canvas.width = w * scale;
  canvas.height = h * scale;
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  if (!candles.length) return;
  const values = candles.map((b) => b.close);
  const lo = Math.min(...values),
    hi = Math.max(...values);
  const plotW = Math.max(1, w - 78);
  const x = (i) => 8 + (plotW * i) / Math.max(1, values.length - 1);
  const y = (v) => (hi === lo ? h / 2 : 12 + ((h - 25) * (hi - v)) / (hi - lo));
  ctx.font = "12px system-ui";
  ctx.fillStyle = "#526775";
  for (const frac of [0, 0.5, 1]) {
    const v = lo + frac * (hi - lo),
      py = y(v);
    ctx.strokeStyle = "#dce6e4";
    ctx.beginPath();
    ctx.moveTo(4, py);
    ctx.lineTo(plotW + 10, py);
    ctx.stroke();
    ctx.fillText(fmt(v), plotW + 15, py + 4);
    if (hi === lo) break;
  }
  ctx.beginPath();
  values.forEach((v, i) =>
    i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)),
  );
  ctx.strokeStyle = "#007e70";
  ctx.lineWidth = 2;
  ctx.stroke();
  ctx.fillStyle = "#007e70";
  ctx.beginPath();
  ctx.arc(x(values.length - 1), y(values.at(-1)), 3, 0, 2 * Math.PI);
  ctx.fill();
}
canvas.onpointermove = (event) => {
  if (!candles.length) return;
  const ratio =
    (event.clientX - canvas.getBoundingClientRect().left - 8) /
    Math.max(1, canvas.clientWidth - 78);
  inspectBar(
    candles[Math.round(Math.min(1, Math.max(0, ratio)) * (candles.length - 1))],
  );
};
canvas.onpointerleave = () => inspectBar(candles.at(-1));
new ResizeObserver(draw).observe(canvas);

async function refreshChart() {
  const version = ++chartVersion;
  const row = items.find((r) => r.id === chosen);
  if (!row) return;
  el("detail-title").textContent = row.symbol + " · 收盘价走势";
  try {
    const bars = await api("/api/history/" + row.id + "?interval=" + interval);
    if (version !== chartVersion) return;
    candles = bars;
    el("range").textContent = bars.length
      ? candleTime(bars[0]) +
        " — " +
        candleTime(bars.at(-1)) +
        " · " +
        bars.length +
        " 个数据点"
      : "暂无记录";
    inspectBar(bars.at(-1));
    draw();
  } catch (err) {
    if (version !== chartVersion) return;
    candles = [];
    draw();
    inspectBar(null);
    el("range").textContent = err.message;
  }
}
async function refresh() {
  const version = ++listVersion;
  try {
    const data = await api("/api/watchlist");
    if (version !== listVersion) return;
    items = data;
    renderRows();
    el("connection").textContent = "● 服务已连接";
    await refreshChart();
  } catch {
    if (version === listVersion)
      el("connection").textContent = "○ 连接中断，正在重试";
  }
}
el("filter").onchange = () => {
  renderRows();
  refreshChart();
};
el("market").onchange = () => {
  el("symbol").placeholder = el("market").selectedOptions[0].dataset.example;
};
el("add-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const row = await api("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        market: el("market").value,
        symbol: el("symbol").value,
      }),
    });
    chosen = row.id;
    el("filter").value = "";
    el("symbol").value = "";
    el("notice").textContent = row.symbol + " 已加入采集清单";
    await refresh();
  } catch (err) {
    el("notice").textContent = err.message;
  }
};
el("cancel").onclick = () => el("confirm").close();
el("delete").onclick = async () => {
  try {
    await api("/api/watchlist/" + pendingDelete, { method: "DELETE" });
    el("notice").textContent = "已移除标的及其本地行情";
    await refresh();
  } catch (err) {
    el("notice").textContent = err.message;
  }
  el("confirm").close();
};
document.querySelectorAll("[data-interval]").forEach(
  (button) =>
    (button.onclick = () => {
      interval = button.dataset.interval;
      document
        .querySelectorAll("[data-interval]")
        .forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      refreshChart();
    }),
);
refresh();
setInterval(refresh, 10000);
