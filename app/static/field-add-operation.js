(function () {
  const form = document.getElementById("ops-wizard");
  if (!form) return;

  const kindInput = document.getElementById("op_kind");
  const step1 = document.getElementById("ops-step-1");
  const step2 = document.getElementById("ops-step-2");
  const step2Title = document.getElementById("ops-step2-title");
  const step2Hint = document.getElementById("ops-step2-hint");
  const backBtn = document.getElementById("ops-wizard-back");
  const tiles = form.querySelectorAll(".ops-type-tile");
  const panels = form.querySelectorAll(".ops-kind-panel");
  const dateInput = document.getElementById("op_date");
  const customInput = document.getElementById("custom_type");
  const acresInput = document.getElementById("acres");
  const costInput = document.getElementById("cost");
  const plantRows = document.getElementById("plant-rows");
  const plantAddBtn = document.getElementById("plant-add-row");
  const plantTemplate = document.getElementById("plant-row-template");

  const LABELS = {
    planting: "Planting",
    spraying: "Spraying",
    dry_fertilizer: "Dry Fertilizer",
    sidedress: "Sidedress",
    harvest: "Harvest",
    lime: "Lime",
    custom: "Add New",
  };

  const HINTS = {
    planting: "Add one or more hybrids — each with its own rate and units applied.",
    spraying: "Pick the mix, then optionally log weather for spray-record audits later.",
    dry_fertilizer: "Assign from inventory when you can so on-hand and field cost stay aligned.",
    sidedress: "Log product + quantity from inventory, or name it free-text if bought outside the pool.",
    harvest: "Yield and moisture for this pass — use Marketing and Storage to put bushels into a bin.",
    lime: "Track lime like fertilizer so pH work shows up in field economics.",
    custom: "Name the pass, set the date, add cost if it mattered.",
  };

  function money(n) {
    if (n == null || !isFinite(n)) return "—";
    return "$" + Number(n).toLocaleString("en-US", { maximumFractionDigits: 0 });
  }

  function num(el) {
    if (!el) return 0;
    const v = parseFloat(String(el.value || "").replace(",", ""));
    return isFinite(v) ? v : 0;
  }

  function selectedOption(sel) {
    if (!sel || sel.selectedIndex < 0) return null;
    return sel.options[sel.selectedIndex];
  }

  function productDollars(selId, qtyId) {
    const sel = document.getElementById(selId);
    const qty = num(document.getElementById(qtyId));
    const opt = selectedOption(sel);
    if (!opt || !opt.value || qty <= 0) return 0;
    const uc = parseFloat(opt.getAttribute("data-unit-cost") || "0");
    return isFinite(uc) ? uc * qty : 0;
  }

  function plantingDollars(acres) {
    if (!plantRows) return 0;
    let total = 0;
    plantRows.querySelectorAll("[data-plant-row]").forEach((row) => {
      const sel = row.querySelector(".plant-hybrid");
      const opt = selectedOption(sel);
      if (!opt || !opt.value) return;
      const units = num(row.querySelector(".plant-units"));
      const cpu = parseFloat(opt.getAttribute("data-cpu") || "");
      const cpa = parseFloat(opt.getAttribute("data-cpa") || "");
      if (units > 0 && isFinite(cpu)) {
        total += units * cpu;
      } else if (isFinite(cpa) && acres > 0) {
        total += cpa * acres;
      }
    });
    return total;
  }

  function syncPlantRemoveButtons() {
    if (!plantRows) return;
    const rows = plantRows.querySelectorAll("[data-plant-row]");
    rows.forEach((row) => {
      const btn = row.querySelector(".plant-row-remove");
      if (btn) btn.disabled = rows.length <= 1;
    });
  }

  function addPlantRow() {
    if (!plantRows || !plantTemplate) return;
    const node = plantTemplate.content.cloneNode(true);
    plantRows.appendChild(node);
    syncPlantRemoveButtons();
    updateLive();
  }

  if (plantAddBtn) {
    plantAddBtn.addEventListener("click", (e) => {
      e.preventDefault();
      addPlantRow();
    });
  }

  if (plantRows) {
    plantRows.addEventListener("click", (e) => {
      const btn = e.target.closest(".plant-row-remove");
      if (!btn) return;
      e.preventDefault();
      const rows = plantRows.querySelectorAll("[data-plant-row]");
      if (rows.length <= 1) return;
      const row = btn.closest("[data-plant-row]");
      if (row) row.remove();
      syncPlantRemoveButtons();
      updateLive();
    });
  }

  function updateLive() {
    const kind = (kindInput.value || "").trim();
    const acres = num(acresInput);
    let product = 0;

    if (kind === "planting") {
      product = plantingDollars(acres);
    } else if (kind === "spraying") {
      const opt = selectedOption(document.getElementById("spray_mix_id"));
      const cpa = opt ? parseFloat(opt.getAttribute("data-cpa") || "") : NaN;
      if (isFinite(cpa) && acres > 0) product = cpa * acres;
    } else if (kind === "dry_fertilizer") {
      product = productDollars("product_id", "quantity");
    } else if (kind === "sidedress") {
      product = productDollars("product_id_sd", "quantity_sd");
    } else if (kind === "lime") {
      product = productDollars("product_id_lime", "quantity_lime");
    }

    const hire = num(costInput);
    const cpa = acres > 0 && product > 0 ? product / acres : null;

    const aEl = document.getElementById("ops-live-acres");
    const pEl = document.getElementById("ops-live-product");
    const cEl = document.getElementById("ops-live-cpa");
    const hEl = document.getElementById("ops-live-hire");
    if (aEl) aEl.textContent = acres ? acres.toLocaleString("en-US", { maximumFractionDigits: 2 }) + " ac" : "—";
    if (pEl) pEl.textContent = product ? money(product) : "—";
    if (cEl) cEl.textContent = cpa != null ? ("$" + cpa.toFixed(2)) : "—";
    if (hEl) hEl.textContent = hire ? money(hire) : "—";
  }

  function showStep2(kind) {
    kindInput.value = kind;
    tiles.forEach((t) => {
      const on = t.getAttribute("data-op-kind") === kind;
      t.classList.toggle("is-selected", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    panels.forEach((p) => {
      p.hidden = p.getAttribute("data-kind") !== kind;
    });
    if (step2Title) {
      step2Title.textContent = (LABELS[kind] || "Details") + " details";
    }
    if (step2Hint) {
      step2Hint.textContent = HINTS[kind] || "";
    }
    if (step1) step1.hidden = true;
    if (step2) step2.hidden = false;
    if (dateInput) dateInput.focus();
    syncPlantRemoveButtons();
    updateLive();
  }

  function showStep1() {
    if (step2) step2.hidden = true;
    if (step1) step1.hidden = false;
  }

  tiles.forEach((tile) => {
    tile.addEventListener("click", () => {
      const kind = tile.getAttribute("data-op-kind");
      if (kind) showStep2(kind);
    });
  });

  if (backBtn) {
    backBtn.addEventListener("click", (e) => {
      e.preventDefault();
      showStep1();
    });
  }

  form.addEventListener("input", updateLive);
  form.addEventListener("change", updateLive);

  form.addEventListener("submit", (e) => {
    const kind = (kindInput.value || "").trim();
    if (!kind) {
      e.preventDefault();
      showStep1();
      alert("Choose an operation type first.");
      return;
    }
    if (kind === "custom") {
      const ct = ((customInput && customInput.value) || "").trim();
      if (!ct) {
        e.preventDefault();
        if (customInput) customInput.focus();
        alert("Enter a custom type name.");
        return;
      }
    }
    const d = ((dateInput && dateInput.value) || "").trim();
    if (!d) {
      e.preventDefault();
      if (dateInput) dateInput.focus();
      alert("Date is required.");
    }
  });

  syncPlantRemoveButtons();

  // Work-order / deep-link preselect: ?kind=spraying (also from template preselect_kind)
  const pre = (form.getAttribute("data-preselect-kind") || "").trim().toLowerCase();
  const KIND_ALIASES = {
    spray: "spraying",
    spraying: "spraying",
    planting: "planting",
    plant: "planting",
    fertilizer: "dry_fertilizer",
    dry_fertilizer: "dry_fertilizer",
    fert: "dry_fertilizer",
    sidedress: "sidedress",
    lime: "lime",
    harvest: "harvest",
    custom: "custom",
  };
  const mapped = KIND_ALIASES[pre];
  if (mapped && LABELS[mapped]) {
    showStep2(mapped);
  }
})();
