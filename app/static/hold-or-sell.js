/* Hold or sell desk — crop + multi-location net after carry/trucking/basis. */
(function () {
  const DATA = window.HOLD_SELL_DATA;
  if (!DATA) {
    document.body.innerHTML = "<p style='padding:2rem'>Missing hold-or-sell-data.js</p>";
    return;
  }

  const LS = "beam.holdSell.v2";
  const DAYS_MO = 30.4375;
  const HORIZON_MO = 18;
  const LOC_COLORS = {
    Kellogg: "#0d6b38",
    Dayton: "#c9920e",
    Bloomingburg: "#2f4b8a",
    Sidney: "#c23a12",
  };

  const state = {
    crop: "corn",
    visible: { Kellogg: true, Dayton: true, Bloomingburg: true, Sidney: true },
    tableLoc: "Kellogg",
    basisMode: "seasonal",
    grain: {},
    actual: {},
    actualNow: {},
    carry: {
      corn: { apr: 7, storage: 0.03, shrinkPct: 0.08, extraPts: 0, shrinkFactor: 1.25, handling: 0.02, trucking: 0.18, markMode: "cash" },
      soybeans: { apr: 7, storage: 0.04, shrinkPct: 0.1, extraPts: 0, shrinkFactor: 1.25, handling: 0.02, trucking: 0.18, markMode: "cash" },
    },
    strip: DATA.strip,
    history: DATA.history,
    quoteAsOf: DATA.generated,
  };

  try {
    const saved = JSON.parse(localStorage.getItem(LS) || "null");
    if (saved && typeof saved === "object") {
      Object.assign(state, {
        crop: saved.crop || state.crop,
        visible: { ...state.visible, ...(saved.visible || {}) },
        tableLoc: saved.tableLoc || state.tableLoc,
        basisMode: saved.basisMode || state.basisMode,
        grain: saved.grain || {},
        actual: saved.actual || {},
        actualNow: saved.actualNow || (typeof saved.actualNow === "string" ? {} : {}),
        carry: {
          corn: { ...state.carry.corn, ...((saved.carry || {}).corn || {}) },
          soybeans: { ...state.carry.soybeans, ...((saved.carry || {}).soybeans || {}) },
        },
      });
      if (saved.strip) state.strip = saved.strip;
      if (saved.history) state.history = saved.history;
      if (saved.quoteAsOf) state.quoteAsOf = saved.quoteAsOf;
    }
  } catch (e) { /* ignore */ }

  let histChart;
  let netChart;
  let stackChart;

  function save() {
    const payload = {
      crop: state.crop, visible: state.visible, tableLoc: state.tableLoc, basisMode: state.basisMode,
      grain: state.grain, actual: state.actual, actualNow: state.actualNow, carry: state.carry,
      strip: state.strip, history: state.history, quoteAsOf: state.quoteAsOf,
    };
    try { localStorage.setItem(LS, JSON.stringify(payload)); } catch (e) { /* ignore */ }
  }

  function cropKey() { return state.crop === "soybeans" ? "soybeans" : "corn"; }
  function cropLabel() { return cropKey() === "soybeans" ? "Soybeans" : "Corn"; }
  function stripCrop() { return cropLabel(); }
  function carry() { return state.carry[cropKey()]; }

  function availableLocs() {
    return Object.keys(DATA.locations).filter((id) => {
      const c = DATA.locations[id][cropKey()];
      return c && c.available && !c.hidden;
    });
  }

  function activeLocs() {
    return availableLocs().filter((id) => state.visible[id] !== false);
  }

  function cropBlock(loc) {
    return (DATA.locations[loc] || {})[cropKey()];
  }

  function num(v) {
    const n = parseFloat(String(v ?? "").replace(",", ""));
    return Number.isFinite(n) ? n : null;
  }
  function money(n, d) {
    if (n == null || !Number.isFinite(n)) return "—";
    return "$" + n.toFixed(d);
  }
  function cents(n, d) {
    if (n == null || !Number.isFinite(n)) return "—";
    return (n > 0 ? "+" : "") + n.toFixed(d) + "¢";
  }
  function fmtDate(d) {
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  }
  function ymd(d) {
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }
  function addMonths(d, n) { return new Date(d.getFullYear(), d.getMonth() + n, d.getDate()); }
  function weekU(d) {
    const jan1 = new Date(d.getFullYear(), 0, 1);
    const days = Math.floor((d - jan1) / 86400000);
    return Math.floor((days + jan1.getDay()) / 7);
  }
  function quarterInfo(d) {
    const q = Math.floor(d.getMonth() / 3) + 1;
    return { year: d.getFullYear(), q, key: d.getFullYear() + "-Q" + q, label: "Q" + q + " " + d.getFullYear() };
  }
  function parseISO(s) {
    if (!s) return null;
    const [y, m, d] = s.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  function today() {
    const n = new Date();
    return new Date(n.getFullYear(), n.getMonth(), n.getDate());
  }
  function stripRows() { return (state.strip[stripCrop()] || []).slice(); }
  function frontRow() {
    const rows = stripRows().filter((r) => r.price != null);
    return rows.find((r) => r.is_front) || rows[0] || null;
  }
  function contractForDate(d) {
    const rows = stripRows();
    for (const r of rows) {
      const ltd = parseISO(r.last_trading_day);
      if (ltd && ltd >= d) return r;
    }
    return rows[rows.length - 1] || null;
  }

  function histBasis(loc, d) {
    const block = cropBlock(loc);
    if (!block || !block.available) return null;
    const wRow = block.weekly && block.weekly[String(weekU(d))];
    if (wRow && wRow.avg != null && wRow.n >= 2) return wRow.avg;
    const mRow = block.monthly && block.monthly[String(d.getMonth() + 1)];
    return mRow && mRow.avg != null ? mRow.avg : null;
  }

  function postedBasis(loc, d) {
    const curve = ((cropBlock(loc) || {}).latestCurve) || [];
    let best = null;
    let bestDelta = 1e9;
    for (const c of curve) {
      if (c.month == null) continue;
      if (c.year === d.getFullYear() && c.month === d.getMonth() + 1) return c.basis;
      if (c.end) {
        const end = parseISO(c.end);
        const delta = Math.abs(end - d);
        if (delta < bestDelta) { bestDelta = delta; best = c.basis; }
      }
    }
    return (best != null && bestDelta <= 20 * 86400000) ? best : null;
  }

  function actualKey(loc, periodKey) {
    return cropKey() + "|" + loc + "|" + periodKey;
  }

  /** Actual basis replaces historical for that window only; blank reverts to hist/posted. */
  function basisFor(loc, d, periodKey, isNow) {
    const typed = isNow
      ? num(state.actualNow[cropKey() + "|" + loc])
      : num(state.actual[actualKey(loc, periodKey)]);
    if (typed != null) return { value: typed, source: "actual" };
    if (state.basisMode === "posted") {
      const p = postedBasis(loc, d);
      if (p != null) return { value: p, source: "posted" };
    }
    const h = histBasis(loc, d);
    if (h != null) return { value: h, source: "seasonal" };
    const p = postedBasis(loc, d);
    if (p != null) return { value: p, source: "posted" };
    return { value: null, source: "none" };
  }

  function markPrice(fut, basisCents) {
    if (fut == null) return null;
    if (carry().markMode === "futures") return fut;
    return fut + (basisCents || 0) / 100;
  }

  function carryParts(fut, basisCents, days) {
    const c = carry();
    const mark = markPrice(fut, basisCents) || 0;
    const months = Math.max(0, days) / DAYS_MO;
    const years = Math.max(0, days) / 365;
    const interest = mark * (num(c.apr) || 0) / 100 * years;
    const storage = (num(c.storage) || 0) * months;
    const handlingShrink = (num(c.shrinkPct) || 0) / 100 * mark * months;
    const extraPts = num(c.extraPts) || 0;
    const factor = (num(c.shrinkFactor) || 0) / 100;
    const moisture = days > 0 ? extraPts * factor * mark : 0;
    const inout = days > 0 ? (num(c.handling) || 0) : 0;
    const trucking = days > 0 ? (num(c.trucking) || 0) : 0;
    const monthlyRate = (mark * (num(c.apr) || 0) / 100 / 12) + (num(c.storage) || 0) + ((num(c.shrinkPct) || 0) / 100 * mark);
    return {
      mark, interest, storage, handlingShrink, moisture, inout, trucking, monthlyRate,
      total: interest + storage + handlingShrink + moisture + inout + trucking,
    };
  }

  function defaultGrain(qIndex) {
    if (qIndex === 0) return "weekly";
    if (qIndex === 1) return "bimonthly";
    return "monthly";
  }

  function periodDates(start, end, grain) {
    const out = [];
    if (grain === "weekly") {
      let d = new Date(start.getFullYear(), start.getMonth(), start.getDate());
      while (d <= end) {
        out.push(new Date(d));
        d = new Date(d.getFullYear(), d.getMonth(), d.getDate() + 7);
      }
    } else if (grain === "bimonthly") {
      let cur = new Date(start.getFullYear(), start.getMonth(), 1);
      while (cur <= end) {
        for (const day of [1, 15]) {
          const d = new Date(cur.getFullYear(), cur.getMonth(), day);
          if (d >= start && d <= end) out.push(d);
        }
        cur = new Date(cur.getFullYear(), cur.getMonth() + 1, 1);
      }
    } else {
      let cur = new Date(start.getFullYear(), start.getMonth(), 1);
      if (cur < start) cur = new Date(start.getFullYear(), start.getMonth() + 1, 1);
      if (start.getDate() <= 2) out.push(new Date(start));
      while (cur <= end) {
        out.push(new Date(cur));
        cur = new Date(cur.getFullYear(), cur.getMonth() + 1, 1);
      }
    }
    return out;
  }

  function quarters() {
    const start = today();
    const end = addMonths(start, HORIZON_MO);
    const list = [];
    let cursor = new Date(start.getFullYear(), Math.floor(start.getMonth() / 3) * 3, 1);
    while (cursor <= end) {
      const qEnd = new Date(cursor.getFullYear(), cursor.getMonth() + 3, 0);
      const qStart = cursor < start ? start : new Date(cursor);
      const info = quarterInfo(qStart);
      const clippedEnd = qEnd > end ? end : qEnd;
      if (qStart <= clippedEnd) list.push({ ...info, start: qStart, end: clippedEnd });
      cursor = new Date(cursor.getFullYear(), cursor.getMonth() + 3, 1);
    }
    return list;
  }

  function ensureTableLoc() {
    const avail = availableLocs();
    if (!avail.includes(state.tableLoc)) state.tableLoc = avail[0] || "Kellogg";
  }

  function buildRowsForLoc(loc) {
    const qs = quarters();
    const now = today();
    const front = frontRow();
    const nowBasis = basisFor(loc, now, "now", true);
    const nowFut = front && front.price != null ? Number(front.price) : null;
    const nowCash = nowFut != null && nowBasis.value != null ? nowFut + nowBasis.value / 100 : nowFut;
    const rows = [{
      isNow: true, quarter: quarterInfo(now), date: now, key: "now",
      label: "Now · " + fmtDate(now), contract: front, futures: nowFut,
      hist: histBasis(loc, now), posted: postedBasis(loc, now),
      basis: nowBasis, futCarry: 0, basisCarry: 0, days: 0, cost: 0, truck: 0, cash: nowCash, net: 0,
    }];
    qs.forEach((q, qi) => {
      const grain = state.grain[q.key] || defaultGrain(qi);
      periodDates(q.start, q.end, grain).forEach((d) => {
        if (ymd(d) === ymd(now)) return;
        const key = ymd(d);
        const contract = contractForDate(d);
        const fut = contract && contract.price != null ? Number(contract.price) : null;
        const b = basisFor(loc, d, key, false);
        const cash = fut != null && b.value != null ? fut + b.value / 100 : fut;
        const days = Math.max(0, Math.round((d - now) / 86400000));
        const parts = carryParts(nowFut, nowBasis.value, days);
        const futCarry = fut != null && nowFut != null ? (fut - nowFut) * 100 : null;
        const basisCarry = b.value != null && nowBasis.value != null ? b.value - nowBasis.value : null;
        const net = cash != null && nowCash != null ? (cash - parts.total - nowCash) * 100 : null;
        rows.push({
          isNow: false, quarter: q, date: d, key,
          label: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
          contract, futures: fut, hist: histBasis(loc, d), posted: postedBasis(loc, d),
          basis: b, futCarry, basisCarry, days,
          cost: parts.total * 100, truck: parts.trucking * 100, cash, net,
        });
      });
    });
    return { rows, nowCash, nowFut, nowBasis };
  }

  function setStatus(msg, err) {
    const el = document.getElementById("status");
    el.textContent = msg || "";
    el.className = "status" + (err ? " err" : "");
  }

  function renderCropSeg() {
    const seg = document.getElementById("cropSeg");
    seg.className = "seg " + (cropKey() === "soybeans" ? "soy" : "corn");
    seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.crop === cropKey()));
  }

  function renderLocs() {
    const avail = availableLocs();
    document.getElementById("locs").innerHTML = avail.map((id) => {
      const on = state.visible[id] !== false;
      return `<label class="loc ${on ? "" : "off"}">
        <span class="swatch" style="background:${LOC_COLORS[id] || "#666"}"></span>
        <input type="checkbox" data-id="${id}" ${on ? "checked" : ""} />
        <strong>${id}</strong>
      </label>`;
    }).join("");
    document.getElementById("locs").querySelectorAll("input").forEach((inp) => {
      inp.addEventListener("change", () => {
        state.visible[inp.dataset.id] = inp.checked;
        save();
        refresh({ reread: false, forms: false });
      });
    });

    ensureTableLoc();
    const sel = document.getElementById("tableLoc");
    sel.innerHTML = avail.map((id) => `<option value="${id}">${id}</option>`).join("");
    sel.value = state.tableLoc;
  }

  function renderCarryForm() {
    const c = carry();
    document.getElementById("apr").value = c.apr;
    document.getElementById("storage").value = c.storage;
    document.getElementById("trucking").value = c.trucking;
    document.getElementById("markMode").value = c.markMode;
    document.getElementById("handling").value = c.handling;
    document.getElementById("shrinkPct").value = c.shrinkPct;
    document.getElementById("extraPts").value = c.extraPts;
    document.getElementById("shrinkFactor").value = c.shrinkFactor;
    document.getElementById("actualNow").value = state.actualNow[cropKey() + "|" + state.tableLoc] || "";
    document.getElementById("basisMode").value = state.basisMode;
    document.getElementById("tableLoc").value = state.tableLoc;
  }

  function readCarryForm() {
    const c = carry();
    c.apr = num(document.getElementById("apr").value) ?? c.apr;
    c.storage = num(document.getElementById("storage").value) ?? 0;
    c.trucking = num(document.getElementById("trucking").value) ?? 0;
    c.markMode = document.getElementById("markMode").value;
    c.handling = num(document.getElementById("handling").value) ?? 0;
    c.shrinkPct = num(document.getElementById("shrinkPct").value) ?? 0;
    c.extraPts = num(document.getElementById("extraPts").value) ?? 0;
    c.shrinkFactor = num(document.getElementById("shrinkFactor").value) ?? 0;
    const nowKey = cropKey() + "|" + state.tableLoc;
    const nowVal = document.getElementById("actualNow").value;
    if (String(nowVal).trim() === "") delete state.actualNow[nowKey];
    else state.actualNow[nowKey] = nowVal;
    state.basisMode = document.getElementById("basisMode").value;
    state.tableLoc = document.getElementById("tableLoc").value;
  }

  function renderStrip() {
    const rows = stripRows();
    const el = document.getElementById("strip");
    let html = "";
    rows.forEach((r, i) => {
      html += `<div class="month ${r.is_front ? "front" : ""}">
        <div class="lab">${r.label}${r.is_front ? " · front" : ""}</div>
        <input data-i="${i}" inputmode="decimal" value="${r.price != null ? Number(r.price).toFixed(4) : ""}" />
        <em>${r.short}</em>
      </div>`;
      if (i < rows.length - 1) {
        const n = rows[i + 1];
        const sp = r.price != null && n.price != null ? (Number(n.price) - Number(r.price)) * 100 : null;
        const cls = sp == null ? "" : sp > 0 ? "carry" : sp < 0 ? "inverse" : "";
        const word = sp == null ? "" : sp > 0 ? "carry" : sp < 0 ? "inverse" : "flat";
        html += `<div class="spread ${cls}"><strong>${cents(sp, 1)}</strong><span>${word}</span></div>`;
      }
    });
    el.innerHTML = html || "<p class='hint'>Update futures prices to load the strip.</p>";
    el.querySelectorAll("input").forEach((inp) => {
      inp.addEventListener("change", () => {
        const i = Number(inp.dataset.i);
        const v = num(inp.value);
        if (state.strip[stripCrop()][i]) state.strip[stripCrop()][i].price = v;
        save();
        refresh({ reread: false, forms: false });
      });
    });
  }

  function renderRate(nowFut, nowBasis) {
    const parts = carryParts(nowFut, nowBasis && nowBasis.value, DAYS_MO);
    const shrinkMo = (num(carry().shrinkPct) || 0) / 100 * (parts.mark || 0);
    document.getElementById("rateBox").innerHTML =
      `<div><div class="l">Mark</div><b>${money(parts.mark, 4)}</b></div>
       <div><div class="l">Interest / mo</div><b>${money(parts.interest, 4)}</b></div>
       <div><div class="l">Storage / mo</div><b>${money(parts.storage, 4)}</b></div>
       <div><div class="l">Shrink / mo</div><b>${money(shrinkMo, 4)}</b></div>
       <div><div class="l">Trucking (one-time)</div><b>${money(num(carry().trucking) || 0, 4)}</b></div>
       <div><div class="l">Monthly rate</div><b>${money(parts.monthlyRate, 4)}</b> / bu</div>`;
  }

  function renderQuarters() {
    const qs = quarters();
    document.getElementById("quarters").innerHTML = qs.map((q, i) => {
      const g = state.grain[q.key] || defaultGrain(i);
      return `<label class="qchip"><strong>${q.label}</strong>
        <select data-q="${q.key}">
          <option value="weekly" ${g === "weekly" ? "selected" : ""}>Weekly</option>
          <option value="bimonthly" ${g === "bimonthly" ? "selected" : ""}>Bi-monthly</option>
          <option value="monthly" ${g === "monthly" ? "selected" : ""}>Monthly</option>
        </select></label>`;
    }).join("");
    document.getElementById("quarters").querySelectorAll("select").forEach((sel) => {
      sel.addEventListener("change", () => {
        state.grain[sel.dataset.q] = sel.value;
        save();
        refresh({ reread: false, forms: false });
      });
    });
  }

  function renderTable(model) {
    const { rows } = model;
    let html = "";
    let lastQ = "";
    rows.forEach((r) => {
      if (r.quarter.key !== lastQ) {
        lastQ = r.quarter.key;
        html += `<tr class="qhead"><td colspan="12">${r.quarter.label} · ${state.tableLoc}</td></tr>`;
      }
      const cls = [r.isNow ? "now" : "", r.net > 0.5 ? "pos" : r.net < -0.5 ? "neg" : ""].join(" ");
      const actualVal = r.isNow
        ? (state.actualNow[cropKey() + "|" + state.tableLoc] || "")
        : (state.actual[actualKey(state.tableLoc, r.key)] ?? "");
      const input = r.isNow
        ? `<span class="muted">use box above</span>`
        : `<input class="basis" data-k="${r.key}" inputmode="decimal" value="${actualVal}" placeholder="—" />`;
      const used = r.basis && r.basis.value != null
        ? cents(r.basis.value, 1) + (r.basis.source === "actual" ? " act" : r.basis.source === "posted" ? " post" : " hist")
        : "—";
      html += `<tr class="${cls}">
        <td class="l">${r.label}${r.contract ? " · " + r.contract.short : ""}</td>
        <td>${r.futures != null ? money(r.futures, 2) : "—"}</td>
        <td>${cents(r.futCarry, 1)}</td>
        <td>${r.hist != null ? cents(r.hist, 1) : "—"}</td>
        <td>${r.posted != null ? cents(r.posted, 1) : "—"}</td>
        <td>${input}</td>
        <td>${used}</td>
        <td>${cents(r.basisCarry, 1)}</td>
        <td>${cents(r.cost, 1)}</td>
        <td>${cents(r.truck, 1)}</td>
        <td>${r.cash != null ? money(r.cash, 2) : "—"}</td>
        <td class="net">${cents(r.net, 1)}</td>
      </tr>`;
    });
    const tb = document.getElementById("tbody");
    tb.innerHTML = html;
    tb.querySelectorAll("input.basis").forEach((inp) => {
      inp.addEventListener("change", () => {
        const k = actualKey(state.tableLoc, inp.dataset.k);
        if (String(inp.value).trim() === "") delete state.actual[k];
        else state.actual[k] = inp.value;
        save();
        refresh({ reread: false, forms: false });
      });
    });
  }

  function renderHistChart() {
    const ctx = document.getElementById("histChart");
    if (!ctx || typeof Chart === "undefined") return;
    const series = state.history[stripCrop()] || [];
    if (histChart) histChart.destroy();
    histChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: series.map((p) => p.date),
        datasets: [{
          label: cropLabel() + " nearby",
          data: series.map((p) => p.price),
          borderColor: cropKey() === "soybeans" ? "#2f4b8a" : "#c9920e",
          backgroundColor: "transparent",
          borderWidth: 2, pointRadius: 0, tension: 0.15,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: true, labels: { boxWidth: 12 } },
          tooltip: { callbacks: { label(item) { return " " + money(item.parsed.y, 2) + "/bu"; } } },
        },
        scales: {
          x: { ticks: { maxTicksLimit: 8, callback(val) { const lab = this.getLabelForValue(val); return lab ? String(lab).slice(0, 7) : ""; } }, title: { display: true, text: "Week" } },
          y: { title: { display: true, text: "$ / bu" }, ticks: { callback: (v) => "$" + Number(v).toFixed(2) } },
        },
      },
    });
  }

  function renderNetChart() {
    const ctx = document.getElementById("netChart");
    if (!ctx || typeof Chart === "undefined") return;
    const locs = activeLocs();
    const models = {};
    locs.forEach((loc) => { models[loc] = buildRowsForLoc(loc); });

    // Shared label set from first active location (non-now rows)
    const baseRows = locs.length ? models[locs[0]].rows.filter((r) => !r.isNow) : [];
    const labels = baseRows.map((r) => r.label);

    const datasets = locs.map((loc) => {
      const byKey = {};
      models[loc].rows.filter((r) => !r.isNow).forEach((r) => { byKey[r.key] = r.net; });
      return {
        label: loc + " net ¢",
        data: baseRows.map((r) => (byKey[r.key] != null ? byKey[r.key] : null)),
        borderColor: LOC_COLORS[loc] || "#333",
        backgroundColor: LOC_COLORS[loc] || "#333",
        borderWidth: 2.25,
        pointRadius: 2.5,
        tension: 0.2,
      };
    });

    if (netChart) netChart.destroy();
    netChart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 12, usePointStyle: true } },
          tooltip: {
            callbacks: {
              label(item) {
                const v = item.parsed.y;
                return " " + item.dataset.label + ": " + (v == null ? "—" : cents(v, 1));
              },
            },
          },
        },
        scales: {
          x: { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, title: { display: true, text: "Move window" } },
          y: {
            title: { display: true, text: "Net vs sell today (¢ / bu)" },
            ticks: { callback: (v) => (v > 0 ? "+" : "") + v + "¢" },
          },
        },
      },
      plugins: [{
        id: "zeroLine",
        afterDraw(c) {
          const y = c.scales.y;
          if (y.min > 0 || y.max < 0) return;
          const yPix = y.getPixelForValue(0);
          const { ctx: g, chartArea } = c;
          g.save();
          g.strokeStyle = "rgba(42,64,51,0.45)";
          g.setLineDash([5, 4]);
          g.beginPath();
          g.moveTo(chartArea.left, yPix);
          g.lineTo(chartArea.right, yPix);
          g.stroke();
          g.fillStyle = "rgba(42,64,51,0.8)";
          g.font = "11px sans-serif";
          g.fillText("Sell now = 0", chartArea.right - 72, yPix - 5);
          g.restore();
        },
      }],
    });
  }

  function renderStackChart(model) {
    const ctx = document.getElementById("stackChart");
    if (!ctx || typeof Chart === "undefined") return;
    const rows = model.rows.filter((r) => !r.isNow);
    if (stackChart) stackChart.destroy();
    stackChart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: rows.map((r) => r.label),
        datasets: [
          { label: "Futures carry ¢", data: rows.map((r) => r.futCarry), backgroundColor: "#c9920e", stack: "m" },
          { label: "Basis carry ¢", data: rows.map((r) => r.basisCarry), backgroundColor: "#2f4b8a", stack: "m" },
          { label: "Cost (carry+truck) ¢", data: rows.map((r) => r.cost == null ? null : -r.cost), backgroundColor: "#c23a12", stack: "m" },
          { type: "line", label: "Net ¢", data: rows.map((r) => r.net), borderColor: "#0d6b38", backgroundColor: "#0d6b38", tension: 0.2, pointRadius: 2, yAxisID: "y2" },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { position: "bottom", labels: { boxWidth: 10, font: { size: 11 } } } },
        scales: {
          x: { stacked: true, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
          y: { stacked: true, title: { display: true, text: "Stack ¢ / bu" } },
          y2: { stacked: false, position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "Net ¢" } },
        },
      },
    });
  }

  function renderFooter() {
    const spot = DATA.spotRange[cropKey()] || DATA.spotRange.corn;
    document.getElementById("footer").innerHTML =
      "Spot basis " + (spot ? spot[0] + " to " + spot[1] : "") +
      " · " + DATA.source +
      ". Dayton = corn only. Sidney = soybeans only. Actual basis overrides historical for that window only. Delayed CME — informational only.";
    document.getElementById("asOfPill").textContent = state.quoteAsOf
      ? ("Quotes " + String(state.quoteAsOf).replace("T", " ").slice(0, 16) + " UTC")
      : "Quotes delayed";
  }

  function refresh(opts) {
    try {
      const reread = !opts || opts.reread !== false;
      const forms = !opts || opts.forms !== false;
      if (reread) readCarryForm();
      ensureTableLoc();
      renderCropSeg();
      renderLocs();
      if (forms) renderCarryForm();
      renderStrip();
      const model = buildRowsForLoc(state.tableLoc);
      const front = frontRow();
      renderRate(front && front.price, model.nowBasis);
      renderQuarters();
      renderTable(model);
      renderHistChart();
      renderNetChart();
      renderStackChart(model);
      renderFooter();
      save();
    } catch (err) {
      setStatus(String(err && err.message ? err.message : err), true);
      console.error(err);
    }
  }

  function toBu(raw) {
    if (raw == null) return null;
    const v = Number(raw);
    if (!Number.isFinite(v)) return null;
    return Math.round((v > 40 ? v / 100 : v) * 10000) / 10000;
  }

  async function fetchYahooTicker(ticker) {
    const url = "https://query1.finance.yahoo.com/v8/finance/chart/" + encodeURIComponent(ticker) + "?interval=1d&range=5d";
    const proxies = [url, "https://corsproxy.io/?" + encodeURIComponent(url), "https://api.allorigins.win/raw?url=" + encodeURIComponent(url)];
    for (const u of proxies) {
      try {
        const r = await fetch(u);
        if (!r.ok) continue;
        const data = await r.json();
        const meta = (((data.chart || {}).result) || [])[0];
        if (!meta || !meta.meta) continue;
        const last = meta.meta.regularMarketPrice || meta.meta.chartPreviousClose;
        const prev = meta.meta.chartPreviousClose;
        return { price: toBu(last), change: last != null && prev != null ? toBu(last) - toBu(prev) : null };
      } catch (e) { /* next */ }
    }
    return null;
  }

  async function updateFromYahoo() {
    const next = { Corn: [], Soybeans: [] };
    for (const crop of ["Corn", "Soybeans"]) {
      const rows = (state.strip[crop] || DATA.strip[crop] || []).map((r) => ({ ...r }));
      for (const row of rows) {
        const q = await fetchYahooTicker(row.ticker);
        if (q && q.price != null) { row.price = q.price; row.change = q.change; }
      }
      next[crop] = rows;
    }
    state.strip = next;
    state.quoteAsOf = new Date().toISOString();
  }

  async function updateFutures() {
    const btn = document.getElementById("btnUpdate");
    btn.disabled = true;
    setStatus("Updating delayed CME prices…");
    try {
      let usedLive = false;
      try {
        const r = await fetch("/risk/hold-sell/quotes", { credentials: "same-origin" });
        if (r.ok) {
          const data = await r.json();
          if (data.strip) state.strip = data.strip;
          if (data.history && (data.history.Corn || []).length) state.history = data.history;
          state.quoteAsOf = data.as_of || new Date().toISOString();
          if (data.error) setStatus(data.error + " — showing what loaded.", true);
          else setStatus("Futures updated.");
          usedLive = true;
        }
      } catch (e) { /* fall through */ }
      if (!usedLive) {
        await updateFromYahoo();
        setStatus("Futures updated from delayed Yahoo quotes.");
      }
      save();
      refresh({ reread: false, forms: false });
    } catch (e) {
      setStatus("Could not reach a quote feed. Type strip prices by hand.", true);
    } finally {
      btn.disabled = false;
    }
  }

  document.getElementById("cropSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.crop = b.dataset.crop;
    save();
    refresh({ reread: false, forms: true });
  });
  document.getElementById("btnUpdate").addEventListener("click", updateFutures);
  document.getElementById("tableLoc").addEventListener("change", () => {
    state.tableLoc = document.getElementById("tableLoc").value;
    save();
    refresh({ reread: false, forms: true });
  });
  ["apr", "storage", "trucking", "markMode", "handling", "shrinkPct", "extraPts", "shrinkFactor", "actualNow", "basisMode"].forEach((id) => {
    const el = document.getElementById(id);
    el.addEventListener("change", () => { readCarryForm(); save(); refresh({ reread: false, forms: false }); });
    if (el.tagName === "INPUT") el.addEventListener("input", () => { readCarryForm(); refresh({ reread: false, forms: false }); });
  });

  refresh({ reread: false, forms: true });
  (function loadCharts() {
    if (typeof Chart !== "undefined") return;
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js";
    s.async = true;
    s.onload = function () { refresh({ reread: false, forms: false }); };
    document.head.appendChild(s);
  })();
})();
