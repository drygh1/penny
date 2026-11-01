// Penny web UI. Plain JS that talks to the JSON API in app/main.py.

const $ = (sel) => document.querySelector(sel);
const fmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const fmt0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const money = (n) => fmt.format(n);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const monthLabel = (ym, style = "short") => {
  const [y, m] = ym.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString("en-US", { month: style, year: style === "long" ? "numeric" : "2-digit" });
};
const dayLabel = (iso, withYear = false) => {
  const d = new Date(iso + "T12:00:00");
  const opts = { month: "short", day: "numeric" };
  if (withYear || d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("en-US", opts);
};
// "Nov 21, 2023 to Oct 1, 2026". The year is left off only when both ends are this year.
const rangeLabel = (first, last) => {
  const thisYear = String(new Date().getFullYear());
  const withYear = !(first.startsWith(thisYear) && last.startsWith(thisYear));
  return `${dayLabel(first, withYear)} to ${dayLabel(last, withYear)}`;
};
const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const thisMonth = () => isoDay(new Date()).slice(0, 7);

// Each category keeps the same color everywhere. The server gives each of up to 20
// categories its own colorblind-checked color slot; any beyond that share a neutral gray.
// Slot order is also the donut's slice order, which keeps neighbors distinct.
const OTHER_LABEL = "Everything else";
const categoryNames = () => categories.map((c) => c.name);
const slotOf = (cat) => categories.find((c) => c.name === cat)?.color_slot ?? null;
const colorVar = (cat) => {
  const slot = slotOf(cat);
  return slot ? `var(--series-${slot})` : "var(--series-other)";
};
const colorValue = (cat) => css(colorVar(cat).slice(4, -1));
const dot = (cat) => `<span class="dot" style="background:${colorVar(cat)}" aria-hidden="true"></span>`;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) location.replace("/login");  // on a phone whose sign-in ran out
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

let status = {};
let categories = [];  // [{name, color_slot, locked, transactions}]
let logos = {};  // merchant -> picture version, for merchants that have a picture
const charts = {};

async function refreshLogos() {
  logos = await api("/api/merchant-logos");
}

// A merchant's picture if one was uploaded, otherwise its first letter.
function avatar(merchant, name, cls = "avatar") {
  if (merchant && logos[merchant]) {
    const src = `/api/merchant-logo?merchant=${encodeURIComponent(merchant)}&v=${logos[merchant]}`;
    return `<div class="${cls}" aria-hidden="true"><img src="${src}" alt=""></div>`;
  }
  return `<div class="${cls}" aria-hidden="true">${esc((name || "?").charAt(0).toUpperCase())}</div>`;
}

// ---------- routing ----------

const VIEWS = ["dashboard", "review", "subscriptions", "transactions", "merchants", "accounts", "settings"];

// "#settings/connect" shows Settings and scrolls to the element with id "connect".
async function route() {
  let [view, anchor] = (location.hash.slice(1) || "dashboard").split("/");
  if (!VIEWS.includes(view)) view = "dashboard";
  const hasData = !!status.mode;
  if (!hasData && view !== "settings") view = "welcome";

  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.hash === `#${view}`));

  const loaders = { settings: () => Promise.all([loadCategories(), loadImport(), status.remote ? null : loadData(),
                                                 status.remote ? null : loadUpdatesCard(), loadShareCard()]), dashboard: loadDashboard, review: () => { keepVisible.clear(); return loadReview(); }, subscriptions: loadSubscriptions, transactions: loadTransactions, merchants: loadMerchants, accounts: loadAccounts };
  await Promise.all([loaders[view]?.(), hasData && loadGuide(view)]);
  if (view === "settings") showConnectState(anchor === "connect");
  if (anchor) document.getElementById(anchor)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function refreshStatus() {
  status = await api("/api/status");
  $("#sync-btn").hidden = !status.connected;
  $("#stop-btn").hidden = status.desktop || status.remote;  // quitting the Mac app stops Penny
  showReviewCount(status.to_review);
  // On a phone over Meshnet: the Mac-only settings are hidden (the server refuses them anyway).
  for (const id of ["#connect", "#data-card", "#updates-card"]) $(id).hidden = status.remote;
  $("#phone-card").hidden = !status.remote;
  $("#demo-banner").hidden = status.mode !== "demo" || status.remote;
  document.querySelectorAll('[data-action="demo"]').forEach((b) => b.hidden = status.remote || b.hidden);
  $("#last-sync").textContent = status.mode === "demo"
    ? "Demo data"
    : status.last_sync ? `Synced ${new Date(status.last_sync).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}` : "";
}

// ---------- charts ----------

function baseChartOptions() {
  const muted = css("--muted"), grid = css("--grid");
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: css("--surface"), titleColor: css("--text"), bodyColor: css("--text-2"),
        borderColor: css("--axis"), borderWidth: 1, padding: 10, displayColors: false,
        callbacks: { label: (c) => money(c.parsed.y) },
      },
    },
    scales: {
      x: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: muted } },
      y: { grid: { color: grid }, border: { display: false }, ticks: { color: muted, callback: (v) => fmt0.format(v) } },
    },
  };
}

function drawChart(id, config) {
  charts[id]?.destroy();
  charts[id] = new Chart(document.getElementById(id), config);
}

// ---------- dashboard ----------

// The period being looked at: a year, plus a month ("01".."12") or "" for the whole year.
const period = { year: thisMonth().slice(0, 4), month: thisMonth().slice(5) };
const periodKey = () => period.month ? `${period.year}-${period.month}` : period.year;
const periodLabel = (key = periodKey()) => key.length === 4 ? key : monthLabel(key, "long");
let years = [];

async function loadDashboard() {
  const [periods, flow, accounts, subs, b] = await Promise.all([
    api("/api/periods"),
    api(flowView === "years" ? "/api/spending/yearly" : `/api/spending/monthly?year=${period.year}`),
    api("/api/accounts"),
    api("/api/subscriptions"),
    api(`/api/spending/breakdown?period=${periodKey()}`),
  ]);
  years = periods.years.length ? periods.years : [period.year];
  if (!years.includes(period.year)) years = [period.year, ...years].sort().reverse();
  renderPeriodControls();

  const key = periodKey();
  const ongoing = key === thisMonth() || key === thisMonth().slice(0, 4);
  $("#period-title").textContent = periodLabel() + (ongoing ? " so far" : "");
  for (const id of ["#went-period", "#came-period", "#merchants-period"]) $(id).textContent = periodLabel();
  $("#flow-title").textContent = flowView === "years" ? "Income & spending by year" : `Income & spending in ${period.year}`;
  $("#flow-hint").textContent = `Click a ${flowView === "years" ? "year" : "month"} to look closer. ` +
    "Transfers between your accounts aren't counted as income or spending.";
  document.querySelectorAll("[data-flow-view]").forEach((btn) => btn.setAttribute("aria-pressed", btn.dataset.flowView === flowView));
  document.querySelectorAll("[data-went-view]").forEach((btn) =>
    btn.setAttribute("aria-pressed", (btn.dataset.wentView === "month") === !!period.month));

  // Tiles: compare with the previous period of the same length.
  const prev = await api(`/api/spending/breakdown?period=${shiftPeriod(-1)}`);
  $("#t-income").textContent = fmt0.format(b.income);
  $("#t-spent").textContent = fmt0.format(b.total);
  $("#t-saved").textContent = (b.net < 0 ? "−" : "") + fmt0.format(Math.abs(b.net));
  $("#t-saved").className = "value " + (b.net < 0 ? "negative-text" : "");
  $("#t-income-sub").innerHTML = compare(b.income, prev.income, true);
  $("#t-spent-sub").innerHTML = compare(b.total, prev.total, false);
  $("#t-saved-sub").textContent = b.income > 0
    ? `${Math.round((b.net / b.income) * 100)}% of income` : "";
  $("#t-networth").textContent = fmt0.format(accounts.net_worth);
  $("#t-subs").textContent = `${money(subs.monthly_total)}/mo in subscriptions & bills`;

  drawFlowChart(flow);
  // The picked month or year in words, under the chart. It stays put through the
  // redraw, unlike the chart's pop-up, which a tap on a phone barely shows.
  const net = b.income - b.total;
  $("#flow-summary").innerHTML = `<b>${esc(periodLabel())}${ongoing ? " so far" : ""}</b>
    <span>Income ${money(b.income)}</span><span>Spending ${money(b.total)}</span>
    <span class="${net < 0 ? "negative-text" : ""}">${net < 0 ? "Overspent" : "Saved"} ${money(Math.abs(net))}</span>`;
  renderBreakdown(b);

  // Upcoming charges in the next 30 days
  const soon = new Date(); soon.setDate(soon.getDate() + 30);
  const today = isoDay(new Date());
  const upcoming = subs.items
    .filter((s) => s.active && !s.ignored && s.next_date >= today && new Date(s.next_date) <= soon)
    .sort((a, b) => a.next_date.localeCompare(b.next_date));
  $("#upcoming").innerHTML = upcoming.length
    ? upcoming.map((s) => itemHTML({
        title: s.name, meta: `${s.cadence} · ${s.category}`, amount: money(s.amount),
        amountMeta: dayLabel(s.next_date), category: s.category, merchant: s.merchant,
      })).join("")
    : `<div class="empty">Nothing expected in the next 30 days.</div>`;
}

// "▲ $321 vs September". Up is good for income, bad for spending.
function compare(now, before, upIsGood) {
  if (!before) return "";
  const diff = now - before;
  if (Math.abs(diff) < 0.5) return `Same as ${periodLabel(shiftPeriod(-1))}`;
  const good = (diff > 0) === upIsGood;
  return `<span class="${good ? "delta-down" : "delta-up"}">${diff > 0 ? "▲" : "▼"} ${fmt0.format(Math.abs(diff))}</span> vs ${periodLabel(shiftPeriod(-1))}`;
}

// The period `step` months (or years, when viewing a whole year) away.
function shiftPeriod(step) {
  if (!period.month) return String(Number(period.year) + step);
  const d = new Date(Number(period.year), Number(period.month) - 1 + step, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function setPeriod(key) {
  period.year = key.slice(0, 4);
  period.month = key.length === 7 ? key.slice(5) : "";
  loadDashboard();
}

function renderPeriodControls() {
  $("#period-year").innerHTML = years.map((y) => `<option${y === period.year ? " selected" : ""}>${y}</option>`).join("");
  const names = Array.from({ length: 12 }, (_, i) => new Date(2000, i, 1).toLocaleDateString("en-US", { month: "long" }));
  $("#period-month").innerHTML = `<option value="">Whole year</option>` + names.map((n, i) => {
    const m = String(i + 1).padStart(2, "0");
    return `<option value="${m}"${m === period.month ? " selected" : ""}>${n}</option>`;
  }).join("");
}

$("#period-year").onchange = (e) => setPeriod(e.target.value + (period.month ? `-${period.month}` : ""));
$("#period-month").onchange = (e) => setPeriod(period.year + (e.target.value ? `-${e.target.value}` : ""));
// The Month | Year switch on "Where it went" is a shortcut for the month menu's
// "Whole year", so the whole page follows. Going back to Month returns to the
// month you were on, or this month (December for past years).
let lastMonth = period.month;
document.querySelectorAll("[data-went-view]").forEach((btn) => btn.onclick = () => {
  if (btn.dataset.wentView === "year") {
    if (period.month) { lastMonth = period.month; setPeriod(period.year); }
  } else if (!period.month) {
    const now = thisMonth();
    setPeriod(`${period.year}-${lastMonth || (period.year === now.slice(0, 4) ? now.slice(5) : "12")}`);
  }
});
$("#period-prev").onclick = () => setPeriod(shiftPeriod(-1));
$("#period-next").onclick = () => setPeriod(shiftPeriod(1));

// Whether the chart shows the months of one year or every year side by side.
// Remembered in this browser only.
let flowView = "months";
try { flowView = localStorage.getItem("penny.flowView") === "years" ? "years" : "months"; } catch {}

document.querySelectorAll("[data-flow-view]").forEach((btn) => btn.onclick = () => {
  flowView = btn.dataset.flowView;
  try { localStorage.setItem("penny.flowView", flowView); } catch {}
  loadDashboard();
});

// Income and spending side by side, per month of the year or per year.
// Clicking a month zooms in (again goes back to the whole year); clicking a
// year shows that whole year.
function drawFlowChart(rows) {
  const byYear = flowView === "years";
  // On a phone-width chart, months are one letter and amounts short ($5k), so 24 bars fit.
  const narrow = $("#chart-monthly").parentElement.clientWidth < 520;
  const bars = rows.map((r) => byYear
    ? { ...r, key: r.year, label: narrow ? `'${r.year.slice(2)}` : r.year, title: r.year, faded: r.year !== period.year }
    : { ...r, key: r.month, label: monthLabel(r.month).split(" ")[0].slice(0, narrow ? 1 : 3), title: monthLabel(r.month, "long"),
        faded: !!period.month && r.month.slice(5) !== period.month });
  const ongoing = (key) => key === thisMonth() || key === thisMonth().slice(0, 4);
  const income = colorHex("--income"), spending = colorHex("--spending");
  const o = baseChartOptions();
  if (narrow) {
    const short = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 });
    o.scales.y.ticks.callback = (v) => short.format(v);
    o.scales.y.ticks.maxTicksLimit = 5;
    o.scales.x.ticks.maxRotation = 0;
    o.scales.x.ticks.autoSkip = false;
  }
  o.plugins.tooltip.displayColors = true;
  o.plugins.tooltip.callbacks = {
    title: (items) => { const b = bars[items[0].dataIndex]; return b.title + (ongoing(b.key) ? " (so far)" : ""); },
    label: (c) => ` ${c.dataset.label}: ${money(c.parsed.y)}`,
    footer: (items) => {
      const b = bars[items[0].dataIndex];
      const net = b.income - b.spending;
      return `${net < 0 ? "Overspent" : "Saved"}: ${money(Math.abs(net))}`;
    },
  };
  o.plugins.tooltip.footerColor = css("--text");
  o.onClick = (_, els) => {
    if (!els.length) return;
    const key = bars[els[0].index].key;
    if (byYear) setPeriod(key);
    else setPeriod(period.month === key.slice(5) ? period.year : key);
  };
  o.onHover = (e, els) => (e.native.target.style.cursor = els.length ? "pointer" : "default");
  const bar = { borderRadius: { topLeft: narrow ? 2 : 4, topRight: narrow ? 2 : 4 }, borderSkipped: "bottom",
                maxBarThickness: byYear ? 48 : 22, categoryPercentage: narrow ? 0.82 : 0.7, barPercentage: 0.9 };
  const fill = (color) => bars.map((b) => (b.faded ? color + "59" : color));
  drawChart("chart-monthly", {
    type: "bar",
    data: {
      labels: bars.map((b) => b.label),
      datasets: [
        { label: "Income", data: bars.map((b) => b.income), backgroundColor: fill(income), ...bar },
        { label: "Spending", data: bars.map((b) => b.spending), backgroundColor: fill(spending), ...bar },
      ],
    },
    options: o,
  });
}

// CSS custom properties can point at other properties; Chart.js needs the final hex.
function colorHex(name) {
  const probe = document.createElement("span");
  probe.style.color = `var(${name})`;
  document.body.appendChild(probe);
  const [r, g, b] = getComputedStyle(probe).color.match(/\d+/g).map(Number);
  probe.remove();
  return "#" + [r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("");
}

function renderBreakdown(b) {
  const pct = (v) => Math.round((v / b.total) * 100);
  const max = Math.max(...b.categories.map((c) => c.total), 1);
  $("#category-bars").innerHTML = b.categories.length
    ? b.categories.map((c) => `
        <div class="bar-row" data-category="${esc(c.category)}" data-month="${b.period}" title="See transactions">
          <span class="bar-label">${dot(c.category)}${esc(c.category)}</span>
          <span class="bar-value">${money(c.total)} · ${pct(c.total)}%</span>
          <div class="bar-track"><div class="bar-fill" style="width:${(c.total / max) * 100}%;background:${colorVar(c.category)}"></div></div>
        </div>`).join("")
    : `<div class="empty">No spending in ${periodLabel(b.period)}.</div>`;
  const listRow = (m, meta) => itemHTML({
    title: m.name, meta, amount: money(m.total), category: m.category || null, merchant: m.merchant,
  }).replace('<div class="item">', `<div class="item clickable" data-tx-merchant="${esc(m.merchant)}" data-month="${b.period}">`);
  $("#merchant-list").innerHTML = b.merchants.length
    ? b.merchants.map((m) => listRow(m, `${m.category} · ${m.count} ${m.count === 1 ? "charge" : "charges"}`)).join("")
    : `<div class="empty">No spending in ${periodLabel(b.period)}.</div>`;
  renderIncome(b);
  drawDonut(b);
}

// Income by source, drawn like spending by category. Sources color by their
// all-time rank (from the server), not this period's, so colors don't shuffle.
const sourceColorVar = (s) => s.color_slot ? `var(--series-${s.color_slot})` : "var(--series-other)";

function renderIncome(b) {
  const sources = b.income_sources;
  const max = Math.max(...sources.map((s) => s.total), 1);
  const pct = (v) => Math.round((v / b.income) * 100);
  $("#income-bars").innerHTML = sources.length
    ? sources.map((s) => `
        <div class="bar-row" data-tx-merchant="${esc(s.merchant)}" data-month="${b.period}" title="See transactions">
          <span class="bar-label"><span class="dot" style="background:${sourceColorVar(s)}" aria-hidden="true"></span>${esc(s.name)}</span>
          <span class="bar-value">${money(s.total)} · ${pct(s.total)}% · ${s.count} ${s.count === 1 ? "deposit" : "deposits"}</span>
          <div class="bar-track"><div class="bar-fill" style="width:${(s.total / max) * 100}%;background:${sourceColorVar(s)}"></div></div>
        </div>`).join("")
    : `<div class="empty">No income in ${periodLabel(b.period)}.</div>`;

  // Slices in color-slot order, like the spending donut; uncolored sources
  // (and any past the top 10 listed) share one gray slice.
  const colored = sources.filter((s) => s.color_slot).sort((x, y) => x.color_slot - y.color_slot);
  const slices = colored.map((s) => ({ label: s.name, value: s.total, color: css(`--series-${s.color_slot}`), merchant: s.merchant }));
  const rest = Math.round((b.income - colored.reduce((sum, s) => sum + s.total, 0)) * 100) / 100;
  if (rest > 0) {
    slices.push({ label: OTHER_LABEL, value: rest, color: colorValue(OTHER_LABEL),
                  parts: sources.filter((s) => !s.color_slot).map((s) => s.name) });
  }
  $("#income-donut-total").textContent = fmt0.format(b.income);
  $("#income-donut-month").textContent = periodLabel(b.period);
  drawDonutChart("chart-income-donut", slices, b.income, (s) => {
    txFilters = { month: b.period, category: "", q: s.merchant };
    location.hash = "transactions";
  });
}

// Donut slices go in the fixed palette order, so each color always sits
// next to the same neighbors. Only categories without a color merge into one gray slice.
function drawDonut(b) {
  const byCat = Object.fromEntries(b.categories.map((c) => [c.category, c.total]));
  const colored = categories.filter((c) => c.color_slot && byCat[c.name])
    .sort((a, b) => a.color_slot - b.color_slot).map((c) => c.name);
  const slices = colored.map((c) => ({ label: c, value: byCat[c], color: colorValue(c) }));
  const rest = b.categories.filter((c) => !colored.includes(c.category));
  if (rest.length) {
    slices.push({
      label: OTHER_LABEL, value: rest.reduce((sum, c) => sum + c.total, 0),
      color: colorValue(OTHER_LABEL), parts: rest.map((c) => c.category),
    });
  }
  $("#donut-total").textContent = fmt0.format(b.total);
  $("#donut-month").textContent = periodLabel(b.period);
  drawDonutChart("chart-donut", slices, b.total, (s) => {
    txFilters = { month: b.period, category: s.label, q: "" };
    location.hash = "transactions";
  });
}

// A donut of `slices` ({label, value, color, parts?}). Clicking a slice calls
// onPick, except the gray one that groups several (`parts`).
function drawDonutChart(id, slices, total, onPick) {
  drawChart(id, {
    type: "doughnut",
    data: {
      labels: slices.map((s) => s.label),
      datasets: [{
        data: slices.map((s) => s.value),
        backgroundColor: slices.map((s) => s.color),
        borderColor: css("--surface"), borderWidth: 2, hoverOffset: 6,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, cutout: "68%",
      layout: { padding: 8 },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: css("--surface"), titleColor: css("--text"), bodyColor: css("--text-2"),
          borderColor: css("--axis"), borderWidth: 1, padding: 10, boxPadding: 4,
          callbacks: {
            label: (c) => ` ${money(c.parsed)} · ${Math.round((c.parsed / total) * 100)}%`,
            afterLabel: (c) => slices[c.dataIndex].parts ? ` ${slices[c.dataIndex].parts.join(", ")}` : "",
          },
        },
      },
      onClick: (_, els) => {
        const s = els.length && slices[els[0].index];
        if (s && !s.parts) onPick(s);
      },
      onHover: (e, els) => (e.native.target.style.cursor = els.length ? "pointer" : "default"),
    },
  });
}

$("#view-dashboard").addEventListener("click", (e) => {
  const row = e.target.closest("[data-tx-merchant]");
  if (!row) return;
  txFilters = { month: row.dataset.month, category: "", q: row.dataset.txMerchant };
  location.hash = "transactions";
});

// One row in a list. `amount`, `extra` and `actions` are HTML; the rest is text.
function itemHTML({ title, meta = "", amount = "", amountMeta = "", extra = "", category = null, merchant = null, actions = "", icon = true }) {
  return `<div class="item">
    ${icon ? avatar(merchant, title) : ""}
    <div class="main"><div class="title">${esc(title)}${extra}</div><div class="meta">${category ? dot(category) : ""}${esc(meta)}</div></div>
    <div class="amount">${amount}${amountMeta ? `<div class="meta">${esc(amountMeta)}</div>` : ""}</div>
    ${actions}
  </div>`;
}

$("#category-bars").addEventListener("click", (e) => {
  const row = e.target.closest(".bar-row");
  if (!row) return;
  txFilters = { month: row.dataset.month, category: row.dataset.category, q: "" };
  location.hash = "transactions";
});

// ---------- review ----------
// New transactions wait here until you check their category. ✓ confirms it;
// picking another category changes just that purchase (and checks it), with
// "Use for all" to change the merchant's default too.

let reviewShow = "unchecked";
let reviewRemaining = 0;
// In "Unchecked", a row you just changed stays until you leave the page, so its
// "Use for all" link is still there to click.
const keepVisible = new Set();

function showReviewCount(n) {
  reviewRemaining = n;
  const badge = $("#review-count");
  badge.hidden = !n;
  badge.textContent = n > 99 ? "99+" : n;
}

async function loadReview() {
  const r = await api(`/api/review?show=${reviewShow}`);
  if (reviewShow === "unchecked" && keepVisible.size) {
    const recent = await api("/api/review?show=recent&limit=500");
    const kept = recent.items.filter((t) => keepVisible.has(t.id));
    r.items = [...kept, ...r.items.filter((t) => !keepVisible.has(t.id))].sort((a, b) => a.posted - b.posted);
  }
  showReviewCount(r.remaining);
  document.querySelectorAll("[data-review-show]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.reviewShow === reviewShow));
  $("#review-title").textContent = reviewShow === "unchecked" ? "To review" : "Last 60 days";
  $("#review-sub").textContent = r.remaining
    ? `${r.remaining} ${r.remaining === 1 ? "transaction" : "transactions"} not checked yet` : "All caught up";
  $("#review-all").hidden = !r.remaining;
  $("#review-list").innerHTML = r.items.length ? r.items.map(reviewRow).join("")
    : `<div class="empty">${reviewShow === "unchecked"
        ? "Nothing to review. New transactions show up here after each sync."
        : "No transactions in the last 60 days."}</div>`;
}

function reviewRow(t) {
  const options = categoryNames().map((c) => `<option${c === t.category ? " selected" : ""}>${esc(c)}</option>`).join("");
  const id = esc(t.id);
  const after = t.edited
    ? `<div class="meta review-after">Changed for this purchase ·
         <button class="link-btn" data-review-all-merchant="${id}" data-merchant="${esc(t.merchant)}" data-category="${esc(t.category)}"${t.description_rule ? ` data-pattern="${esc(t.description_rule)}"` : ""}>Use for all ${esc(t.name)}</button></div>` : "";
  const check = t.reviewed
    ? `<button class="link-btn review-done" data-review-undo="${id}" title="Mark as not checked">Checked · Undo</button>`
    : `<button class="btn small secondary review-ok" data-review-ok="${id}" title="The category is right">✓</button>`;
  return `<div class="item review-row${t.reviewed ? " checked" : ""}" data-review-row="${id}">
      ${avatar(t.merchant, t.name)}
      <div class="main">
        <div class="title">${esc(t.name)}${t.pending ? '<span class="pending">pending</span>' : ""}</div>
        <div class="meta">${dayLabel(t.date)} · ${esc(t.account_name)}</div>
        <div class="meta example">${esc(t.description)}</div>
      </div>
      <div class="amount ${t.amount > 0 ? "positive" : ""}">${t.amount > 0 ? "+" : ""}${money(t.amount)}</div>
      <div class="review-controls">
        <label class="m-category">${dot(t.category)}<select data-review-cat="${id}" aria-label="Category for ${esc(t.name)}">${options}</select></label>
        ${after}
      </div>
      ${check}
    </div>`;
}

document.querySelectorAll("[data-review-show]").forEach((btn) => btn.onclick = () => {
  reviewShow = btn.dataset.reviewShow;
  loadReview();
});

$("#review-list").addEventListener("change", async (e) => {
  const sel = e.target.closest("[data-review-cat]");
  if (!sel) return;
  keepVisible.add(sel.dataset.reviewCat);
  await api("/api/transaction-category", { method: "PUT", body: { id: sel.dataset.reviewCat, category: sel.value } });
  loadReview();
});

$("#review-list").addEventListener("click", async (e) => {
  const ok = e.target.closest("[data-review-ok]");
  const undo = e.target.closest("[data-review-undo]");
  const all = e.target.closest("[data-review-all-merchant]");
  if (ok) {
    const row = ok.closest("[data-review-row]");
    if (reviewShow === "unchecked") row.remove();  // gone right away; the reload confirms it
    await api("/api/review", { method: "POST", body: { ids: [ok.dataset.reviewOk] } });
  } else if (undo) {
    keepVisible.delete(undo.dataset.reviewUndo);
    await api("/api/review", { method: "POST", body: { ids: [undo.dataset.reviewUndo], reviewed: false } });
  } else if (all) {
    // Same as "Use for all" on the Transactions page: the merchant's default (or
    // the description rule that set it) takes this category, and this purchase follows it.
    const { merchant, category, pattern, reviewAllMerchant: id } = all.dataset;
    if (pattern) await api("/api/description-rules", { method: "PUT", body: { merchant, pattern, category } });
    else await api("/api/merchant-rules", { method: "PUT", body: { merchant, category } });
    await api(`/api/transaction-category?id=${encodeURIComponent(id)}`, { method: "DELETE" });
  } else return;
  loadReview();
});

$("#review-all").onclick = async () => {
  const n = reviewRemaining;
  if (n > 20 && !confirm(`Mark all ${n} as checked? Their categories stay as they are.`)) return;
  await api("/api/review", { method: "POST", body: { all_unchecked: true } });
  keepVisible.clear();
  loadReview();
};

// ---------- subscriptions ----------

async function loadSubscriptions() {
  const subs = await api("/api/subscriptions");
  const active = subs.items.filter((s) => s.active && !s.ignored);
  const activeSubs = active.filter((s) => s.kind === "subscription");
  const activeBills = active.filter((s) => s.kind === "bill");
  const inactive = subs.items.filter((s) => !s.active && !s.ignored);
  const ignored = subs.items.filter((s) => s.ignored);
  const count = (n, word) => `${n} ${word}${n === 1 ? "" : "s"} · per month`;

  $("#s-monthly").textContent = money(subs.subscriptions_monthly);
  $("#s-count").textContent = count(activeSubs.length, "subscription");
  $("#b-monthly").textContent = money(subs.bills_monthly);
  $("#b-count").textContent = count(activeBills.length, "bill");
  $("#s-yearly").textContent = fmt0.format(subs.yearly_total);

  const row = (s, action, label) => {
    const badge = s.previous_amount != null
      ? `<span class="badge">${s.amount > s.previous_amount ? "▲ Price up" : "▼ Price down"} from ${money(s.previous_amount)}</span>` : "";
    const meta = s.active
      ? `${s.cadence} · next ${dayLabel(s.next_date)}`
      : `${s.cadence} · last charged ${dayLabel(s.last_date)}`;
    const options = categoryNames().map((c) => `<option${c === s.category ? " selected" : ""}>${esc(c)}</option>`).join("");
    const select = `<select data-sub-merchant="${esc(s.merchant)}"${s.description_rule ? ` data-pattern="${esc(s.description_rule)}" title="Set by a rule for descriptions containing “${esc(s.description_rule)}”"` : ""} aria-label="Category for ${esc(s.name)}">${options}</select>`;
    const other = s.kind === "bill" ? "subscription" : "bill";
    const move = s.active && !s.ignored
      ? `<button class="link-btn" data-sub-kind="${other}" data-key="${esc(s.key)}">Move to ${other}s</button>` : "";
    const btn = `<button class="link-btn" data-sub-action="${action}" data-key="${esc(s.key)}">${label}</button>`;
    return itemHTML({
      title: s.name, meta, amount: money(s.amount),
      amountMeta: s.cadence === "monthly" ? "" : `${money(s.monthly_cost)}/mo`,
      extra: badge, category: s.category, merchant: s.merchant,
      actions: `<div class="sub-controls">${select}<div class="sub-buttons">${move}${btn}</div></div>`,
    });
  };
  const fill = (el, list, action, label, empty) =>
    ($(el).innerHTML = list.length ? list.map((s) => row(s, action, label)).join("") : `<div class="empty">${empty}</div>`);

  fill("#subs-active", activeSubs, "ignore", "Not recurring", "No subscriptions found yet. They show up after 2 to 3 charges.");
  fill("#bills-active", activeBills, "ignore", "Not recurring", "No bills found yet. Rent and utilities show up here after 2 to 3 charges.");
  fill("#subs-inactive", inactive, "ignore", "Hide", "Nothing here.");
  fill("#subs-ignored", ignored, "restore", "Restore", "Nothing hidden.");
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-sub-action]");
  if (!btn) return;
  await api("/api/subscriptions/override", {
    method: "POST",
    body: { merchant: btn.dataset.key, status: btn.dataset.subAction === "ignore" ? "ignored" : null },
  });
  loadSubscriptions();
});

$("#view-subscriptions").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-sub-kind]");
  if (!btn) return;
  await api("/api/subscriptions/kind", { method: "PUT", body: { key: btn.dataset.key, kind: btn.dataset.subKind } });
  loadSubscriptions();
});

$("#view-subscriptions").addEventListener("change", async (e) => {
  const sel = e.target.closest("select[data-sub-merchant]");
  if (!sel) return;
  const { subMerchant: merchant, pattern } = sel.dataset;
  // Same as the transactions list: a category set by a description rule changes that rule.
  if (pattern) {
    await api("/api/description-rules", { method: "PUT", body: { merchant, pattern, category: sel.value } });
  } else {
    await api("/api/merchant-rules", { method: "PUT", body: { merchant, category: sel.value } });
  }
  loadSubscriptions();
});

// ---------- transactions ----------

let txFilters = { month: "", category: "", q: "" };
let searchTimer;

async function loadTransactions() {
  const monthSel = $("#tx-month"), catSel = $("#tx-category");
  // Rebuilt every time, since imports and syncs can add months.
  const p = await api("/api/periods");
  monthSel.innerHTML = `<option value="">All dates</option>` + p.years.map((y) =>
    `<optgroup label="${y}"><option value="${y}">All of ${y}</option>` +
    p.months.filter((m) => m.startsWith(y)).map((m) => `<option value="${m}">${monthLabel(m, "long")}</option>`).join("") +
    `</optgroup>`).join("");
  // Rebuilt every time, since categories can be added or deleted.
  catSel.innerHTML = `<option value="">All categories</option>` +
    categoryNames().map((c) => `<option>${esc(c)}</option>`).join("");
  if (!categoryNames().includes(txFilters.category)) txFilters.category = "";
  monthSel.value = txFilters.month;
  catSel.value = txFilters.category;
  $("#tx-search").value = txFilters.q;

  const params = new URLSearchParams(Object.entries(txFilters).filter(([, v]) => v));
  const rows = await api(`/api/transactions?${params}`);
  const catOptions = (current) => categoryNames().map((c) => `<option${c === current ? " selected" : ""}>${esc(c)}</option>`).join("");
  $("#tx-body").innerHTML = rows.length
    ? rows.map((t) => `<tr>
        <td>${dayLabel(t.date)}</td>
        <td class="desc"><div class="tx-merchant">${avatar(t.merchant, t.name, "avatar small")}<div class="tx-name">${esc(t.name)}${t.pending ? '<span class="pending">pending</span>' : ""}<div class="meta">${esc(t.description)}</div></div></div></td>
        <td class="cat">${dot(t.category)}<select data-tx-id="${esc(t.id)}" aria-label="Category">${catOptions(t.category)}</select>${t.edited
          ? `<div class="meta tx-edited">Just this one ·
              <button class="link-btn" data-tx-reset="${esc(t.id)}" title="Back to ${esc(t.default_category)}">Reset</button> ·
              <button class="link-btn" data-tx-default="${esc(t.id)}" data-merchant="${esc(t.merchant)}" data-category="${esc(t.category)}"${t.description_rule ? ` data-pattern="${esc(t.description_rule)}"` : ""}>Use for all ${t.description_rule ? `“${esc(t.description_rule)}”` : esc(t.name)}</button></div>`
          : ""}</td>
        <td class="muted">${esc(t.account_name)}</td>
        <td class="num ${t.amount > 0 ? "positive" : ""}">${t.amount > 0 ? "+" : ""}${money(t.amount)}</td>
      </tr>`).join("")
    : `<tr><td colspan="5" class="empty">No transactions match.</td></tr>`;
}

$("#tx-month").onchange = (e) => { txFilters.month = e.target.value; loadTransactions(); };
$("#tx-category").onchange = (e) => { txFilters.category = e.target.value; loadTransactions(); };
$("#tx-search").oninput = (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { txFilters.q = e.target.value; loadTransactions(); }, 250);
};
// The dropdown changes just that purchase. A merchant's default category is set
// on the Merchants page, or from an edited purchase with "Use for all".
$("#tx-body").addEventListener("change", async (e) => {
  if (!e.target.matches("select[data-tx-id]")) return;
  await api("/api/transaction-category", { method: "PUT", body: { id: e.target.dataset.txId, category: e.target.value } });
  loadTransactions();
  refreshStatus();  // choosing a category checks it, so the Review count drops
});
$("#tx-body").addEventListener("click", async (e) => {
  const reset = e.target.closest("[data-tx-reset]");
  const makeDefault = e.target.closest("[data-tx-default]");
  if (reset) {
    await api(`/api/transaction-category?id=${encodeURIComponent(reset.dataset.txReset)}`, { method: "DELETE" });
  } else if (makeDefault) {
    const { merchant, category, pattern, txDefault } = makeDefault.dataset;
    // A purchase under a description rule changes that rule, which wins over the merchant's default.
    if (pattern) await api("/api/description-rules", { method: "PUT", body: { merchant, pattern, category } });
    else await api("/api/merchant-rules", { method: "PUT", body: { merchant, category } });
    await api(`/api/transaction-category?id=${encodeURIComponent(txDefault)}`, { method: "DELETE" });
  } else return;
  loadTransactions();
});

// ---------- merchants ----------

let merchantRows = [];
let logoTarget = null;  // merchant whose picture is being picked

async function loadMerchants() {
  [merchantRows] = await Promise.all([api("/api/merchants"), refreshLogos(),
    ...[...openDescriptions].map(loadDescriptions)]);
  renderMerchants();
}

function renderMerchants() {
  const q = $("#m-search").value.trim().toLowerCase();
  const filter = $("#m-filter").value;
  const rows = merchantRows.filter((m) =>
    (!q || m.name.toLowerCase().includes(q) || m.example.toLowerCase().includes(q)) &&
    (filter !== "rules" || m.rule_category) &&
    (filter !== "logos" || logos[m.merchant]));

  $("#merchant-rows").innerHTML = rows.length ? rows.map((m) => {
    const key = esc(m.merchant);
    const effective = m.rule_category || m.auto_category;
    const options = `<option value="">Automatic (${esc(m.auto_category)})</option>` +
      categoryNames().map((c) => `<option value="${esc(c)}"${c === m.rule_category ? " selected" : ""}>${esc(c)}</option>`).join("");
    const hasLogo = !!logos[m.merchant];
    const open = openDescriptions.has(m.merchant);
    const descTag = m.description_rules
      ? `<span class="rule-tag">${m.description_rules} description ${m.description_rules === 1 ? "rule" : "rules"}</span>` : "";
    const descToggle = m.variants > 1 || m.description_rules
      ? ` · <button class="link-btn" data-desc-toggle="${key}" aria-expanded="${open}">${open ? "Hide" : "Descriptions"} (${m.variants})</button>` : "";
    return `<div class="item merchant-row">
      <button class="avatar-btn" data-logo-pick="${key}" title="${hasLogo ? "Change" : "Add"} picture for ${esc(m.name)}">${avatar(m.merchant, m.name)}</button>
      <div class="main">
        <div class="title">${esc(m.name)}${m.rule_category ? '<span class="rule-tag">Rule</span>' : ""}${descTag}</div>
        <div class="meta">${m.count} ${m.count === 1 ? "transaction" : "transactions"} · ${money(m.spent)} spent · last ${dayLabel(m.last_date)}${descToggle} · <button class="link-btn" data-combine="${key}">Combine…</button></div>
        <div class="meta example">${esc(m.example)}</div>${m.combined.length ? `
        <div class="meta combined">Includes ${m.combined.map((c) =>
          `${esc(c.name)} <button class="link-btn" data-split="${esc(c.merchant)}" title="Make ${esc(c.name)} its own merchant again">Separate</button>`).join(", ")}</div>` : ""}
      </div>
      <div class="m-controls">
        <label class="m-category">${dot(effective)}<select data-rule-merchant="${key}" aria-label="Category for ${esc(m.name)}">${options}</select></label>
        <div class="m-photo">
          <button class="link-btn" data-logo-pick="${key}">${hasLogo ? "Change picture" : "Add picture"}</button>
          ${hasLogo ? `<button class="link-btn" data-logo-remove="${key}">Remove</button>` : ""}
        </div>
      </div>
    </div>${open ? `<div class="m-descriptions" data-desc-panel="${key}">${descriptionPanel(m)}</div>` : ""}`;
  }).join("") : `<div class="empty">No merchants match.</div>`;
}

// ---------- a merchant's descriptions & description rules ----------

const openDescriptions = new Set();   // merchants whose descriptions are showing
const descriptionData = new Map();    // merchant -> /api/merchant-descriptions result

async function loadDescriptions(merchant) {
  descriptionData.set(merchant, await api(`/api/merchant-descriptions?merchant=${encodeURIComponent(merchant)}`));
}

// Each different description (numbers ignored) gets its own category choice.
// Choosing one saves a rule for descriptions like that one, which wins over
// the merchant's own category. Broader rules ("contains cashout") can be added below.
function descriptionPanel(m) {
  const data = descriptionData.get(m.merchant);
  if (!data) return `<div class="empty">Loading…</div>`;
  const merchantCategory = m.rule_category || m.auto_category;
  const key = esc(m.merchant);
  const options = (selected) => categoryNames().map((c) => `<option value="${esc(c)}"${c === selected ? " selected" : ""}>${esc(c)}</option>`).join("");
  const rows = data.descriptions.map((d) => {
    const own = d.rule === d.key;
    const fallback = d.rule && !own ? `Rule “${d.rule}” (${d.category})` : `Same as merchant (${merchantCategory})`;
    return `<div class="desc-row">
      <div>
        <div class="desc-text">${dot(d.category)}${esc(d.example)}</div>
        <div class="meta">${d.count} ${d.count === 1 ? "transaction" : "transactions"} · ${money(d.total)} · last ${dayLabel(d.last_date)}</div>
      </div>
      <select data-desc-merchant="${key}" data-desc-key="${esc(d.key)}" aria-label="Category for this description">
        <option value="">${esc(fallback)}</option>${options(own ? d.category : null)}
      </select>
    </div>`;
  }).join("");
  const exact = new Set(data.descriptions.map((d) => d.key));
  const broad = data.rules.filter((r) => !exact.has(r.pattern));
  const broadRows = broad.map((r) => `<div class="desc-row">
      <div class="desc-text">${dot(r.category)}Descriptions containing <span class="rule-pattern">${esc(r.pattern)}</span> → ${esc(r.category)}</div>
      <button class="link-btn danger-link" data-desc-remove="${key}" data-pattern="${esc(r.pattern)}">Remove</button>
    </div>`).join("");
  return `${rows}${broadRows}
    <form class="row" data-desc-add="${key}">
      <input name="pattern" placeholder="Description contains… (e.g. cashout)" aria-label="Description contains" required>
      <select name="category" aria-label="Category">${options(null)}</select>
      <button class="btn small">Add rule</button>
    </form>
    <p class="msg" data-desc-msg="${key}"></p>`;
}

async function refreshDescriptions(merchant) {
  await Promise.all([loadDescriptions(merchant), api("/api/merchants").then((rows) => { merchantRows = rows; })]);
  renderMerchants();
}

$("#merchant-rows").addEventListener("change", async (e) => {
  const sel = e.target.closest("select[data-desc-key]");
  if (!sel) return;
  const merchant = sel.dataset.descMerchant, pattern = sel.dataset.descKey;
  if (sel.value) {
    await api("/api/description-rules", { method: "PUT", body: { merchant, pattern, category: sel.value } });
  } else {
    await api(`/api/description-rules?merchant=${encodeURIComponent(merchant)}&pattern=${encodeURIComponent(pattern)}`, { method: "DELETE" });
  }
  refreshDescriptions(merchant);
});

$("#merchant-rows").addEventListener("submit", async (e) => {
  const form = e.target.closest("form[data-desc-add]");
  if (!form) return;
  e.preventDefault();
  const merchant = form.dataset.descAdd;
  try {
    await api("/api/description-rules", { method: "PUT", body: { merchant, pattern: form.pattern.value, category: form.category.value } });
    refreshDescriptions(merchant);
  } catch (err) {
    const msg = [...document.querySelectorAll("[data-desc-msg]")].find((p) => p.dataset.descMsg === merchant);
    if (msg) { msg.className = "msg error"; msg.textContent = err.message; }
  }
});

$("#m-search").oninput = renderMerchants;
$("#m-filter").onchange = renderMerchants;

$("#merchant-rows").addEventListener("change", async (e) => {
  const sel = e.target.closest("select[data-rule-merchant]");
  if (!sel) return;
  const merchant = sel.dataset.ruleMerchant;
  if (sel.value) {
    await api("/api/merchant-rules", { method: "PUT", body: { merchant, category: sel.value } });
  } else {
    await api(`/api/merchant-rules?merchant=${encodeURIComponent(merchant)}`, { method: "DELETE" });
  }
  loadMerchants();
});

$("#merchant-rows").addEventListener("click", async (e) => {
  const pick = e.target.closest("[data-logo-pick]");
  const remove = e.target.closest("[data-logo-remove]");
  const toggle = e.target.closest("[data-desc-toggle]");
  const removeRule = e.target.closest("[data-desc-remove]");
  const combine = e.target.closest("[data-combine]");
  const split = e.target.closest("[data-split]");
  if (combine) {
    openCombine(combine.dataset.combine);
  } else if (split) {
    await api(`/api/merchants/combine?alias=${encodeURIComponent(split.dataset.split)}`, { method: "DELETE" });
    loadMerchants();
  } else if (toggle) {
    const merchant = toggle.dataset.descToggle;
    if (openDescriptions.delete(merchant)) return renderMerchants();
    openDescriptions.add(merchant);
    renderMerchants();
    await loadDescriptions(merchant);
    renderMerchants();
  } else if (removeRule) {
    const merchant = removeRule.dataset.descRemove;
    await api(`/api/description-rules?merchant=${encodeURIComponent(merchant)}&pattern=${encodeURIComponent(removeRule.dataset.pattern)}`, { method: "DELETE" });
    refreshDescriptions(merchant);
  } else if (pick) {
    logoTarget = pick.dataset.logoPick;
    $("#logo-input").value = "";
    $("#logo-input").click();
  } else if (remove) {
    await api(`/api/merchant-logo?merchant=${encodeURIComponent(remove.dataset.logoRemove)}`, { method: "DELETE" });
    loadMerchants();
  }
});

// ---------- combining merchants ----------
// One payer or seller can show up under several names ("Acme Corp",
// "Acme Payroll"). Combining counts them all as the one you started from.

let combineTarget = null;
const combineChecked = new Set();

function openCombine(merchant) {
  combineTarget = merchantRows.find((m) => m.merchant === merchant);
  combineChecked.clear();
  $("#combine-title").textContent = `Combine with ${combineTarget.name}`;
  $("#combine-text").textContent = `Pick the merchants that are really ${combineTarget.name}. Their transactions will count as ${combineTarget.name}, here and on the dashboard.`;
  $("#combine-msg").textContent = "";
  // Start the search with the first word, which finds most other spellings.
  $("#combine-search").value = combineTarget.name.split(" ")[0];
  renderCombineOptions();
  $("#combine-dialog").showModal();
}

function renderCombineOptions() {
  const q = $("#combine-search").value.trim().toLowerCase();
  const others = merchantRows.filter((m) => m.merchant !== combineTarget.merchant &&
    (!q || m.name.toLowerCase().includes(q) || m.example.toLowerCase().includes(q) || combineChecked.has(m.merchant)));
  $("#combine-options").innerHTML = others.length ? others.slice(0, 100).map((m) => `
      <label><input type="checkbox" value="${esc(m.merchant)}"${combineChecked.has(m.merchant) ? " checked" : ""}>
        ${avatar(m.merchant, m.name, "avatar small")}<span>${esc(m.name)}</span>
        <span class="meta">${m.count} ${m.count === 1 ? "transaction" : "transactions"}</span></label>`).join("")
    : `<div class="empty">No other merchants match.</div>`;
}

$("#combine-search").oninput = renderCombineOptions;
$("#combine-options").onchange = (e) => {
  if (e.target.checked) combineChecked.add(e.target.value); else combineChecked.delete(e.target.value);
};
$("#combine-form").onsubmit = async (e) => {
  if (e.submitter?.value !== "combine") return;
  e.preventDefault();
  try {
    await api("/api/merchants/combine", { method: "POST", body: { merchant: combineTarget.merchant, others: [...combineChecked] } });
    $("#combine-dialog").close();
    loadMerchants();
  } catch (err) {
    $("#combine-msg").className = "msg error"; $("#combine-msg").textContent = err.message;
  }
};

$("#logo-input").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file || !logoTarget) return;
  try {
    const image = await squareImage(file);
    await api("/api/merchant-logo", { method: "PUT", body: { merchant: logoTarget, image } });
    loadMerchants();
  } catch (err) {
    alert(err.message);
  }
};

// Center-crop to a square and shrink, so uploads stay small whatever the source.
function squareImage(file, size = 160) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const side = Math.min(img.naturalWidth, img.naturalHeight);
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = size;
      canvas.getContext("2d").drawImage(img,
        (img.naturalWidth - side) / 2, (img.naturalHeight - side) / 2, side, side, 0, 0, size, size);
      URL.revokeObjectURL(url);
      resolve(canvas.toDataURL("image/png"));
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("That file couldn't be opened as an image. Try a PNG or JPEG."));
    };
    img.src = url;
  });
}

// ---------- accounts / net worth ----------

async function loadAccounts() {
  const a = await api("/api/accounts");
  $("#a-networth").textContent = fmt0.format(a.net_worth);
  $("#a-assets").textContent = fmt0.format(a.assets);
  $("#a-debts").textContent = fmt0.format(Math.abs(a.debts));

  const o = baseChartOptions();
  o.plugins.tooltip.callbacks.title = (items) => new Date(a.history[items[0].dataIndex].day + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  o.scales.x.ticks.maxTicksLimit = 8;
  o.scales.y.beginAtZero = false;
  drawChart("chart-networth", {
    type: "line",
    data: {
      labels: a.history.map((h) => new Date(h.day + "T12:00:00").toLocaleDateString("en-US", { month: "short", year: "2-digit" })),
      datasets: [{
        data: a.history.map((h) => h.net_worth),
        borderColor: css("--accent"), borderWidth: 2, cubicInterpolationMode: "monotone",
        pointRadius: 0, pointHoverRadius: 5, pointHoverBackgroundColor: css("--accent"),
        pointHoverBorderColor: css("--surface"), pointHoverBorderWidth: 2,
        fill: false,
      }],
    },
    options: o,
  });

  const accountRow = (acc) => `<div data-account-row="${esc(acc.id)}">${itemHTML({
        title: acc.name,
        meta: [acc.org_name, acc.nickname ? `Bank calls it “${acc.bank_name}”` : ""].filter(Boolean).join(" · "),
        amount: money(acc.balance),
        amountMeta: acc.available_balance != null && acc.available_balance !== acc.balance ? `${money(acc.available_balance)} available` : "",
        actions: `<button class="link-btn" data-rename-account="${esc(acc.id)}" data-name="${esc(acc.name)}" data-bank-name="${esc(acc.bank_name)}">Rename</button>`,
      })}</div>`;
  // Money you owe (credit cards, loans) goes under Debts; everything else, $0 included, is an asset.
  const assets = a.accounts.filter((acc) => acc.balance >= 0);
  const debts = a.accounts.filter((acc) => acc.balance < 0);
  $("#assets-total").textContent = money(a.assets);
  $("#debts-total").textContent = money(Math.abs(a.debts));
  $("#account-list").innerHTML = assets.length ? assets.map(accountRow).join("") : `<div class="empty">No accounts.</div>`;
  $("#debt-list").innerHTML = debts.length ? debts.map(accountRow).join("") : `<div class="empty">No debts.</div>`;
}

// Renaming swaps the row for a small form. Saving a blank name goes back to the bank's.
$("#view-accounts").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-rename-account]");
  if (!btn) return;
  const { renameAccount: id, name, bankName } = btn.dataset;
  const row = btn.closest("[data-account-row]");
  row.innerHTML = `<form class="item row rename-form">
      <input name="name" value="${esc(name)}" placeholder="${esc(bankName)}" maxlength="60" aria-label="Account name">
      <button class="btn small">Save</button>
      <button type="button" class="link-btn" data-rename-cancel>Cancel</button>
      <p class="msg"></p>
    </form>`;
  const form = row.querySelector("form");
  form.name.focus();
  form.name.select();
  form.querySelector("[data-rename-cancel]").onclick = loadAccounts;
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    try {
      await api("/api/accounts/name", { method: "PUT", body: { id, name: form.name.value } });
      loadAccounts();
    } catch (err) {
      const msg = form.querySelector(".msg");
      msg.className = "msg error"; msg.textContent = err.message;
    }
  };
});

// ---------- CSV import (in Settings) ----------

const imp = { text: "", headers: [], sample: [], mapping: null, fromPdf: false };

async function loadImport() {
  const [a, history] = await Promise.all([api("/api/accounts"), api("/api/import/summary")]);
  const sel = $("#imp-account"), current = sel.value;
  sel.innerHTML = a.accounts.map((acc) =>
    `<option value="${esc(acc.id)}">${esc(acc.name)}${acc.org_name ? ` · ${esc(acc.org_name)}` : ""}</option>`).join("") +
    `<option value="__new">New account (not in SimpleFIN)…</option>`;
  if ([...sel.options].some((o) => o.value === current)) sel.value = current;
  $("#imp-new-account").hidden = sel.value !== "__new";

  $("#imp-history").innerHTML = history.length ? `<h3>Imported so far</h3>` + history.map((h) => itemHTML({
    title: h.name, meta: `${h.count} imported transactions · ${rangeLabel(h.first_date, h.last_date)}`,
    actions: `<button class="link-btn danger-link" data-undo-import="${esc(h.id)}" data-manual="${h.manual}">Remove</button>`,
  })).join("") : "";
}

$("#imp-account").onchange = () => { $("#imp-new-account").hidden = $("#imp-account").value !== "__new"; };

$("#imp-file").onchange = async (e) => {
  const files = [...e.target.files];
  if (!files.length) return;
  const pdfs = files.filter((f) => /\.pdf$/i.test(f.name));
  const msg = $("#imp-msg");
  msg.textContent = "";
  $("#imp-statements").innerHTML = "";
  if (pdfs.length && pdfs.length !== files.length || !pdfs.length && files.length > 1) {
    msg.className = "msg error";
    msg.textContent = "Choose one CSV file, or one or more PDF statements, not a mix.";
    return;
  }
  imp.fromPdf = pdfs.length > 0;
  imp.fileName = imp.fromPdf ? null : files[0].name;
  if (!imp.fromPdf) {
    imp.text = await files[0].text();
    imp.mapping = null;
    return previewImport();
  }
  msg.className = "msg"; msg.textContent = `Reading ${pdfs.length} ${pdfs.length === 1 ? "statement" : "statements"}…`;
  try {
    const body = { files: await Promise.all(pdfs.map(async (f) => ({ name: f.name, data: await toBase64(f) }))) };
    const r = await api("/api/import/pdf", { method: "POST", body });
    msg.textContent = "";
    imp.text = r.csv;
    imp.mapping = r.mapping;
    await previewImport();
    renderStatements(r.statements, r.mapping.no_balance);
  } catch (err) {
    msg.className = "msg error"; msg.textContent = err.message;
  }
};

function toBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",")[1]);
    reader.onerror = () => reject(new Error(`Couldn't read ${file.name}.`));
    reader.readAsDataURL(file);
  });
}

// One line per PDF: whether its transactions add up to the statement's own totals.
function renderStatements(statements, noBalance) {
  const rows = statements.map((s) => {
    if (s.error) return `<div class="statement error"><span class="icon">✕</span><span>${esc(s.error)}</span></div>`;
    const ok = s.check?.ok;
    const note = !s.check ? "couldn't check totals"
      : ok ? "matches the statement's totals"
      : `doesn't match the statement (expected ${money(Math.abs(s.check.expected))}, read ${money(Math.abs(s.check.found))}). Check this one after importing`;
    return `<div class="statement ${ok ? "ok" : "warn"}"><span class="icon">${ok ? "✓" : "!"}</span>
      <span>${esc(s.name)} <span class="meta">${esc(s.period)} · ${s.count} transactions · ${esc(note)}</span></span></div>`;
  });
  if (noBalance) {
    rows.push(`<p class="muted">Year-end summaries list purchases and refunds but not card payments, so they add to your spending history but aren't used for past balances on the Net worth chart.</p>`);
  }
  $("#imp-statements").innerHTML = rows.join("");
}

async function previewImport() {
  const msg = $("#imp-msg");
  msg.textContent = "";
  try {
    const p = await api("/api/import/preview", { method: "POST", body: { csv: imp.text, mapping: imp.mapping } });
    imp.headers = p.headers; imp.sample = p.sample_rows; imp.mapping = p.mapping;
    renderMapping();
    renderPreview(p);
    $("#imp-mapping").hidden = false;
  } catch (err) {
    $("#imp-mapping").hidden = true;
    msg.className = "msg error"; msg.textContent = err.message;
  }
}

function renderMapping() {
  const example = (i) => imp.sample.map((r) => r[i]).find((v) => v && v.trim()) || "";
  const options = (selected, extra = "") => `<option value="">—</option>${extra}` + imp.headers.map((h, i) =>
    `<option value="${i}"${i === selected ? " selected" : ""}>${esc(h)}${example(i) ? ` (e.g. ${esc(example(i).slice(0, 24))})` : ""}</option>`).join("");
  const m = imp.mapping;
  const split = m.amount == null && (m.debit != null || m.credit != null);
  document.querySelectorAll("#imp-mapping select[data-map]").forEach((sel) => {
    const key = sel.dataset.map;
    sel.innerHTML = key === "amount"
      ? options(m.amount, `<option value="split"${split ? " selected" : ""}>Separate money in / money out columns</option>`)
      : options(m[key]);
  });
  document.querySelectorAll("#imp-mapping [data-split]").forEach((el) => (el.hidden = !split));
  $("#imp-flip").checked = !!m.flip;
  $("#imp-flip").closest("label").hidden = split || imp.fromPdf;
  // PDF statements are already in a fixed layout, so there are no columns to check.
  $("#imp-mapping .map-grid").hidden = imp.fromPdf;
  $("#imp-sign-hint").hidden = imp.fromPdf;
  $("#imp-columns-title").textContent = imp.fromPdf ? "Preview" : "Check the columns";
}

function renderPreview(p) {
  const summary = $("#imp-summary"), go = $("#imp-go");
  if (p.problem) {
    summary.className = "msg error"; summary.textContent = p.problem;
    $("#imp-preview").innerHTML = "";
    go.disabled = true;
    return;
  }
  summary.className = "msg";
  let text = `${p.count} transactions from ${rangeLabel(p.first_date, p.last_date)}.`;
  if (p.error_count) {
    text += ` ${p.error_count} ${p.error_count === 1 ? "row" : "rows"} couldn't be read and will be skipped` +
      ` (for example row ${p.errors[0].row}: "${p.errors[0].text}").`;
  }
  summary.textContent = text;
  $("#imp-preview").innerHTML = p.preview.map((t) => `<tr>
    <td>${dayLabel(t.date)}</td>
    <td class="desc">${esc(t.name)}<div class="meta">${esc(t.description)}</div></td>
    <td>${dot(t.category)}${esc(t.category)}</td>
    <td class="num ${t.amount > 0 ? "positive" : ""}">${t.amount > 0 ? "+" : ""}${money(t.amount)}</td>
  </tr>`).join("");
  go.disabled = !p.count;
  go.textContent = `Import ${p.count} transactions`;
}

$("#imp-mapping").addEventListener("change", (e) => {
  const sel = e.target.closest("select[data-map]");
  const m = imp.mapping;
  if (sel) {
    const key = sel.dataset.map, v = sel.value;
    if (key === "amount" && v === "split") {
      m.amount = null; m.flip = false;
    } else {
      m[key] = v === "" ? null : Number(v);
      if (key === "amount") { m.debit = null; m.credit = null; }
    }
  } else if (e.target.id === "imp-flip") {
    m.flip = e.target.checked;
  } else {
    return;
  }
  renderMapping();
  previewImport();
});

$("#imp-go").onclick = async () => {
  const go = $("#imp-go"), msg = $("#imp-msg");
  const account = $("#imp-account").value;
  const body = { csv: imp.text, mapping: imp.mapping, file_name: imp.fileName };
  if (account === "__new") {
    body.new_account = {
      name: $("#imp-new-name").value, org_name: $("#imp-new-org").value,
      balance: Number($("#imp-new-balance").value || 0),
    };
  } else {
    body.account_id = account;
  }
  go.disabled = true;
  try {
    const r = await api("/api/import", { method: "POST", body });
    msg.className = "msg";
    msg.textContent = `Imported ${r.imported} transactions.` +
      (r.skipped ? ` Skipped ${r.skipped} that Penny already had.` : "") +
      (r.unreadable ? ` ${r.unreadable} unreadable rows were left out.` : "");
    $("#imp-mapping").hidden = true;
    $("#imp-statements").innerHTML = "";
    $("#imp-file").value = "";
    await refreshStatus();
    await loadImport();
    if (account === "__new") $("#imp-account").value = r.account_id;
    $("#imp-new-account").hidden = $("#imp-account").value !== "__new";
  } catch (err) {
    msg.className = "msg error"; msg.textContent = err.message;
  } finally {
    go.disabled = false;
  }
};

$("#imp-history").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-undo-import]");
  if (!btn) return;
  const manual = btn.dataset.manual === "true";
  if (!confirm(manual
    ? "Remove this account and all of its imported transactions?"
    : "Remove all imported transactions from this account? Transactions from SimpleFIN stay.")) return;
  await api(`/api/import?account_id=${encodeURIComponent(btn.dataset.undoImport)}`, { method: "DELETE" });
  await refreshStatus();
  loadImport();
});

// ---------- categories (in Settings) ----------

async function refreshCategories() {
  categories = await api("/api/categories");
}

async function loadCategories() {
  await refreshCategories();
  $("#category-rows").innerHTML = categories.map((c) => `
    <div class="cat-row" data-name="${esc(c.name)}">
      <span class="cat-name">${dot(c.name)}${esc(c.name)}</span>
      <span class="muted">${c.transactions} ${c.transactions === 1 ? "transaction" : "transactions"}</span>
      ${c.locked
        ? `<span class="muted locked" title="Needed for spending totals">Built in</span>`
        : `<button class="link-btn danger-link" data-delete-category="${esc(c.name)}">Delete</button>`}
    </div>`).join("");
}

$("#add-category-form").onsubmit = async (e) => {
  e.preventDefault();
  const input = $("#new-category"), msg = $("#category-msg");
  try {
    const res = await api("/api/categories", { method: "POST", body: { name: input.value } });
    input.value = "";
    msg.className = "msg";
    msg.textContent = `Added ${res.name}. Assign merchants to it on the Merchants page.`;
    loadCategories();
  } catch (err) {
    msg.className = "msg error"; msg.textContent = err.message;
  }
};

$("#category-rows").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-delete-category]");
  if (!btn) return;
  const cat = categories.find((c) => c.name === btn.dataset.deleteCategory);
  const dlg = $("#delete-category-dialog");
  $("#delete-category-title").textContent = `Delete ${cat.name}?`;
  $("#delete-category-text").textContent = cat.transactions
    ? `Its ${cat.transactions} ${cat.transactions === 1 ? "transaction" : "transactions"} and any merchant rules will move to:`
    : "Any merchant rules and future transactions for it will move to:";
  $("#delete-category-move").innerHTML = categoryNames().filter((n) => n !== cat.name)
    .map((n) => `<option${n === "Other" ? " selected" : ""}>${esc(n)}</option>`).join("");
  dlg.dataset.name = cat.name;
  dlg.showModal();
});

$("#delete-category-form").onsubmit = async (e) => {
  const dlg = $("#delete-category-dialog");
  if (e.submitter?.value !== "delete") return;  // Cancel just closes the dialog
  try {
    const params = new URLSearchParams({ name: dlg.dataset.name, move_to: $("#delete-category-move").value });
    await api(`/api/categories?${params}`, { method: "DELETE" });
    $("#category-msg").className = "msg";
    $("#category-msg").textContent = `Deleted ${dlg.dataset.name}.`;
  } catch (err) {
    $("#category-msg").className = "msg error"; $("#category-msg").textContent = err.message;
  }
  loadCategories();
};

// ---------- your data & backups (in Settings) ----------

const BACKUP_REASONS = {
  daily: "Daily backup", manual: "Made by you", "before-delete": "Before deleting data",
  "before-restore": "Before a restore", "before-undo-import": "Before removing an import",
  "before-category-delete": "Before deleting a category",
};

async function loadData() {
  const d = await api("/api/data");
  $("#data-summary").innerHTML = d.transactions
    ? `<b>${d.transactions.toLocaleString()} transactions</b> from ${rangeLabel(d.first_date, d.last_date)} ` +
      `in ${d.accounts} ${d.accounts === 1 ? "account" : "accounts"}, saved on this Mac. ` +
      `Transactions stay in Penny after they're older than what SimpleFIN provides.`
    : "No transactions yet.";
  const o = d.originals, parts = [];
  if (o.simplefin) parts.push(`${o.simplefin} SimpleFIN ${o.simplefin === 1 ? "download" : "downloads"}`);
  if (o.pdf) parts.push(`${o.pdf} PDF ${o.pdf === 1 ? "statement" : "statements"}`);
  if (o.csv) parts.push(`${o.csv} CSV ${o.csv === 1 ? "file" : "files"}`);
  $("#data-originals").textContent = parts.length
    ? `Originals kept inside Penny's database, exactly as received: ${parts.join(", ")}.` : "";
  $("#demo-btn").hidden = d.real_data;

  const last = d.backups[0];
  $("#backup-summary").textContent = d.real_data
    ? `${last ? `Last backup ${backupTime(last)}. ` : ""}Penny backs up once a day and before anything destructive, ` +
      `keeping 14 daily and 12 monthly copies in ${d.backup_dir}.`
    : "Backups start once you connect a bank or import transactions.";
  $("#backup-list").innerHTML = d.backups.length ? d.backups.map((b) => itemHTML({
    title: backupTime(b), meta: `${BACKUP_REASONS[b.reason] || b.reason} · ${Math.max(1, Math.round(b.bytes / 1024))} KB`,
    actions: `<button class="link-btn" data-restore="${esc(b.name)}">Restore</button>`, icon: false,
  })).join("") : `<div class="empty">No backups yet.</div>`;
}

function backupTime(b) {
  const d = new Date(`${b.day}T${b.time.slice(0, 2)}:${b.time.slice(2, 4)}:00`);
  return d.toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
}

$("#backup-now").onclick = async () => {
  const msg = $("#data-msg");
  try {
    await api("/api/backups", { method: "POST" });
    msg.className = "msg"; msg.textContent = "Backed up.";
    loadData();
  } catch (err) {
    msg.className = "msg error"; msg.textContent = err.message;
  }
};
$("#backup-reveal").onclick = () => api("/api/backups/reveal", { method: "POST" });

$("#backup-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-restore]");
  if (!btn) return;
  if (!confirm("Restore this backup? Penny's data goes back to how it was then. The current data is backed up first, so you can switch back.")) return;
  const msg = $("#data-msg");
  try {
    await api("/api/backups/restore", { method: "POST", body: { name: btn.dataset.restore } });
    msg.className = "msg"; msg.textContent = "Restored.";
    await Promise.all([refreshStatus(), refreshCategories(), refreshLogos()]);
    route();
  } catch (err) {
    msg.className = "msg error"; msg.textContent = err.message;
  }
});

// ---------- getting started ----------
// A checklist on the dashboard until the main steps are done (or it's hidden),
// and one tip at the top of each page until "Got it". The server works out
// which steps are done and remembers what was dismissed.

const GUIDE_STEPS = {
  demo: { title: "Look around with demo data",
          text: "Made-up transactions, so you can see what Penny does before connecting anything." },
  connect: { title: "Connect your bank",
             text: "Through SimpleFIN Bridge, about $15 a year. It takes about 10 minutes.",
             action: `<a class="btn small" href="#settings/connect">Show me how</a>` },
  import: { title: "Bring in older history",
            text: "Your bank's CSV downloads fill in the months before your first sync.",
            action: `<a class="btn small secondary" href="#settings/import-card">Import</a>` },
  categories: { title: "Check your categories",
                text: "Penny guesses from keywords. Fix a few merchants and it remembers for next time.",
                action: `<a class="btn small secondary" href="#merchants">Open Merchants</a>` },
  subscriptions: { title: "Review subscriptions and bills",
                   text: "See what charges you on a schedule, and hide anything that isn't recurring.",
                   action: `<a class="btn small secondary" href="#subscriptions">Open Subscriptions</a>` },
  review: { title: "Check your new transactions",
            text: "New transactions wait on the Review page. Confirm each category with ✓, or pick a better one.",
            action: `<a class="btn small secondary" href="#review">Open Review</a>` },
  phone: { title: "Use Penny on your iPhone",
           text: "Through NordVPN Meshnet or Tailscale, which connect only your own devices.",
           action: `<a class="btn small secondary" href="#settings/share-card">Set it up</a>` },
};

async function loadGuide(view) {
  if (["dashboard", "review", "subscriptions", "transactions", "merchants", "accounts"].includes(view)) {
    await api("/api/guide/seen", { method: "POST", body: { view } });
  }
  const g = await api("/api/guide");
  document.querySelectorAll("[data-tip]").forEach((tip) => (tip.hidden = g.tips_dismissed.includes(tip.dataset.tip)));
  renderGuide(g);
}

function renderGuide(g) {
  const required = g.steps.filter((s) => !s.optional);
  const el = $("#guide");
  // Setting up happens on the Mac, so the phone doesn't show the checklist.
  el.hidden = status.remote || g.dismissed || required.every((s) => s.done);
  if (el.hidden) return;
  const done = g.steps.filter((s) => s.done).length;
  el.innerHTML = `
    <div class="card-head">
      <h2>Getting started</h2>
      <div class="row"><span class="muted">${done} of ${g.steps.length} done</span>
        <button class="link-btn" data-guide-dismiss>Hide</button></div>
    </div>
    <ol class="guide-steps">${g.steps.map((s, i) => {
      const step = GUIDE_STEPS[s.id];
      return `<li class="${s.done ? "done" : ""}">
        <span class="guide-mark" aria-hidden="true">${s.done ? "✓" : i + 1}</span>
        <div class="guide-text">
          <div class="title">${step.title}${s.optional ? ' <span class="muted">· Optional</span>' : ""}${s.done ? '<span class="sr-only"> (done)</span>' : ""}</div>
          <div class="meta">${step.text}</div>
        </div>
        ${!s.done && step.action ? step.action : ""}
      </li>`;
    }).join("")}</ol>`;
}

document.addEventListener("click", async (e) => {
  const tip = e.target.closest("[data-tip-dismiss]");
  const guideHide = e.target.closest("[data-guide-dismiss]");
  if (tip) {
    tip.closest("[data-tip]").hidden = true;
    await api("/api/guide/dismiss", { method: "POST", body: { view: tip.dataset.tipDismiss } });
  } else if (guideHide) {
    $("#guide").hidden = true;
    await api("/api/guide/dismiss", { method: "POST", body: {} });
  }
});

// Once connected, the connect card shrinks to a status line; the steps stay one click away.
function showConnectState(expand) {
  $("#connect-state").textContent = status.connected ? "Connected" : "";
  $("#connect-steps").open = expand || !status.connected;
}

// ---------- updates (Mac app only) ----------

async function checkForUpdate(force = false) {
  if (!status.desktop) return null;
  const u = await api(`/api/update${force ? "?force=true" : ""}`);
  const banner = $("#update-banner");
  banner.hidden = !u.available;
  if (u.available) {
    banner.innerHTML = `<div><b>Penny ${esc(u.latest)} is out.</b> You have ${esc(u.current)}.
        ${u.notes ? `<div class="meta update-notes">${esc(u.notes)}</div>` : ""}</div>
      <div class="row">
        <a class="btn small" href="${esc(u.url)}" target="_blank" rel="noopener">Download</a>
        <button class="link-btn" data-update-dismiss="${esc(u.latest)}">Not now</button>
      </div>`;
  }
  return u;
}

async function loadUpdatesCard() {
  $("#updates-card").hidden = !status.desktop;
  if (!status.desktop) return;
  const u = await api("/api/update");
  $("#app-version").textContent = `Version ${u.current}`;
  $("#updates-on").checked = u.enabled;
}

$("#updates-on").onchange = async (e) => {
  await api("/api/update", { method: "POST", body: { enabled: e.target.checked } });
};
$("#update-check").onclick = async () => {
  const msg = $("#update-msg");
  msg.className = "msg"; msg.textContent = "Checking…";
  try {
    const u = await checkForUpdate(true);
    msg.textContent = !u.enabled ? "Turn on the checkbox above to check."
      : u.available ? `Penny ${u.latest} is available. See the note at the top.` : "You have the newest version.";
  } catch (err) {
    msg.className = "msg error"; msg.textContent = err.message;
  }
};
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-update-dismiss]");
  if (!btn) return;
  $("#update-banner").hidden = true;
  await api("/api/update", { method: "POST", body: { dismiss: btn.dataset.updateDismiss } });
});

// ---------- using Penny on an iPhone (Mac side) ----------
// See app/remote.py: off until turned on, only over Meshnet/Tailscale, passcode required.

async function loadShareCard() {
  $("#share-card").hidden = status.remote;
  if (status.remote) return;
  renderShare(await api("/api/remote"));
}

function renderShare(s) {
  const addrs = [...new Set([...(s.address ? [s.address] : []), ...s.addresses])];
  $("#share-addresses").innerHTML = addrs.length
    ? addrs.map((a) => `<label class="check"><input type="radio" name="share-addr" value="${esc(a)}"${a === s.address ? " checked" : ""}>
        ${esc(a)}${s.addresses.includes(a) ? "" : ' <span class="muted">(not on this Mac right now)</span>'}</label>`).join("")
    : `<p class="msg">No Meshnet address found. Turn on Meshnet in NordVPN on this Mac, then come back.</p>`;
  $("#share-pass-state").textContent = s.has_passcode ? "A passcode is set. Saving a new one signs every device out." : "";
  $("#share-on").checked = s.enabled;
  $("#share-on").disabled = !(s.address && s.has_passcode);
  $("#share-awake").checked = s.keep_awake;
  $("#share-state").textContent = s.listening ? "On" : s.enabled ? "Waiting" : "Off";
  const msg = $("#share-msg");
  msg.className = s.error ? "msg error" : "msg";
  msg.textContent = s.error || (s.listening ? `Sharing at ${s.url}. ${s.devices} ${s.devices === 1 ? "device" : "devices"} signed in.` : "");
  $("#share-ready").hidden = !s.listening;
  $("#share-signout").hidden = !s.devices;
  if (s.listening) {
    $("#share-url").textContent = s.url;
    fetch("/api/remote/qr").then((r) => r.text()).then((svg) => { $("#share-qr").innerHTML = svg; });
  }
}

async function saveShare(body) {
  try {
    renderShare(await api("/api/remote", { method: "POST", body }));
  } catch (err) {
    $("#share-msg").className = "msg error"; $("#share-msg").textContent = err.message;
    loadShareCard().catch(() => {});
  }
}

// "Show" lets you check what you typed; a saved passcode can't be shown (only a hash is kept).
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-show-pass]");
  if (!btn) return;
  const input = document.getElementById(btn.dataset.showPass);
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  btn.textContent = show ? "Hide" : "Show";
  btn.setAttribute("aria-pressed", show);
  input.focus();
});

$("#share-addresses").onchange = (e) => e.target.name === "share-addr" && saveShare({ address: e.target.value });
$("#share-pass-form").onsubmit = async (e) => {
  e.preventDefault();
  await saveShare({ passcode: $("#share-pass").value });
  $("#share-pass").value = "";
  $("#share-pass").type = "password";  // hidden again once it's saved
  const show = document.querySelector('[data-show-pass="share-pass"]');
  show.textContent = "Show"; show.setAttribute("aria-pressed", false);
};
$("#share-on").onchange = (e) => saveShare({ enabled: e.target.checked });
$("#share-awake").onchange = (e) => saveShare({ keep_awake: e.target.checked });
$("#share-signout").onclick = async () => {
  if (!confirm("Sign out every phone and device? They'll need the passcode again.")) return;
  await api("/api/remote/sign-out-all", { method: "POST" });
  loadShareCard();
};
$("#phone-signout").onclick = async () => {
  await api("/api/logout", { method: "POST" });
  location.replace("/login");
};

// ---------- settings & actions ----------

// msg is the status line to report into, or null to use alerts.
async function runAction(action, msg) {
  const show = (text, isError = false) => {
    if (!msg) { if (isError) alert(text); return; }
    msg.className = isError ? "msg error" : "msg";
    msg.textContent = text;
  };
  try {
    const confirmations = {
      disconnect: "Disconnect SimpleFIN? Everything already downloaded stays in Penny. You can reconnect anytime with a new setup token.",
      "delete-all-data": "Delete all accounts, transactions and saved originals from Penny? A backup is made first, so this can be undone from Settings → Restore a backup.",
    };
    if (confirmations[action] && !confirm(confirmations[action])) return;
    show({ sync: "Syncing…", demo: "Loading demo data…", disconnect: "Disconnecting…", "delete-all-data": "Deleting…" }[action]);
    const res = await api(`/api/${action}`, { method: "POST" });
    show({
      sync: () => syncSummary(res),
      disconnect: () => "Disconnected. Your data is still here.",
      "delete-all-data": () => `Deleted. A backup was saved first in ${res.backup_dir}.`,
    }[action]?.() ?? "Done.");
    await refreshStatus();
    if (action === "demo") location.hash = "dashboard";
    await route();
  } catch (err) {
    show(err.message, true);
  }
}

function syncSummary(res) {
  let s = `Synced ${res.accounts} accounts and ${res.transactions} transactions.`;
  if (res.errors?.length) s += ` SimpleFIN reported: ${res.errors.join("; ")}`;
  return s;
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-action]");
  if (btn) runAction(btn.dataset.action, btn.closest("#view-settings") ? $("#data-msg") : null);
});

$("#sync-btn").onclick = async () => {
  const btn = $("#sync-btn");
  btn.disabled = true; btn.textContent = "Syncing…";
  try {
    const res = await api("/api/sync", { method: "POST" });
    if (res.errors?.length) alert(syncSummary(res));
    await refreshStatus();
    await route();
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false; btn.textContent = "Sync";
  }
};

$("#connect-form").onsubmit = async (e) => {
  e.preventDefault();
  const msg = $("#connect-msg"), btn = e.target.querySelector("button");
  msg.className = "msg"; msg.textContent = "Connecting and downloading your transactions. This can take a minute…";
  btn.disabled = true;
  try {
    const res = await api("/api/simplefin/connect", { method: "POST", body: { setup_token: $("#setup-token").value } });
    $("#setup-token").value = "";
    msg.textContent = syncSummary(res);
    await refreshStatus();
  } catch (err) {
    msg.className = "msg error"; msg.textContent = err.message;
  } finally {
    btn.disabled = false;
  }
};

$("#stop-btn").onclick = async () => {
  await api("/api/shutdown", { method: "POST" }).catch(() => {});
  document.body.innerHTML = `<main><div class="card welcome">
    <h1>Penny has stopped</h1>
    <p>Open the Penny app to start it again. You can close this tab.</p>
  </div></main>`;
};

window.addEventListener("hashchange", route);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", route);

(async () => {
  await Promise.all([refreshCategories(), refreshStatus(), refreshLogos()]);
  route();
  checkForUpdate().catch(() => {});  // offline is fine; it tries again next launch
})();
