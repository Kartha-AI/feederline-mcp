# SPDX-License-Identifier: Apache-2.0
"""Tool chain through run_tool (the same path the MCP handlers take), against the loaded IEEE feeders."""

import time

import pytest

from feederline_mcp import engine
from feederline_mcp.adapters.base import Principal
from feederline_mcp.tools._runner import run_tool
from feederline_mcp.tools.engine import capacity, der, load_feeder, power_flow, viz, voltages
from loaders.common import mrid

pytestmark = pytest.mark.db
P = Principal("tester", ["*"])


def call(services, sid, mod, cls, raw, needs_model=True, with_services=False):
    body = (lambda s, a: mod.execute(services, s, a)) if with_services else mod.execute
    return run_tool(services, sid, P, mod.__name__.rsplit(".", 1)[-1], cls, raw, body, needs_model=needs_model)


def load(services, sid, n, case="operational"):
    raw = {"root_mrid": str(mrid(f"ieee{n}", "breaker", "head")), "region_code": f"IEEE{n}", "case": case}
    return call(services, sid, load_feeder, load_feeder.LoadFeederInput, raw, needs_model=False, with_services=True)


def test_tool_chain_and_envelope(loaded, services, ds):
    env = load(services, "chain", 13)
    assert env["success"] and env["data"]["buses"] == 17 and env["model"]["root_name"] == "IEEE13 Head"
    pf = call(services, "chain", power_flow, power_flow.PowerFlowInput, {})
    assert pf["success"] and pf["data"]["converged"] and pf["meta"]["study_id"]
    assert 3400 < pf["data"]["total_load_kw"] < 3700
    cv = call(services, "chain", voltages, voltages.CheckVoltagesInput, {"min_pu": 0.95, "max_pu": 1.05})
    assert cv["success"] and all(not v["name"].startswith("cn_") for v in cv["data"]["violations"])
    od = call(services, "chain", der, der.OptimizeDerInput, {"capacity_kw": 300}, with_services=True)
    assert od["success"] and od["data"]["recommended"]["name"]
    img = call(services, "chain", viz, viz.VisualizationInput, {"plot": "loading", "top_n": 5})
    assert img["success"] and len(img["data"]["image_png_base64"]) > 1000
    services.sink_pool.submit(lambda: None).result()  # drain async sink writes
    rows = ds.query("SELECT tool FROM studies.results r JOIN studies.models m USING (model_id) WHERE m.model_id = %(m)s",
                    {"m": env["data"]["model_id"]}, ["*"])
    assert {"power_flow", "der"} <= {r["tool"] for r in rows}


def test_capacity_restores_circuit(loaded, services, tmp_path):
    assert load(services, "cap", 123)["success"]
    s = services.store.get("cap")
    before, after = tmp_path / "b", tmp_path / "a"
    before.mkdir(), after.mkdir()
    engine.save_circuit(s.dss, str(before))
    env = call(services, "cap", capacity, capacity.AnalyzeCapacityInput, {"bus_mrid": "83", "max_kw": 2000}, with_services=True)
    assert env["success"] and env["data"]["binding_constraint"] in ("overvoltage", "thermal", "reverse_flow", None)
    assert env["data"]["curve"]
    engine.save_circuit(s.dss, str(after))
    for f in before.iterdir():
        assert f.read_text().splitlines()[1:] == (after / f.name).read_text().splitlines()[1:], f.name


def test_errors_are_typed(loaded, services):
    assert call(services, "fresh", power_flow, power_flow.PowerFlowInput, {})["errors"][0]["code"] == "no_model"
    assert load(services, "err", 13)["success"]
    bad_bus = call(services, "err", capacity, capacity.AnalyzeCapacityInput, {"bus_mrid": "nope"}, with_services=True)
    assert bad_bus["errors"][0]["code"] == "bus_not_found"
    nc = call(services, "err", power_flow, power_flow.PowerFlowInput, {"max_iterations": 1})
    assert nc["success"] is False and nc["data"] is None and nc["errors"][0]["code"] == "not_converged"
    wrong = load(services, "err", 13, case="planning")
    wrong_region = call(services, "err", load_feeder, load_feeder.LoadFeederInput,
                        {"root_mrid": str(mrid("ieee13", "breaker", "head")), "region_code": "IEEE34"},
                        needs_model=False, with_services=True)
    assert wrong["success"] and wrong_region["errors"][0]["code"] == "not_found"


def test_request_id_returns_cached_envelope(loaded, services):
    assert load(services, "rid", 13)["success"]
    a = call(services, "rid", power_flow, power_flow.PowerFlowInput, {"request_id": "r1"})
    time.sleep(0.01)
    b = call(services, "rid", power_flow, power_flow.PowerFlowInput, {"request_id": "r1"})
    assert a == b
