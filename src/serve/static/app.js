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

  function handleShortcuts(event) {
    if (event.defaultPrevented) {
      return;
    }
    if (event.key === "?") {
      document.dispatchEvent(new CustomEvent("gc:shortcuts-requested"));
    }
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
  });

  document.addEventListener("keydown", handleShortcuts);
})();
