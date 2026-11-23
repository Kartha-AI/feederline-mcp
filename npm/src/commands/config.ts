// SPDX-License-Identifier: Apache-2.0
import { spawn } from "node:child_process";

import { bridgeEntry, claudeCodeCommand, desktopConfigPath, desktopSnippet, mcpRemoteEntry, writeDesktopConfig } from "../desktop.js";
import { CliError, out } from "../log.js";
import { endpoint } from "../state.js";
import { NPM_PACKAGE } from "../version.js";
import { requireState } from "./stack.js";

export interface ConfigOptions {
  writeDesktop?: boolean;
  claudeCode?: boolean;
}

export function config(opts: ConfigOptions = {}): void {
  const state = requireState();
  const { url, token } = endpoint(state);
  if (opts.claudeCode) {
    out(claudeCodeCommand(url, token));
    return;
  }
  if (opts.writeDesktop) {
    const { path, backup } = writeDesktopConfig(bridgeEntry());
    out(`Added "feederline-mcp" to ${path}`);
    if (backup) out(`Backup of the previous file: ${backup}`);
    out("Restart Claude Desktop; the feederline-mcp tools appear in the tools menu.");
    return;
  }
  out(`Claude Desktop: add this to ${desktopConfigPath()}`);
  out("(or run `npx -y " + NPM_PACKAGE + " config --write-desktop`), then restart Claude Desktop.");
  out("");
  out(desktopSnippet(bridgeEntry()));
  out("");
  out("Claude Code:");
  out("");
  out(`  ${claudeCodeCommand(url, token)}`);
  out("");
  out("Without the bridge (community mcp-remote package):");
  out("");
  out(desktopSnippet(mcpRemoteEntry(url, token)));
  out("");
  out("One Claude chat is one MCP session: a new chat starts with no feeder loaded, so call load_feeder again.");
}

export async function inspect(): Promise<void> {
  const state = requireState();
  const { url, token } = endpoint(state);
  const args = ["-y", "@modelcontextprotocol/inspector", "--transport", "http", "--server-url", url, "--header", `Authorization: Bearer ${token}`];
  const win = process.platform === "win32";
  // On Windows npx is a .cmd shim that needs a shell; quote arguments containing spaces for it.
  const argv = win ? args.map((a) => (/\s/.test(a) ? `"${a}"` : a)) : args;
  await new Promise<void>((resolve, reject) => {
    const child = spawn(win ? "npx.cmd" : "npx", argv, { stdio: "inherit", shell: win });
    child.on("error", (e) => reject(new CliError(`Could not start the MCP Inspector: ${e.message}`, "Check that npx works: npx --version")));
    child.on("close", () => resolve());
  });
}
