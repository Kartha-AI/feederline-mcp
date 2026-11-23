// SPDX-License-Identifier: Apache-2.0
import { admin, compose } from "../docker.js";
import { waitHealthy } from "../health.js";
import { CliError, out } from "../log.js";
import { endpoint, ensureState, type State } from "../state.js";
import { NPM_PACKAGE, VERSION } from "../version.js";
import { printChecks, runChecks } from "./doctor.js";

export interface UpOptions {
  ha?: boolean;
  demo?: boolean; // commander maps --no-demo to demo=false
  smartds?: boolean;
  pull?: boolean; // --no-pull uses images already present (for locally built images)
  quietNextSteps?: boolean;
}

export interface AdminStatus {
  version: string;
  migration: string | null;
  feeders: { root_mrid: string; name: string; region_code: string; substation: string | null; kv: number | null }[];
  telemetry: { first: string; last: string } | null;
}

export async function adminStatus(state: State): Promise<AdminStatus> {
  const raw = await admin(state, ["status", "--json"], { capture: true });
  return JSON.parse(raw.trim().split("\n").pop() || "{}") as AdminStatus;
}

/** Days of demo telemetry still missing between the last rollup bucket and yesterday (UTC), capped at 30. */
export function missingDemoDays(telemetry: AdminStatus["telemetry"], now = new Date(), window = 30): number {
  if (!telemetry) return window;
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const last = new Date(telemetry.last);
  const lastDay = Date.UTC(last.getUTCFullYear(), last.getUTCMonth(), last.getUTCDate());
  const gap = Math.floor((today - lastDay) / 86_400_000) - 1; // yesterday is the last day the demo fills
  return Math.max(0, Math.min(window, gap));
}

export async function up(opts: UpOptions = {}): Promise<State> {
  const checks = await runChecks();
  const failed = checks.filter((c) => !c.ok && c.critical);
  if (failed.length) {
    printChecks(checks);
    throw new CliError("Prerequisites are missing (see above).", `Fix the failed checks, then run: npx -y ${NPM_PACKAGE} up`);
  }
  const { state, created } = ensureState(VERSION);
  if (created) out("Created ~/.feederline-mcp/config.json with a new random access token.");

  if (opts.pull !== false) {
    out("Pulling images...");
    await compose(state, ["pull", "--quiet", ...(opts.ha ? ["postgres", "mcp", "proxy"] : ["postgres", "mcp"])], { ha: opts.ha });
  }
  out("Starting Postgres...");
  await compose(state, ["up", "-d", "--wait", "postgres"], { ha: opts.ha, quiet: true });
  out("Applying migrations...");
  await admin(state, ["migrate"], { quiet: true });
  out("Starting the MCP server...");
  await compose(state, ["up", "-d", "--wait", ...(opts.ha ? ["mcp", "mcp-b", "proxy"] : ["mcp"])], { ha: opts.ha, quiet: true });

  if (opts.demo !== false) {
    const st = await adminStatus(state);
    const regions = new Set(st.feeders.map((f) => f.region_code));
    if (!["IEEE13", "IEEE34", "IEEE123"].every((r) => regions.has(r))) {
      out("Loading the IEEE 13/34/123 test feeders...");
      await admin(state, ["load", "ieee"], { quiet: true });
    }
    const days = missingDemoDays(st.telemetry);
    if (days > 0) {
      out(`Generating ${days} day(s) of demo telemetry (deterministic, seed 42)...`);
      await admin(state, ["simulate", "--backfill-days", String(days), "--seed", "42"], { quiet: true });
    }
  }
  if (opts.smartds) {
    out("Loading NREL SMART-DS P1R (downloads about 7 MB)...");
    await admin(state, ["load", "smartds", "--region", "P1R", "--substation", "p1rhs0_1247"], { quiet: true });
  }

  const { url } = endpoint(state, {});
  const health = await waitHealthy(url, 90_000);
  if (!health) throw new CliError(`The server did not become healthy at ${url}.`, `npx -y ${NPM_PACKAGE} logs mcp`);
  out(`feederline-mcp ${health.version ?? VERSION} is running at ${url} (${health.feeders ?? 0} feeders loaded).`);
  if (!opts.quietNextSteps) {
    out("");
    out("Next: connect Claude.");
    out(`  npx -y ${NPM_PACKAGE} config                  # print the Claude Desktop / Claude Code settings`);
    out(`  npx -y ${NPM_PACKAGE} config --write-desktop  # or add it to Claude Desktop for you (backup first)`);
    out("Then restart Claude Desktop and ask: \"List the feeders available, with region, substation and nominal voltage.\"");
  }
  return state;
}
