import assert from "node:assert/strict";
import { test } from "node:test";

import applib from "../../src/serve/static/applib.js";

test("row cache computes once until invalidated", () => {
  const cache = applib.createRowCache();
  const rows = [1, 2, 3];
  let calls = 0;
  const compute = () => {
    calls += 1;
    return rows;
  };
  assert.equal(cache.get(compute), rows);
  assert.equal(cache.get(compute), rows);
  assert.equal(calls, 1);
  cache.invalidate();
  assert.equal(cache.get(compute), rows);
  assert.equal(calls, 2);
});

test("start guard refuses a second begin until end", () => {
  const guard = applib.createStartGuard();
  assert.equal(guard.begin(), true);
  assert.equal(guard.begin(), false);
  guard.end();
  assert.equal(guard.begin(), true);
});

test("parseCloneLimit clamps missing and negative values to zero", () => {
  assert.equal(applib.parseCloneLimit("42"), 42);
  assert.equal(applib.parseCloneLimit("3.9"), 3);
  assert.equal(applib.parseCloneLimit("abc"), 0);
  assert.equal(applib.parseCloneLimit("-1"), 0);
  assert.equal(applib.parseCloneLimit(""), 0);
});

test("isEditableTarget detects form controls and contenteditable", () => {
  assert.equal(applib.isEditableTarget(null), false);
  assert.equal(applib.isEditableTarget({ tagName: "INPUT" }), true);
  assert.equal(applib.isEditableTarget({ tagName: "textarea" }), true);
  assert.equal(applib.isEditableTarget({ tagName: "div" }), false);
  assert.equal(applib.isEditableTarget({ tagName: "div", isContentEditable: true }), true);
});

test("countInputsFor keeps only the inputs a comparator needs", () => {
  assert.equal(applib.countInputsFor("range"), "range");
  for (const comparator of [">", ">=", "<", "<=", "=", "eq", "", "bogus"]) {
    assert.equal(applib.countInputsFor(comparator), "value");
  }
});

test("toast messages and duration are stable", () => {
  assert.equal(applib.requestErrorMessage(500), "Request failed (500)");
  assert.equal(applib.requestErrorMessage(""), "Request failed");
  assert.equal(applib.cloneErrorMessage(), "Clone request failed");
  assert.equal(applib.cloneStartedMessage(), "Clone started");
  assert.equal(applib.TOAST_DURATION_MS, 6000);
});
