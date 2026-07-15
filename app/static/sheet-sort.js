/**
 * Click column headers to sort sheet tables (client-side only).
 * Sorts tbody data rows only — never moves thead or tfoot totals.
 * Use: table.sheet-table[data-sortable]
 */
(function () {
  function cellValue(td) {
    if (!td) return { kind: "empty", raw: "", num: null };
    var sel = td.querySelector("select");
    if (sel) {
      var opt = sel.options[sel.selectedIndex];
      var t = ((opt && opt.text) || sel.value || "").trim();
      return { kind: "text", raw: t.toLowerCase(), num: null };
    }
    var input = td.querySelector("input:not([type=hidden])");
    if (input) {
      var v = String(input.value || "").trim();
      var n = parseFloat(v.replace(/,/g, ""));
      if (v !== "" && isFinite(n) && /^-?[\d.,]+$/.test(v.replace(/\s/g, ""))) {
        return { kind: "num", raw: v.toLowerCase(), num: n };
      }
      return { kind: "text", raw: v.toLowerCase(), num: null };
    }
    var text = (td.textContent || "").replace(/\s+/g, " ").trim();
    if (!text || text === "—") return { kind: "empty", raw: "", num: null };
    var cleaned = text.replace(/[$,%\s]/g, "").replace(/,/g, "");
    var pn = parseFloat(cleaned);
    if (cleaned !== "" && isFinite(pn) && /^-?[\d.]+$/.test(cleaned)) {
      return { kind: "num", raw: text.toLowerCase(), num: pn };
    }
    return { kind: "text", raw: text.toLowerCase(), num: null };
  }

  function compare(a, b, dir) {
    var av = cellValue(a);
    var bv = cellValue(b);
    if (av.kind === "empty" && bv.kind === "empty") return 0;
    if (av.kind === "empty") return 1;
    if (bv.kind === "empty") return -1;
    var mul = dir === "desc" ? -1 : 1;
    if (av.num !== null && bv.num !== null) {
      if (av.num < bv.num) return -1 * mul;
      if (av.num > bv.num) return 1 * mul;
      return 0;
    }
    if (av.raw < bv.raw) return -1 * mul;
    if (av.raw > bv.raw) return 1 * mul;
    return 0;
  }

  function clearMarks(ths) {
    ths.forEach(function (th) {
      th.removeAttribute("aria-sort");
      th.classList.remove("is-sorted-asc", "is-sorted-desc");
    });
  }

  function wireTable(table) {
    var thead = table.tHead;
    var tbody = table.tBodies[0];
    var tfoot = table.tFoot;
    if (!thead || !tbody) return;
    var wrap = table.closest(".sheet-wrap");
    var ths = Array.prototype.slice.call(thead.querySelectorAll("th"));
    ths.forEach(function (th, colIndex) {
      th.classList.add("sheet-sortable");
      th.tabIndex = 0;
      th.setAttribute("role", "columnheader");
      th.setAttribute("title", "Click to sort");
      if (!th.querySelector(".sheet-sort-ind")) {
        var ind = document.createElement("span");
        ind.className = "sheet-sort-ind";
        ind.setAttribute("aria-hidden", "true");
        th.appendChild(ind);
      }
      function sortBy() {
        var cur = th.getAttribute("aria-sort");
        var next = cur === "ascending" ? "descending" : "ascending";
        clearMarks(ths);
        th.setAttribute("aria-sort", next);
        th.classList.add(next === "ascending" ? "is-sorted-asc" : "is-sorted-desc");
        var scrollTop = wrap ? wrap.scrollTop : 0;
        var rows = Array.prototype.slice.call(tbody.querySelectorAll(":scope > tr.sheet-row"));
        rows.sort(function (ra, rb) {
          return compare(ra.children[colIndex], rb.children[colIndex], next === "ascending" ? "asc" : "desc");
        });
        var frag = document.createDocumentFragment();
        rows.forEach(function (row) { frag.appendChild(row); });
        tbody.appendChild(frag);
        // Pin footer after body — never let totals float into the data set
        if (tfoot) table.appendChild(tfoot);
        if (wrap) wrap.scrollTop = scrollTop;
      }
      th.addEventListener("click", function (e) {
        if (e.target.closest("a, input, select, button, label")) return;
        e.preventDefault();
        sortBy();
      });
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          sortBy();
        }
      });
    });
  }

  document.querySelectorAll("table.sheet-table[data-sortable]").forEach(wireTable);
})();
