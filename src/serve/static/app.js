(function () {
  "use strict";

  var root = document.documentElement;

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

  function showToast(message) {
    var toast = document.getElementById("toast");
    if (!toast) {
      return;
    }
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(function () {
      toast.hidden = true;
    }, 6000);
  }

  function isEditable(target) {
    if (!target) {
      return false;
    }
    if (target.isContentEditable) {
      return true;
    }
    var tag = target.tagName ? target.tagName.toLowerCase() : "";
    return tag === "input" || tag === "textarea" || tag === "select" || tag === "option";
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

  function selectableRows() {
    return Array.prototype.slice.call(document.querySelectorAll("tbody tr")).filter(function (row) {
      return !row.hidden && row.querySelector("a[href]");
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
    var next;
    if (current < 0) {
      next = delta > 0 ? 0 : rows.length - 1;
    } else {
      next = Math.min(Math.max(current + delta, 0), rows.length - 1);
    }
    rows.forEach(function (row) {
      row.classList.remove("row-selected");
    });
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
    if (isEditable(event.target)) {
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

  function copyHash() {
    var button = document.getElementById("copy-hash");
    if (!button) {
      return;
    }
    button.addEventListener("click", function () {
      var text = button.getAttribute("data-copy") || "";
      if (window.navigator.clipboard && window.navigator.clipboard.writeText) {
        window.navigator.clipboard.writeText(text).then(
          function () {
            showToast("Hash copied");
          },
          function () {
            showToast("Copy failed");
          }
        );
      } else {
        showToast("Copy failed");
      }
    });
  }

  function selectedMode() {
    var checked = document.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "shallow";
  }

  function startClone(button) {
    var modal = document.getElementById("clone-modal");
    var runId =
      button.getAttribute("data-run-id") || (modal ? modal.getAttribute("data-run-id") : "");
    var number = document.getElementById("clone-limit-input");
    var slider = document.getElementById("clone-limit");
    var raw = number ? number.value : slider ? slider.value : "0";
    var limit = window.parseInt(raw, 10);
    if (window.isNaN(limit) || limit < 0) {
      limit = 0;
    }
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
        if (!response.ok) {
          throw new Error("clone request failed");
        }
        showToast("Clone started");
        if (window.htmx && window.htmx.ajax) {
          window.htmx.ajax("GET", "/partials/runs/" + runId + "/clone-progress", {
            target: "#clone-progress",
            swap: "outerHTML"
          });
        }
      })
      .catch(function () {
        showToast("Clone request failed");
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
        showToast(status ? "Request failed (" + status + ")" : "Request failed");
      });
      document.body.addEventListener("htmx:sendError", function () {
        showToast("Network error");
      });
    }
    syncCloneLimit();
    copyHash();
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

  document.addEventListener("keydown", handleKeyboard);
})();
