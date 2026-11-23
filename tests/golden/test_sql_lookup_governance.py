# SPDX-License-Identifier: Apache-2.0
"""search_equipment, describe_equipment, resolve precedence in SQL, model_changes and validate_feeder."""

import pytest

from feederline_mcp.adapters.base import Principal
from feederline_mcp.compiler import compile_feeder
from feederline_mcp.studies import ToolError
from feederline_mcp.tools.sql import governance as G
from feederline_mcp.tools.sql import lookup as L
from feederline_mcp.tools.sql.resolve import resolve_mrid
from loaders.common import mrid

pytestmark = pytest.mark.db
ALL = Principal("tester", ["*"])
LOAD_671 = mrid("ieee13", "load", "671")


def test_search_finds_load_671_first(loaded, sql_tool):
    env = sql_tool(L.search_equipment, L.SearchEquipmentInput, {"query": "671", "region_code": "IEEE13"})
    top = env["data"]["rows"][0]
    assert env["data"]["viz"] == "table" and top["name"] == "671" and top["score"] == 0.9
    assert top["feeder_name"] == "IEEE13 Head" and top["substation_name"] == "IEEE13 Substation"
    assert all({"mrid", "name", "cim_class"} <= set(r) for r in env["data"]["rows"])
    tag = sql_tool(L.search_equipment, L.SearchEquipmentInput, {"query": "IEEE13_671_P", "region_code": "IEEE13"})
    assert tag["data"]["rows"][0]["mrid"] == str(LOAD_671) and tag["data"]["rows"][0]["score"] == 1.0


@pytest.mark.parametrize("ref", [str(LOAD_671), "ieee13:load:671", "IEEE13_671_P", "671"])
def test_resolve_precedence_in_sql(loaded, ds, ref):
    assert resolve_mrid(ds, ALL, ref, region_code="IEEE13") == LOAD_671


def test_resolve_trigram_and_ambiguity(loaded, ds):
    assert resolve_mrid(ds, ALL, "IEEE13 Hea", region_code="IEEE13") == mrid("ieee13", "breaker", "head")
    with pytest.raises(ToolError) as e:
        resolve_mrid(ds, ALL, "L1")  # a segment named L1 in both IEEE34 and IEEE123
    assert e.value.code == "ambiguous" and {c["region_code"] for c in e.value.extra["candidates"]} == {"IEEE34", "IEEE123"}
    assert resolve_mrid(ds, ALL, "L1", region_code="IEEE34") == mrid("ieee34", "line", "l1")
    with pytest.raises(ToolError) as nf:
        resolve_mrid(ds, ALL, "no such device", region_code="IEEE13")
    assert nf.value.code == "not_found"


def test_describe_equipment(loaded, sql_tool):
    d = sql_tool(L.describe_equipment, L.DescribeEquipmentInput, {"mrid": "671", "region_code": "IEEE13"})["data"]
    row = d["rows"][0]
    assert row["cim_class"] == "EnergyConsumer" and row["feeder_name"] == "IEEE13 Head" and "customer_count" in row
    assert d["terminals"][0]["connectivity_node"]["name"] == "671"
    assert {o["name"] for o in d["terminals"][0]["other_devices"]} >= {"670671", "671680", "671684"}
    assert {m["measurement_type"] for m in d["measurements"]} == {"ThreePhaseActivePower", "ThreePhaseReactivePower"}
    assert all(m["stale"] is False and m["unit"] in ("kW", "kVAr") for m in d["measurements"])
    assert d["location"]["geometry"]["type"] == "Point"
    xf = sql_tool(L.describe_equipment, L.DescribeEquipmentInput, {"mrid": "XFM1", "region_code": "IEEE13"})["data"]
    assert [e["end_number"] for e in xf["ends"]] == [1, 2]


@pytest.mark.parametrize("n", [13, 34, 123])
@pytest.mark.parametrize("case", ["operational", "planning"])
def test_validate_counts_equal_compile_report(loaded, ds, sql_tool, n, case):
    root = mrid(f"ieee{n}", "breaker", "head")
    m = compile_feeder(ds, ALL, root, case, f"IEEE{n}").model
    env = sql_tool(G.validate_feeder, G.ValidateFeederInput, {"feeder_mrid": str(root), "region_code": f"IEEE{n}",
                                                             "case": case})
    rows = {r["kind"]: r["count"] for r in env["data"]["rows"]}
    assert rows["defaulted_impedance"] == m.defaulted_impedances
    assert rows["load_defaulted"] == m.loads_defaulted
    if case == "operational":
        assert rows["stale_switch"] == m.stale_switches


def test_model_changes_since_time_and_study(loaded, services, sql_tool):
    root = str(mrid("ieee13", "breaker", "head"))
    env = sql_tool(G.model_changes, G.ModelChangesInput, {"feeder_mrid": root, "region_code": "IEEE13",
                                                         "since": "2000-01-01T00:00:00Z"})
    assert env["data"]["summary"]["updated"] == 35 and env["data"]["summary"]["since_kind"] == "timestamp"
    none = sql_tool(G.model_changes, G.ModelChangesInput, {"feeder_mrid": root, "region_code": "IEEE13",
                                                          "since": "2999-01-01T00:00:00Z"})
    assert none["data"]["rows"] == []
    from feederline_mcp.tools._runner import run_tool
    from feederline_mcp.tools.engine import load_feeder, power_flow

    assert run_tool(services, "mc", ALL, "load_feeder", load_feeder.LoadFeederInput,
                    {"root_mrid": root, "region_code": "IEEE13"}, lambda s, a: load_feeder.execute(services, s, a),
                    needs_model=False)["success"]
    pf = run_tool(services, "mc", ALL, "power_flow", power_flow.PowerFlowInput, {}, power_flow.execute)
    services.sink_pool.submit(lambda: None).result()
    by_study = sql_tool(G.model_changes, G.ModelChangesInput, {"feeder_mrid": root, "region_code": "IEEE13",
                                                              "since": pf["meta"]["study_id"]})
    assert by_study["success"] and by_study["data"]["summary"]["since_kind"] == "study"
    assert by_study["data"]["rows"] == []  # nothing changed since that study's model was compiled
