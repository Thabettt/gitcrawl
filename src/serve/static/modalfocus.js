(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.gitcrawlModalFocus = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function nextFocusIndex(current, length, backwards) {
    if (length <= 0) {
      return -1;
    }
    if (current < 0) {
      return backwards ? length - 1 : 0;
    }
    if (backwards) {
      return current <= 0 ? length - 1 : current - 1;
    }
    return current >= length - 1 ? 0 : current + 1;
  }

  return { nextFocusIndex: nextFocusIndex };
});
