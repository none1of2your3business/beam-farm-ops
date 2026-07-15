(function () {
  function host() {
    return document.getElementById("app-toast-host");
  }

  function showToast(message, level) {
    var box = host();
    if (!box || !message) return;
    var el = document.createElement("div");
    el.className = "app-toast is-" + (level || "ok");
    el.setAttribute("role", "status");
    var title =
      level === "error" ? "Not completed" : level === "warn" ? "Check this" : "Done";
    el.innerHTML =
      '<button type="button" class="app-toast-close" aria-label="Dismiss">×</button>' +
      '<p class="app-toast-title">' +
      title +
      "</p>" +
      '<p class="app-toast-msg"></p>';
    el.querySelector(".app-toast-msg").textContent = message;
    el.querySelector(".app-toast-close").addEventListener("click", function () {
      el.remove();
    });
    box.appendChild(el);
    window.setTimeout(function () {
      if (el.parentNode) el.remove();
    }, 7000);
  }

  window.showAppToast = showToast;

  if (window.__APP_FLASH__ && window.__APP_FLASH__.message) {
    showToast(window.__APP_FLASH__.message, window.__APP_FLASH__.level || "ok");
  }

  function selectedNames(picker) {
    var names = [];
    picker.querySelectorAll(".field-picker-item").forEach(function (item) {
      var cb = item.querySelector('input[type="checkbox"]');
      if (cb && cb.checked) {
        var label = item.querySelector(".field-picker-name");
        names.push(label ? label.textContent.trim() : cb.value);
      }
    });
    return names;
  }

  function updateSummary(picker) {
    var text = picker.querySelector(".field-picker-summary-text");
    if (!text) return;
    var names = selectedNames(picker);
    var empty = text.getAttribute("data-empty") || "No fields selected yet";
    if (!names.length) {
      text.textContent = empty;
      text.classList.add("muted");
      return;
    }
    text.classList.remove("muted");
    if (names.length <= 3) {
      text.textContent = names.length + " selected: " + names.join(", ");
    } else {
      text.textContent =
        names.length + " selected: " + names.slice(0, 3).join(", ") + " +" + (names.length - 3) + " more";
    }
  }

  function closeModal(picker) {
    var modal = picker.querySelector("[data-field-modal]");
    if (modal) modal.hidden = true;
    document.body.classList.remove("field-picker-open");
    updateSummary(picker);
  }

  function openModal(picker) {
    var modal = picker.querySelector("[data-field-modal]");
    if (modal) modal.hidden = false;
    document.body.classList.add("field-picker-open");
  }

  document.querySelectorAll("[data-field-picker]").forEach(function (picker) {
    var grid = picker.querySelector(".field-picker-grid");
    var openBtn = picker.querySelector(".field-picker-open");
    var allBtn = picker.querySelector(".field-picker-all");
    var noneBtn = picker.querySelector(".field-picker-none");
    var cropFilter = "all";

    function items() {
      return picker.querySelectorAll(".field-picker-item");
    }
    function boxes() {
      return picker.querySelectorAll('input[type="checkbox"].field-picker-check');
    }
    function visibleBoxes() {
      var out = [];
      items().forEach(function (item) {
        if (item.classList.contains("is-filtered-out")) return;
        var cb = item.querySelector('input[type="checkbox"].field-picker-check');
        if (cb) out.push(cb);
      });
      return out;
    }

    function applyCropFilter(filter) {
      cropFilter = filter || "all";
      var visible = 0;
      items().forEach(function (item) {
        var crop = item.getAttribute("data-crop") || "";
        var show = cropFilter === "all" || crop === cropFilter;
        item.classList.toggle("is-filtered-out", !show);
        if (show) visible += 1;
      });
      picker.querySelectorAll(".field-picker-filter").forEach(function (btn) {
        btn.classList.toggle("is-active", btn.getAttribute("data-crop-filter") === cropFilter);
      });
      var empty = picker.querySelector(".field-picker-empty-filter");
      if (empty) empty.hidden = visible > 0 || !items().length;
    }

    if (openBtn) {
      openBtn.addEventListener("click", function (e) {
        e.preventDefault();
        openModal(picker);
      });
    }

    picker.querySelectorAll("[data-field-modal-close]").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        closeModal(picker);
      });
    });

    picker.querySelectorAll(".field-picker-filter").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        applyCropFilter(btn.getAttribute("data-crop-filter") || "all");
      });
    });

    if (allBtn) {
      allBtn.addEventListener("click", function (e) {
        e.preventDefault();
        visibleBoxes().forEach(function (cb) {
          cb.checked = true;
        });
        updateSummary(picker);
      });
    }
    if (noneBtn) {
      noneBtn.addEventListener("click", function (e) {
        e.preventDefault();
        boxes().forEach(function (cb) {
          cb.checked = false;
        });
        updateSummary(picker);
      });
    }

    if (grid) {
      grid.addEventListener("change", function () {
        updateSummary(picker);
      });
    }

    applyCropFilter("all");
    updateSummary(picker);

    var form = picker.closest("form");
    if (form) {
      form.addEventListener("submit", function (e) {
        var checks = boxes();
        if (!checks.length) return;
        var any = false;
        checks.forEach(function (cb) {
          if (cb.checked) any = true;
        });
        if (!any) {
          e.preventDefault();
          showToast("Choose at least one field before assigning.", "error");
          openModal(picker);
        }
      });
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    document.querySelectorAll("[data-field-picker]").forEach(function (picker) {
      var modal = picker.querySelector("[data-field-modal]");
      if (modal && !modal.hidden) closeModal(picker);
    });
  });
})();
