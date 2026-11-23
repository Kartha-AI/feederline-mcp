// SPDX-License-Identifier: Apache-2.0
// down, reset, status, logs, load: thin wrappers over docker compose and the admin CLI.

import { createInterface } from "node:readline/promises";

import { admin, compose, services } from "../docker.js";
import { getHealth } from "../health.js";
import { CliError, out } from "../log.js";
import { endpoint, readState, type State } from "../state.js";
import { NPM_PACKAGE } from "../version.js";
import { adminStatus } from "./up.js";

export function requireState(): State {
  const state = readState();
  if (!state) throw new CliError("feederline-mcp has not been set up on this machine yet.", `npx -y ${NPM_PACKAGE} up`);
  return state;
}

export async function down(): Promise<void> {
  const state = requireState();
  await compose(state, ["--profile", "admin", "down", "--remove-orphans"], { ha: true, quiet: true });
  out("Stopped. Data volumes are kept; `up` starts again in seconds.");
}

export async function reset(yes = false): Promise<void> {
  const state = requireState();
  if (!yes) {
    if (!process.stdin.isTTY) throw new CliError("reset needs confirmation.", `npx -y ${NPM_PACKAGE} reset --yes`);
    const rl = createInterface({ input: process.stdin, output: process.stdout });
    const answer = await rl.question("This deletes the feederline-mcp containers and ALL loaded data (volumes). Type 'reset' to continue: ");
    rl.close();
    if (answer.trim() !== "reset") {
      out("Cancelled.");
      return;
    }
  }
  await compose(state, ["--profile", "admin", "down", "--volumes", "--remove-orphans"], { ha: true, quiet: true });
  out("Removed containers and volumes. Your access token in ~/.feederline-mcp/config.json is kept.");
}

export async function status(json = false): Promise<void> {
  const state = requireState();
  const { url } = endpoint(state, {});
  const svc = await services(state, true);
  const health = await getHealth(url);
  const st = health ? await adminStatus(state).catch(() => null) : null;
  if (json) {
    out(JSON.stringify({ url, services: svc, health, status: st }, null, 2));
    return;
  }
  out(`URL:      ${url}`);
  out(`Server:   ${health ? `${health.status} (version ${health.version}, migration ${health.migration ?? "none"})` : "not reachable"}`);
  for (const s of svc) out(`  ${s.Service.padEnd(10)} ${s.State}${s.Health ? ` (${s.Health})` : ""}`);
  if (!svc.length) out(`  no containers; run: npx -y ${NPM_PACKAGE} up`);
  if (st) {
    out("");
    out("Feeders:");
    out(`  ${"Region".padEnd(10)} ${"Name".padEnd(22)} ${"kV".padEnd(8)} Root ID`);
    for (const f of st.feeders) out(`  ${f.region_code.padEnd(10)} ${f.name.padEnd(22)} ${String(f.kv ?? "").padEnd(8)} ${f.root_mrid}`);
    out("");
    out(st.telemetry ? `Telemetry: ${st.telemetry.first.slice(0, 10)} to ${st.telemetry.last.slice(0, 10)}` : "Telemetry: none");
  }
}

export async function logs(service?: string): Promise<void> {
  const state = requireState();
  await compose(state, ["logs", "-f", "--tail", "200", ...(service ? [service] : [])], { ha: true, input: "inherit" });
}

export async function load(dataset: string, rest: string[]): Promise<void> {
  if (!["ieee", "smartds", "hifld"].includes(dataset)) throw new CliError(`Unknown dataset '${dataset}'.`, `npx -y ${NPM_PACKAGE} load ieee|smartds|hifld --help`);
  const state = requireState();
  await admin(state, ["load", dataset, ...rest]);
}
