# SPDX-License-Identifier: Apache-2.0
"""Sessions survive a replica change: snapshot store round trip, and two app instances sharing one store."""

import json
import os

import pytest
from starlette.testclient import TestClient

from feederline_mcp import config
from feederline_mcp.adapters.base import Principal
from feederline_mcp.sessions import SessionStore
from feederline_mcp.studies import ToolError
from feederline_mcp.tools._runner import Services, run_tool
from feederline_mcp.tools.engine import load_feeder, power_flow
from loaders.common import mrid
from tests.conftest import MCP_URL

pytestmark = pytest.mark.db
P = Principal("alice", ["*"])
ROOT13 = str(mrid("ieee13", "breaker", "head"))


def test_store_round_trip(loaded, ds, tmp_path):
    from feederline_mcp.adapters.local import PostgresStudySink

    a = Services(ds, PostgresStudySink(ds), SessionStore(snapshot_dir=tmp_path))
    raw = {"root_mrid": ROOT13, "region_code": "IEEE13", "case": "planning"}
    env = run_tool(a, "s1", P, "load_feeder", load_feeder.LoadFeederInput, raw,
                   lambda s, x: load_feeder.execute(a, s, x), needs_model=False)
    assert env["success"]
    first = run_tool(a, "s1", P, "run_power_flow", power_flow.PowerFlowInput, {}, power_flow.execute)

    b = Services(ds, PostgresStudySink(ds), SessionStore(snapshot_dir=tmp_path))  # another replica
    second = run_tool(b, "s1", P, "run_power_flow", power_flow.PowerFlowInput, {}, power_flow.execute)
    assert second["success"] and b.store.get("s1").rehydrated
    assert second["data"] == first["data"]
    assert second["model"] == first["model"]
    with pytest.raises(ToolError) as e:
        SessionStore(snapshot_dir=tmp_path).for_ctx("s1", Principal("mallory", ["*"]))
    assert e.value.code == "forbidden"


def _rpc(client, body, sid=None):
    headers = {"Authorization": "Bearer t0k", "Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}
    if sid:
        headers["mcp-session-id"] = sid
    r = client.post("/mcp", content=json.dumps(body), headers=headers)
    assert r.status_code == 200, (r.status_code, r.text)
    data = [line[5:].strip() for line in r.text.splitlines() if line.startswith("data:")]
    return r, (json.loads(data[-1]) if data else None)


def test_two_replicas_share_sessions(loaded, tmp_path, monkeypatch):
    monkeypatch.setenv("FEEDERLINE_MCP_NO_APP", "1")
    from feederline_mcp.app import create_app

    settings = config.Settings(database_url=MCP_URL, bearer_token="t0k", auth_mode="on", snapshot_dir=tmp_path)
    with TestClient(create_app(settings)) as a, TestClient(create_app(settings)) as b:
        r, _ = _rpc(a, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        sid = r.headers["mcp-session-id"]
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "load_feeder", "arguments": {"root_mrid": ROOT13, "region_code": "IEEE13"}}}
        _, loaded_env = _rpc(b, call, sid)  # replica B never saw initialize
        env = json.loads(loaded_env["result"]["content"][0]["text"])
        assert env["success"] and env["meta"]["session_id"] == sid
        pf = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "run_power_flow", "arguments": {}}}
        _, on_a = _rpc(a, pf, sid)  # back on A: rehydrates the feeder B loaded
        env_a = json.loads(on_a["result"]["content"][0]["text"])
        assert env_a["success"] and env_a["model"]["root_name"] == "IEEE13 Head"
        unknown = a.post("/mcp", content=json.dumps(pf), headers={
            "Authorization": "Bearer t0k", "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json", "mcp-session-id": "f" * 32})
        assert unknown.status_code == 404
    assert os.path.exists(tmp_path / sid / "model" / "Master.dss")


class DictMirror:
    """In-memory SnapshotMirror for replicas without a shared disk: they share nothing but this."""

    def __init__(self):
        self.files = {}

    def push(self, session_id, local_dir):
        for f in local_dir.rglob("*"):
            if f.is_file():
                self.files[(session_id, f.relative_to(local_dir).as_posix())] = f.read_bytes()

    def pull(self, session_id, local_dir):
        hit = [(k, v) for k, v in self.files.items() if k[0] == session_id]
        for (_, rel), data in hit:
            (local_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (local_dir / rel).write_bytes(data)
        return bool(hit)


def test_replicas_without_shared_disk_use_the_mirror(loaded, ds, tmp_path):
    from feederline_mcp.adapters.local import PostgresStudySink

    mirror = DictMirror()
    a = Services(ds, PostgresStudySink(ds), SessionStore(snapshot_dir=tmp_path / "a", mirror=mirror))
    b = Services(ds, PostgresStudySink(ds), SessionStore(snapshot_dir=tmp_path / "b", mirror=mirror))
    a.store.mark("m1", P)
    assert b.store.resumable("m1", P) and not b.store.resumable("m1", Principal("mallory", ["*"]))
    raw = {"root_mrid": ROOT13, "region_code": "IEEE13", "case": "planning"}
    assert run_tool(a, "m1", P, "load_feeder", load_feeder.LoadFeederInput, raw,
                    lambda s, x: load_feeder.execute(a, s, x), needs_model=False)["success"]
    first = run_tool(a, "m1", P, "run_power_flow", power_flow.PowerFlowInput, {}, power_flow.execute)
    second = run_tool(b, "m1", P, "run_power_flow", power_flow.PowerFlowInput, {}, power_flow.execute)
    assert b.store.get("m1").rehydrated and second["data"] == first["data"]
