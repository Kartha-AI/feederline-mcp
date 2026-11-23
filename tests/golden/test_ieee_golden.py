# SPDX-License-Identifier: Apache-2.0
"""Compile IEEE 13/34/123 from CIM, solve, compare every fixture bus/phase voltage."""

import json
import time
from pathlib import Path

import pytest

from feederline_mcp import engine
from feederline_mcp.adapters.base import Principal
from feederline_mcp.compiler import compile_feeder
from loaders.common import mrid

pytestmark = pytest.mark.db
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ALL = Principal("golden", ["*"])
EXPECTED_COUNTS = {
    13: {"ACLineSegment": 11, "Breaker": 1, "EnergyConsumer": 15, "LinearShuntCompensator": 2, "LoadBreakSwitch": 1,
         "PowerTransformer": 5},
    34: {"ACLineSegment": 32, "Breaker": 1, "EnergyConsumer": 68, "LinearShuntCompensator": 2, "PowerTransformer": 8},
    123: {"ACLineSegment": 118, "Breaker": 1, "EnergyConsumer": 91, "LinearShuntCompensator": 4, "LoadBreakSwitch": 8,
          "PowerTransformer": 8},
}
# Planning is the exact CIM round trip. Operational seeds every load as constant P from one ThreePhaseActivePower
# measurement, which spreads P equally across phases; IEEE34's long unbalanced laterals amplify that to ~0.14%.
TOLERANCE = {("planning", 13): 1e-3, ("planning", 34): 1e-3, ("planning", 123): 1e-3,
             ("operational", 13): 1e-3, ("operational", 34): 2.5e-3, ("operational", 123): 1e-3}


def solve(ds, n, case):
    t0 = time.perf_counter()
    cf = compile_feeder(ds, ALL, mrid(f"ieee{n}", "breaker", "head"), case, f"IEEE{n}")
    compile_s = time.perf_counter() - t0
    ctx = engine.new_context()
    engine.run(ctx, cf.commands)
    res = engine.solve(ctx)
    return cf, ctx, res, compile_s


@pytest.mark.parametrize("case", ["planning", "operational"])
@pytest.mark.parametrize("n", [13, 34, 123])
def test_voltages_match_fixture(loaded, ds, n, case):
    cf, ctx, res, _ = solve(ds, n, case)
    assert res.converged
    fixture = json.loads((FIXTURES / f"ieee{n}_solution.json").read_text())
    got = {}
    for nv in engine.node_voltages(ctx):
        human = cf.names.get(nv.bus, (None, nv.bus))[1].lower()
        got[(human, str(nv.node))] = nv.volts
    worst = 0.0
    for bus, b in fixture["buses"].items():
        for node, v in b["nodes"].items():
            assert (bus, node) in got, f"{bus}.{node} missing from compiled circuit"
            worst = max(worst, abs(got[(bus, node)] - v["volts"]) / v["volts"])
    assert worst < TOLERANCE[(case, n)], f"worst deviation {worst:.4%}"


@pytest.mark.parametrize("n", [13, 34, 123])
def test_compile_report_counts(loaded, ds, n):
    cf, _, _, compile_s = solve(ds, n, "operational")
    m = cf.model
    assert m.counts == EXPECTED_COUNTS[n]
    assert m.loads_measured == EXPECTED_COUNTS[n]["EnergyConsumer"] and m.loads_defaulted == 0
    assert m.defaulted_impedances == 0 and m.stale_switches == 0
    assert m.telemetry_ts is not None and m.cim_updated_at is not None
    assert compile_s < 1.0


def test_planning_uses_nameplate(loaded, ds):
    cf, *_ = solve(ds, 13, "planning")
    assert cf.model.loads_defaulted == 15 and cf.model.loads_measured == 0 and cf.model.warnings == []
