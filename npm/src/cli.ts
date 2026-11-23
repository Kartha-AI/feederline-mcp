#!/usr/bin/env node
// SPDX-License-Identifier: Apache-2.0
import { Command } from "commander";

import { config, inspect } from "./commands/config.js";
import { connect } from "./commands/connect.js";
import { doctor } from "./commands/doctor.js";
import { down, load, logs, reset, status } from "./commands/stack.js";
import { up } from "./commands/up.js";
import { CliError } from "./log.js";
import { VERSION } from "./version.js";

const program = new Command();
program
  .name("feederline-mcp")
  .description("Run the feederline-mcp server (CIM grid model + OpenDSS studies over MCP) locally and connect Claude to it.")
  .version(VERSION);

program
  .command("up")
  .description("start Postgres and the MCP server, migrate, load the IEEE feeders and seed 30 days of demo telemetry")
  .option("--ha", "two server replicas behind nginx (failover demo)")
  .option("--no-demo", "skip loading the IEEE feeders and demo telemetry")
  .option("--smartds", "also load NREL SMART-DS P1R (about 7 MB download)")
  .option("--no-pull", "use images already present locally")
  .action(async (o) => void (await up(o)));

program.command("down").description("stop the containers (data volumes are kept)").action(down);

program
  .command("reset")
  .description("remove the containers and all data volumes (asks for confirmation)")
  .option("-y, --yes", "skip the confirmation prompt")
  .action(async (o) => reset(Boolean(o.yes)));

program
  .command("status")
  .description("container state, server URL, loaded feeders with root IDs, telemetry date range")
  .option("--json", "machine-readable output")
  .action(async (o) => status(Boolean(o.json)));

program.command("logs [service]").description("follow container logs (postgres, mcp)").action(logs);

program
  .command("load <dataset> [args...]")
  .description("load a public dataset: ieee | smartds [--region --substation --date] | hifld [--state]")
  .allowUnknownOption(true)
  .action(async (dataset: string, args: string[]) => load(dataset, args ?? []));

program
  .command("config")
  .description("print the Claude Desktop and Claude Code connection settings")
  .option("--write-desktop", "merge the server into the Claude Desktop config (writes a backup first)")
  .option("--claude-code", "print only the Claude Code command")
  .action((o) => config(o));

program.command("connect").description("stdio bridge for Claude Desktop (used in claude_desktop_config.json)").action(connect);
program.command("inspect").description("open the MCP Inspector against the local server").action(inspect);

program
  .command("doctor")
  .description("check Node.js, Docker, Compose, ports, disk space and registry access")
  .action(async () => {
    if (!(await doctor())) process.exitCode = 1;
  });

program.parseAsync(process.argv).catch((e: unknown) => {
  const err = e as Error;
  process.stderr.write(`error: ${err.message}\n`);
  if (e instanceof CliError && e.fix) process.stderr.write(`fix:   ${e.fix}\n`);
  else if (!(e instanceof CliError)) process.stderr.write(`${err.stack ?? ""}\n`);
  process.exit(1);
});
