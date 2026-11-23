# SPDX-License-Identifier: Apache-2.0
import threading

import pytest

from feederline_mcp import engine
from feederline_mcp.adapters.base import Principal
from feederline_mcp.sessions import SessionStore
from feederline_mcp.studies import ToolError

P = Principal("u", ["*"])
CIRCUIT = [
    "Clear", "New Circuit.{n} bus1=src basekv=12.47 pu=1.0",
    "New Line.l1 bus1=src bus2=b1 length=1 units=km r1=0.3 x1=0.6 r0=0.9 x0=1.8",
    "New Load.ld bus1=b1 kv=12.47 kw={kw} kvar=100",
    "Set voltagebases=[12.47]", "CalcVoltageBases",
]


def build(s, name, kw):
    engine.run(s.dss, [c.format(n=name, kw=kw) for c in CIRCUIT])
    return engine.solve(s.dss)


def test_two_sessions_are_isolated(tmp_path):
    store = SessionStore(snapshot_dir=tmp_path)
    a, b = store.for_ctx("a", P), store.for_ctx("b", P)
    build(a, "fa", 1000)
    build(b, "fb", 3000)
    assert a.dss.ActiveCircuit.Name == "fa" and b.dss.ActiveCircuit.Name == "fb"
    assert engine.totals(a.dss)["total_load_kw"] < engine.totals(b.dss)["total_load_kw"]


def test_other_principal_cannot_reuse_session(tmp_path):
    store = SessionStore(snapshot_dir=tmp_path)
    store.for_ctx("a", P)
    with pytest.raises(ToolError) as e:
        store.for_ctx("a", Principal("v", ["*"]))
    assert e.value.code == "forbidden"


def test_lru_eviction_and_idle_ttl(tmp_path):
    store = SessionStore(max_contexts=2, idle_ttl_s=3600, snapshot_dir=tmp_path)
    store.for_ctx("a", P)
    store.for_ctx("b", P)
    store.for_ctx("c", P)
    assert store.get("a") is None and len(store) == 2
    store.idle_ttl_s = 0
    assert store.evict_idle() == 2 and len(store) == 0


def test_busy_when_tool_running(tmp_path):
    store = SessionStore(snapshot_dir=tmp_path)
    s = store.for_ctx("a", P)
    with store.acquire(s), pytest.raises(ToolError) as e, store.acquire(s):
        pass
    assert e.value.code == "busy"


def test_restore_after_sweep_is_byte_identical(tmp_path):
    store = SessionStore(snapshot_dir=tmp_path / "snap")
    s = store.for_ctx("a", P)
    build(s, "f", 1000)
    s.solved = True
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir(), after.mkdir()
    engine.save_circuit(s.dss, str(before))
    with store.sweep(s):
        engine.add_der(s.dss, "pv1", "b1", [1, 2, 3], 12.47, 500, "pv")
        engine.run(s.dss, ["Edit Load.ld kw=2500"])
        engine.solve(s.dss)
    engine.save_circuit(s.dss, str(after))
    for f in sorted(before.iterdir()):
        a = f.read_text().splitlines()[1:]  # first line carries the save timestamp
        b = (after / f.name).read_text().splitlines()[1:]
        assert a == b, f.name


def test_engine_runs_on_worker_threads():
    out = {}

    def work():
        ctx = engine.new_context()
        engine.run(ctx, [c.format(n="t", kw=10) for c in CIRCUIT])
        out["ok"] = engine.solve(ctx).converged

    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert out["ok"]
