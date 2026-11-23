// SPDX-License-Identifier: Apache-2.0
// Docker and docker compose detection and calls. Compose files ship in the package's compose/ directory.

import { spawn, spawnSync, type StdioOptions } from "node:child_process";
import { join } from "node:path";

import { CliError, isProtocolMode } from "./log.js";
import type { State } from "./state.js";
import { COMPOSE_DIR, NPM_PACKAGE, VERSION } from "./version.js";

let composeCmd: string[] | null = null;

export function dockerRunning(): boolean {
  const r = spawnSync("docker", ["info", "--format", "{{.ServerVersion}}"], { encoding: "utf8" });
  return r.status === 0;
}

export function dockerInstalled(): boolean {
  const r = spawnSync("docker", ["--version"], { encoding: "utf8" });
  return r.status === 0;
}

/** `docker compose` (v2 plugin) or the standalone `docker-compose` v2 binary. */
export function detectCompose(): string[] | null {
  if (composeCmd) return composeCmd;
  const plugin = spawnSync("docker", ["compose", "version", "--short"], { encoding: "utf8" });
  if (plugin.status === 0 && /^v?2\./.test(plugin.stdout.trim())) return (composeCmd = ["docker", "compose"]);
  const standalone = spawnSync("docker-compose", ["version", "--short"], { encoding: "utf8" });
  if (standalone.status === 0 && /^v?2\./.test(standalone.stdout.trim())) return (composeCmd = ["docker-compose"]);
  return null;
}

export interface ComposeOptions {
  ha?: boolean;
  capture?: boolean; // collect stdout instead of streaming it
  quiet?: boolean; // discard output unless the command fails
  input?: "inherit" | "ignore";
}

export function composeEnv(state: State): NodeJS.ProcessEnv {
  return {
    ...process.env,
    FEEDERLINE_VERSION: process.env.FEEDERLINE_VERSION || VERSION,
    FEEDERLINE_MCP_TOKEN: state.token,
    FEEDERLINE_MCP_PORT: String(state.mcpPort),
    FEEDERLINE_PG_PORT: String(state.pgPort),
    FEEDERLINE_PROJECT: state.project,
    COMPOSE_IGNORE_ORPHANS: "true", // one-shot admin runs use the base file while --ha replicas may be up
  };
}

export function composeFiles(ha = false): string[] {
  const files = ["-f", join(COMPOSE_DIR, "docker-compose.yml")];
  if (ha) files.push("-f", join(COMPOSE_DIR, "compose.ha.yml"));
  return files;
}

/** Run `docker compose -p <project> -f ... <args>`. Resolves with captured stdout (when capture) or "". */
export function compose(state: State, args: string[], opts: ComposeOptions = {}): Promise<string> {
  const cmd = detectCompose();
  if (!cmd) throw new CliError("Docker Compose v2 was not found.", `Install Docker Desktop, or Docker Engine with the compose plugin, then run: npx -y ${NPM_PACKAGE} doctor`);
  const [bin, ...pre] = cmd;
  const full = [...pre, "-p", state.project, ...composeFiles(opts.ha), ...args];
  // In protocol mode (connect) the child's stdout must not reach ours: send it to stderr.
  const stdoutTarget = opts.capture || opts.quiet ? "pipe" : isProtocolMode() ? 2 : "inherit";
  const stderrTarget = opts.quiet ? "pipe" : "inherit";
  const stdio: StdioOptions = [opts.input ?? "ignore", stdoutTarget, stderrTarget];
  return new Promise((resolve, reject) => {
    const child = spawn(bin, full, { env: composeEnv(state), stdio });
    let stdout = "";
    let stderr = "";
    child.stdout?.on("data", (d: Buffer) => (stdout += d.toString()));
    child.stderr?.on("data", (d: Buffer) => (stderr += d.toString()));
    child.on("error", (e) => reject(new CliError(`Could not run ${bin}: ${e.message}`, `npx -y ${NPM_PACKAGE} doctor`)));
    child.on("close", (code) => {
      if (code === 0) resolve(stdout);
      else {
        if (opts.quiet && (stdout || stderr)) process.stderr.write(stdout + stderr);
        reject(new CliError(`docker compose ${args.join(" ")} failed (exit ${code}).`, `npx -y ${NPM_PACKAGE} logs`));
      }
    });
  });
}

/** Run the admin CLI inside the server image: `docker compose run --rm admin <args>`. */
export function admin(state: State, args: string[], opts: ComposeOptions = {}): Promise<string> {
  return compose(state, ["--profile", "admin", "run", "--rm", "--no-deps", "admin", ...args], opts);
}

export interface ServiceState {
  Service: string;
  State: string;
  Health?: string;
}

export async function services(state: State, ha = false): Promise<ServiceState[]> {
  const raw = await compose(state, ["ps", "--all", "--format", "json"], { capture: true, ha });
  const text = raw.trim();
  if (!text) return [];
  // compose v2 prints either a JSON array or one JSON object per line depending on version
  if (text.startsWith("[")) return JSON.parse(text) as ServiceState[];
  return text.split("\n").filter(Boolean).map((l) => JSON.parse(l) as ServiceState);
}
