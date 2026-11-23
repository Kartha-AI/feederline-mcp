// SPDX-License-Identifier: Apache-2.0
// Claude Desktop and Claude Code connection settings, and a safe merge into claude_desktop_config.json.

import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import { CliError } from "./log.js";
import { NPM_PACKAGE, SERVER_NAME } from "./version.js";

export interface ServerEntry {
  command: string;
  args: string[];
  env?: Record<string, string>;
}

export function bridgeEntry(): ServerEntry {
  return { command: "npx", args: ["-y", NPM_PACKAGE, "connect"] };
}

export function mcpRemoteEntry(url: string, token: string): ServerEntry {
  return {
    command: "npx",
    args: ["-y", "mcp-remote", url, "--header", "Authorization:${FEEDERLINE_AUTH}"],
    env: { FEEDERLINE_AUTH: `Bearer ${token}` },
  };
}

export function desktopSnippet(entry: ServerEntry): string {
  return JSON.stringify({ mcpServers: { [SERVER_NAME]: entry } }, null, 2);
}

export function claudeCodeCommand(url: string, token: string): string {
  return `claude mcp add --transport http ${SERVER_NAME} ${url} --header "Authorization: Bearer ${token}"`;
}

export function desktopConfigPath(platform: NodeJS.Platform = process.platform, env: NodeJS.ProcessEnv = process.env): string {
  if (platform === "darwin") return join(homedir(), "Library", "Application Support", "Claude", "claude_desktop_config.json");
  if (platform === "win32") {
    const appData = env.APPDATA || join(homedir(), "AppData", "Roaming");
    return join(appData, "Claude", "claude_desktop_config.json");
  }
  return join(env.XDG_CONFIG_HOME || join(homedir(), ".config"), "Claude", "claude_desktop_config.json");
}

/** Pure merge: returns a new config with only mcpServers[SERVER_NAME] set; every other key is preserved. */
export function mergeDesktopConfig(existing: unknown, entry: ServerEntry): Record<string, unknown> {
  if (existing !== undefined && (typeof existing !== "object" || existing === null || Array.isArray(existing))) {
    throw new CliError("The Claude Desktop config is not a JSON object; refusing to modify it.", "Fix the file by hand, or paste the JSON printed by `config`.");
  }
  const base = { ...((existing as Record<string, unknown>) ?? {}) };
  const servers = base.mcpServers;
  if (servers !== undefined && (typeof servers !== "object" || servers === null || Array.isArray(servers))) {
    throw new CliError("`mcpServers` in the Claude Desktop config is not an object; refusing to modify it.", "Fix the file by hand, or paste the JSON printed by `config`.");
  }
  base.mcpServers = { ...((servers as Record<string, unknown>) ?? {}), [SERVER_NAME]: entry };
  return base;
}

function timestamp(d = new Date()): string {
  return d.toISOString().replace(/[:.]/g, "-");
}

/** Merge the entry into the Claude Desktop config file, writing a timestamped backup first. Returns paths. */
export function writeDesktopConfig(entry: ServerEntry, path = desktopConfigPath()): { path: string; backup: string | null } {
  let existing: unknown = undefined;
  let backup: string | null = null;
  if (existsSync(path)) {
    const text = readFileSync(path, "utf8");
    if (text.trim()) {
      try {
        existing = JSON.parse(text);
      } catch {
        throw new CliError(`${path} is not valid JSON; refusing to modify it.`, "Fix the file by hand, or paste the JSON printed by `config`.");
      }
    }
    backup = `${path}.${timestamp()}.bak`;
    copyFileSync(path, backup);
  } else {
    mkdirSync(dirname(path), { recursive: true });
  }
  const merged = mergeDesktopConfig(existing, entry);
  writeFileSync(path, JSON.stringify(merged, null, 2) + "\n");
  return { path, backup };
}
