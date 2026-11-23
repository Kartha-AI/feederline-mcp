// SPDX-License-Identifier: Apache-2.0

export interface Health {
  status: string;
  version?: string;
  db?: string;
  migration?: string | null;
  feeders?: number | null;
  sessions?: number;
  engine?: string;
}

export function healthUrl(mcpUrl: string): string {
  const u = new URL(mcpUrl);
  u.pathname = "/healthz";
  u.search = "";
  return u.toString();
}

export async function getHealth(mcpUrl: string, timeoutMs = 3000): Promise<Health | null> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(healthUrl(mcpUrl), { signal: ctl.signal });
    const body = (await res.json()) as Health;
    return body;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export function isHealthy(h: Health | null): boolean {
  return Boolean(h && h.status === "ok" && h.db === "ok");
}

export async function waitHealthy(mcpUrl: string, timeoutMs: number, wantMigration = true): Promise<Health | null> {
  const deadline = Date.now() + timeoutMs;
  let last: Health | null = null;
  while (Date.now() < deadline) {
    last = await getHealth(mcpUrl);
    if (isHealthy(last) && (!wantMigration || last?.migration)) return last;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return null;
}
