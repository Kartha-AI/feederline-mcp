# SPDX-License-Identifier: Apache-2.0
"""SMART-DS gate: a 10k-node substation compiles < 5 s, solves < 2 s, holds < 100 MB per session, and matches the
original DSS solve. Needs `make load-smartds REGION=P1R` (skipped otherwise)."""

import gc
import json
import os
import subprocess
import time

import pytest

from feederline_mcp import engine
from feederline_mcp.adapters.base import Principal
from feederline_mcp.compiler import compile_feeder
from feederline_mcp.tools._runner import run_tool
from feederline_mcp.tools.engine import load_feeder
from loaders.common import mrid
from loaders.smartds import substation_dir

pytestmark = pytest.mark.db
REGION, SUB = os.environ.get("SMARTDS_REGION", "P1R"), os.environ.get("SMARTDS_SUBSTATION", "p1rhs0_1247")
ROOT = mrid(f"smartds:{REGION}:{SUB}", "breaker", "head")
ALL = Principal("gate", ["*"])


@pytest.fixture(scope="module")
def loaded_smartds(ds):
    if not ds.query("SELECT 1 FROM conducting_equipment WHERE mrid = %(m)s", {"m": ROOT}, ["*"]):
        pytest.skip(f"SMART-DS {REGION}/{SUB} not loaded (make load-smartds REGION={REGION})")
    return json.loads((substation_dir(REGION, SUB).parent / f"{SUB}_solution.json").read_text())


def rss_mb() -> float:
    return int(subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())])) / 1024


def test_compile_and_solve_times_and_accuracy(loaded_smartds, ds):
    t0 = time.perf_counter()
    cf = compile_feeder(ds, ALL, ROOT, "planning", REGION)
    ctx = engine.new_context()
    engine.run(ctx, cf.commands)
    compile_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    res = engine.solve(ctx)
    solve_s = time.perf_counter() - t1
    assert res.converged
    assert ctx.ActiveCircuit.NumNodes >= 10_000
    assert compile_s < 5.0, compile_s
    assert solve_s < 2.0, solve_s
    got = {(cf.names.get(n.bus, (None, n.bus))[1].lower(), str(n.node)): n.volts for n in engine.node_voltages(ctx)}
    worst = 0.0
    for bus, b in loaded_smartds["buses"].items():
        for node, v in b["nodes"].items():
            if v["volts"] > 1:
                worst = max(worst, abs(got[(bus, node)] - v["volts"]) / v["volts"])
    assert worst < 1e-3, worst


def test_session_memory_under_100mb(loaded_smartds, services):
    raw = {"root_mrid": str(ROOT), "region_code": REGION, "case": "planning"}

    def load(sid):
        env = run_tool(services, sid, ALL, "load_feeder", load_feeder.LoadFeederInput, raw,
                       lambda s, a: load_feeder.execute(services, s, a), needs_model=False)
        assert env["success"], env["errors"]

    load("warm")  # allocator, pool and import warm-up
    gc.collect()
    before = rss_mb()
    load("measured")
    gc.collect()
    assert rss_mb() - before < 100
