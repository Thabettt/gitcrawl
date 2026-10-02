import assert from "node:assert/strict";
import { test } from "node:test";

import errors from "../../src/serve/static/errors.js";

test("param and hint win when present", () => {
  assert.equal(
    errors.apiErrorText(
      400,
      { error: "invalid_param", param: "limit", hint: "limit must be an integer >= 0" },
      "Clone request failed"
    ),
    "limit: limit must be an integer >= 0"
  );
});

test("hint alone is used", () => {
  assert.equal(
    errors.apiErrorText(400, { error: "invalid_param", hint: "body must be JSON" }, "fallback"),
    "body must be JSON"
  );
});

test("timeout responses include retry_after", () => {
  assert.equal(
    errors.apiErrorText(503, { error: "timeout", retry_after: 30 }, "fallback"),
    "Timed out — retry in 30s"
  );
});

test("run_not_found is human readable", () => {
  assert.equal(
    errors.apiErrorText(404, { error: "run_not_found", run_id: 4 }, "fallback"),
    "Run not found"
  );
});

test("detail and bare error strings are used as-is", () => {
  assert.equal(errors.apiErrorText(500, { detail: "Not Found" }, "fallback"), "Not Found");
  assert.equal(
    errors.apiErrorText(500, { error: "internal_error" }, "fallback"),
    "internal error"
  );
});

test("unknown envelopes fall back, status zero means network error", () => {
  assert.equal(errors.apiErrorText(400, null, "Clone request failed"), "Clone request failed");
  assert.equal(errors.apiErrorText(400, "text", "Clone request failed"), "Clone request failed");
  assert.equal(errors.apiErrorText(0, null, "Clone request failed"), "Network error");
});
