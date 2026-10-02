(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.gitcrawlErrors = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function apiErrorText(status, body, fallback) {
    if (body && typeof body === "object") {
      if (typeof body.hint === "string" && body.hint) {
        return body.param ? body.param + ": " + body.hint : body.hint;
      }
      if (body.error === "timeout" && body.retry_after) {
        return "Timed out — retry in " + body.retry_after + "s";
      }
      if (body.error === "run_not_found") {
        return "Run not found";
      }
      if (typeof body.detail === "string" && body.detail) {
        return body.detail;
      }
      if (typeof body.error === "string" && body.error) {
        return body.error.replace(/_/g, " ");
      }
    }
    if (status) {
      return fallback || "Request failed";
    }
    return "Network error";
  }

  return { apiErrorText: apiErrorText };
});
