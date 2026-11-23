// SPDX-License-Identifier: Apache-2.0
// stdio <-> Streamable HTTP bridge for Claude Desktop. stdout carries only MCP protocol messages; every log line goes
// to stderr. One HTTP MCP session per bridge process, so one Claude chat keeps its loaded feeder.

import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { JSONRPCMessage } from "@modelcontextprotocol/sdk/types.js";

import { getHealth, isHealthy, waitHealthy } from "../health.js";
import { CliError, enableProtocolMode, info } from "../log.js";
import { endpoint, readState } from "../state.js";
import { NPM_PACKAGE } from "../version.js";
import { up } from "./up.js";

const START_TIMEOUT_MS = 90_000;

function isRequest(m: JSONRPCMessage): m is JSONRPCMessage & { id: string | number; method: string } {
  return "method" in m && "id" in m && m.id !== undefined;
}

async function ensureServer(): Promise<{ url: string; token: string }> {
  const state = readState();
  const ep = endpoint(state);
  if (ep.overridden) return ep; // non-default setup: the user manages the server
  if (isHealthy(await getHealth(ep.url))) return ep;
  info("feederline-mcp: server not running; starting it (up --no-demo)...");
  const deadline = Date.now() + START_TIMEOUT_MS;
  try {
    const started = await Promise.race([
      up({ demo: state ? false : true, pull: !state, quietNextSteps: true }),
      new Promise<never>((_, rej) => setTimeout(() => rej(new Error("timeout")), START_TIMEOUT_MS)),
    ]);
    const fresh = endpoint(started);
    if (await waitHealthy(fresh.url, Math.max(1000, deadline - Date.now()))) return fresh;
  } catch (e) {
    info(`feederline-mcp: ${(e as Error).message}`);
  }
  throw new CliError("feederline-mcp server is not running and could not be started.", `npx -y ${NPM_PACKAGE} up`);
}

export async function connect(): Promise<void> {
  enableProtocolMode();
  const { url, token } = await ensureServer();
  if (!token)
    throw new CliError(
      `A server is running at ${url} but no access token was found (~/.feederline-mcp/config.json is created by \`up\`).`,
      `For the from-source stack (make up) set FEEDERLINE_MCP_TOKEN=dev-token in the client config; otherwise run: npx -y ${NPM_PACKAGE} up`,
    );

  const http = new StreamableHTTPClientTransport(new URL(url), {
    requestInit: { headers: { Authorization: `Bearer ${token}` } },
  });
  const stdio = new StdioServerTransport();
  let closing = false;

  const shutdown = async (code = 0) => {
    if (closing) return;
    closing = true;
    await Promise.allSettled([http.close(), stdio.close()]);
    process.exit(code);
  };

  http.onmessage = (msg) => {
    // Record the negotiated protocol version so later HTTP requests carry MCP-Protocol-Version.
    const result = (msg as { result?: { protocolVersion?: unknown } }).result;
    if (result && typeof result.protocolVersion === "string") http.setProtocolVersion(result.protocolVersion);
    stdio.send(msg).catch((e) => info(`feederline-mcp: stdout write failed: ${(e as Error).message}`));
  };
  http.onerror = (e) => info(`feederline-mcp: ${e.message}`);
  http.onclose = () => void shutdown(0);

  stdio.onmessage = (msg) => {
    http.send(msg).catch((e: Error) => {
      info(`feederline-mcp: request to ${url} failed: ${e.message}`);
      if (isRequest(msg)) {
        const reply: JSONRPCMessage = {
          jsonrpc: "2.0",
          id: msg.id,
          error: { code: -32603, message: `feederline-mcp server unreachable at ${url}. Run: npx -y ${NPM_PACKAGE} up` },
        };
        stdio.send(reply).catch(() => undefined);
      }
    });
  };
  stdio.onerror = (e) => info(`feederline-mcp: ${e.message}`);
  stdio.onclose = () => void shutdown(0);

  process.stdin.on("end", () => void shutdown(0));
  process.on("SIGINT", () => void shutdown(0));
  process.on("SIGTERM", () => void shutdown(0));

  await http.start();
  await stdio.start();
  info(`feederline-mcp: bridging stdio to ${url}`);
}
