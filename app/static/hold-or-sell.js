/* Hold or sell desk — corn/soy, CME strip, carry, local basis. */
(function () {
  const DATA = window.HOLD_SELL_DATA;
  if (!DATA) {
    document.body.innerHTML = "<p style='padding:2rem'>Missing hold-or-sell-data.js</p>";
    return;
  }

  const LS = "beam.holdSell.v1";
  const DAYS_MO = 30.4375;
  const HORIZON_MO = 18;
  if (/(?:\?|&)embed=1/.test(location.search)) document.body.classList.add("embed");

  const state = {
    crop: "corn",
    location: "Kellogg",
    basisMode: "seasonal",
    grain: {},
    actual: {},
    actualNow: "",
    carry: {
      corn: { apr: 7, storage: 0.03, shrinkPct: 0.08, extraPts: 0, shrinkFactor: 1.25, handling: 0.02, markMode: "cash" },
      soybeans: { apr: 7, storage: 0.04, shrinkPct: 0.1, extraPts: 0, shrinkFactor: 1.25, handling: 0.02, markMode: "cash" },
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
        location: saved.location || state.location,
        basisMode: saved.basisMode || state.basisMode,
        grain: saved.grain || {},
        actual: saved.actual || {},
        actualNow: saved.actualNow || "",
        carry: { ...state.carry, ...(saved.carry || {}) },
      });
      if (saved.strip) state.strip = saved.strip;
      if (saved.history) state.history = saved.history;
      if (saved.quoteAsOf) state.quoteAsOf = saved.quoteAsOf;
    }
  } catch (e) { /* ignore */ }

  let histChart;
  let netChart;

  function save() {
    const { crop, location, basisMode, grain, actual, actualNow, carry, strip, history, quoteAsOf } = state;
    try {
      localStorage.setItem(LS, JSON.stringify({ crop, location, basisMode, grain, actual, actualNow, carry, strip, history, quoteAsOf }));
    } catch (e) { /* ignore */ }
  }

  function cropKey() { return state.crop === "soybeans" ? "soybeans" : "corn"; }
  function cropLabel() { return cropKey() === "soybeans" ? "Soybeans" : "Corn"; }
  function stripCrop() { return cropLabel(); }
  function carry() { return state.carry[cropKey()]; }
  function locBlock() { return DATA.locations[state.location] || DATA.locations.Kellogg; }
  function cropBlock() { return locBlock()[cropKey()]; }

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
  function addMonths(d, n) {
    return new Date(d.getFullYear(), d.getMonth() + n, d.getDate());
  }
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
  function histBasis(d) {
    const block = cropBlock();
    if (!block) return null;
    const wRow = block.weekly && block.weekly[String(weekU(d))];
    if (wRow && wRow.avg != null && wRow.n >= 2) return wRow.avg;
    const mRow = block.monthly && block.monthly[String(d.getMonth() + 1)];
    return mRow && mRow.avg != null ? mRow.avg : null;
  }
  function postedBasis(d) {
    const curve = ((cropBlock() || {}).latestCurve) || [];
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
  function actualKey(periodKey) { return cropKey() + "|" + state.location + "|" + periodKey; }
  function basisFor(d, periodKey, isNow) {
    const typed = isNow ? num(state.actualNow) : num(state.actual[actualKey(periodKey)]);
    if (typed != null) return { value: typed, source: "actual" };
    if (state.basisMode === "posted") {
      const p = postedBasis(d);
      if (p != null) return { value: p, source: "posted" };
    }
    const h = histBasis(d);
    if (h != null) return { value: h, source: "seasonal" };
    const p = postedBasis(d);
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
    const monthlyRate = (mark * (num(c.apr) || 0) / 100 / 12) + (num(c.storage) || 0) + ((num(c.shrinkPct) || 0) / 100 * mark);
    return { mark, interest, storage, handlingShrink, moisture, inout, monthlyRate, total: interest + storage + handlingShrink + moisture + inout };
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
  function buildRows() {
    const qs = quarters();
    const now = today();
    const front = frontRow();
    const nowBasis = basisFor(now, "now", true);
    const nowFut = front && front.price != null ? Number(front.price) : null;
    const nowCash = nowFut != null && nowBasis.value != null ? nowFut + nowBasis.value / 100 : nowFut;
    const rows = [{
      isNow: true, quarter: quarterInfo(now), date: now, key: "now",
      label: "Now · " + fmtDate(now), contract: front, futures: nowFut,
      hist: histBasis(now), posted: postedBasis(now), actual: num(state.actualNow),
      basis: nowBasis, futCarry: 0, basisCarry: 0, days: 0, cost: 0, cash: nowCash, net: 0,
    }];
    qs.forEach((q, qi) => {
      const grain = state.grain[q.key] || defaultGrain(qi);
      periodDates(q.start, q.end, grain).forEach((d) => {
        if (ymd(d) === ymd(now)) return;
        const key = ymd(d);
        const contract = contractForDate(d);
        const fut = contract && contract.price != null ? Number(contract.price) : null;
        const b = basisFor(d, key, false);
        const cash = fut != null && b.value != null ? fut + b.value / 100 : fut;
        const days = Math.max(0, Math.round((d - now) / 86400000));
        const parts = carryParts(nowFut, nowBasis.value, days);
        const futCarry = fut != null && nowFut != null ? (fut - nowFut) * 100 : null;
        const basisCarry = b.value != null && nowBasis.value != null ? b.value - nowBasis.value : null;
        const net = cash != null && nowCash != null ? (cash - parts.total - nowCash) * 100 : null;
        rows.push({
          isNow: false, quarter: q, date: d, key,
          label: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
          contract, futures: fut, hist: histBasis(d), posted: postedBasis(d),
          actual: num(state.actual[actualKey(key)]), basis: b, futCarry, basisCarry,
          days, cost: parts.total * 100, cash, net,
        });
      });
    });
    return { rows, nowCash, nowFut, nowBasis };
  }
  function bestRow(rows) {
    let best = rows[0];
    for (const r of rows) {
      if (r.net == null) continue;
      if (best.net == null || r.net > best.net) best = r;
    }
    return best;
  }
  function setStatus(msg, err) {
    const el = document.getElementById("status");
    el.textContent = msg || "";
    el.className = "status" + (err ? " err" : "");
  }
  function renderLocs() {
    const sel = document.getElementById("location");
    sel.innerHTML = Object.keys(DATA.locations).map((id) => {
      const c = DATA.locations[id][cropKey()];
      const tag = !c.available ? " — limited" : c.thin ? " — thin" : "";
      return `<option value="${id}">${id}${tag}</option>`;
    }).join("");
    sel.value = state.location;
  }
  function renderLocNote() {
    const el = document.getElementById("locNote");
    const c = cropBlock();
    const notes = [];
    if (state.location === "Dayton" && cropKey() === "soybeans") {
      notes.push("Cargill Dayton bid sheets in this history are corn-only. Enter actual soybean basis below, or pick Kellogg / Bloomingburg / Sidney.");
    } else if (state.location === "Sidney" && cropKey() === "corn") {
      notes.push("Sidney corn history is sparse. Seasonal figures blend available Cargill Sidney / Sidney North bids — type actual basis when you have a posted number.");
    } else if (c && !c.available) {
      notes.push("Limited seasonal history for this elevator and crop. Posted bids and your actual basis still work.");
    }
    if (c && c.latestDate) notes.push("Latest posted curve: " + c.latestDate + ".");
    el.hidden = notes.length === 0;
    el.textContent = notes.join(" ");
  }
  function renderCropSeg() {
    const seg = document.getElementById("cropSeg");
    seg.className = "seg " + (cropKey() === "soybeans" ? "soy" : "corn");
    seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.crop === cropKey()));
  }
  function renderCarryForm() {
    const c = carry();
    document.getElementById("apr").value = c.apr;
    document.getElementById("storage").value = c.storage;
    document.getElementById("markMode").value = c.markMode;
    document.getElementById("handling").value = c.handling;
    document.getElementById("shrinkPct").value = c.shrinkPct;
    document.getElementById("extraPts").value = c.extraPts;
    document.getElementById("shrinkFactor").value = c.shrinkFactor;
    document.getElementById("actualNow").value = state.actualNow;
    document.getElementById("basisMode").value = state.basisMode;
  }
  function readCarryForm() {
    const c = carry();
    c.apr = num(document.getElementById("apr").value) ?? c.apr;
    c.storage = num(document.getElementById("storage").value) ?? 0;
    c.markMode = document.getElementById("markMode").value;
    c.handling = num(document.getElementById("handling").value) ?? 0;
    c.shrinkPct = num(document.getElementById("shrinkPct").value) ?? 0;
    c.extraPts = num(document.getElementById("extraPts").value) ?? 0;
    c.shrinkFactor = num(document.getElementById("shrinkFactor").value) ?? 0;
    state.actualNow = document.getElementById("actualNow").value;
    state.basisMode = document.getElementById("basisMode").value;
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
        refresh({ reread: false });
      });
    });
  }
  function renderRate(nowFut, nowBasis) {
    const parts = carryParts(nowFut, nowBasis && nowBasis.value, DAYS_MO);
    const shrinkMo = (num(carry().shrinkPct) || 0) / 100 * (parts.mark || 0);
    const moist = carryParts(nowFut, nowBasis && nowBasis.value, 90).moisture;
    document.getElementById("rateBox").innerHTML =
      `<div><div class="l">Mark</div><b>${money(parts.mark, 4)}</b></div>
       <div><div class="l">Interest / mo</div><b>${money(parts.interest, 4)}</b></div>
       <div><div class="l">Storage / mo</div><b>${money(parts.storage, 4)}</b></div>
       <div><div class="l">Handling shrink / mo</div><b>${money(shrinkMo, 4)}</b></div>
       <div><div class="l">Monthly rate</div><b>${money(parts.monthlyRate, 4)}</b> / bu</div>
       <div><div class="l">Extra moisture shrink (one-time)</div><b>${money(moist, 4)}</b></div>
       <div><div class="l">In-and-out handling</div><b>${money(num(carry().handling) || 0, 4)}</b></div>`;
  }
  function renderDecision(model) {
    const { rows, nowCash } = model;
    const best = bestRow(rows);
    const box = document.getElementById("decision");
    const hold = best && !best.isNow && best.net != null && best.net > 0.5;
    box.className = "card decision " + (hold ? "hold" : "sell");
    document.getElementById("decKicker").textContent = cropLabel() + " · " + state.location;
    if (!best || best.net == null) {
      document.getElementById("decTitle").textContent = "Need a futures price";
      document.getElementById("decWhy").textContent = "Update futures or type the strip prices, then the net hold vs sell call appears here.";
    } else if (hold) {
      document.getElementById("decTitle").textContent = "Hold into " + best.label;
      document.getElementById("decWhy").textContent =
        "After interest, storage, and shrink, that window beats selling today by " +
        cents(best.net, 1) + ". Futures carry " + cents(best.futCarry, 1) +
        ", basis carry " + cents(best.basisCarry, 1) + ", cost " + cents(best.cost, 1) + ".";
    } else {
      document.getElementById("decTitle").textContent = "Sell now";
      const why = best.isNow
        ? "No later window covers the cost of carry after expected basis."
        : "The best later window still loses " + cents(best.net, 1) + " versus cash today.";
      document.getElementById("decWhy").textContent = why + " Inverse markets and harvest-weak basis are the usual reasons.";
    }
    const later = rows.filter((r) => !r.isNow && r.net != null).sort((a, b) => b.net - a.net)[0];
    document.getElementById("mNow").textContent = money(nowCash, 2);
    document.getElementById("mLater").textContent = later ? money(later.cash, 2) : "—";
    document.getElementById("mLater").parentElement.querySelector(".l").textContent =
      later ? "Best later cash · " + later.label : "Best later cash";
    document.getElementById("mCost").textContent = later ? cents(later.cost, 1) : "—";
    const netEl = document.getElementById("mNet");
    netEl.textContent = later ? cents(later.net, 1) : "—";
    netEl.className = "v " + (later && later.net > 0 ? "good" : later && later.net < 0 ? "bad" : "");
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
        refresh({ reread: false });
      });
    });
  }
  function renderTable(model) {
    const { rows } = model;
    const best = bestRow(rows);
    let html = "";
    let lastQ = "";
    rows.forEach((r) => {
      if (r.quarter.key !== lastQ) {
        lastQ = r.quarter.key;
        html += `<tr class="qhead"><td colspan="11">${r.quarter.label}</td></tr>`;
      }
      const cls = [r.isNow ? "now" : "", best === r ? "best" : "", r.net > 0.5 ? "pos" : r.net < -0.5 ? "neg" : ""].join(" ");
      const call = r.isNow ? "Sell today" : best === r && r.net > 0.5 ? "Best hold" : r.net > 2 ? "Hold" : r.net < -5 ? "Don't wait" : "Flat";
      const actualVal = r.isNow ? state.actualNow : (state.actual[actualKey(r.key)] ?? "");
      const input = r.isNow
        ? `<span class="muted">use box above</span>`
        : `<input class="basis" data-k="${r.key}" inputmode="decimal" value="${actualVal}" placeholder="—" />`;
      html += `<tr class="${cls}">
        <td class="l">${r.label}${r.contract ? " · " + r.contract.short : ""}</td>
        <td>${r.futures != null ? money(r.futures, 2) : "—"}</td>
        <td>${cents(r.futCarry, 1)}</td>
        <td>${r.hist != null ? cents(r.hist, 1) : "—"}</td>
        <td>${r.posted != null ? cents(r.posted, 1) : "—"}</td>
        <td>${input}</td>
        <td>${cents(r.basisCarry, 1)}</td>
        <td>${cents(r.cost, 1)}</td>
        <td>${r.cash != null ? money(r.cash, 2) : "—"}</td>
        <td class="net">${cents(r.net, 1)}</td>
        <td class="l call">${call}</td>
      </tr>`;
    });
    const tb = document.getElementById("tbody");
    tb.innerHTML = html;
    tb.querySelectorAll("input.basis").forEach((inp) => {
      inp.addEventListener("change", () => {
        const k = actualKey(inp.dataset.k);
        if (String(inp.value).trim() === "") delete state.actual[k];
        else state.actual[k] = inp.value;
        save();
        refresh({ reread: false });
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
          label: cropLabel() + " nearby (delayed)",
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
  function renderNetChart(model) {
    const ctx = document.getElementById("netChart");
    if (!ctx || typeof Chart === "undefined") return;
    const rows = model.rows.filter((r) => !r.isNow);
    if (netChart) netChart.destroy();
    netChart = new Chart(ctx, {
      type: "bar",
      data: {
        labels: rows.map((r) => r.label),
        datasets: [
          { label: "Futures carry ¢", data: rows.map((r) => r.futCarry), backgroundColor: "#c9920e", stack: "m" },
          { label: "Basis carry ¢", data: rows.map((r) => r.basisCarry), backgroundColor: "#2f4b8a", stack: "m" },
          { label: "Cost of carry ¢", data: rows.map((r) => r.cost == null ? null : -r.cost), backgroundColor: "#c23a12", stack: "m" },
          { type: "line", label: "Net vs now ¢", data: rows.map((r) => r.net), borderColor: "#0d6b38", backgroundColor: "#0d6b38", tension: 0.2, pointRadius: 3, yAxisID: "y2" },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { position: "bottom", labels: { boxWidth: 10, font: { size: 11 } } } },
        scales: {
          x: { stacked: true, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 } },
          y: { stacked: true, title: { display: true, text: "Carry stack ¢ / bu" } },
          y2: { stacked: false, position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "Net ¢ / bu" } },
        },
      },
    });
  }
  function renderFooter() {
    const spot = DATA.spotRange[cropKey()] || DATA.spotRange.corn;
    document.getElementById("footer").innerHTML =
      "Spot basis history " + (spot ? spot[0] + " to " + spot[1] : "") +
      " · " + DATA.source +
      ". Shrink: extra moisture points × factor × price (farm 1.25%/pt, elevator ~1.35–1.4%); handling/quality default 0.08%/mo corn and 0.10%/mo soy (~0.5–0.6% over 6 months, ISU 0.5–1% per season). Delayed CME via Yahoo. Informational only — not trading advice.";
    document.getElementById("asOfPill").textContent = state.quoteAsOf
      ? ("Quotes " + String(state.quoteAsOf).replace("T", " ").slice(0, 16) + " UTC")
      : "Quotes delayed";
  }
  function refresh(opts) {
    try {
    const reread = !opts || opts.reread !== false;
    const forms = !opts || opts.forms !== false;
    if (reread) readCarryForm();
    renderCropSeg();
    renderLocs();
    renderLocNote();
    if (forms) renderCarryForm();
    renderStrip();
    const model = buildRows();
    const front = frontRow();
    renderRate(front && front.price, model.nowBasis);
    renderDecision(model);
    renderQuarters();
    renderTable(model);
    renderHistChart();
    renderNetChart(model);
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
      refresh({ reread: false });
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
  document.getElementById("location").addEventListener("change", (e) => {
    state.location = e.target.value;
    save();
    refresh({ reread: false, forms: true });
  });
  document.getElementById("btnUpdate").addEventListener("click", updateFutures);
  ["apr", "storage", "markMode", "handling", "shrinkPct", "extraPts", "shrinkFactor", "actualNow", "basisMode"].forEach((id) => {
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
