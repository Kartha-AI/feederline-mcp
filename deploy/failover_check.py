# SPDX-License-Identifier: Apache-2.0
"""Failover check for the two-replica compose stack: make up && make load-ieee && make ha-check.

Opens an MCP session through the proxy, loads IEEE 123 and solves, stops the replica that served it, then solves
again: the other replica must resume the session and rehydrate the feeder with identical results.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import httpx

URL = os.environ.get("MCP_URL", f"http://localhost:{os.environ.get('MCP_PORT', '8000')}/mcp")
HEADERS = {"Authorization": "Bearer dev-token", "Accept": "application/json, text/event-stream",
           "Content-Type": "application/json"}
IEEE123_HEAD = "1dfeb41b-bf91-52f3-b6af-7b400b93c5b8"


def rpc(body: dict, sid: str | None = None) -> tuple[httpx.Response, dict | None]:
    headers = {**HEADERS, **({"mcp-session-id": sid} if sid else {})}
    r = httpx.post(URL, content=json.dumps(body), headers=headers, timeout=120)
    data = [line[5:] for line in r.text.splitlines() if line.startswith("data:")]
    return r, (json.loads(data[-1]) if data else None)


def tool(name: str, args: dict, sid: str, rid: int) -> tuple[str, dict]:
    r, msg = rpc({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}}, sid)
    return r.headers.get("x-served-by", "?"), json.loads(msg["result"]["content"][0]["text"])


def main() -> int:
    r, _ = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ha-check", "version": "1"}}})
    sid = r.headers["mcp-session-id"]
    rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    print(f"session {sid} opened on {r.headers.get('x-served-by')}")
    by, env = tool("load_feeder", {"root_mrid": IEEE123_HEAD, "region_code": "IEEE123"}, sid, 2)
    print(f"load_feeder on {by}: success={env['success']} {env['meta']['duration_ms']} ms")
    by, before = tool("run_power_flow", {}, sid, 3)
    print(f"run_power_flow on {by}: losses {before['data']['losses_kw']} kW")
    subprocess.run(["docker", "compose", "stop", by], check=True, capture_output=True)
    print(f"stopped {by}")
    try:
        by2, after = tool("run_power_flow", {}, sid, 4)
        print(f"run_power_flow on {by2}: success={after['success']} losses {after['data']['losses_kw']} kW")
        ok = by2 != by and after["success"] and after["data"] == before["data"]
    finally:
        subprocess.run(["docker", "compose", "start", by], check=True, capture_output=True)
        print(f"restarted {by}")
    print("failover OK" if ok else "failover FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
