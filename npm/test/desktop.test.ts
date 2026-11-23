import { existsSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { bridgeEntry, claudeCodeCommand, desktopConfigPath, mcpRemoteEntry, mergeDesktopConfig, writeDesktopConfig } from "../src/desktop.js";

describe("Claude Desktop config merge", () => {
  it("adds the server to an empty config", () => {
    expect(mergeDesktopConfig(undefined, bridgeEntry())).toEqual({
      mcpServers: { "feederline-mcp": { command: "npx", args: ["-y", "@kartha-energy/feederline-mcp", "connect"] } },
    });
  });

  it("keeps other servers and top-level keys", () => {
    const existing = { globalShortcut: "Ctrl+Space", mcpServers: { other: { command: "x", args: [] } } };
    const merged = mergeDesktopConfig(existing, bridgeEntry());
    expect(merged.globalShortcut).toBe("Ctrl+Space");
    expect(Object.keys(merged.mcpServers as object).sort()).toEqual(["feederline-mcp", "other"]);
    expect(existing.mcpServers).not.toHaveProperty("feederline-mcp"); // input not mutated
  });

  it("replaces a previous feederline-mcp entry", () => {
    const merged = mergeDesktopConfig({ mcpServers: { "feederline-mcp": { command: "old", args: [] } } }, bridgeEntry());
    expect((merged.mcpServers as Record<string, { command: string }>)["feederline-mcp"].command).toBe("npx");
  });

  it("refuses non-object configs", () => {
    expect(() => mergeDesktopConfig([], bridgeEntry())).toThrow(/not a JSON object/);
    expect(() => mergeDesktopConfig({ mcpServers: [] }, bridgeEntry())).toThrow(/mcpServers/);
  });

  it("writes a timestamped backup and preserves other servers on disk", () => {
    const dir = mkdtempSync(join(tmpdir(), "flm-desk-"));
    const path = join(dir, "claude_desktop_config.json");
    writeFileSync(path, JSON.stringify({ mcpServers: { other: { command: "x", args: ["y"] } } }));
    const { backup } = writeDesktopConfig(bridgeEntry(), path);
    expect(backup && existsSync(backup)).toBe(true);
    expect(JSON.parse(readFileSync(backup!, "utf8"))).toEqual({ mcpServers: { other: { command: "x", args: ["y"] } } });
    const written = JSON.parse(readFileSync(path, "utf8"));
    expect(Object.keys(written.mcpServers).sort()).toEqual(["feederline-mcp", "other"]);
  });

  it("creates the file and its directory when missing, without a backup", () => {
    const dir = mkdtempSync(join(tmpdir(), "flm-desk-"));
    const path = join(dir, "Claude", "claude_desktop_config.json");
    const { backup } = writeDesktopConfig(bridgeEntry(), path);
    expect(backup).toBeNull();
    expect(readdirSync(join(dir, "Claude"))).toEqual(["claude_desktop_config.json"]);
  });

  it("leaves invalid JSON untouched", () => {
    const dir = mkdtempSync(join(tmpdir(), "flm-desk-"));
    const path = join(dir, "claude_desktop_config.json");
    writeFileSync(path, "{ not json");
    expect(() => writeDesktopConfig(bridgeEntry(), path)).toThrow(/not valid JSON/);
    expect(readFileSync(path, "utf8")).toBe("{ not json");
    expect(readdirSync(dir)).toEqual(["claude_desktop_config.json"]);
  });
});

describe("paths and snippets", () => {
  it("uses the per-OS config location", () => {
    expect(desktopConfigPath("darwin")).toMatch(/Library[\\/]Application Support[\\/]Claude[\\/]claude_desktop_config\.json$/);
    expect(desktopConfigPath("win32", { APPDATA: "C:\\Users\\u\\AppData\\Roaming" })).toMatch(/Roaming[\\/]Claude[\\/]claude_desktop_config\.json$/);
    expect(desktopConfigPath("linux", { XDG_CONFIG_HOME: "/x" })).toMatch(/^[\\/]x[\\/]Claude[\\/]claude_desktop_config\.json$/);
  });

  it("prints the Claude Code command and mcp-remote fallback with the token", () => {
    expect(claudeCodeCommand("http://127.0.0.1:8000/mcp", "abc")).toBe(
      'claude mcp add --transport http feederline-mcp http://127.0.0.1:8000/mcp --header "Authorization: Bearer abc"',
    );
    expect(mcpRemoteEntry("http://127.0.0.1:8000/mcp", "abc").env).toEqual({ FEEDERLINE_AUTH: "Bearer abc" });
  });
});
