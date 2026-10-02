(function (root, factory) {
  "use strict";
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.gitcrawlRecentFilters = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MAX = 5;

  function parse(raw) {
    if (!raw) {
      return [];
    }
    var data;
    try {
      data = JSON.parse(raw);
    } catch (error) {
      return [];
    }
    if (!Array.isArray(data)) {
      return [];
    }
    var entries = [];
    for (var index = 0; index < data.length; index += 1) {
      var entry = data[index];
      if (!entry || typeof entry !== "object" || typeof entry.label !== "string") {
        continue;
      }
      if (!Array.isArray(entry.fields)) {
        continue;
      }
      var fields = [];
      for (var fieldIndex = 0; fieldIndex < entry.fields.length; fieldIndex += 1) {
        var field = entry.fields[fieldIndex];
        if (
          field &&
          typeof field.name === "string" &&
          typeof field.value === "string" &&
          field.value
        ) {
          fields.push({ name: field.name, value: field.value });
        }
      }
      if (fields.length) {
        entries.push({ label: entry.label, fields: fields });
      }
      if (entries.length >= MAX) {
        break;
      }
    }
    return entries;
  }

  function toQuery(fields) {
    var parts = [];
    for (var index = 0; index < fields.length; index += 1) {
      var field = fields[index];
      if (!field || !field.name || !field.value) {
        continue;
      }
      parts.push(encodeURIComponent(field.name) + "=" + encodeURIComponent(field.value));
    }
    return parts.join("&");
  }

  function label(fields) {
    for (var index = 0; index < fields.length; index += 1) {
      if (fields[index].name === "name" && fields[index].value) {
        return fields[index].value;
      }
    }
    for (var queryIndex = 0; queryIndex < fields.length; queryIndex += 1) {
      if (fields[queryIndex].name === "keywords" && fields[queryIndex].value) {
        return fields[queryIndex].value;
      }
    }
    return fields.length ? "Untitled filter" : "";
  }

  function push(entries, entry) {
    if (!entry || !entry.fields || !entry.fields.length) {
      return entries.slice(0, MAX);
    }
    var key = toQuery(entry.fields);
    if (!key) {
      return entries.slice(0, MAX);
    }
    var next = [entry];
    for (var index = 0; index < entries.length && next.length < MAX; index += 1) {
      var existing = entries[index];
      if (existing && existing.fields && toQuery(existing.fields) !== key) {
        next.push(existing);
      }
    }
    return next.slice(0, MAX);
  }

  return { MAX: MAX, parse: parse, toQuery: toQuery, label: label, push: push };
});
