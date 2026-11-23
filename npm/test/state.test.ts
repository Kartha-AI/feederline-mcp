import { mkdtempSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { endpoint, ensureState, readState, statePath } from "../src/state.js";

function env(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
  return { FEEDERLINE_HOME: mkdtempSync(join(tmpdir(), "flm-")), ...extra };
}

describe("state", () => {
  it("creates a random 32-byte token on first run and keeps it", () => {
    const e = env();
    const first = ensureState("0.1.0", e);
    expect(first.created).toBe(true);
    expect(first.state.token).toMatch(/^[0-9a-f]{64}$/);
    const second = ensureState("0.1.0", e);
    expect(second.created).toBe(false);
    expect(second.state.token).toBe(first.state.token);
  });

  it("writes the state file owner-only", () => {
    const e = env();
    ensureState("0.1.0", e);
    if (process.platform !== "win32") expect(statSync(statePath(e)).mode & 0o777).toBe(0o600);
  });

  it("defaults ports and lets env override them", () => {
    const e = env({ FEEDERLINE_MCP_PORT: "8123", FEEDERLINE_PG_PORT: "6543" });
    const { state } = ensureState("0.1.0", e);
    expect(state.mcpPort).toBe(8123);
    expect(state.pgPort).toBe(6543);
    expect(JSON.parse(readFileSync(statePath(e), "utf8")).mcpPort).toBe(8123);
    expect(ensureState("0.1.0", { FEEDERLINE_HOME: e.FEEDERLINE_HOME }).state.mcpPort).toBe(8123);
  });

  it("ignores invalid port values", () => {
    const { state } = ensureState("0.1.0", env({ FEEDERLINE_MCP_PORT: "nope" }));
    expect(state.mcpPort).toBe(8000);
  });

  it("records the package version", () => {
    const e = env();
    ensureState("0.1.0", e);
    ensureState("0.2.0", e);
    expect(readState(e)?.version).toBe("0.2.0");
  });

  it("endpoint honours URL and token overrides", () => {
    const e = env();
    const { state } = ensureState("0.1.0", e);
    expect(endpoint(state, {})).toEqual({ url: "http://127.0.0.1:8000/mcp", token: state.token, overridden: false });
    expect(endpoint(state, { FEEDERLINE_MCP_URL: "http://h:1/mcp", FEEDERLINE_MCP_TOKEN: "t" })).toEqual({ url: "http://h:1/mcp", token: "t", overridden: true });
  });

  it("returns null when no state exists", () => {
    expect(readState(env())).toBeNull();
  });
});
