/**
 * App-wide form autosave + leave guard.
 *
 * Autosaves POST forms whose action looks like a save endpoint, or that have
 * data-autosave. Forms with data-autosave-own keep their own script.
 * Other dirty POST forms get a beforeunload warning instead.
 */
(function () {
  "use strict";

  var AUTO_MS = 1000;
  var statusEl = null;
  var forms = new Map(); // form -> state

  function ensureStatus() {
    if (statusEl && document.body.contains(statusEl)) return statusEl;
    statusEl = document.getElementById("app-autosave-status");
    if (!statusEl) {
      statusEl = document.createElement("div");
      statusEl.id = "app-autosave-status";
      statusEl.className = "app-autosave-status";
      statusEl.setAttribute("aria-live", "polite");
      statusEl.hidden = true;
      document.body.appendChild(statusEl);
    }
    return statusEl;
  }

  function setStatus(text, cls) {
    var el = ensureStatus();
    if (!text) {
      el.hidden = true;
      el.textContent = "";
      el.className = "app-autosave-status";
      return;
    }
    el.hidden = false;
    el.textContent = text;
    el.className = "app-autosave-status" + (cls ? " " + cls : "");
  }

  function formActionUrl(form) {
    // Never use form.action — if the form has controls named "action",
    // browsers return that RadioNodeList instead of the URL.
    var raw = form.getAttribute("action");
    if (raw == null || raw === "") return window.location.pathname;
    return raw;
  }

  function actionPath(form) {
    var raw = formActionUrl(form);
    try {
      return new URL(raw, window.location.origin).pathname.toLowerCase();
    } catch (e) {
      return String(raw).split("?")[0].toLowerCase();
    }
  }

  function hasFileInput(form) {
    return !!form.querySelector('input[type="file"]');
  }

  function isSystemForm(form) {
    if (!form || (form.method || "get").toLowerCase() !== "post") return true;
    if (form.hasAttribute("data-no-guard")) return true;
    if (form.classList.contains("year-switcher")) return true;
    if (form.closest(".topbar-save")) return true;
    if (form.enctype === "multipart/form-data" && hasFileInput(form)) return true;

    var path = actionPath(form);
    var blocked = [
      "/login",
      "/logout",
      "/backup/",
      "/years/activate",
      "/password",
      "/delete",
      "/discard",
      "/restore",
      "/merge",
      "/upload",
      "/import",
      "/commit",
    ];
    for (var i = 0; i < blocked.length; i++) {
      if (path.indexOf(blocked[i]) !== -1) return true;
    }
    return false;
  }

  function canAutosave(form) {
    if (isSystemForm(form)) return false;
    if (form.hasAttribute("data-autosave-own")) return false;
    if (form.hasAttribute("data-no-autosave")) return false;
    if (form.getAttribute("data-autosave") === "0") return false;
    if (form.hasAttribute("data-autosave")) return true;
    var path = actionPath(form);
    if (path.indexOf("/buy") !== -1) return false;
    if (path.indexOf("/save") !== -1) return true;
    if (/(^|\/)save$/.test(path)) return true;
    if (form.classList.contains("sheet-form")) return true;
    if (path.indexOf("/add") !== -1 || path.indexOf("/new") !== -1) return false;
    // Edit panels with an explicit Save / Update button
    var btns = form.querySelectorAll('button[type="submit"], input[type="submit"]');
    for (var i = 0; i < btns.length; i++) {
      var label = (btns[i].textContent || btns[i].value || "").trim().toLowerCase();
      if (label.indexOf("add ") === 0 || label.indexOf("create") === 0 || label.indexOf("delete") !== -1) continue;
      if (label === "save" || label === "save all" || label === "update" || label.indexOf("save ") === 0) {
        return true;
      }
    }
    return false;
  }

  function trackable(form) {
    // Leave-guard (and maybe autosave) for normal POST entry forms
    if (isSystemForm(form)) return false;
    if (form.hasAttribute("data-autosave-own")) return false;
    return true;
  }

  function getState(form) {
    var st = forms.get(form);
    if (!st) {
      st = {
        dirty: false,
        saving: false,
        timer: null,
        autosave: canAutosave(form),
        baseline: "",
      };
      forms.set(form, st);
    }
    return st;
  }

  function snapshot(form) {
    try {
      var fd = new FormData(form);
      // Normalize multi-action forms to save-only for comparison/save
      if (!fd.has("action") && form.querySelector('[name="action"]')) {
        // ok
      }
      var parts = [];
      fd.forEach(function (v, k) {
        if (typeof v === "string") parts.push(k + "=" + v);
        else parts.push(k + "=[file]");
      });
      return parts.join("&");
    } catch (e) {
      return String(Date.now());
    }
  }

  function prepareFormData(form) {
    var fd = new FormData(form);
    var force = form.getAttribute("data-autosave-action");
    if (force) {
      fd.set("action", force);
    } else if (!fd.has("action")) {
      // Prefer safe save when multi-submit forms omit action on fetch
      var saveBtn = form.querySelector('button[name="action"][value="save"], input[name="action"][value="save"]');
      if (saveBtn) fd.set("action", "save");
    }
    return fd;
  }

  function markDirty(form) {
    var st = getState(form);
    st.dirty = true;
    if (st.autosave) {
      setStatus("Unsaved changes…", "is-dirty");
      schedule(form);
    } else {
      setStatus("Unsaved — save before leaving", "is-dirty");
    }
  }

  function schedule(form) {
    var st = getState(form);
    if (st.timer) clearTimeout(st.timer);
    st.timer = setTimeout(function () {
      st.timer = null;
      autosave(form);
    }, AUTO_MS);
  }

  function saveUrl(form) {
    var action = formActionUrl(form);
    var join = action.indexOf("?") >= 0 ? "&" : "?";
    return action + join + "format=json";
  }

  function autosave(form, opts) {
    opts = opts || {};
    var st = getState(form);
    if (!st.autosave) return Promise.resolve(false);
    if (!st.dirty && !opts.force) return Promise.resolve(true);
    if (st.saving && !opts.beacon) {
      schedule(form);
      return Promise.resolve(false);
    }

    var fd = prepareFormData(form);

    if (opts.beacon && typeof navigator.sendBeacon === "function") {
      try {
        if (navigator.sendBeacon(saveUrl(form), fd)) {
          st.dirty = false;
          return Promise.resolve(true);
        }
      } catch (e) {}
      return Promise.resolve(false);
    }

    st.dirty = false;
    st.saving = true;
    setStatus("Saving…", "is-saving");

    return fetch(formActionUrl(form), {
      method: "POST",
      body: fd,
      headers: {
        "X-Requested-With": "fetch",
        Accept: "application/json",
      },
      credentials: "same-origin",
    })
      .then(function (res) {
        var ctype = (res.headers.get("content-type") || "").toLowerCase();
        if (ctype.indexOf("application/json") !== -1) {
          return res.json().then(function (data) {
            if (!res.ok || !data || data.ok === false) {
              var err = new Error((data && (data.error || data.message)) || "Save failed");
              err.data = data;
              throw err;
            }
            return data;
          });
        }
        if (res.ok) return { ok: true };
        throw new Error("Save failed");
      })
      .then(function (data) {
        st.saving = false;
        st.baseline = snapshot(form);
        if (st.dirty) {
          setStatus("Unsaved changes…", "is-dirty");
          schedule(form);
          return false;
        }
        var msg = (data && data.message) || "All changes saved";
        if (data && data.saved != null) {
          msg = "Saved · " + data.saved + " updated";
        }
        setStatus(msg, "is-saved");
        return true;
      })
      .catch(function (err) {
        st.saving = false;
        st.dirty = true;
        setStatus((err && err.message) || "Save failed — use Save before leaving", "is-error");
        return false;
      });
  }

  function anyDirty() {
    var dirty = false;
    forms.forEach(function (st) {
      if (st.dirty) dirty = true;
    });
    return dirty;
  }

  function bindForm(form) {
    if (!trackable(form) || form.__autosaveBound) return;
    form.__autosaveBound = true;
    var st = getState(form);
    st.baseline = snapshot(form);

    function onEdit(e) {
      var t = e.target;
      if (!t) return;
      // Ignore pure UI controls that shouldn't dirty (e.g. field picker for apply-only)
      if (t.hasAttribute && t.hasAttribute("data-no-dirty")) return;
      markDirty(form);
    }

    form.addEventListener("input", onEdit);
    form.addEventListener("change", onEdit);

    form.addEventListener("submit", function () {
      if (st.timer) clearTimeout(st.timer);
      st.dirty = false;
      st.saving = false;
      setStatus("", "");
    });
  }

  function scan() {
    document.querySelectorAll("form").forEach(bindForm);
  }

  document.addEventListener("DOMContentLoaded", scan);
  if (document.readyState !== "loading") scan();

  window.addEventListener("pagehide", function () {
    forms.forEach(function (st, form) {
      if (st.dirty && st.autosave) autosave(form, { force: true, beacon: true });
    });
  });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "hidden") return;
    forms.forEach(function (st, form) {
      if (st.dirty && st.autosave) autosave(form, { force: true, beacon: true });
    });
  });

  window.addEventListener("beforeunload", function (e) {
    if (!anyDirty()) return;
    // Last-chance beacon for autosave forms
    forms.forEach(function (st, form) {
      if (st.dirty && st.autosave) autosave(form, { force: true, beacon: true });
    });
    if (!anyDirty()) return;
    e.preventDefault();
    e.returnValue = "";
    return "";
  });

  // Intercept in-app link clicks: flush autosave, or confirm if still dirty
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var href = a.getAttribute("href") || "";
    if (!href || href.charAt(0) === "#" || href.indexOf("javascript:") === 0) return;
    if (a.target === "_blank" || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (!anyDirty()) return;

    e.preventDefault();
    e.stopPropagation();

    var jobs = [];
    forms.forEach(function (st, form) {
      if (st.dirty && st.autosave) {
        if (st.timer) clearTimeout(st.timer);
        jobs.push(autosave(form, { force: true }));
      }
    });

    Promise.all(jobs).then(function () {
      if (!anyDirty()) {
        window.location.href = a.href;
        return;
      }
      var ok = window.confirm("You have unsaved changes. Leave this page anyway?");
      if (ok) {
        forms.forEach(function (st) { st.dirty = false; });
        window.location.href = a.href;
      }
    });
  }, true);

  window.BeamAutosave = {
    scan: scan,
    markDirty: function (form) {
      if (form) markDirty(form);
    },
    setStatus: setStatus,
  };
})();
