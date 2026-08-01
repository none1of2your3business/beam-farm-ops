/**
 * Contract desk table: sortable headers, corn and soy blocks stay separate.
 * Moves pricing-event detail rows with their parent contract row.
 */
(function () {
  function cropRank(crop) {
    if (crop === "Corn") return 0;
    if (crop === "Soybeans") return 1;
    return 2;
  }

  function cellValue(td) {
    if (!td) return { kind: "empty", raw: "", num: null };
    var explicit = td.getAttribute("data-sort-value");
    if (explicit !== null && explicit !== "") {
      var en = parseFloat(explicit);
      if (isFinite(en) && /^-?[\d.]+$/.test(String(explicit).trim())) {
        return { kind: "num", raw: explicit, num: en };
      }
      return { kind: "text", raw: String(explicit).toLowerCase(), num: null };
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

  function compareCells(a, b, dir) {
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

  function movePair(tbody, row, eventsRow) {
    tbody.appendChild(row);
    if (eventsRow) tbody.appendChild(eventsRow);
  }

  function wireTable(table) {
    var thead = table.tHead;
    var tbody = table.tBodies[0];
    if (!thead || !tbody) return;

    var ths = Array.prototype.slice.call(thead.querySelectorAll("th"));
    ths.forEach(function (th, colIndex) {
      if (th.hasAttribute("data-no-sort")) return;
      th.classList.add("sheet-sortable");
      th.tabIndex = 0;
      th.setAttribute("role", "columnheader");
      th.setAttribute("title", "Click to sort (within corn / soy groups)");
      if (!th.querySelector(".sheet-sort-ind")) {
        var ind = document.createElement("span");
        ind.className = "sheet-sort-ind";
        ind.setAttribute("aria-hidden", "true");
        th.appendChild(ind);
      }

      function sortBy(forceDir) {
        var cur = th.getAttribute("aria-sort");
        var next =
          forceDir ||
          (cur === "ascending" ? "descending" : cur === "descending" ? "ascending" : "ascending");
        clearMarks(ths);
        th.setAttribute("aria-sort", next);
        th.classList.add(next === "ascending" ? "is-sorted-asc" : "is-sorted-desc");
        var dir = next === "ascending" ? "asc" : "desc";

        var pairs = [];
        Array.prototype.slice.call(tbody.querySelectorAll(":scope > tr.mkt-desk-row")).forEach(function (row) {
          var id = row.dataset.id;
          var ev = id ? document.getElementById("ev-" + id) : null;
          pairs.push({ row: row, ev: ev, crop: cropRank(row.dataset.crop || "") });
        });

        pairs.sort(function (pa, pb) {
          if (pa.crop !== pb.crop) return pa.crop - pb.crop;
          var cmp = compareCells(
            pa.row.children[colIndex],
            pb.row.children[colIndex],
            dir
          );
          if (cmp !== 0) return cmp;
          var ida = parseInt(pa.row.dataset.id || "0", 10);
          var idb = parseInt(pb.row.dataset.id || "0", 10);
          return ida - idb;
        });

        pairs.forEach(function (p) {
          movePair(tbody, p.row, p.ev);
        });
      }

      th.addEventListener("click", function (e) {
        if (e.target.closest("a, input, select, button, label")) return;
        e.preventDefault();
        sortBy(null);
      });
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          sortBy(null);
        }
      });

      if (th.getAttribute("data-default-sort")) {
        sortBy(th.getAttribute("data-default-sort") === "desc" ? "descending" : "ascending");
      }
    });
  }

  document.querySelectorAll("table[data-desk-sortable]").forEach(wireTable);
})();
