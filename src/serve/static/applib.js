(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.gitcrawlApp = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var TOAST_DURATION_MS = 6000;

  function requestErrorMessage(status) {
    return status ? "Request failed (" + status + ")" : "Request failed";
  }

  function cloneErrorMessage() {
    return "Clone request failed";
  }

  function cloneStartedMessage() {
    return "Clone started";
  }

  function parseCloneLimit(raw) {
    var limit = parseInt(raw, 10);
    if (isNaN(limit) || limit < 0) {
      return 0;
    }
    return limit;
  }

  function isEditableTarget(target) {
    if (!target) {
      return false;
    }
    if (target.isContentEditable) {
      return true;
    }
    var tag = target.tagName ? target.tagName.toLowerCase() : "";
    return tag === "input" || tag === "textarea" || tag === "select" || tag === "option";
  }

  function createRowCache() {
    var cached = null;
    return {
      get: function (compute) {
        if (cached === null) {
          cached = compute();
        }
        return cached;
      },
      invalidate: function () {
        cached = null;
      }
    };
  }

  function createStartGuard() {
    var busy = false;
    return {
      begin: function () {
        if (busy) {
          return false;
        }
        busy = true;
        return true;
      },
      end: function () {
        busy = false;
      }
    };
  }

  return {
    TOAST_DURATION_MS: TOAST_DURATION_MS,
    requestErrorMessage: requestErrorMessage,
    cloneErrorMessage: cloneErrorMessage,
    cloneStartedMessage: cloneStartedMessage,
    parseCloneLimit: parseCloneLimit,
    isEditableTarget: isEditableTarget,
    createRowCache: createRowCache,
    createStartGuard: createStartGuard
  };
});
