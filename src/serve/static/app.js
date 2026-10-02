(function () {
  "use strict";

  var applib = window.gitcrawlApp;
  var root = document.documentElement;
  var busyTimer = null;

  function markBusy() {
    root.setAttribute("data-busy", "true");
    window.clearTimeout(busyTimer);
    busyTimer = window.setTimeout(clearBusy, 8000);
  }

  function clearBusy() {
    root.setAttribute("data-busy", "false");
    window.clearTimeout(busyTimer);
    busyTimer = null;
  }

  function preferredTheme() {
    var current = root.getAttribute("data-theme");
    if (current === "dark" || current === "light") {
      return current;
    }
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    try {
      window.localStorage.setItem("gc-theme", theme);
    } catch (error) {
      window.console.debug("theme persistence unavailable", error);
    }
  }

  function syncToggle(toggle) {
    if (toggle) {
      toggle.setAttribute("aria-pressed", preferredTheme() === "dark" ? "true" : "false");
    }
  }

  var toastTimer = null;

  function hideToast() {
    var toast = document.getElementById("toast");
    if (toast) {
      toast.hidden = true;
    }
    window.clearTimeout(toastTimer);
    toastTimer = null;
  }

  function showToast(message, retry) {
    var toast = document.getElementById("toast");
    if (!toast) {
      return;
    }
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(toastTimer);
    if (retry) {
      var retryButton = document.createElement("button");
      retryButton.type = "button";
      retryButton.className = "button";
      retryButton.setAttribute("data-toast-retry", "true");
      retryButton.textContent = "Retry";
      retryButton.addEventListener("click", function () {
        hideToast();
        retry();
      });
      var dismissButton = document.createElement("button");
      dismissButton.type = "button";
      dismissButton.className = "button ghost";
      dismissButton.setAttribute("data-toast-dismiss", "true");
      dismissButton.setAttribute("aria-label", "Dismiss message");
      dismissButton.textContent = "Dismiss";
      dismissButton.addEventListener("click", hideToast);
      toast.appendChild(retryButton);
      toast.appendChild(dismissButton);
      toastTimer = window.setTimeout(hideToast, 12000);
    } else {
      toastTimer = window.setTimeout(hideToast, 6000);
    }
  }

  function openModal(id) {
    var modal = document.getElementById(id);
    if (!modal) {
      return;
    }
    modal.hidden = false;
    var focusable = modal.querySelector("input, button");
    if (focusable) {
      focusable.focus();
    }
  }

  function closeModal(id) {
    var modal = document.getElementById(id);
    if (modal) {
      modal.hidden = true;
    }
  }

  function toggleShortcutsModal() {
    var modal = document.getElementById("shortcuts-modal");
    if (!modal) {
      return;
    }
    modal.hidden = !modal.hidden;
  }

  function closeOpenModal() {
    var open = document.querySelector(".modal:not([hidden])");
    if (open) {
      open.hidden = true;
      return true;
    }
    return false;
  }

  var rowCache = applib.createRowCache();

  function invalidateRowCache() {
    rowCache.invalidate();
  }

  function selectableRows() {
    return rowCache.get(function () {
      return Array.prototype.slice.call(document.querySelectorAll("tbody tr")).filter(function (row) {
        return !row.hidden && row.querySelector("a[href]");
      });
    });
  }

  function selectRow(delta) {
    var rows = selectableRows();
    if (!rows.length) {
      return;
    }
    var current = -1;
    for (var index = 0; index < rows.length; index += 1) {
      if (rows[index].classList.contains("row-selected")) {
        current = index;
        break;
      }
    }
    var next = window.gitcrawlRowNav.nextIndex(current, delta, rows.length);
    var previous = document.querySelector("tr.row-selected");
    if (previous) {
      previous.classList.remove("row-selected");
    }
    rows[next].classList.add("row-selected");
    if (rows[next].scrollIntoView) {
      rows[next].scrollIntoView({ block: "nearest" });
    }
  }

  function openSelectedRow() {
    var row = document.querySelector("tr.row-selected");
    if (!row) {
      return false;
    }
    var link = row.querySelector("a[href]");
    if (!link) {
      return false;
    }
    window.location.assign(link.href);
    return true;
  }

  var pendingG = false;
  var pendingTimer = null;

  function clearPendingG() {
    pendingG = false;
    if (pendingTimer) {
      window.clearTimeout(pendingTimer);
      pendingTimer = null;
    }
  }

  function handleKeyboard(event) {
    if (event.defaultPrevented) {
      return;
    }
    if (event.key === "Escape") {
      clearPendingG();
      closeOpenModal();
      return;
    }
    if (applib.isEditableTarget(event.target)) {
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) {
      return;
    }
    if (event.key === "?") {
      event.preventDefault();
      toggleShortcutsModal();
      return;
    }
    if (pendingG) {
      clearPendingG();
      if (event.key === "h") {
        event.preventDefault();
        window.location.assign("/");
      } else if (event.key === "f") {
        event.preventDefault();
        window.location.assign("/find");
      } else if (event.key === "r") {
        event.preventDefault();
        window.location.assign("/runs");
      } else if (event.key === "l") {
        event.preventDefault();
        window.location.assign("/filters");
      }
      return;
    }
    if (event.key === "g") {
      pendingG = true;
      pendingTimer = window.setTimeout(clearPendingG, 1200);
      return;
    }
    if (event.key === "/") {
      var search = document.getElementById("quick-find-q");
      if (search) {
        event.preventDefault();
        search.focus();
      }
      return;
    }
    if (event.key === "j") {
      event.preventDefault();
      selectRow(1);
      return;
    }
    if (event.key === "k") {
      event.preventDefault();
      selectRow(-1);
      return;
    }
    if (event.key === "Enter" && openSelectedRow()) {
      event.preventDefault();
    }
  }

  function syncCloneLimit() {
    var slider = document.getElementById("clone-limit");
    var number = document.getElementById("clone-limit-input");
    if (!slider || !number) {
      return;
    }
    slider.addEventListener("input", function () {
      number.value = slider.value;
    });
    number.addEventListener("input", function () {
      var value = window.parseInt(number.value, 10);
      if (!window.isNaN(value)) {
        slider.value = String(value);
      }
    });
  }

  var RECENT_FILTERS_KEY = "gc-recent-filters";

  function storedRecentFilters() {
    try {
      return window.gitcrawlRecentFilters.parse(
        window.localStorage.getItem(RECENT_FILTERS_KEY)
      );
    } catch (error) {
      return [];
    }
  }

  function saveRecentFilters(entries) {
    try {
      window.localStorage.setItem(RECENT_FILTERS_KEY, JSON.stringify(entries));
    } catch (error) {
      window.console.debug("recent filters persistence unavailable", error);
    }
  }

  function formFieldPairs(form) {
    var pairs = [];
    new window.FormData(form).forEach(function (value, name) {
      if (typeof value !== "string" || !value) {
        return;
      }
      if (name === "csrf" || name === "action" || name === "spec_file") {
        return;
      }
      pairs.push({ name: name, value: value });
    });
    return pairs;
  }

  function recordRecentFilter(form) {
    if (!window.gitcrawlRecentFilters) {
      return;
    }
    var fields = formFieldPairs(form);
    if (!fields.length) {
      return;
    }
    saveRecentFilters(
      window.gitcrawlRecentFilters.push(storedRecentFilters(), {
        label: window.gitcrawlRecentFilters.label(fields),
        fields: fields
      })
    );
  }

  function renderRecentFilters() {
    var container = document.getElementById("recent-filters");
    var list = document.getElementById("recent-filters-list");
    if (!container || !list || !window.gitcrawlRecentFilters) {
      return;
    }
    var entries = storedRecentFilters();
    if (!entries.length) {
      container.hidden = true;
      return;
    }
    list.textContent = "";
    entries.forEach(function (entry) {
      var item = document.createElement("li");
      var link = document.createElement("a");
      link.className = "button ghost";
      link.setAttribute("data-recent-filter", "true");
      link.href = "/find?" + window.gitcrawlRecentFilters.toQuery(entry.fields);
      link.textContent = entry.label;
      item.appendChild(link);
      list.appendChild(item);
    });
    container.hidden = false;
  }

  function copyText(text, okMessage) {
    if (window.navigator.clipboard && window.navigator.clipboard.writeText) {
      window.navigator.clipboard.writeText(text).then(
        function () {
          showToast(okMessage);
        },
        function () {
          showToast("Copy failed");
        }
      );
    } else {
      showToast("Copy failed");
    }
  }

  function copyButtons() {
    var hashButton = document.getElementById("copy-hash");
    if (hashButton) {
      hashButton.addEventListener("click", function () {
        copyText(hashButton.getAttribute("data-copy") || "", "Hash copied");
      });
    }
    var linkButton = document.querySelector("[data-copy-url]");
    if (linkButton) {
      linkButton.addEventListener("click", function () {
        copyText(window.location.href, "Link copied");
      });
    }
  }

  function selectedMode() {
    var checked = document.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "shallow";
  }

  var startGuard = applib.createStartGuard();

  function startClone(button) {
    if (button.disabled || !startGuard.begin()) {
      return;
    }
    button.disabled = true;
    var modal = document.getElementById("clone-modal");
    var runId =
      button.getAttribute("data-run-id") || (modal ? modal.getAttribute("data-run-id") : "");
    var number = document.getElementById("clone-limit-input");
    var slider = document.getElementById("clone-limit");
    var raw = number ? number.value : slider ? slider.value : "0";
    var limit = applib.parseCloneLimit(raw);
    var meta = document.querySelector('meta[name="csrf-token"]');
    var headers = { "content-type": "application/json" };
    if (meta) {
      headers["x-csrf-token"] = meta.getAttribute("content") || "";
    }
    window
      .fetch("/runs/" + runId + "/clone", {
        method: "POST",
        headers: headers,
        body: JSON.stringify({ limit: limit, mode: selectedMode() })
      })
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return null;
          })
          .then(function (body) {
            if (!response.ok) {
              var failure = new Error("clone request failed");
              failure.status = response.status;
              failure.body = body;
              throw failure;
            }
            showToast("Clone started");
            if (window.htmx && window.htmx.ajax) {
              window.htmx.ajax("GET", "/partials/runs/" + runId + "/clone-progress", {
                target: "#clone-progress",
                swap: "outerHTML"
              });
            }
          });
      })
      .catch(function (error) {
        var status = error && typeof error.status === "number" ? error.status : 0;
        var body = error && error.body ? error.body : null;
        showToast(
          window.gitcrawlErrors.apiErrorText(status, body, "Clone request failed"),
          function () {
            startClone(button);
          }
        );
      })
      .finally(function () {
        button.disabled = false;
        startGuard.end();
      });
  }

  function refreshCloneProgress(runId) {
    if (window.htmx && window.htmx.ajax) {
      window.htmx.ajax("GET", "/partials/runs/" + runId + "/clone-progress", {
        target: "#clone-progress",
        swap: "outerHTML"
      });
    }
  }

  function cancelClone(button) {
    if (button.disabled) {
      return;
    }
    button.disabled = true;
    var runId = button.getAttribute("data-run-id") || "";
    var meta = document.querySelector('meta[name="csrf-token"]');
    var headers = {};
    if (meta) {
      headers["x-csrf-token"] = meta.getAttribute("content") || "";
    }
    window
      .fetch("/runs/" + runId + "/clone", { method: "DELETE", headers: headers })
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return null;
          })
          .then(function (body) {
            if (!response.ok) {
              var failure = new Error("cancel request failed");
              failure.status = response.status;
              failure.body = body;
              throw failure;
            }
            showToast("Clone cancel requested");
            refreshCloneProgress(runId);
          });
      })
      .catch(function (error) {
        var status = error && typeof error.status === "number" ? error.status : 0;
        var body = error && error.body ? error.body : null;
        showToast(
          window.gitcrawlErrors.apiErrorText(status, body, "Cancel request failed"),
          function () {
            cancelClone(button);
          }
        );
      })
      .finally(function () {
        button.disabled = false;
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var toggle = document.getElementById("theme-toggle");
    syncToggle(toggle);
    if (toggle) {
      toggle.addEventListener("click", function () {
        applyTheme(preferredTheme() === "dark" ? "light" : "dark");
        syncToggle(toggle);
      });
    }
    if (document.body) {
      document.body.addEventListener("htmx:responseError", function (event) {
        var status = event.detail && event.detail.xhr ? event.detail.xhr.status : "";
        showToast(applib.requestErrorMessage(status));
      });
      document.body.addEventListener("htmx:sendError", function () {
        showToast("Network error");
      });
      document.body.addEventListener("htmx:afterSwap", invalidateRowCache);
      document.body.addEventListener("htmx:beforeRequest", markBusy);
      document.body.addEventListener("htmx:afterRequest", clearBusy);
    }
    syncCloneLimit();
    copyButtons();
    renderRecentFilters();
    document.addEventListener("submit", markBusy);
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (form && form.id === "find-form") {
        recordRecentFilter(form);
      }
    });
    window.addEventListener("pageshow", clearBusy);
    var start = document.getElementById("clone-start");
    if (start) {
      start.addEventListener("click", function () {
        startClone(start);
      });
    }
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || !form.hasAttribute || !form.hasAttribute("data-delete-filter")) {
      return;
    }
    var name = form.getAttribute("data-name") || "";
    if (!window.confirm("Delete " + name + "?")) {
      event.preventDefault();
    }
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) {
      return;
    }
    var cancel = target.closest("[data-cancel-clone]");
    if (cancel) {
      cancelClone(cancel);
      return;
    }
    var opener = target.closest("[data-open]");
    if (opener) {
      openModal(opener.getAttribute("data-open"));
      return;
    }
    var closer = target.closest("[data-close]");
    if (closer) {
      closeModal(closer.getAttribute("data-close"));
    }
  });

  document.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0) {
      return;
    }
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
      return;
    }
    var target = event.target;
    var link = target && target.closest ? target.closest("a[href]") : null;
    if (
      !link ||
      link.target ||
      link.hasAttribute("download") ||
      link.hasAttribute("data-close") ||
      link.hasAttribute("data-open")
    ) {
      return;
    }
    var url;
    try {
      url = new URL(link.href, window.location.href);
    } catch (error) {
      return;
    }
    if (url.origin === window.location.origin) {
      markBusy();
    }
  });

  document.addEventListener("keydown", handleKeyboard);
})();
