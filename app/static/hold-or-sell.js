/* Grain Marketing Decisions — net basis chart + cash sale chart + simple actual basis entry. */
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
  const LOC_IDS = ["Kellogg", "Dayton", "Bloomingburg", "Sidney"];
  const DEFAULT_TRUCK = 0.18;

  function defaultTrucking() {
    const out = {};
    LOC_IDS.forEach((id) => { out[id] = DEFAULT_TRUCK; });
    return out;
  }

  const state = {
    crop: "corn",
    visible: { Kellogg: true, Dayton: true, Bloomingburg: true, Sidney: true },
    trucking: defaultTrucking(),
    chartBoth: false,
    grain: {},
    actual: {},
    carry: {
      corn: { apr: 7, storage: 0.03, shrinkPct: 0.08, extraPts: 0, shrinkFactor: 1.25, handling: 0.02, markMode: "cash" },
      soybeans: { apr: 7, storage: 0.04, shrinkPct: 0.1, extraPts: 0, shrinkFactor: 1.25, handling: 0.02, markMode: "cash" },
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
      if (saved.chartBoth != null) state.chartBoth = !!saved.chartBoth;
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
      // Per-location trucking (migrate old single carry.trucking if needed)
      function parseN(v) {
        const n = parseFloat(String(v ?? "").replace(",", ""));
        return Number.isFinite(n) ? n : null;
      }
      const legacyTruck =
        parseN(typeof saved.trucking === "number" ? saved.trucking : null) ??
        parseN((saved.carry || {}).corn && saved.carry.corn.trucking) ??
        parseN((saved.carry || {}).soybeans && saved.carry.soybeans.trucking) ??
        DEFAULT_TRUCK;
      state.trucking = defaultTrucking();
      LOC_IDS.forEach((id) => { state.trucking[id] = legacyTruck; });
      if (saved.trucking && typeof saved.trucking === "object") {
        LOC_IDS.forEach((id) => {
          const v = parseN(saved.trucking[id]);
          if (v != null) state.trucking[id] = v;
        });
      }
      delete state.carry.corn.trucking;
      delete state.carry.soybeans.trucking;
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
        crop: state.crop, visible: state.visible, trucking: state.trucking, chartBoth: state.chartBoth,
        grain: state.grain, actual: state.actual,
        carry: state.carry, strip: state.strip, history: state.history, quoteAsOf: state.quoteAsOf,
      }));
    } catch (e) { /* ignore */ }
  }

  function truckingFor(loc) {
    const v = num(state.trucking[loc]);
    return v != null ? v : DEFAULT_TRUCK;
  }

  function cropKey() { return state.crop === "soybeans" ? "soybeans" : "corn"; }
  function cropLabel() { return cropKey() === "soybeans" ? "Soybeans" : "Corn"; }
  function stripCrop() { return cropLabel(); }
  function carry() { return state.carry[cropKey()]; }

  function withCrop(crop, fn) {
    const prev = state.crop;
    state.crop = crop === "soybeans" ? "soybeans" : "corn";
    try { return fn(); }
    finally { state.crop = prev; }
  }

  function chartCrops() {
    return state.chartBoth ? ["corn", "soybeans"] : [cropKey()];
  }

  function unionLocs() {
    const ids = new Set();
    chartCrops().forEach((c) => {
      withCrop(c, () => availableLocs().forEach((id) => ids.add(id)));
    });
    return LOC_IDS.filter((id) => ids.has(id));
  }

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

  function carryParts(fut, basisCents, days, loc) {
    const c = carry();
    const mark = markPrice(fut, basisCents) || 0;
    const months = Math.max(0, days) / DAYS_MO;
    const years = Math.max(0, days) / 365;
    const interest = mark * (num(c.apr) || 0) / 100 * years;
    const storage = (num(c.storage) || 0) * months;
    const handlingShrink = (num(c.shrinkPct) || 0) / 100 * mark * months;
    const moisture = days > 0 ? (num(c.extraPts) || 0) * ((num(c.shrinkFactor) || 0) / 100) * mark : 0;
    const inout = days > 0 ? (num(c.handling) || 0) : 0;
    const trucking = loc ? truckingFor(loc) : 0; // paid whenever you deliver; per location
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
    const parts = carryParts(nowFut, nowB.value, days, loc);
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
    const both = document.getElementById("chartBoth");
    if (both) both.checked = !!state.chartBoth;
  }

  function renderLocs() {
    const avail = unionLocs();
    document.getElementById("locs").innerHTML = avail.map((id) => {
      const on = state.visible[id] !== false;
      return `<label class="loc ${on ? "" : "off"}">
        <span class="swatch" style="background:${LOC_COLORS[id] || "#666"}"></span>
        <input type="checkbox" data-id="${id}" ${on ? "checked" : ""} />
        <strong>${id}</strong>
      </label>`;
    }).join("");
    document.getElementById("locs").querySelectorAll("input[type=checkbox]").forEach((inp) => {
      inp.addEventListener("change", () => {
        state.visible[inp.dataset.id] = inp.checked;
        save();
        refresh({ reread: false, forms: false, locs: true });
      });
    });
  }

  function renderTrucking() {
    const root = document.getElementById("truckGrid");
    if (!root) return;
    const avail = unionLocs();
    root.innerHTML = avail.map((id) => {
      const truck = truckingFor(id);
      return `<div class="truck-card">
        <div class="name"><span class="swatch" style="background:${LOC_COLORS[id] || "#666"}"></span>${id}</div>
        <label>Trucking $/bu
          <input class="truck" data-truck="${id}" inputmode="decimal" value="${truck}" placeholder="0.18" />
        </label>
      </div>`;
    }).join("") || "<p class='hint'>No locations for this crop.</p>";
    root.querySelectorAll("input.truck").forEach((inp) => {
      const apply = () => {
        const v = num(inp.value);
        state.trucking[inp.dataset.truck] = v != null ? v : 0;
        save();
        refresh({ reread: false, forms: false });
      };
      inp.addEventListener("change", apply);
      inp.addEventListener("input", apply);
    });
  }

  function findBest(metric) {
    const points = timeline();
    const locs = activeLocs();
    const front = frontRow();
    const nowFut = front && front.price != null ? Number(front.price) : null;
    let best = null;
    locs.forEach((loc) => {
      points.forEach((p) => {
        const row = buildPoint(loc, p, nowFut);
        const v = row[metric];
        if (v == null || !Number.isFinite(v)) return;
        if (!best || v > best.value) {
          best = { value: v, loc, label: p.label, isNow: !!p.isNow, key: p.key };
        }
      });
    });
    return best;
  }

  function renderWinners() {
    const el = document.getElementById("winners");
    if (!el) return;
    const bestBasis = findBest("netBasis");
    const bestCash = findBest("cash");

    function card(title, best, fmt) {
      if (!best) {
        return `<div class="winner">
          <div class="winner-lab">${title}</div>
          <div class="winner-val">—</div>
          <div class="winner-meta">No result yet</div>
          <div class="winner-sub">Check a location and load futures/basis.</div>
        </div>`;
      }
      const color = LOC_COLORS[best.loc] || "#666";
      return `<div class="winner">
        <div class="winner-lab">${title}</div>
        <div class="winner-val">${fmt(best.value)}</div>
        <div class="winner-meta"><span class="dot" style="background:${color}"></span>${best.loc} · ${best.label}</div>
        <div class="winner-sub">Best ${title.toLowerCase()} among checked locations</div>
      </div>`;
    }

    el.innerHTML =
      card("Best net basis", bestBasis, (v) => cents(v, 1)) +
      card("Best cash sale", bestCash, (v) => money(v, 2) + "/bu");
    renderStorePick();
  }

  function cropStoreSnapshot(crop) {
    return withCrop(crop, () => {
      const bestCash = findBest("cash");
      const bestBasis = findBest("netBasis");
      const points = timeline();
      const nowPt = points.find((p) => p.isNow) || points[0];
      const locs = activeLocs();
      const front = frontRow();
      const nowFut = front && front.price != null ? Number(front.price) : null;
      let nowCash = null;
      let nowBasis = null;
      let nowLoc = null;
      locs.forEach((loc) => {
        const row = buildPoint(loc, nowPt, nowFut);
        if (row.cash != null && (nowCash == null || row.cash > nowCash)) {
          nowCash = row.cash;
          nowBasis = row.netBasis;
          nowLoc = loc;
        }
      });
      const cashGain = (bestCash && nowCash != null) ? bestCash.value - nowCash : null;
      const basisGain = (bestBasis && nowBasis != null) ? bestBasis.value - nowBasis : null;
      return {
        crop,
        label: crop === "soybeans" ? "Soybeans" : "Corn",
        bestCash, bestBasis, nowCash, nowBasis, nowLoc, cashGain, basisGain,
      };
    });
  }

  function renderStorePick() {
    const el = document.getElementById("storePick");
    if (!el) return;
    const corn = cropStoreSnapshot("corn");
    const soy = cropStoreSnapshot("soybeans");

    function fmtBest(best, kind) {
      if (!best) return "—";
      const v = kind === "cash" ? money(best.value, 2) + "/bu" : cents(best.value, 1);
      return v + " · " + best.loc + " · " + best.label;
    }
    function fmtGain(g, kind) {
      if (g == null || !Number.isFinite(g)) return "—";
      if (kind === "cash") return (g >= 0 ? "+" : "") + money(g, 2) + "/bu vs sell now";
      return (g >= 0 ? "+" : "") + g.toFixed(1) + "¢ vs sell now";
    }

    let winner = null;
    if (corn.cashGain != null && soy.cashGain != null) winner = corn.cashGain >= soy.cashGain ? corn : soy;
    else if (corn.cashGain != null) winner = corn;
    else if (soy.cashGain != null) winner = soy;

    let why = "Need futures and basis on both crops to compare.";
    if (winner) {
      const other = winner.crop === "corn" ? soy : corn;
      const wGain = winner.cashGain;
      const oGain = other.cashGain;
      const later = winner.bestCash && !winner.bestCash.isNow;
      if (wGain != null && wGain <= 0 && (oGain == null || oGain <= 0)) {
        why = "Neither crop pays to store after trucking and hold costs — selling now beats holding both.";
      } else if (later && winner.bestCash) {
        const extra = (wGain - (oGain || 0));
        why = winner.label + " stores better: holding to " + winner.bestCash.loc + " / " + winner.bestCash.label
          + " adds " + money(wGain, 2) + "/bu vs selling now"
          + (oGain != null ? ", " + money(Math.abs(extra), 2) + "/bu more than " + other.label.toLowerCase() + "." : ".");
        if (wGain != null && wGain <= 0.005) {
          why = winner.label + " is the less-bad store, but extra cash vs selling now is about zero after costs.";
        }
      } else {
        why = winner.label + " wins on cash, but the best reading is already now — storing does not add money.";
      }
    }

    el.innerHTML = `
      <div class="winner-lab">Most profitable crop to store</div>
      <div class="pick-val">${winner ? winner.label : "—"}</div>
      <p class="why">${why}</p>
      <div class="store-cmp">
        <article class="${winner && winner.crop === "corn" ? "on" : ""}">
          <h3>Corn</h3>
          <div><span>Best net basis</span><b>${fmtBest(corn.bestBasis, "basis")}</b></div>
          <div><span>Best cash</span><b>${fmtBest(corn.bestCash, "cash")}</b></div>
          <div><span>Store vs now</span><b>${fmtGain(corn.cashGain, "cash")}</b></div>
        </article>
        <article class="${winner && winner.crop === "soybeans" ? "on" : ""}">
          <h3>Soybeans</h3>
          <div><span>Best net basis</span><b>${fmtBest(soy.bestBasis, "basis")}</b></div>
          <div><span>Best cash</span><b>${fmtBest(soy.bestCash, "cash")}</b></div>
          <div><span>Store vs now</span><b>${fmtGain(soy.cashGain, "cash")}</b></div>
        </article>
      </div>`;
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
    const loc = activeLocs()[0] || availableLocs()[0];
    const nowB = loc ? usedBasis(loc, today(), "now") : { value: 0 };
    const parts = carryParts(front && front.price, nowB.value, DAYS_MO, loc);
    const shrinkMo = (num(carry().shrinkPct) || 0) / 100 * (parts.mark || 0);
    document.getElementById("rateBox").innerHTML =
      `<div><div class="muted">Mark</div><b>${money(parts.mark, 4)}</b></div>
       <div><div class="muted">Interest / mo</div><b>${money(parts.interest, 4)}</b></div>
       <div><div class="muted">Storage / mo</div><b>${money(parts.storage, 4)}</b></div>
       <div><div class="muted">Shrink / mo</div><b>${money(shrinkMo, 4)}</b></div>
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

  let basisLocObserver = null;

  function setBasisLocPin(loc) {
    const pin = document.getElementById("basisLocPin");
    if (!pin || !loc) return;
    const color = LOC_COLORS[loc] || "#fff";
    pin.classList.add("on");
    pin.innerHTML = `<span class="swatch" style="background:${color}"></span><strong>${loc}</strong><em>Actual basis</em>`;
  }

  function setupBasisLocPin(mobile) {
    const pin = document.getElementById("basisLocPin");
    if (!mobile || !pin) return;
    if (basisLocObserver) {
      basisLocObserver.disconnect();
      basisLocObserver = null;
    }
    const sections = mobile.querySelectorAll(".bm-loc");
    if (!sections.length) {
      pin.classList.remove("on");
      pin.innerHTML = "";
      return;
    }
    setBasisLocPin(sections[0].dataset.loc);
    if (typeof IntersectionObserver !== "undefined") {
      basisLocObserver = new IntersectionObserver((entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0] && visible[0].target.dataset.loc) {
          setBasisLocPin(visible[0].target.dataset.loc);
        }
      }, { root: null, rootMargin: "-20% 0px -55% 0px", threshold: [0, 0.1, 0.4] });
      sections.forEach((sec) => basisLocObserver.observe(sec));
    }
    mobile.querySelectorAll("input.basis").forEach((inp) => {
      inp.addEventListener("focus", () => setBasisLocPin(inp.dataset.loc));
    });
  }

  function bindBasisInputs(root) {
    if (!root) return;
    root.querySelectorAll("input.basis").forEach((inp) => {
      inp.addEventListener("change", () => {
        const k = actualKey(inp.dataset.loc, inp.dataset.k);
        if (String(inp.value).trim() === "") delete state.actual[k];
        else state.actual[k] = inp.value;
        // Mark row visually without rebuilding the whole list (keeps sticky header + scroll)
        const row = inp.closest(".bm-row, td");
        if (row) {
          if (num(inp.value) != null) row.classList.add("act-on");
          else row.classList.remove("act-on");
        }
        save();
        refresh({ reread: false, forms: false, basis: false });
      });
    });
  }

  function renderBasisEntry() {
    const points = timeline();
    const locs = availableLocs();
    const thead = document.querySelector("#basisTable thead");
    const tb = document.getElementById("tbody");
    const mobile = document.getElementById("basisMobile");

    if (!locs.length) {
      if (thead) thead.innerHTML = "<tr><th class='l'>Time period</th></tr>";
      tb.innerHTML = "<tr><td class='l'>No locations for this crop.</td></tr>";
      if (mobile) mobile.innerHTML = "<p class='hint'>No locations for this crop.</p>";
      return;
    }

    // Header: location name on top spanning both columns, then Hist / Actual under it
    let head = "<tr><th class='l' rowspan='2'>Time period</th>";
    locs.forEach((loc) => {
      const color = LOC_COLORS[loc] || "#666";
      head += `<th class="loc" colspan="2"><span class="loc-dot" style="background:${color}"></span>${loc}</th>`;
    });
    head += "</tr><tr>";
    locs.forEach(() => {
      head += "<th class='sub'>Historical ¢</th><th class='sub'>Actual ¢</th>";
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
        html += `<td class="${usingAct ? "act-on" : ""}"><input class="basis" data-loc="${loc}" data-k="${p.key}" inputmode="decimal" value="${actualVal}" placeholder="—" title="Actual basis · ${loc} · ${p.label}" aria-label="Actual basis cents for ${loc} at ${p.label}" /></td>`;
      });
      html += "</tr>";
    });
    tb.innerHTML = html;
    bindBasisInputs(tb);

    // Mobile: sticky location pin + sticky section headers while typing
    if (mobile) {
      let mhtml = `<div class="bm-pin" id="basisLocPin" aria-live="polite"></div>`;
      locs.forEach((loc) => {
        const color = LOC_COLORS[loc] || "#666";
        mhtml += `<section class="bm-loc" data-loc="${loc}">
          <div class="bm-loc-head"><span class="swatch" style="background:${color}"></span>${loc}</div>`;
        points.forEach((p) => {
          const hist = histBasis(loc, p.date);
          const key = actualKey(loc, p.key);
          const actualVal = state.actual[key] ?? "";
          const usingAct = num(actualVal) != null;
          mhtml += `<div class="bm-row ${p.isNow ? "now" : ""} ${usingAct ? "act-on" : ""}">
            <div class="bm-when">${p.label}</div>
            <div class="bm-hist"><span>Hist</span>${hist != null ? cents(hist, 1) : "—"}</div>
            <label class="bm-act"><span>Actual ¢</span>
              <input class="basis" data-loc="${loc}" data-k="${p.key}" inputmode="decimal" value="${actualVal}" placeholder="¢" title="Actual basis · ${loc} · ${p.label}" aria-label="Actual basis cents for ${loc} at ${p.label}" />
            </label>
          </div>`;
        });
        mhtml += "</section>";
      });
      mobile.innerHTML = mhtml;
      bindBasisInputs(mobile);
      setupBasisLocPin(mobile);
    }
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
    const both = !!state.chartBoth;
    const crops = chartCrops();
    const basisSets = [];
    const cashSets = [];

    crops.forEach((crop) => {
      withCrop(crop, () => {
        const front = frontRow();
        const nowFut = front && front.price != null ? Number(front.price) : null;
        const locs = activeLocs();
        const soy = crop === "soybeans";
        const short = soy ? "Soy" : "Corn";
        locs.forEach((loc) => {
          const color = LOC_COLORS[loc] || "#333";
          const name = both ? short + " · " + loc : loc;
          const style = {
            label: name,
            borderColor: color,
            backgroundColor: color,
            borderWidth: soy && both ? 2 : 2.25,
            borderDash: soy && both ? [7, 4] : [],
            pointRadius: 2.5,
            tension: 0.2,
            yAxisID: both && soy ? "ySoy" : "y",
          };
          basisSets.push({
            ...style,
            yAxisID: "y",
            data: points.map((p) => buildPoint(loc, p, nowFut).netBasis),
          });
          cashSets.push({
            ...style,
            data: points.map((p) => buildPoint(loc, p, nowFut).cash),
          });
        });
      });
    });

    const bctx = document.getElementById("basisChart");
    const cctx = document.getElementById("cashChart");
    if (basisChart) basisChart.destroy();
    if (cashChart) cashChart.destroy();

    const cashScales = both
      ? {
          x: { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 }, title: { display: true, text: "Move window" } },
          y: { title: { display: true, text: "Corn cash ($ / bu)" }, ticks: { callback: (v) => "$" + Number(v).toFixed(2) } },
          ySoy: { position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "Soy cash ($ / bu)" }, ticks: { callback: (v) => "$" + Number(v).toFixed(2) } },
        }
      : {
          x: { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 10 }, title: { display: true, text: "Move window" } },
          y: { title: { display: true, text: "Expected cash sale ($ / bu)" }, ticks: { callback: (v) => "$" + Number(v).toFixed(2) } },
        };

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
        scales: cashScales,
      },
    });
  }

  function renderFooter() {
    document.getElementById("footer").innerHTML =
      "Net basis = used basis − location trucking − interest − storage − shrink. Cash sale = futures + used basis − those same costs. Actual basis overrides historical only where entered. Dayton corn-only · Sidney soybeans-only. Delayed CME — informational only.";
    document.getElementById("asOfPill").textContent = state.quoteAsOf
      ? ("Quotes " + String(state.quoteAsOf).replace("T", " ").slice(0, 16) + " UTC")
      : "Quotes delayed";
  }

  function refresh(opts) {
    try {
      const reread = !opts || opts.reread !== false;
      const forms = !opts || opts.forms !== false;
      const locs = forms || (opts && opts.locs === true);
      const basis = !opts || opts.basis !== false;
      if (reread) readCarryForm();
      renderCropSeg();
      if (locs) {
        renderLocs();
        renderTrucking();
      }
      if (forms) renderCarryForm();
      renderStrip();
      renderRate();
      renderQuarters();
      if (basis) renderBasisEntry();
      renderWinners();
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

  function downloadBlob(blob, filename) {
    const a = document.createElement("a");
    const url = URL.createObjectURL(blob);
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
  }

  function worksheetPayload() {
    readCarryForm();
    return {
      version: 1,
      tool: "Grain Marketing Decisions",
      savedAt: new Date().toISOString(),
      crop: state.crop,
      visible: state.visible,
      trucking: state.trucking,
      chartBoth: state.chartBoth,
      grain: state.grain,
      actual: state.actual,
      carry: state.carry,
      strip: state.strip,
      history: state.history,
      quoteAsOf: state.quoteAsOf,
    };
  }

  function saveWorksheetFile() {
    const payload = worksheetPayload();
    const stamp = new Date().toISOString().slice(0, 10);
    const name = "grain-marketing-decisions-" + cropKey() + "-" + stamp + ".json";
    downloadBlob(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }), name);
    setStatus("Worksheet saved as " + name);
  }

  function applyWorksheet(data) {
    if (!data || typeof data !== "object") throw new Error("Not a worksheet file.");
    if (data.crop) state.crop = data.crop;
    if (data.visible) state.visible = { ...state.visible, ...data.visible };
    if (data.chartBoth != null) state.chartBoth = !!data.chartBoth;
    if (data.trucking && typeof data.trucking === "object") {
      LOC_IDS.forEach((id) => {
        const v = num(data.trucking[id]);
        if (v != null) state.trucking[id] = v;
      });
    }
    if (data.grain) state.grain = data.grain;
    if (data.actual) state.actual = data.actual;
    if (data.carry) {
      state.carry = {
        corn: { ...state.carry.corn, ...((data.carry || {}).corn || {}) },
        soybeans: { ...state.carry.soybeans, ...((data.carry || {}).soybeans || {}) },
      };
      delete state.carry.corn.trucking;
      delete state.carry.soybeans.trucking;
    }
    if (data.strip) state.strip = data.strip;
    if (data.history) state.history = data.history;
    if (data.quoteAsOf) state.quoteAsOf = data.quoteAsOf;
    save();
    refresh({ reread: false, forms: true });
  }

  function loadWorksheetFile(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const data = JSON.parse(String(reader.result || ""));
        applyWorksheet(data);
        setStatus("Loaded " + (file.name || "worksheet") + ".");
      } catch (e) {
        setStatus("Could not load that file. Use a saved worksheet JSON.", true);
      }
    };
    reader.onerror = () => setStatus("Could not read that file.", true);
    reader.readAsText(file);
  }

  function esc(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function buildPrintSummaryHtml() {
    readCarryForm();
    const c = carry();
    const front = frontRow();
    const points = timeline();
    const locs = availableLocs();
    const active = activeLocs();
    const nowFut = front && front.price != null ? Number(front.price) : null;
    const stamp = new Date().toLocaleString();

    const kv =
      `<div class="kv">
        <div><span>Interest APR</span><b>${esc(c.apr)}%</b></div>
        <div><span>Storage $/bu/mo</span><b>${esc(money(num(c.storage) || 0, 4))}</b></div>
        <div><span>Handling $/bu</span><b>${esc(money(num(c.handling) || 0, 4))}</b></div>
        <div><span>Shrink %/mo</span><b>${esc(c.shrinkPct)}</b></div>
        <div><span>Extra moisture pts</span><b>${esc(c.extraPts)}</b></div>
        <div><span>Shrink factor</span><b>${esc(c.shrinkFactor)}</b></div>
        <div><span>Interest mark</span><b>${esc(c.markMode)}</b></div>
        <div><span>Front futures</span><b>${esc(front ? front.label + " " + money(nowFut, 4) : "—")}</b></div>
      </div>`;

    const truckRows = locs.map((id) =>
      `<tr><td>${esc(id)}</td><td>${esc(money(truckingFor(id), 4))}</td><td>${state.visible[id] !== false ? "On chart" : "Hidden"}</td></tr>`
    ).join("");

    // Summary windows: Now + roughly monthly samples through horizon
    const sample = [];
    points.forEach((p, i) => {
      if (p.isNow || i % Math.max(1, Math.floor(points.length / 12)) === 0 || i === points.length - 1) {
        if (!sample.find((s) => s.key === p.key)) sample.push(p);
      }
    });

    let head = "<tr><th>Window</th>";
    active.forEach((loc) => { head += `<th colspan="2">${esc(loc)}</th>`; });
    head += "</tr><tr><th></th>";
    active.forEach(() => { head += "<th>Net basis ¢</th><th>Cash $/bu</th>"; });
    head += "</tr>";

    let body = "";
    sample.forEach((p) => {
      body += `<tr><td>${esc(p.label)}</td>`;
      active.forEach((loc) => {
        const row = buildPoint(loc, p, nowFut);
        body += `<td>${esc(row.netBasis != null ? cents(row.netBasis, 1) : "—")}</td>`;
        body += `<td>${esc(row.cash != null ? money(row.cash, 2) : "—")}</td>`;
      });
      body += "</tr>";
    });

    let basisImg = "";
    let cashImg = "";
    try { if (basisChart) basisImg = basisChart.toBase64Image("image/png", 1); } catch (e) { /* ignore */ }
    try { if (cashChart) cashImg = cashChart.toBase64Image("image/png", 1); } catch (e) { /* ignore */ }

    const bestBasis = findBest("netBasis");
    const bestCash = findBest("cash");

    function winnerBlock(title, best, fmt, why) {
      if (!best) {
        return `<div class="winner-print">
          <div class="winner-lab">${esc(title)}</div>
          <div class="winner-val">—</div>
          <div class="winner-meta">No result yet</div>
        </div>`;
      }
      return `<div class="winner-print">
        <div class="winner-lab">${esc(title)}</div>
        <div class="winner-val">${esc(fmt(best.value))}</div>
        <div class="winner-meta"><strong>${esc(best.loc)}</strong> at <strong>${esc(best.label)}</strong></div>
        <div class="winner-sub">${esc(why)}</div>
      </div>`;
    }

    const winnersHtml = `
      <h2 class="lead">The winners</h2>
      <p class="lead-note">These are the strongest outcomes across every checked elevator and every move window on the charts. Start here — then use the charts and numbers below to see why they won.</p>
      <div class="grid-print winners-print">
        ${winnerBlock(
          "Best net basis",
          bestBasis,
          (v) => cents(v, 1),
          "Highest basis left after trucking and hold costs."
        )}
        ${winnerBlock(
          "Best cash sale",
          bestCash,
          (v) => money(v, 2) + "/bu",
          "Highest all-in cash price after futures, basis, trucking, and hold costs."
        )}
      </div>`;

    const cornSnap = cropStoreSnapshot("corn");
    const soySnap = cropStoreSnapshot("soybeans");
    const storeEl = document.getElementById("storePick");
    const storeWhy = storeEl ? (storeEl.querySelector(".why") || {}).textContent || "" : "";
    const storeVal = storeEl ? (storeEl.querySelector(".pick-val") || {}).textContent || "—" : "—";
    const storeHtml = `
      <h2>Most profitable crop to store</h2>
      <p><strong>${esc(storeVal)}</strong> — ${esc(storeWhy)}</p>
      <div class="grid-print">
        <div>Corn best basis ${esc(cornSnap.bestBasis ? cents(cornSnap.bestBasis.value, 1) + " · " + cornSnap.bestBasis.loc + " · " + cornSnap.bestBasis.label : "—")}<br/>
        Corn best cash ${esc(cornSnap.bestCash ? money(cornSnap.bestCash.value, 2) + "/bu · " + cornSnap.bestCash.loc + " · " + cornSnap.bestCash.label : "—")}<br/>
        Store vs now ${esc(cornSnap.cashGain != null ? money(cornSnap.cashGain, 2) + "/bu" : "—")}</div>
        <div>Soy best basis ${esc(soySnap.bestBasis ? cents(soySnap.bestBasis.value, 1) + " · " + soySnap.bestBasis.loc + " · " + soySnap.bestBasis.label : "—")}<br/>
        Soy best cash ${esc(soySnap.bestCash ? money(soySnap.bestCash.value, 2) + "/bu · " + soySnap.bestCash.loc + " · " + soySnap.bestCash.label : "—")}<br/>
        Store vs now ${esc(soySnap.cashGain != null ? money(soySnap.cashGain, 2) + "/bu" : "—")}</div>
      </div>`;

    const chartsHtml = `
      <h2>Charts</h2>
      <div class="grid-print">
        <div>
          <h3>Net basis after costs (¢/bu)</h3>
          ${basisImg ? `<img class="chart" src="${basisImg}" alt="Net basis chart" />` : "<p>Chart unavailable</p>"}
        </div>
        <div>
          <h3>Cash sale — futures + basis ($/bu)</h3>
          ${cashImg ? `<img class="chart" src="${cashImg}" alt="Cash sale chart" />` : "<p>Chart unavailable</p>"}
        </div>
      </div>`;

    const metricsHtml = `
      <h2>Metrics used</h2>
      <h3>Cost of carry</h3>
      ${kv}
      <h3>Trucking by location</h3>
      <table><thead><tr><th>Location</th><th>Trucking $/bu</th><th>On charts</th></tr></thead><tbody>${truckRows}</tbody></table>
      <h3>Sample windows</h3>
      <table><thead>${head}</thead><tbody>${body}</tbody></table>`;

    const explainHtml = `
      <h2>How this is calculated (plain English)</h2>
      <div class="explain">
        <p><strong>Why the winners are at the top.</strong>
        Marketing comes down to two questions: (1) where is basis strongest after your real costs, and
        (2) where do you take home the most cash if you sell futures and basis together.
        The winners answer those first so you do not have to dig through every line on the charts.</p>

        <p><strong>Basis used.</strong>
        For each location and each time window we use your <em>actual</em> basis if you typed one.
        If that cell is blank, we use seasonal historical basis for that elevator and crop.</p>

        <p><strong>Net basis after costs (¢/bu).</strong>
        Start with used basis, then subtract haul cost (trucking for that elevator) and the cost of holding grain
        (interest, storage, shrink, and handling when the sale is later than today).
        A higher net basis means more of the posted basis is still yours after costs.</p>

        <p><strong>Cash sale ($/bu).</strong>
        Take the CME futures price for that window, add used basis, then subtract the same trucking and hold costs.
        That is the all-in cash number if you sell both the futures and the basis at that location and time.</p>

        <p><strong>How a winner is picked.</strong>
        We look only at locations you checked on the charts. For every move window we compute net basis and cash sale.
        Best net basis = the single highest net-basis reading. Best cash sale = the single highest cash reading.
        They can be different locations or different dates — basis strength and full cash price are not always the same decision.</p>

        <p class="meta">Informational only — delayed quotes, seasonal history, and your typed assumptions. Not a trade recommendation.</p>
      </div>`;

    return `
      <h1>Grain Marketing Decisions</h1>
      <p class="meta">${esc(cropLabel())} · Printed ${esc(stamp)} · Quotes ${esc(state.quoteAsOf || "delayed")}</p>
      ${winnersHtml}
      ${storeHtml}
      ${chartsHtml}
      ${metricsHtml}
      ${explainHtml}
    `;
  }

  function printPdfSummary() {
    const root = document.getElementById("printRoot");
    if (!root) return;
    root.innerHTML = buildPrintSummaryHtml();
    root.setAttribute("aria-hidden", "false");
    setStatus("Print dialog: choose “Save as PDF” for a PDF summary.");
    const cleanup = () => {
      root.innerHTML = "";
      root.setAttribute("aria-hidden", "true");
      window.removeEventListener("afterprint", cleanup);
    };
    window.addEventListener("afterprint", cleanup);
    setTimeout(() => window.print(), 50);
  }

  document.getElementById("cropSeg").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.crop = b.dataset.crop;
    save();
    refresh({ reread: false, forms: true });
  });
  const bothEl = document.getElementById("chartBoth");
  if (bothEl) {
    bothEl.addEventListener("change", () => {
      state.chartBoth = bothEl.checked;
      save();
      refresh({ reread: false, forms: false, locs: true });
    });
  }
  document.getElementById("btnUpdate").addEventListener("click", updateFutures);
  document.getElementById("btnSave").addEventListener("click", saveWorksheetFile);
  document.getElementById("btnLoad").addEventListener("click", () => document.getElementById("fileLoad").click());
  document.getElementById("fileLoad").addEventListener("change", (e) => {
    const f = e.target.files && e.target.files[0];
    loadWorksheetFile(f);
    e.target.value = "";
  });
  document.getElementById("btnPdf").addEventListener("click", printPdfSummary);
  ["apr", "storage", "markMode", "handling", "shrinkPct", "extraPts", "shrinkFactor"].forEach((id) => {
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
