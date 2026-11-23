# SPDX-License-Identifier: Apache-2.0
"""Phase-2 engine tools through run_tool on the IEEE feeders: controls, case comparison, faults, balance, losses,
loading map, regulators, visualization data."""

import base64

import psycopg
import pytest

from feederline_mcp import engine
from feederline_mcp.adapters.base import Principal
from feederline_mcp.tools._runner import run_tool
from feederline_mcp.tools.engine import (
    compare_cases,
    fault_study,
    load_feeder,
    loading,
    losses,
    phase_balance,
    power_flow,
    regulators,
    set_control,
    viz,
)
from feederline_mcp.tools.sql import spatial
from loaders.common import mrid
from tests.conftest import LOADER_URL

pytestmark = pytest.mark.db
P = Principal("tester", ["*"])


def call(services, sid, mod, cls, raw):
    body = mod.execute if mod is power_flow else (
        (lambda s, a: mod.execute(s, a, services)) if mod is viz else (lambda s, a: mod.execute(services, s, a)))
    return run_tool(services, sid, P, mod.__name__.rsplit(".", 1)[-1], cls, raw, body)


def load(services, sid, n):
    raw = {"root_mrid": str(mrid(f"ieee{n}", "breaker", "head")), "region_code": f"IEEE{n}"}
    env = run_tool(services, sid, P, "load_feeder", load_feeder.LoadFeederInput, raw,
                   lambda s, a: load_feeder.execute(services, s, a), needs_model=False)
    assert env["success"]
    return env


def switch_row(name, region):
    with psycopg.connect(LOADER_URL) as conn:
        return conn.execute("SELECT s.open, s.normal_open, s.open_ts FROM switch s JOIN conducting_equipment ce "
                            "USING (mrid) WHERE ce.name = %s AND ce.region_code = %s", (name, region)).fetchone()


def test_set_control_open_head_breaker(loaded, services):
    load(services, "p2-head", 13)
    before = switch_row("IEEE13 Head", "IEEE13")
    sc = call(services, "p2-head", set_control, set_control.SetControlInput,
              {"actions": [{"mrid": "IEEE13 Head", "kind": "open"}]})
    assert sc["model"]["modified"] is True and sc["data"]["rows"][0]["cim_class"] == "Switch"
    pf = call(services, "p2-head", power_flow, power_flow.PowerFlowInput, {})
    if pf["success"]:
        assert abs(pf["data"]["total_load_kw"]) < 1.0 and pf["model"]["modified"] is True
    else:
        assert pf["errors"][0]["code"] == "not_converged"
    assert switch_row("IEEE13 Head", "IEEE13") == before


def test_set_control_switch_tap_and_capacitor(loaded, services):
    load(services, "p2-ctl", 13)
    base = call(services, "p2-ctl", power_flow, power_flow.PowerFlowInput, {})["data"]["total_load_kw"]
    sw = call(services, "p2-ctl", set_control, set_control.SetControlInput,
              {"actions": [{"mrid": "671692", "kind": "open"}]})
    assert sw["success"] and sw["data"]["summary"]["total_load_kw"] < base - 500
    bad = call(services, "p2-ctl", set_control, set_control.SetControlInput,
               {"actions": [{"mrid": "650632", "kind": "open"}]})
    assert bad["errors"][0]["code"] == "invalid_input"
    tc = call(services, "p2-ctl", set_control, set_control.SetControlInput,
              {"actions": [{"mrid": "REG1", "kind": "tap", "value": 2}, {"mrid": "CAP1", "kind": "cap", "value": 0}]})
    assert tc["success"] and tc["data"]["summary"]["control_mode"] == "off" and len(tc["data"]["rows"]) == 2
    regs = {r["name"]: r for r in call(services, "p2-ctl", regulators, regulators.RegulatorStatusInput, {})["data"]["rows"]}
    assert regs["REG1"]["tap_number"] == 2 and regs["REG1"]["delta_taps"] == 2 - regs["REG1"]["measured_tap"]


def test_compare_cases(loaded, services):
    load(services, "p2-cmp", 13)
    plan = call(services, "p2-cmp", compare_cases, compare_cases.CompareCasesInput, {"baseline": "planning"})
    rows = {r["metric"]: r for r in plan["data"]["rows"]}
    assert plan["success"] and rows["converged"]["baseline"] is True and rows["losses_kw"]["delta"] is not None
    opened = call(services, "p2-cmp", compare_cases, compare_cases.CompareCasesInput,
                  {"baseline": "current", "actions": [{"mrid": "671692", "kind": "open"}]})
    rows = {r["metric"]: r for r in opened["data"]["rows"]}
    assert rows["total_load_kw"]["delta"] < -500 and opened["data"]["buses_changed"]
    assert opened["model"]["modified"] is False  # the session circuit is untouched
    pf = call(services, "p2-cmp", power_flow, power_flow.PowerFlowInput, {})
    assert 3400 < pf["data"]["total_load_kw"] < 3700


def test_fault_study_restores_circuit(loaded, services, tmp_path):
    load(services, "p2-flt", 13)
    s = services.store.get("p2-flt")
    before, after = tmp_path / "b", tmp_path / "a"
    before.mkdir(), after.mkdir()
    engine.save_circuit(s.dss, str(before))
    env = call(services, "p2-flt", fault_study, fault_study.FaultStudyInput, {"bus": "671", "fault_type": "3ph"})
    kinds = {}
    for r in env["data"]["rows"]:
        kinds.setdefault(r["kind"], []).append(r)
    assert env["success"] and len(kinds["bolted_isc"]) == 3 and len(kinds["fault_current"]) == 3
    assert {r["name"] for r in kinds["through_current"]} == {"IEEE13 Head"}
    assert all(r["current_a"] > 1000 for r in kinds["bolted_isc"])
    slg = call(services, "p2-flt", fault_study, fault_study.FaultStudyInput,
               {"bus": "671", "fault_type": "slg", "impedance_ohm": 5})
    assert slg["data"]["summary"]["max_fault_current_a"] < env["data"]["summary"]["max_fault_current_a"]
    engine.save_circuit(s.dss, str(after))
    for f in before.iterdir():
        assert f.read_text().splitlines()[1:] == (after / f.name).read_text().splitlines()[1:], f.name


def test_balance_losses_regulators(loaded, services):
    load(services, "p2-rep", 123)
    pb = call(services, "p2-rep", phase_balance, phase_balance.PhaseBalanceInput, {})["data"]
    assert pb["rows"][0]["location"] == "feeder head" and 1 < len(pb["rows"]) <= 11
    assert sum(pb["rows"][0][k] for k in ("kw_a", "kw_b", "kw_c")) == pytest.approx(3615, rel=0.05)
    ls = call(services, "p2-rep", losses, losses.LossesInput, {"top_n": 5})["data"]
    assert len(ls["rows"]) == 5 and ls["rows"][0]["losses_kw"] >= ls["rows"][-1]["losses_kw"]
    assert sum(r["losses_kw"] for r in ls["rows"]) <= ls["summary"]["feeder_losses_kw"]
    rg = call(services, "p2-rep", regulators, regulators.RegulatorStatusInput, {})["data"]
    assert len(rg["rows"]) == 7 and all(r["delta_taps"] == 0 for r in rg["rows"])


def test_line_loading_map_recolours_feeder_geometry(loaded, services, sql_tool):
    load(services, "p2-map", 123)
    table = call(services, "p2-map", loading, loading.LineLoadingInput, {"threshold_pct": 100})["data"]
    assert table["viz"] == "table" and all(r["loading_pct"] >= 100 for r in table["rows"])
    m = call(services, "p2-map", loading, loading.LineLoadingInput, {"as": "map"})["data"]
    geo = sql_tool(spatial.feeder_geometry, spatial.FeederGeometryInput,
                   {"feeder_mrid": "IEEE123 Head", "region_code": "IEEE123"})["data"]
    assert m["viz"] == "map" and {f["properties"]["mrid"] for f in m["features"]} == \
        {f["properties"]["mrid"] for f in geo["features"]}
    segs = [f for f in m["features"] if f["properties"]["cim_class"] == "ACLineSegment"]
    assert segs and all(f["properties"]["loading_pct"] is not None and f["properties"]["v_pu"] for f in segs)


def test_visualization_data_and_png_unchanged(loaded, services):
    load(services, "p2-viz", 13)
    prof = call(services, "p2-viz", viz, viz.VisualizationInput, {"plot": "voltage_profile", "as": "data"})["data"]
    assert prof["viz"] == "profile" and prof["x_unit"] == "km" and len(prof["points"]) > 30
    assert prof["points"][0]["distance_km"] == 0 and max(p["distance_km"] for p in prof["points"]) > 1
    lm = call(services, "p2-viz", viz, viz.VisualizationInput, {"plot": "loading", "as": "data"})["data"]
    assert lm["viz"] == "map"
    a = call(services, "p2-viz", viz, viz.VisualizationInput, {"plot": "loading", "top_n": 5})["data"]
    b = call(services, "p2-viz", viz, viz.VisualizationInput, {"plot": "loading", "top_n": 5, "as": "png"})["data"]
    assert a == b and base64.b64decode(a["image_png_base64"])[:4] == b"\x89PNG"
    assert set(a) == {"plot", "image_png_base64", "caption"}
