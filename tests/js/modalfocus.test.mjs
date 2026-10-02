import assert from "node:assert/strict";
import { test } from "node:test";

import focus from "../../src/serve/static/modalfocus.js";

test("wraps forward and backward", () => {
  assert.equal(focus.nextFocusIndex(0, 3, false), 1);
  assert.equal(focus.nextFocusIndex(2, 3, false), 0);
  assert.equal(focus.nextFocusIndex(0, 3, true), 2);
  assert.equal(focus.nextFocusIndex(1, 3, true), 0);
});

test("starts at either end when nothing is focused", () => {
  assert.equal(focus.nextFocusIndex(-1, 3, false), 0);
  assert.equal(focus.nextFocusIndex(-1, 3, true), 2);
});

test("returns -1 for an empty modal", () => {
  assert.equal(focus.nextFocusIndex(-1, 0, false), -1);
  assert.equal(focus.nextFocusIndex(0, 0, true), -1);
});
