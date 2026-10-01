import assert from "node:assert/strict";
import { test } from "node:test";

import rownav from "../../src/serve/static/rownav.js";

test("nextIndex starts at either end depending on direction", () => {
  assert.equal(rownav.nextIndex(-1, 1, 5), 0);
  assert.equal(rownav.nextIndex(-1, -1, 5), 4);
});

test("nextIndex clamps at both ends", () => {
  assert.equal(rownav.nextIndex(0, -1, 5), 0);
  assert.equal(rownav.nextIndex(4, 1, 5), 4);
});

test("nextIndex returns -1 for an empty list", () => {
  assert.equal(rownav.nextIndex(-1, 1, 0), -1);
});
