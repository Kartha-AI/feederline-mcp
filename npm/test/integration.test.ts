// SPDX-License-Identifier: Apache-2.0
// up -> status -> down against real Docker. Runs only when FEEDERLINE_INTEGRATION=1 (CI, after images are built).
// The CLI runs asynchronously: a spawnSync would block the vitest worker for minutes and its RPC heartbeat times out.
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const CLI = join(__dirname, "..", "dist", "cli.js");
const enabled = process.env.FEEDERLINE_INTEGRATION === "1";

describe.skipIf(!enabled)("integration: up -> status -> down", () => {
  const env = {
    ...process.env,
    FEEDERLINE_HOME: process.env.FEEDERLINE_HOME || mkdtempSync(join(tmpdir(), "flm-int-")),
  };
  const cli = (...args: string[]) =>
    new Promise<{ status: number | null; stdout: string; stderr: string }>((resolve, reject) => {
      const child = spawn(process.execPath, [CLI, ...args], { env, stdio: ["ignore", "pipe", "pipe"], timeout: 600_000 });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (d: Buffer) => (stdout += d.toString()));
      child.stderr.on("data", (d: Buffer) => (stderr += d.toString()));
      child.on("error", reject);
      child.on("close", (status) => resolve({ status, stdout, stderr }));
    });

  it("starts the stack, lists the IEEE feeders with their stable root IDs, and stops", async () => {
    const up = await cli("up", ...(process.env.FEEDERLINE_NO_PULL === "1" ? ["--no-pull"] : []));
    expect(up.status, up.stderr + up.stdout).toBe(0);
    const st = await cli("status", "--json");
    expect(st.status, st.stderr).toBe(0);
    const parsed = JSON.parse(st.stdout);
    const ids = Object.fromEntries(parsed.status.feeders.map((f: { region_code: string; root_mrid: string }) => [f.region_code, f.root_mrid]));
    expect(ids).toMatchObject({
      IEEE13: "c990acfa-4c84-5788-be92-53b20464fbd8",
      IEEE34: "378451c2-afdb-524f-a587-9808c081df60",
      IEEE123: "1dfeb41b-bf91-52f3-b6af-7b400b93c5b8",
    });
    expect(parsed.status.telemetry).not.toBeNull();
    const down = await cli("down");
    expect(down.status, down.stderr).toBe(0);
  }, 900_000);
});
