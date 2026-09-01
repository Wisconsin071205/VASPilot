/** ConsoleClient：针对一个进程内模拟控制台的集成测试（不依赖 VS Code）。 */
import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { ConsoleClient, readDiscoveryFile } from "../out/src/api.js";

function startMockConsole() {
  const state = { files: new Map() };
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      const action = req.url.replace("/api/", "");
      const payload = body ? JSON.parse(body) : {};
      const send = (doc) => {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify(doc));
      };
      if (req.headers["x-vaspilot-token"] !== "tok") {
        return send({ ok: false, error: { code: "unauthorized", message: "bad token" } });
      }
      if (action === "remote.list") {
        return send({
          ok: true,
          path: payload.path,
          entries: [
            { name: "INCAR", type: "file", size: 10 },
            { name: "runs", type: "dir", size: 0 },
          ],
        });
      }
      if (action === "remote.read") {
        const content = state.files.get(payload.path) ?? "INCAR default";
        return send({ ok: true, path: payload.path, content, size: content.length });
      }
      if (action === "remote.write") {
        const expected = payload.expected_sha256 ?? "";
        const cur = state.files.get(payload.path);
        if ((cur === undefined && expected !== "") ||
            (cur !== undefined && expected !== "" && cur !== expected)) {
          return send({ ok: false, error: { code: "remote_changed",
            message: "远端文件已被其他操作修改" } });
        }
        state.files.set(payload.path, payload.content);
        return send({ ok: true, path: payload.path, sha256: "abc", size: 3,
                      mtime_epoch: 1 });
      }
      if (action === "remote.stat") {
        return send({ ok: true, path: payload.path, kind: "regular file",
                      size: 10, mtime_epoch: 5 });
      }
      if (action === "remote.remove") {
        state.files.delete(payload.path);
        return send({ ok: true, trash_id: "t1" });
      }
      if (action === "remote.mkdir") {
        return send({ ok: true, path: payload.path });
      }
      send({ ok: false, error: { code: "unknown_action", message: action } });
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => {
    resolve({ server, port: server.address().port });
  }));
}

test("console client: list/read/write flow and conflict refusal", async () => {
  const { server, port } = await startMockConsole();
  const client = new ConsoleClient(`http://127.0.0.1:${port}`, "tok", fetch);

  const listing = await client.list("minus", "/share/home/jlyang");
  assert.equal(listing.entries.length, 2);

  const read = await client.read("minus", "/share/home/jlyang/INCAR");
  assert.equal(read.content, "INCAR default");

  const write = await client.write(
    "minus", "/share/home/jlyang/INCAR", "new content", "");
  assert.equal(write.ok, true);

  // baseline 过期 → remote_changed（拒绝覆盖）
  await assert.rejects(
    () => client.write("minus", "/share/home/jlyang/INCAR", "clobber",
                       "0".repeat(64)),
    (err) => err.code === "remote_changed"
  );
  server.close();
});

test("console client: unreachable console raises readable error", async () => {
  // 127.0.0.1:1 几乎必然拒绝连接
  const client = new ConsoleClient("http://127.0.0.1:1", "tok", fetch);
  await assert.rejects(
    () => client.list("minus", "/"),
    (err) => /本地控制台未运行/.test(err.message)
  );
});

test("discovery file reader parses ui.json", async () => {
  // 直接验证函数对临时目录的解析（不写真实用户目录）
  const os = await import("node:os");
  await 0;
  const fs = await import("node:fs");
  const path = await import("node:path");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "bridge-"));
  fs.mkdirSync(path.join(tmp, ".vaspilot"));
  fs.writeFileSync(
    path.join(tmp, ".vaspilot", "ui.json"),
    JSON.stringify({ url: "http://127.0.0.1:8930", token: "abc", pid: 1 })
  );
  const info = readDiscoveryFile(tmp);
  assert.equal(info.url, "http://127.0.0.1:8930");
  assert.equal(info.token, "abc");
});
