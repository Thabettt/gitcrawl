import assert from "node:assert/strict";
import { test } from "node:test";

import recent from "../../src/serve/static/recentfilters.js";

test("parse tolerates corrupt and malformed storage", () => {
  assert.deepEqual(recent.parse("{not json"), []);
  assert.deepEqual(recent.parse(JSON.stringify({ a: 1 })), []);
  assert.deepEqual(recent.parse(JSON.stringify([{ label: 3, fields: [] }, null])), []);
});

test("parse caps entries and drops fields without values", () => {
  const entry = { label: "x", fields: [{ name: "keywords", value: "rust" }] };
  assert.equal(recent.parse(JSON.stringify([entry, entry, entry, entry, entry, entry])).length, 5);
  assert.deepEqual(
    recent.parse(JSON.stringify([{ label: "x", fields: [{ name: "keywords", value: "" }] }])),
    []
  );
});

test("toQuery encodes names and values", () => {
  assert.equal(
    recent.toQuery([{ name: "keywords", value: "stars:>500" }]),
    "keywords=stars%3A%3E500"
  );
});

test("label prefers the saved name, then keywords", () => {
  assert.equal(recent.label([{ name: "keywords", value: "rust" }]), "rust");
  assert.equal(
    recent.label([
      { name: "name", value: "Mine" },
      { name: "keywords", value: "rust" }
    ]),
    "Mine"
  );
  assert.equal(recent.label([]), "");
});

test("push dedupes by query and keeps the newest first", () => {
  const first = { label: "one", fields: [{ name: "keywords", value: "rust" }] };
  const second = { label: "two", fields: [{ name: "keywords", value: "go" }] };
  const list = recent.push(recent.push([], first), second);
  assert.deepEqual(
    list.map(function (entry) {
      return entry.label;
    }),
    ["two", "one"]
  );
  assert.deepEqual(
    recent.push(list, first).map(function (entry) {
      return entry.label;
    }),
    ["one", "two"]
  );
});

test("push caps at MAX and ignores empty entries", () => {
  let list = [];
  for (let index = 0; index < 8; index += 1) {
    list = recent.push(list, {
      label: String(index),
      fields: [{ name: "keywords", value: String(index) }]
    });
  }
  assert.equal(list.length, 5);
  assert.equal(list[0].label, "7");
  assert.equal(recent.push(list, { label: "empty", fields: [] }).length, 5);
});
