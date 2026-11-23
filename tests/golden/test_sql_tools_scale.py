# SPDX-License-Identifier: Apache-2.0
"""Size budget on SMART-DS and latency gates (marked slow) for the SQL tool family."""

import json
import os
import time

import pytest

from feederline_mcp.tools.sql import spatial as SP
from feederline_mcp.tools.sql import topology as T
from loaders.common import mrid
from tests.contract.test_sql_tools_schema import SQL_TOOLS

pytestmark = pytest.mark.db
REGION, SUB = os.environ.get("SMARTDS_REGION", "P1R"), os.environ.get("SMARTDS_SUBSTATION", "p1rhs0_1247")
SMARTDS_ROOT = str(mrid(f"smartds:{REGION}:{SUB}", "breaker", "head"))
R123 = str(mrid("ieee123", "breaker", "head"))
BUDGET = 200_000


@pytest.fixture(scope="module")
def smartds(ds):
    if not ds.query("SELECT 1 FROM conducting_equipment WHERE mrid = %(m)s", {"m": SMARTDS_ROOT}, ["*"]):
        pytest.skip(f"SMART-DS {REGION}/{SUB} not loaded")


@pytest.mark.parametrize("fn,cls,key", [(SP.feeder_geometry, SP.FeederGeometryInput, "feeder_mrid"),
                                        (T.trace_downstream, T.TraceDownstreamInput, "mrid")])
def test_aggregated_fits_the_budget_and_full_points_at_a_resource(smartds, sql_tool, fn, cls, key):
    agg = sql_tool(fn, cls, {key: SMARTDS_ROOT, "region_code": REGION})
    assert agg["success"] and len(json.dumps(agg)) < BUDGET
    full = sql_tool(fn, cls, {key: SMARTDS_ROOT, "region_code": REGION, "detail": "full"})
    assert len(json.dumps(full)) < BUDGET
    assert full["data"]["truncated"] is True and full["data"]["resource"].startswith(f"grid://feeder/{SMARTDS_ROOT}/")


@pytest.mark.slow
@pytest.mark.parametrize("tool", sorted(SQL_TOOLS))
def test_each_sql_tool_under_200ms_on_ieee123(loaded, sql_tool, tool):
    fn, cls, _, raw = SQL_TOOLS[tool]
    raw = {**raw, "region_code": "IEEE123"}
    if tool == "find_path":
        raw.update(from_mrid="83", to_mrid=R123)
    if tool in ("equipment_near", "nearest_substation", "affected_by_area"):
        bbox = sql_tool(SP.feeder_geometry, SP.FeederGeometryInput,
                        {"feeder_mrid": R123, "region_code": "IEEE123", "detail": "summary"})["data"]["bbox"]
        lat, lon = (bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2
        raw.update({"lat": lat, "lon": lon} if tool != "affected_by_area" else {"polygon": {"type": "Polygon", "coordinates": [
            [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]], [bbox[0], bbox[1]]]]}})
    if tool in ("describe_equipment", "latest_readings", "isolate", "trace_upstream"):
        raw["mrid"] = "L115"
    if tool == "search_equipment":
        raw["query"] = "L11"
    sql_tool(fn, cls, raw)  # warm
    t0 = time.perf_counter()
    env = sql_tool(fn, cls, raw)
    ms = (time.perf_counter() - t0) * 1000
    assert env["success"] or env["errors"][0]["code"] in ("not_found",), env["errors"]
    assert ms < 200, f"{tool}: {ms:.0f} ms"


@pytest.mark.slow
def test_trace_downstream_full_under_1s_on_smartds(smartds, sql_tool):
    raw = {"mrid": SMARTDS_ROOT, "region_code": REGION, "detail": "full", "limit": 5000}
    sql_tool(T.trace_downstream, T.TraceDownstreamInput, raw)
    t0 = time.perf_counter()
    assert sql_tool(T.trace_downstream, T.TraceDownstreamInput, raw)["success"]
    assert time.perf_counter() - t0 < 1.0


@pytest.mark.slow
def test_affected_by_area_under_2s_on_a_hifld_county(ds, sql_tool):
    box = ds.query("""SELECT ST_XMin(e) x0, ST_YMin(e) y0, ST_XMax(e) x1, ST_YMax(e) y1 FROM (
                      SELECT ST_Extent(l.geom) e FROM conducting_equipment ce JOIN location l ON l.mrid = ce.location_mrid
                      WHERE ce.region_code = 'CA-Kern') s""", {}, ["*"])[0]
    if box["x0"] is None:
        pytest.skip("HIFLD not loaded (make load-hifld STATE=CA)")
    poly = {"type": "Polygon", "coordinates": [[[box["x0"], box["y0"]], [box["x1"], box["y0"]], [box["x1"], box["y1"]],
                                                [box["x0"], box["y1"]], [box["x0"], box["y0"]]]]}
    t0 = time.perf_counter()
    env = sql_tool(SP.affected_by_area, SP.AffectedByAreaInput, {"polygon": poly, "region_code": "CA-Kern"})
    assert env["success"] and env["data"]["summary"]["segments"] > 100
    assert time.perf_counter() - t0 < 2.0
