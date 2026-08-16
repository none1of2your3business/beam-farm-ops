/* Hold or sell — net basis chart + cash sale chart + simple actual basis entry. */
(function () {
  const DATA = window.HOLD_SELL_DATA;
  if (!DATA) {
    document.body.innerHTML = "<p style='padding:2rem'>Missing hold-or-sell-data.js</p>";
    return;
  }

  const LS = "beam.holdSell.v3";
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
    grain: {},
    actual: {},
    carry: {
      corn: { apr: 7, storage: 0.03, shrinkPct: 0.08, extraPts: 0, shrinkFactor: 1.25, handling: 0.02, trucking: 0.18, markMode: "cash" },
      soybeans: { apr: 7, storage: 0.04, shrinkPct: 0.1, extraPts: 0, shrinkFactor: 1.25, handling: 0.02, trucking: 0.18, markMode: "cash" },
    },
    strip: DATA.strip,
    history: DATA.history,
    quoteAsOf: DATA.generated,
  };

  try {
    const saved = JSON.parse(localStorage.getItem(LS) || localStorage.getItem("beam.holdSell.v2") || "null");
    if (saved && typeof saved === "object") {
      state.crop = saved.crop || state.crop;
      state.visible = { ...state.visible, ...(saved.visible || {}) };
      state.grain = saved.grain || {};
      state.actual = saved.actual || {};
      // migrate old actualNow into actual keys
      if (saved.actualNow && typeof saved.actualNow === "object") {
        Object.keys(saved.actualNow).forEach((k) => {
          const parts = k.split("|");
          if (parts.length === 2) {
            const nk = parts[0] + "|" + parts[1] + "|now";
            if (saved.actualNow[k] != null && saved.actualNow[k] !== "") state.actual[nk] = saved.actualNow[k];
          }
        });
      }
      state.carry = {
        corn: { ...state.carry.corn, ...((saved.carry || {}).corn || {}) },
        soybeans: { ...state.carry.soybeans, ...((saved.carry || {}).soybeans || {}) },
      };
      if (saved.strip) state.strip = saved.strip;
      if (saved.history) state.history = saved.history;
      if (saved.quoteAsOf) state.quoteAsOf = saved.quoteAsOf;
    }
  } catch (e) { /* ignore */ }

  let basisChart;
  let cashChart;

  function save() {
    try {
      localStorage.setItem(LS, JSON.stringify({
        crop: state.crop, visible: state.visible, grain: state.grain, actual: state.actual,
        carry: state.carry, strip: state.strip, history: state.history, quoteAsOf: state.quoteAsOf,
      }));
    } catch (e) { /* ignore */ }
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
  function cropBlock(loc) { return (DATA.locations[loc] || {})[cropKey()]; }

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

  function actualKey(loc, periodKey) {
    return cropKey() + "|" + loc + "|" + periodKey;
  }

  /** Actual replaces historical for that window only; blank reverts to historical. */
  function usedBasis(loc, d, periodKey) {
    const typed = num(state.actual[actualKey(loc, periodKey)]);
    if (typed != null) return { value: typed, source: "actual", hist: histBasis(loc, d) };
    const h = histBasis(loc, d);
    return { value: h, source: h == null ? "none" : "seasonal", hist: h };
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
    const moisture = days > 0 ? (num(c.extraPts) || 0) * ((num(c.shrinkFactor) || 0) / 100) * mark : 0;
    const inout = days > 0 ? (num(c.handling) || 0) : 0;
    const trucking = num(c.trucking) || 0; // paid whenever you deliver
    const holdCost = interest + storage + handlingShrink + moisture + inout;
    return {
      mark, interest, storage, handlingShrink, moisture, inout, trucking, holdCost,
      total: holdCost + trucking,
      monthlyRate: (mark * (num(c.apr) || 0) / 100 / 12) + (num(c.storage) || 0) + ((num(c.shrinkPct) || 0) / 100 * mark),
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

  function timeline() {
    const now = today();
    const qs = quarters();
    const points = [{ date: now, key: "now", label: "Now · " + fmtDate(now), isNow: true }];
    qs.forEach((q, qi) => {
      const grain = state.grain[q.key] || defaultGrain(qi);
      periodDates(q.start, q.end, grain).forEach((d) => {
        if (ymd(d) === ymd(now)) return;
        points.push({
          date: d, key: ymd(d),
          label: d.toLocaleDateString(undefined, { month: "short", day: "numeric" }),
          isNow: false, quarter: q,
        });
      });
    });
    return points;
  }

  /**
   * Per location / window:
   * - usedBasis ¢
   * - netBasis ¢ = usedBasis − trucking¢ − holdCost¢  (everything on the basis side)
   * - cash $ = futures + usedBasis/100 − holdCost − trucking  (sell futures + basis together)
   */
  function buildPoint(loc, point, nowFut) {
    const days = point.isNow ? 0 : Math.max(0, Math.round((point.date - today()) / 86400000));
    const b = usedBasis(loc, point.date, point.key);
    const contract = point.isNow ? frontRow() : contractForDate(point.date);
    const fut = contract && contract.price != null ? Number(contract.price) : null;
    // Interest mark uses nearby + current basis for holding cost from today
    const nowB = usedBasis(loc, today(), "now");
    const parts = carryParts(nowFut, nowB.value, days);
    const truckC = parts.trucking * 100;
    const holdC = parts.holdCost * 100;
    const netBasis = b.value != null ? b.value - truckC - holdC : null;
    const cash = (fut != null && b.value != null)
      ? fut + b.value / 100 - parts.holdCost - parts.trucking
      : null;
    return {
      ...point, loc, hist: b.hist, used: b.value, source: b.source,
      futures: fut, netBasis, cash, truckC, holdC, costC: truckC + holdC,
    };
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

  function renderRate() {
    const front = frontRow();
    const loc = availableLocs()[0];
    const nowB = loc ? usedBasis(loc, today(), "now") : { value: 0 };
    const parts = carryParts(front && front.price, nowB.value, DAYS_MO);
    const shrinkMo = (num(carry().shrinkPct) || 0) / 100 * (parts.mark || 0);
    document.getElementById("rateBox").innerHTML =
      `<div><div class="muted">Mark</div><b>${money(parts.mark, 4)}</b></div>
       <div><div class="muted">Interest / mo</div><b>${money(parts.interest, 4)}</b></div>
       <div><div class="muted">Storage / mo</div><b>${money(parts.storage, 4)}</b></div>
       <div><div class="muted">Shrink / mo</div><b>${money(shrinkMo, 4)}</b></div>
       <div><div class="muted">Trucking</div><b>${money(num(carry().trucking) || 0, 4)}</b></div>
       <div><div class="muted">Monthly hold rate</div><b>${money(parts.monthlyRate, 4)}</b></div>`;
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

  function renderBasisEntry() {
    const points = timeline();
    const locs = availableLocs();
    const thead = document.querySelector("#basisTable thead");
    const tb = document.getElementById("tbody");

    if (!locs.length) {
      if (thead) thead.innerHTML = "<tr><th class='l'>Time period</th></tr>";
      tb.innerHTML = "<tr><td class='l'>No locations for this crop.</td></tr>";
      return;
    }

    // Header row 1: location names spanning hist + actual
    // Header row 2: Historical / Actual under each location
    let head = "<tr><th class='l' rowspan='2'>Time period</th>";
    locs.forEach((loc) => {
      head += `<th class="loc" colspan="2">${loc}</th>`;
    });
    head += "</tr><tr>";
    locs.forEach(() => {
      head += "<th>Hist ¢</th><th>Actual ¢</th>";
    });
    head += "</tr>";
    if (thead) thead.innerHTML = head;

    let html = "";
    points.forEach((p) => {
      html += `<tr class="${p.isNow ? "now" : ""}"><td class="l">${p.label}</td>`;
      locs.forEach((loc) => {
        const hist = histBasis(loc, p.date);
        const key = actualKey(loc, p.key);
        const actualVal = state.actual[key] ?? "";
        const usingAct = num(actualVal) != null;
        html += `<td class="hist">${hist != null ? cents(hist, 1) : "—"}</td>`;
        html += `<td class="${usingAct ? "act-on" : ""}"><input class="basis" data-loc="${loc}" data-k="${p.key}" inputmode="decimal" value="${actualVal}" placeholder="—" /></td>`;
      });
      html += "</tr>";
    });
    tb.innerHTML = html;
    tb.querySelectorAll("input.basis").forEach((inp) => {
      inp.addEventListener("change", () => {
        const k = actualKey(inp.dataset.loc, inp.dataset.k);
        if (String(inp.value).trim() === "") delete state.actual[k];
        else state.actual[k] = inp.value;
        save();
        refresh({ reread: false, forms: false });
      });
    });
  }

  function zeroLinePlugin() {
    return {
      id: "zeroLine",
      afterDraw(c) {
        const y = c.scales.y;
        if (!y || y.min > 0 || y.max < 0) return;
        const yPix = y.getPixelForValue(0);
        const { ctx: g, chartArea } = c;
        g.save();
        g.strokeStyle = "rgba(42,64,51,0.45)";
        g.setLineDash([5, 4]);
        g.beginPath();
        g.moveTo(chartArea.left, yPix);
        g.lineTo(chartArea.right, yPix);
        g.stroke();
        g.restore();
      },
    };
  }

  function renderCharts() {
    if (typeof Chart === "undefined") return;
    const points = timeline();
    const labels = points.map((p) => p.label);
    const front = frontRow();
    const nowFut = front && front.price != null ? Number(front.price) : null;
    const locs = activeLocs();

    const basisSets = locs.map((loc) => ({
      label: loc,
      data: points.map((p) => buildPoint(loc, p, nowFut).netBasis),
      borderColor: LOC_COLORS[loc] || "#333",
      backgroundColor: LOC_COLORS[loc] || "#333",
      borderWidth: 2.25, pointRadius: 2.5, tension: 0.2,
    }));
    const cashSets = locs.map((loc) => ({
      label: loc,
      data: points.map((p) => buildPoint(loc, p, nowFut).cash),
      borderColor: LOC_COLORS[loc] || "#333",
      backgroundColor: LOC_COLORS[loc] || "#333",
      borderWidth: 2.25, pointRadius: 2.5, tension: 0.2,
    }));

    const bctx = document.getElementById("basisChart");
    const cctx = document.getElementById("cashChart");
    if (basisChart) basisChart.destroy();
    if (cashChart) cashChart.destroy();

    basisChart = new Chart(bctx, {
      type: "line",
      data: { labels, datasets: basisSets },
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
          x: { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 }, title: { display: true, text: "Move window" } },
          y: { title: { display: true, text: "Net basis after costs (¢ / bu)" }, ticks: { callback: (v) => (v > 0 ? "+" : "") + v + "¢" } },
        },
      },
      plugins: [zeroLinePlugin()],
    });

    cashChart = new Chart(cctx, {
      type: "line",
      data: { labels, datasets: cashSets },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 12, usePointStyle: true } },
          tooltip: {
            callbacks: {
              label(item) {
                const v = item.parsed.y;
                return " " + item.dataset.label + ": " + (v == null ? "—" : money(v, 2) + "/bu");
              },
            },
          },
        },
        scales: {
          x: { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 }, title: { display: true, text: "Move window" } },
          y: { title: { display: true, text: "Expected cash sale ($ / bu)" }, ticks: { callback: (v) => "$" + Number(v).toFixed(2) } },
        },
      },
    });
  }

  function renderFooter() {
    document.getElementById("footer").innerHTML =
      "Net basis = used basis − trucking − interest − storage − shrink. Cash sale = futures + used basis − those same costs. Actual basis overrides historical only where entered. Dayton corn-only · Sidney soybeans-only. Delayed CME — informational only.";
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
      if (forms) renderCarryForm();
      renderStrip();
      renderRate();
      renderQuarters();
      renderBasisEntry();
      renderCharts();
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
  ["apr", "storage", "trucking", "markMode", "handling", "shrinkPct", "extraPts", "shrinkFactor"].forEach((id) => {
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
