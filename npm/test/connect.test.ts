// The stdout rule: in `connect` mode stdout carries only MCP (JSON-RPC) messages; logs go to stderr.
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const CLI = join(__dirname, "..", "dist", "cli.js");

interface Seen {
  method?: string;
  auth?: string;
  session?: string;
  protocol?: string;
}

function body(req: IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    let b = "";
    req.on("data", (d) => (b += d));
    req.on("end", () => resolve(b));
  });
}

let server: Server;
let url = "";
const seen: Seen[] = [];

beforeAll(async () => {
  server = createServer(async (req, res) => {
    if (req.method === "GET") return res.writeHead(405).end();
    if (req.method === "DELETE") return res.writeHead(200).end();
    const msg = JSON.parse(await body(req));
    seen.push({
      method: msg.method,
      auth: req.headers.authorization,
      session: req.headers["mcp-session-id"] as string | undefined,
      protocol: req.headers["mcp-protocol-version"] as string | undefined,
    });
    if (msg.id === undefined) return res.writeHead(202).end();
    const result =
      msg.method === "initialize"
        ? { protocolVersion: "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "fake", version: "0" } }
        : { tools: [{ name: "load_feeder", inputSchema: { type: "object" } }] };
    res.writeHead(200, { "content-type": "application/json", "mcp-session-id": "sess-1" });
    res.end(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result }));
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/mcp`;
});

afterAll(() => server.close());

// stages[i] is written after i responses have arrived (a real client waits for the initialize result).
function run(targetUrl: string, stages: object[][], expectResponses: number) {
  return new Promise<{ stdout: string; stderr: string }>((resolve, reject) => {
    const child = spawn(process.execPath, [CLI, "connect"], {
      env: { ...process.env, FEEDERLINE_HOME: mkdtempSync(join(tmpdir(), "flm-c-")), FEEDERLINE_MCP_URL: targetUrl, FEEDERLINE_MCP_TOKEN: "tok" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`timeout; stdout=${stdout} stderr=${stderr}`));
    }, 15000);
    let sent = 0;
    const sendStage = () => {
      for (const m of stages[sent] ?? []) child.stdin.write(JSON.stringify(m) + "\n");
      sent += 1;
    };
    child.stdout.on("data", (d) => {
      stdout += d;
      const n = stdout.split("\n").filter(Boolean).length;
      if (n >= expectResponses) child.stdin.end();
      else while (sent <= n) sendStage();
    });
    child.stderr.on("data", (d) => (stderr += d));
    child.on("close", () => {
      clearTimeout(timer);
      resolve({ stdout, stderr });
    });
    sendStage();
  });
}

function assertOnlyJsonRpc(stdout: string) {
  const lines = stdout.split("\n").filter((l) => l.length);
  expect(lines.length).toBeGreaterThan(0);
  for (const line of lines) {
    const msg = JSON.parse(line); // throws on any non-JSON output
    expect(msg.jsonrpc).toBe("2.0");
  }
  expect(stdout.endsWith("\n")).toBe(true);
}

const INIT = { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "test", version: "0" } } };
const INITIALIZED = { jsonrpc: "2.0", method: "notifications/initialized" };
const LIST = { jsonrpc: "2.0", id: 2, method: "tools/list", params: {} };

describe("connect bridge", () => {
  it("forwards both ways, keeps one session, and writes only JSON-RPC to stdout", async () => {
    const { stdout, stderr } = await run(url, [[INIT], [INITIALIZED, LIST]], 2);
    assertOnlyJsonRpc(stdout);
    const replies = stdout.trim().split("\n").map((l) => JSON.parse(l));
    expect(replies.map((r) => r.id)).toEqual([1, 2]);
    expect(replies[1].result.tools[0].name).toBe("load_feeder");
    expect(stderr).toContain("bridging stdio");
    const list = seen.find((s) => s.method === "tools/list");
    expect(list?.auth).toBe("Bearer tok");
    expect(list?.session).toBe("sess-1");
    expect(list?.protocol).toBe("2025-06-18");
  });

  it("answers requests with a JSON-RPC error when the server is unreachable", async () => {
    const { stdout, stderr } = await run("http://127.0.0.1:9/mcp", [[INIT]], 1);
    assertOnlyJsonRpc(stdout);
    const reply = JSON.parse(stdout.trim());
    expect(reply.id).toBe(1);
    expect(reply.error.message).toMatch(/unreachable/);
    expect(stderr).toMatch(/failed/);
  });
});
