// SPDX-License-Identifier: Apache-2.0
// Per-user state: ~/.feederline-mcp/config.json holds the bearer token, ports, version and compose project name.

import { randomBytes } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export interface State {
  token: string;
  mcpPort: number;
  pgPort: number;
  version: string;
  project: string;
}

export const DEFAULT_MCP_PORT = 8000;
export const DEFAULT_PG_PORT = 5433;
export const DEFAULT_PROJECT = "feederline-mcp";

export function stateDir(env: NodeJS.ProcessEnv = process.env): string {
  return env.FEEDERLINE_HOME || join(homedir(), ".feederline-mcp");
}

export function statePath(env: NodeJS.ProcessEnv = process.env): string {
  return join(stateDir(env), "config.json");
}

export function newToken(): string {
  return randomBytes(32).toString("hex");
}

function envPort(value: string | undefined, fallback: number): number {
  const n = value ? Number.parseInt(value, 10) : NaN;
  return Number.isInteger(n) && n > 0 && n < 65536 ? n : fallback;
}

export function readState(env: NodeJS.ProcessEnv = process.env): State | null {
  const path = statePath(env);
  if (!existsSync(path)) return null;
  const raw = JSON.parse(readFileSync(path, "utf8")) as Partial<State>;
  if (!raw.token || typeof raw.token !== "string") return null;
  return {
    token: raw.token,
    mcpPort: envPort(String(raw.mcpPort ?? ""), DEFAULT_MCP_PORT),
    pgPort: envPort(String(raw.pgPort ?? ""), DEFAULT_PG_PORT),
    version: raw.version ?? "",
    project: raw.project || DEFAULT_PROJECT,
  };
}

export function writeState(state: State, env: NodeJS.ProcessEnv = process.env): void {
  const dir = stateDir(env);
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const path = statePath(env);
  writeFileSync(path, JSON.stringify(state, null, 2) + "\n", { mode: 0o600 });
  try {
    chmodSync(path, 0o600); // the token is a credential; no-op on Windows
  } catch {
    /* ignore */
  }
}

/** Read the state file, creating it with a random 32-byte token on first run. Env ports override saved ones. */
export function ensureState(version: string, env: NodeJS.ProcessEnv = process.env): { state: State; created: boolean } {
  const existing = readState(env);
  const state: State = existing ?? {
    token: newToken(),
    mcpPort: DEFAULT_MCP_PORT,
    pgPort: DEFAULT_PG_PORT,
    version,
    project: DEFAULT_PROJECT,
  };
  state.mcpPort = envPort(env.FEEDERLINE_MCP_PORT, state.mcpPort);
  state.pgPort = envPort(env.FEEDERLINE_PG_PORT, state.pgPort);
  if (env.FEEDERLINE_PROJECT) state.project = env.FEEDERLINE_PROJECT;
  const changed =
    !existing || existing.version !== version || existing.mcpPort !== state.mcpPort || existing.pgPort !== state.pgPort || existing.project !== state.project;
  state.version = version;
  if (changed) writeState(state, env);
  return { state, created: !existing };
}

/** Where the MCP endpoint is and how to authenticate, honouring FEEDERLINE_MCP_URL / FEEDERLINE_MCP_TOKEN. */
export function endpoint(state: State | null, env: NodeJS.ProcessEnv = process.env): { url: string; token: string; overridden: boolean } {
  const overridden = Boolean(env.FEEDERLINE_MCP_URL);
  const url = env.FEEDERLINE_MCP_URL || `http://127.0.0.1:${state?.mcpPort ?? DEFAULT_MCP_PORT}/mcp`;
  const token = env.FEEDERLINE_MCP_TOKEN || state?.token || "";
  return { url, token, overridden };
}
