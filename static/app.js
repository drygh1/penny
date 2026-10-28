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
const dayLabel = (iso) => {
  const d = new Date(iso + "T12:00:00");
  const opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("en-US", opts);
};
const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const thisMonth = () => isoDay(new Date()).slice(0, 7);

// Each category keeps the same color everywhere. The server gives up to eight
// categories a colorblind-checked color slot; the rest share a neutral gray.
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

const VIEWS = ["dashboard", "subscriptions", "transactions", "merchants", "accounts", "settings"];

async function route() {
  let view = location.hash.slice(1) || "dashboard";
  if (!VIEWS.includes(view)) view = "dashboard";
  const hasData = !!status.mode;
  if (!hasData && view !== "settings") view = "welcome";

  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${view}`));
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.hash === `#${view}`));

  const loaders = { settings: () => Promise.all([loadCategories(), loadImport()]), dashboard: loadDashboard, subscriptions: loadSubscriptions, transactions: loadTransactions, merchants: loadMerchants, accounts: loadAccounts };
  if (loaders[view]) await loaders[view]();
}

async function refreshStatus() {
  status = await api("/api/status");
  $("#sync-btn").hidden = !status.connected;
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

async function loadDashboard() {
  const [monthly, accounts, subs] = await Promise.all([
    api("/api/spending/monthly?months=12"),
    api("/api/accounts"),
    api("/api/subscriptions"),
  ]);

  $("#t-networth").textContent = fmt0.format(accounts.net_worth);
  $("#t-subs").textContent = money(subs.monthly_total);

  const cur = monthly.find((m) => m.month === thisMonth());
  const prev = monthly.find((m) => m.month === prevMonth(thisMonth()));
  $("#t-spent").textContent = fmt0.format(cur?.spending ?? 0);
  if (prev) {
    const diff = (cur?.spending ?? 0) - prev.spending;
    $("#t-spent-sub").innerHTML = `<span class="${diff > 0 ? "delta-up" : "delta-down"}">${diff > 0 ? "▲" : "▼"} ${fmt0.format(Math.abs(diff))}</span> vs all of last month`;
  }

  drawChart("chart-monthly", {
    type: "bar",
    data: {
      labels: monthly.map((m) => monthLabel(m.month)),
      datasets: [{
        data: monthly.map((m) => m.spending),
        backgroundColor: monthly.map((m) => (m.month === thisMonth() ? css("--accent-soft") : css("--accent"))),
        borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "bottom",
        maxBarThickness: 36, categoryPercentage: 0.7,
      }],
    },
    options: (() => {
      const o = baseChartOptions();
      o.plugins.tooltip.callbacks.title = (items) => monthLabel(monthly[items[0].dataIndex].month, "long") + (monthly[items[0].dataIndex].month === thisMonth() ? " (so far)" : "");
      o.onClick = (_, els) => { if (els.length) setBreakdownMonth(monthly[els[0].index].month); };
      o.onHover = (e, els) => (e.native.target.style.cursor = els.length ? "pointer" : "default");
      return o;
    })(),
  });

  const sel = $("#breakdown-month");
  sel.innerHTML = [...monthly].reverse().map((m) => `<option value="${m.month}">${monthLabel(m.month, "long")}</option>`).join("");
  sel.onchange = () => loadBreakdown(sel.value);
  await loadBreakdown(sel.value || thisMonth());

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

function prevMonth(ym) {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(y, m - 2, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function setBreakdownMonth(month) {
  $("#breakdown-month").value = month;
  loadBreakdown(month);
}

async function loadBreakdown(month) {
  const b = await api(`/api/spending/breakdown?month=${month}`);
  const pct = (v) => Math.round((v / b.total) * 100);
  const max = Math.max(...b.categories.map((c) => c.total), 1);
  $("#category-bars").innerHTML = b.categories.length
    ? b.categories.map((c) => `
        <div class="bar-row" data-category="${esc(c.category)}" data-month="${month}" title="See transactions">
          <span class="bar-label">${dot(c.category)}${esc(c.category)}</span>
          <span class="bar-value">${money(c.total)} · ${pct(c.total)}%</span>
          <div class="bar-track"><div class="bar-fill" style="width:${(c.total / max) * 100}%;background:${colorVar(c.category)}"></div></div>
        </div>`).join("")
    : `<div class="empty">No spending this month.</div>`;
  $("#merchant-list").innerHTML = b.merchants.length
    ? b.merchants.map((m) => itemHTML({
        title: m.name, meta: `${m.category} · ${m.count} ${m.count === 1 ? "charge" : "charges"}`,
        amount: money(m.total), category: m.category, merchant: m.merchant,
      })).join("")
    : `<div class="empty">No spending this month.</div>`;
  drawDonut(b);
}

// Donut slices go in the fixed palette order, so each color always sits
// next to the same neighbors. Small categories merge into one gray slice.
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
  $("#donut-month").textContent = monthLabel(b.month, "long");
  drawChart("chart-donut", {
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
            label: (c) => ` ${money(c.parsed)} · ${Math.round((c.parsed / b.total) * 100)}%`,
            afterLabel: (c) => slices[c.dataIndex].parts ? ` ${slices[c.dataIndex].parts.join(", ")}` : "",
          },
        },
      },
      onClick: (_, els) => {
        const s = els.length && slices[els[0].index];
        if (s && !s.parts) {
          txFilters = { month: b.month, category: s.label, q: "" };
          location.hash = "transactions";
        }
      },
      onHover: (e, els) => (e.native.target.style.cursor = els.length ? "pointer" : "default"),
    },
  });
}

// One row in a list. `amount`, `extra` and `actions` are HTML; the rest is text.
function itemHTML({ title, meta = "", amount = "", amountMeta = "", extra = "", category = null, merchant = null, actions = "" }) {
  return `<div class="item">
    ${avatar(merchant, title)}
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

// ---------- subscriptions ----------

async function loadSubscriptions() {
  const subs = await api("/api/subscriptions");
  const active = subs.items.filter((s) => s.active && !s.ignored);
  const inactive = subs.items.filter((s) => !s.active && !s.ignored);
  const ignored = subs.items.filter((s) => s.ignored);

  $("#s-monthly").textContent = money(subs.monthly_total);
  $("#s-yearly").textContent = fmt0.format(subs.yearly_total);
  $("#s-count").textContent = active.length;

  const row = (s, action, label) => {
    const badge = s.previous_amount != null
      ? `<span class="badge">${s.amount > s.previous_amount ? "▲ Price up" : "▼ Price down"} from ${money(s.previous_amount)}</span>` : "";
    const meta = s.active
      ? `${s.cadence} · next ${dayLabel(s.next_date)} · ${s.category}`
      : `${s.cadence} · last charged ${dayLabel(s.last_date)}`;
    const btn = `<button class="link-btn" data-sub-action="${action}" data-merchant="${esc(s.merchant)}">${label}</button>`;
    return itemHTML({
      title: s.name, meta, amount: money(s.amount),
      amountMeta: s.cadence === "monthly" ? "" : `${money(s.monthly_cost)}/mo`,
      extra: badge, category: s.category, merchant: s.merchant, actions: btn,
    });
  };
  const fill = (el, list, action, label, empty) =>
    ($(el).innerHTML = list.length ? list.map((s) => row(s, action, label)).join("") : `<div class="empty">${empty}</div>`);

  fill("#subs-active", active, "ignore", "Not a subscription", "No recurring charges found yet. They show up after 2 to 3 charges.");
  fill("#subs-inactive", inactive, "ignore", "Hide", "Nothing here.");
  fill("#subs-ignored", ignored, "restore", "Restore", "Nothing hidden.");
}

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-sub-action]");
  if (!btn) return;
  await api("/api/subscriptions/override", {
    method: "POST",
    body: { merchant: btn.dataset.merchant, status: btn.dataset.subAction === "ignore" ? "ignored" : null },
  });
  loadSubscriptions();
});

// ---------- transactions ----------

let txFilters = { month: "", category: "", q: "" };
let searchTimer;

async function loadTransactions() {
  const monthSel = $("#tx-month"), catSel = $("#tx-category");
  if (monthSel.options.length <= 1) {
    const months = await api("/api/spending/monthly?months=24");
    monthSel.innerHTML += [...months].reverse().map((m) => `<option value="${m.month}">${monthLabel(m.month, "long")}</option>`).join("");
  }
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
        <td class="cat">${dot(t.category)}<select data-merchant="${esc(t.merchant)}" aria-label="Category">${catOptions(t.category)}</select></td>
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
$("#tx-body").addEventListener("change", async (e) => {
  if (!e.target.matches("select[data-merchant]")) return;
  await api("/api/merchant-rules", { method: "PUT", body: { merchant: e.target.dataset.merchant, category: e.target.value } });
  loadTransactions();
});

// ---------- merchants ----------

let merchantRows = [];
let logoTarget = null;  // merchant whose picture is being picked

async function loadMerchants() {
  [merchantRows] = await Promise.all([api("/api/merchants"), refreshLogos()]);
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
    return `<div class="item merchant-row">
      <button class="avatar-btn" data-logo-pick="${key}" title="${hasLogo ? "Change" : "Add"} picture for ${esc(m.name)}">${avatar(m.merchant, m.name)}</button>
      <div class="main">
        <div class="title">${esc(m.name)}${m.rule_category ? '<span class="rule-tag">Rule</span>' : ""}</div>
        <div class="meta">${m.count} ${m.count === 1 ? "transaction" : "transactions"} · ${money(m.spent)} spent · last ${dayLabel(m.last_date)}</div>
        <div class="meta example">${esc(m.example)}</div>
      </div>
      <div class="m-controls">
        <label class="m-category">${dot(effective)}<select data-rule-merchant="${key}" aria-label="Category for ${esc(m.name)}">${options}</select></label>
        <div class="m-photo">
          <button class="link-btn" data-logo-pick="${key}">${hasLogo ? "Change picture" : "Add picture"}</button>
          ${hasLogo ? `<button class="link-btn" data-logo-remove="${key}">Remove</button>` : ""}
        </div>
      </div>
    </div>`;
  }).join("") : `<div class="empty">No merchants match.</div>`;
}

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
  if (pick) {
    logoTarget = pick.dataset.logoPick;
    $("#logo-input").value = "";
    $("#logo-input").click();
  } else if (remove) {
    await api(`/api/merchant-logo?merchant=${encodeURIComponent(remove.dataset.logoRemove)}`, { method: "DELETE" });
    loadMerchants();
  }
});

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

  $("#account-list").innerHTML = a.accounts.length
    ? a.accounts.map((acc) => itemHTML({
        title: acc.name, meta: acc.org_name || "", amount: money(acc.balance),
        amountMeta: acc.available_balance != null && acc.available_balance !== acc.balance ? `${money(acc.available_balance)} available` : "",
      })).join("")
    : `<div class="empty">No accounts.</div>`;
}

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
    title: h.name, meta: `${h.count} imported transactions · ${dayLabel(h.first_date)} to ${dayLabel(h.last_date)}`,
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
  let text = `${p.count} transactions from ${dayLabel(p.first_date)} to ${dayLabel(p.last_date)}.`;
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
  const body = { csv: imp.text, mapping: imp.mapping };
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

// ---------- settings & actions ----------

// msg is the status line to report into, or null to use alerts.
async function runAction(action, msg) {
  const show = (text, isError = false) => {
    if (!msg) { if (isError) alert(text); return; }
    msg.className = isError ? "msg error" : "msg";
    msg.textContent = text;
  };
  try {
    if (action === "disconnect" && !confirm("Disconnect SimpleFIN and delete all synced data from this computer?")) return;
    show({ sync: "Syncing…", demo: "Loading demo data…", disconnect: "Deleting…" }[action]);
    const res = await api(`/api/${action}`, { method: "POST" });
    show(action === "sync" ? syncSummary(res) : "Done.");
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
})();
