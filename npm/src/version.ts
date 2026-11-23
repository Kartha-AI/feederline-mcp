// SPDX-License-Identifier: Apache-2.0
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const PACKAGE_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
export const COMPOSE_DIR = join(PACKAGE_ROOT, "compose");

export const VERSION: string = (JSON.parse(readFileSync(join(PACKAGE_ROOT, "package.json"), "utf8")) as { version: string }).version;

export const SERVER_NAME = "feederline-mcp";
export const NPM_PACKAGE = "@kartha-energy/feederline-mcp";
