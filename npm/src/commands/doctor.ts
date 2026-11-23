// SPDX-License-Identifier: Apache-2.0
import { statfsSync } from "node:fs";
import { createServer } from "node:net";
import { homedir } from "node:os";

import { detectCompose, dockerInstalled, dockerRunning } from "../docker.js";
import { getHealth } from "../health.js";
import { out } from "../log.js";
import { endpoint, readState, DEFAULT_MCP_PORT, DEFAULT_PG_PORT } from "../state.js";

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
  fix?: string;
  critical: boolean;
}

function portFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const srv = createServer();
    srv.once("error", () => resolve(false));
    srv.once("listening", () => srv.close(() => resolve(true)));
    srv.listen(port, "127.0.0.1");
  });
}

async function registryReachable(): Promise<boolean> {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 5000);
  try {
    const res = await fetch("https://ghcr.io/v2/", { signal: ctl.signal });
    return res.status === 401 || res.ok; // 401 = reachable, anonymous
  } catch {
    return false;
  } finally {
    clearTimeout(t);
  }
}

export async function runChecks(): Promise<Check[]> {
  const checks: Check[] = [];
  const major = Number.parseInt(process.versions.node.split(".")[0], 10);
  checks.push({ name: "Node.js", ok: major >= 18, detail: `v${process.versions.node}`, fix: "Install Node.js 18 or newer from https://nodejs.org", critical: true });

  const installed = dockerInstalled();
  const running = installed && dockerRunning();
  checks.push({
    name: "Docker",
    ok: running,
    detail: !installed ? "not installed" : running ? "running" : "installed but not running",
    fix: !installed ? "Install Docker Desktop (https://docs.docker.com/get-docker/)" : "Start Docker Desktop (or `sudo systemctl start docker`)",
    critical: true,
  });

  const compose = running ? detectCompose() : null;
  checks.push({ name: "Docker Compose v2", ok: Boolean(compose), detail: compose ? compose.join(" ") : "not found", fix: "Update Docker Desktop, or install the compose plugin (https://docs.docker.com/compose/install/)", critical: true });

  const state = readState();
  const ours = await getHealth(endpoint(state).url, 1500); // our own server already holding the port is fine
  for (const [label, port] of [["MCP port", state?.mcpPort ?? DEFAULT_MCP_PORT], ["Postgres port", state?.pgPort ?? DEFAULT_PG_PORT]] as const) {
    const free = ours ? true : await portFree(port);
    checks.push({
      name: `${label} ${port}`,
      ok: free,
      detail: ours ? "in use by feederline-mcp" : free ? "free" : "in use by another program",
      fix: label === "MCP port" ? "Free the port, or set FEEDERLINE_MCP_PORT=<port> and rerun `up`" : "Free the port, or set FEEDERLINE_PG_PORT=<port> and rerun `up`",
      critical: true,
    });
  }

  try {
    const s = statfsSync(homedir());
    const freeGb = (s.bavail * s.bsize) / 1e9;
    checks.push({ name: "Disk space", ok: freeGb >= 3, detail: `${freeGb.toFixed(1)} GB free`, fix: "Free at least 3 GB (images ~1.5 GB plus data)", critical: false });
  } catch {
    checks.push({ name: "Disk space", ok: true, detail: "not checked", critical: false });
  }

  const reach = await registryReachable();
  checks.push({ name: "Image registry (ghcr.io)", ok: reach, detail: reach ? "reachable" : "unreachable", fix: "Check your network or proxy settings; images are pulled from ghcr.io", critical: false });
  return checks;
}

export function printChecks(checks: Check[]): void {
  for (const c of checks) {
    out(`${c.ok ? "ok  " : "FAIL"}  ${c.name}: ${c.detail}`);
    if (!c.ok && c.fix) out(`      fix: ${c.fix}`);
  }
}

export async function doctor(): Promise<boolean> {
  const checks = await runChecks();
  printChecks(checks);
  return checks.every((c) => c.ok || !c.critical);
}
