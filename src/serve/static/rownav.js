(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.gitcrawlRowNav = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function nextIndex(current, delta, length) {
    if (length <= 0) {
      return -1;
    }
    if (current < 0) {
      return delta > 0 ? 0 : length - 1;
    }
    return Math.min(Math.max(current + delta, 0), length - 1);
  }

  return { nextIndex: nextIndex };
});
