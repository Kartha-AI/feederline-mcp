# SPDX-License-Identifier: Apache-2.0
"""Contract for the SQL tool family and the phase-2 engine tools: output schemas, input/shape JSON-schema snapshot
(UPDATE_SNAPSHOTS=1 to refresh), data.viz on every SQL result, and region isolation on every SQL tool."""

import asyncio
import json
import os
from datetime import date
from pathlib import Path

import pytest

from feederline_mcp import shapes
from feederline_mcp.adapters.base import Principal
from feederline_mcp.studies import Envelope
from feederline_mcp.tools.engine import (
    compare_cases,
    fault_study,
    loading,
    losses,
    phase_balance,
    regulators,
    set_control,
)
from feederline_mcp.tools.sql import governance as G
from feederline_mcp.tools.sql import lookup as L
from feederline_mcp.tools.sql import spatial as SP
from feederline_mcp.tools.sql import telemetry as TE
from feederline_mcp.tools.sql import topology as T
from loaders.common import mrid

SNAPSHOT = Path(__file__).parent / "sql_tools_schemas.json"
R13 = str(mrid("ieee13", "breaker", "head"))
R123 = str(mrid("ieee123", "breaker", "head"))
BOX = {"type": "Polygon", "coordinates": [[[-117.3, 32.7], [-117.1, 32.7], [-117.1, 32.9], [-117.3, 32.9], [-117.3, 32.7]]]}

# tool -> (body, input class, raw for IEEE13, raw aimed at IEEE123 for the region test)
SQL_TOOLS = {
    "search_equipment": (L.search_equipment, L.SearchEquipmentInput, {"query": "671"}, {"query": "SW1"}),
    "describe_equipment": (L.describe_equipment, L.DescribeEquipmentInput, {"mrid": "671"}, {"mrid": R123}),
    "trace_downstream": (T.trace_downstream, T.TraceDownstreamInput, {"mrid": R13}, {"mrid": R123}),
    "trace_upstream": (T.trace_upstream, T.TraceUpstreamInput, {"mrid": "671"}, {"mrid": R123}),
    "find_path": (T.find_path, T.FindPathInput, {"from_mrid": "671", "to_mrid": "611"},
                  {"from_mrid": R123, "to_mrid": R123}),
    "isolate": (T.isolate, T.IsolateInput, {"mrid": "692675"}, {"mrid": R123}),
    "tie_options": (T.tie_options, T.TieOptionsInput, {"feeder_mrid": R13}, {"feeder_mrid": R123}),
    "connectivity_report": (T.connectivity_report, T.ConnectivityReportInput, {"feeder_mrid": R13},
                            {"feeder_mrid": R123}),
    "equipment_near": (SP.equipment_near, SP.EquipmentNearInput, {"lat": 32.8, "lon": -117.2}, {"lat": 32.8, "lon": -117.0}),
    "nearest_substation": (SP.nearest_substation, SP.NearestSubstationInput, {"lat": 32.8, "lon": -117.2},
                           {"lat": 32.8, "lon": -117.0}),
    "affected_by_area": (SP.affected_by_area, SP.AffectedByAreaInput, {"polygon": BOX}, {"polygon": BOX}),
    "feeder_geometry": (SP.feeder_geometry, SP.FeederGeometryInput, {"feeder_mrid": R13}, {"feeder_mrid": R123}),
    "latest_readings": (TE.latest_readings, TE.LatestReadingsInput, {"mrid": "671"}, {"mrid": R123}),
    "measurement_history": (TE.measurement_history, TE.MeasurementHistoryInput,
                            {"measurement_mrids": ["IEEE13_671_P"], "from": "2026-10-20T00:00:00Z",
                             "to": "2026-10-21T00:00:00Z"},
                            {"measurement_mrids": ["IEEE123_HEAD_P"], "from": "2026-10-20T00:00:00Z",
                             "to": "2026-10-21T00:00:00Z"}),
    "load_profile": (TE.load_profile, TE.LoadProfileInput, {"feeder_mrid": R13, "date": "2026-10-20"},
                     {"feeder_mrid": R123, "date": "2026-10-20"}),
    "peak_load": (TE.peak_load, TE.PeakLoadInput, {"feeder_mrid": R13, "from": "2026-10-20", "to": "2026-10-21"},
                  {"feeder_mrid": R123, "from": "2026-10-20", "to": "2026-10-21"}),
    "limit_exceedances": (TE.limit_exceedances, TE.LimitExceedancesInput, {}, {}),
    "telemetry_quality": (TE.telemetry_quality, TE.TelemetryQualityInput, {"feeder_mrid": R13, "days": 1},
                          {"feeder_mrid": R123, "days": 1}),
    "model_changes": (G.model_changes, G.ModelChangesInput, {"feeder_mrid": R13, "since": "2026-01-01T00:00:00Z"},
                      {"feeder_mrid": R123, "since": "2026-01-01T00:00:00Z"}),
    "validate_feeder": (G.validate_feeder, G.ValidateFeederInput, {"feeder_mrid": R13}, {"feeder_mrid": R123}),
}
ENGINE_TOOLS = {
    "set_control": set_control.SetControlInput, "compare_cases": compare_cases.CompareCasesInput,
    "fault_study": fault_study.FaultStudyInput, "phase_balance": phase_balance.PhaseBalanceInput,
    "losses_by_segment": losses.LossesInput, "line_loading": loading.LineLoadingInput,
    "regulator_status": regulators.RegulatorStatusInput,
}


def registered():
    os.environ["FEEDERLINE_MCP_NO_APP"] = "1"
    from mcp.server.fastmcp import FastMCP

    from feederline_mcp.tools import register_all
    from feederline_mcp.tools._runner import AreaCache

    class Stub:
        areas, cache = AreaCache(), {}

    m = FastMCP("contract")
    register_all(m, Stub())
    return {t.name: t for t in asyncio.run(m.list_tools())}


def test_every_new_tool_declares_an_output_schema():
    tools = registered()
    for name in [*SQL_TOOLS, *ENGINE_TOOLS]:
        schema = tools[name].outputSchema
        assert schema and set(schema["properties"]) == {"success", "data", "model", "warnings", "errors", "meta"}, name
    assert tools["measurement_history"].inputSchema["required"] == ["measurement_mrids", "region_code", "from", "to"]
    assert "as" in tools["create_visualization"].inputSchema["properties"]
    assert "as" in tools["line_loading"].inputSchema["properties"]


def test_json_schema_snapshot():
    current = {
        "inputs": {k: v[1].model_json_schema() for k, v in SQL_TOOLS.items()}
        | {k: v.model_json_schema() for k, v in ENGINE_TOOLS.items()},
        "shapes": {k: v.model_json_schema(by_alias=True) for k, v in shapes.SHAPES.items()},
        "envelope": Envelope.model_json_schema(),
    }
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
    assert json.loads(SNAPSHOT.read_text()) == json.loads(json.dumps(current))


@pytest.mark.db
@pytest.mark.parametrize("tool", sorted(SQL_TOOLS))
def test_every_sql_result_names_its_shape(loaded, sql_tool, tool):
    fn, cls, raw, _ = SQL_TOOLS[tool]
    env = sql_tool(fn, cls, {**raw, "region_code": "IEEE13"})
    assert env["success"], env["errors"]
    assert env["data"]["viz"] in shapes.SHAPES and "truncated" in env["data"] and "resource" in env["data"]
    assert env["model"] is None and env["meta"]["engine"] is None
    shapes.SHAPES[env["data"]["viz"]].model_validate(env["data"])


@pytest.mark.db
@pytest.mark.parametrize("tool", sorted(SQL_TOOLS))
@pytest.mark.parametrize("region", ["IEEE13", "IEEE123"])
def test_region_b_is_forbidden_for_every_sql_tool(loaded, sql_tool, tool, region):
    fn, cls, _, raw_b = SQL_TOOLS[tool]
    env = sql_tool(fn, cls, {**raw_b, "region_code": region}, principal=Principal("a", ["IEEE13"]))
    if region == "IEEE13" and tool in ("search_equipment", "equipment_near", "nearest_substation", "affected_by_area",
                                       "limit_exceedances", "measurement_history"):
        # region-A searches see nothing of region B: empty, or the B-only reference does not resolve
        assert env["success"] or env["errors"][0]["code"] == "not_found"
        if env["success"]:
            assert all("IEEE123" not in json.dumps(r) for r in env["data"].get("rows", []))
        return
    assert env["success"] is False and env["errors"][0]["code"] == "forbidden", (tool, region, env["errors"])


def test_dates_parse():
    assert TE.LoadProfileInput(feeder_mrid="x", region_code="R", date="2026-10-20").date == date(2026, 10, 20)
