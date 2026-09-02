import assert from "node:assert/strict";
import { test } from "node:test";
import {
  openRefusal,
  parseWakeQuery,
  sha256Hex,
  sliceEntries,
  wakeUri,
} from "../out/src/core.js";

test("denylist refuses binary data files regardless of size", () => {
  for (const name of ["WAVECAR", "chgcar", "AECCAR0", "AECCAR2"]) {
    const refusal = openRefusal(name, 10, 32 * 1048576);
    assert.match(refusal, /二进制数据/);
  }
});

test("oversize text files are refused with readable reason", () => {
  const refusal = openRefusal("OUTCAR", 40 * 1048576, 32 * 1048576);
  assert.match(refusal, /超过上限/);
});

test("normal text file is allowed", () => {
  assert.equal(openRefusal("INCAR", 1908, 32 * 1048576), null);
});

test("sha256Hex matches known vector", () => {
  assert.equal(
    sha256Hex("hello\n"),
    "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
  );
});

test("sliceEntries caps directory listings without recursion", () => {
  const entries = Array.from({ length: 1200 }, (_, i) => `f${i}`);
  assert.equal(sliceEntries(entries, 500).length, 500);
  assert.equal(sliceEntries(entries, 3).length, 3);
});

test("wakeUri matches the console-side launcher format", () => {
  assert.equal(
    wakeUri("minus", "/share/home/jlyang/my dir/a.txt", "file"),
    "vscode://huwei-team.huwei-agent-vscode-bridge/open" +
      "?server=minus&path=%2Fshare%2Fhome%2Fjlyang%2Fmy+dir%2Fa.txt&kind=file"
  );
});

test("folder context menu URI is accepted by the UriHandler parser", () => {
  const uri = new URL(wakeUri("cl12", "/share/home/user/calc", "folder"));
  assert.deepEqual(parseWakeQuery(uri.search.slice(1)), {
    server: "cl12",
    path: "/share/home/user/calc",
    kind: "folder",
  });
});

test("UriHandler parser refuses incomplete or unsafe wake requests", () => {
  assert.throws(() => parseWakeQuery("server=cl12&path=%2Ftmp&kind=unknown"));
  assert.throws(() => parseWakeQuery("server=cl12&path=relative&kind=file"));
});
