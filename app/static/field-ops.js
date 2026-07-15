(function () {
  var filters = document.querySelectorAll(".ops-type-filter");
  var rows = document.querySelectorAll(".ops-ledger-row");
  var empty = document.getElementById("ops-filter-empty");
  if (!filters.length || !rows.length) return;

  function apply(filter) {
    var visible = 0;
    rows.forEach(function (row) {
      var type = row.getAttribute("data-ops-type") || "";
      var show = filter === "all" || type === filter;
      row.hidden = !show;
      if (show) visible += 1;
    });
    filters.forEach(function (btn) {
      btn.classList.toggle("is-active", btn.getAttribute("data-ops-filter") === filter);
    });
    if (empty) empty.hidden = visible > 0;
  }

  filters.forEach(function (btn) {
    btn.addEventListener("click", function () {
      apply(btn.getAttribute("data-ops-filter") || "all");
    });
  });
})();
