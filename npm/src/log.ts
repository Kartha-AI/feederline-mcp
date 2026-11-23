// SPDX-License-Identifier: Apache-2.0
// Output helpers. In `connect` mode stdout belongs to the MCP protocol, so everything human-readable goes to stderr
// and child processes have their stdout redirected to stderr as well.

let protocolMode = false;

export function enableProtocolMode(): void {
  protocolMode = true;
  // Any stray console.log in a dependency would corrupt the protocol stream.
  console.log = (...args: unknown[]) => console.error(...args);
  console.info = (...args: unknown[]) => console.error(...args);
}

export function isProtocolMode(): boolean {
  return protocolMode;
}

/** Human-facing output: stdout normally, stderr in protocol mode. */
export function out(line = ""): void {
  if (protocolMode) process.stderr.write(line + "\n");
  else process.stdout.write(line + "\n");
}

export function info(line: string): void {
  process.stderr.write(line + "\n");
}

export class CliError extends Error {
  constructor(
    message: string,
    public fix?: string,
  ) {
    super(message);
  }
}
