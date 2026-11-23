# SPDX-License-Identifier: Apache-2.0
"""HIFLD gate: spatial (radius) queries < 1 s with RLS on, deterministic mRIDs, no orphan terminals, every line
part snapped or reported. Needs `make load-hifld STATE=CA` (skipped otherwise)."""

import time

import pytest

from feederline_mcp.compiler import queries
from loaders.common import mrid

pytestmark = pytest.mark.db
SF = {"lat": 37.7749, "lon": -122.4194, "radius_m": 50_000, "limit": 50}


@pytest.fixture(scope="module")
def hifld(ds):
    n = ds.query("SELECT count(*) AS n FROM substation WHERE gis_oid LIKE 'hifld:%%'", {}, ["*"])[0]["n"]
    if n == 0:
        pytest.skip("HIFLD not loaded (make load-hifld STATE=CA)")
    return n


def test_mrids_match_python_uuid5(hifld, ds):
    row = ds.query("SELECT mrid, gis_oid FROM substation WHERE gis_oid LIKE 'hifld:%%' LIMIT 1", {}, ["*"])[0]
    assert row["mrid"] == mrid("hifld", "substation", row["gis_oid"].split(":", 2)[2])


def test_no_orphan_terminals_and_two_terminals_per_segment(hifld, ds):
    orphans = ds.query("""
        SELECT count(*) AS n FROM terminal t
        LEFT JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
        WHERE cn.mrid IS NULL""", {}, ["*"])[0]["n"]
    assert orphans == 0
    bad = ds.query("""
        SELECT count(*) AS n FROM conducting_equipment ce
        WHERE ce.gis_oid LIKE 'hifld:line:%%'
          AND (SELECT count(*) FROM terminal t WHERE t.conducting_equipment_mrid = ce.mrid) <> 2""", {}, ["*"])[0]["n"]
    assert bad == 0


def test_transmission_edges_built(hifld, ds):
    rows = ds.query("SELECT count(*) AS n FROM network_edges WHERE grid_tier = 'Transmission'", {}, ["*"])
    lines = ds.query("SELECT count(*) AS n FROM conducting_equipment WHERE gis_oid LIKE 'hifld:line:%%'", {}, ["*"])
    assert rows[0]["n"] == lines[0]["n"] > 1000


@pytest.mark.parametrize("regions", [["*"], ["CA-SanFrancisco", "CA-SanMateo"]])
def test_radius_query_under_one_second_with_rls(hifld, ds, regions):
    ds.query(queries.SUBSTATIONS_NEAR, SF, regions)  # warm
    t0 = time.perf_counter()
    subs = ds.query(queries.SUBSTATIONS_NEAR, SF, regions)
    segs = ds.query(queries.SEGMENTS_NEAR, SF, regions)
    assert time.perf_counter() - t0 < 1.0
    assert subs and segs
    assert subs == sorted(subs, key=lambda r: r["km"]) and all(r["km"] <= 50 for r in subs)
    if regions != ["*"]:
        assert {r["region_code"] for r in subs} <= set(regions)
        assert {r["region_code"] for r in segs} <= set(regions)


def test_rls_hides_other_regions(hifld, ds):
    assert ds.query(queries.SUBSTATIONS_NEAR, SF, ["CA-Fresno"]) == []


# --- spatial SQL tools on the IEEE feeders -------------------------------------------------------------------
from feederline_mcp.tools.sql import spatial as SP  # noqa: E402

IEEE13_ROOT = str(mrid("ieee13", "breaker", "head"))


def _box(bbox, pad):
    x0, y0, x1, y1 = bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad
    return {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}


def test_feeder_geometry_aggregated_ieee123(loaded, ds, sql_tool):
    env = sql_tool(SP.feeder_geometry, SP.FeederGeometryInput, {"feeder_mrid": "IEEE123 Head", "region_code": "IEEE123"})
    d = env["data"]
    assert env["success"] and d["viz"] == "map" and d["type"] == "FeatureCollection" and len(d["bbox"]) == 4
    want = ds.query("SELECT mrid::text AS m FROM conducting_equipment WHERE region_code = 'IEEE123' "
                    "AND cim_class IN ('ACLineSegment', 'LoadBreakSwitch', 'Breaker')", {}, ["*"])
    got = [f["properties"]["mrid"] for f in d["features"]]
    assert len(got) == len(set(got)) and {r["m"] for r in want} <= set(got)
    for f in d["features"]:
        g = f["geometry"]
        assert g["type"] in ("Point", "LineString") and g["coordinates"]
        assert {"mrid", "name", "cim_class", "feeder_mrid", "energised", "loading_pct", "v_pu"} <= set(f["properties"])
        assert f["properties"]["loading_pct"] is None and f["properties"]["cim_class"] != "EnergyConsumer"
    x0, y0, x1, y1 = d["bbox"]
    assert x0 < x1 and y0 < y1


def test_affected_by_area(loaded, sql_tool):
    bbox = sql_tool(SP.feeder_geometry, SP.FeederGeometryInput,
                    {"feeder_mrid": IEEE13_ROOT, "region_code": "IEEE13", "detail": "summary"})["data"]["bbox"]
    env = sql_tool(SP.affected_by_area, SP.AffectedByAreaInput, {"polygon": _box(bbox, 0.001), "region_code": "IEEE13"})
    d = env["data"]
    assert env["success"] and d["summary"]["customers"] == 15 and d["summary"]["segments"] == 11
    assert d["features"][0]["properties"]["cim_class"] == "Area"
    assert {f["properties"].get("role") for f in d["features"][1:]} >= {"isolate", "in_area"}
    empty = sql_tool(SP.affected_by_area, SP.AffectedByAreaInput,
                     {"polygon": _box([0, 0, 0.01, 0.01], 0), "region_code": "IEEE13"})
    assert empty["success"] and empty["data"]["features"] == [] and empty["data"]["summary"]["customers"] == 0


def test_equipment_near_and_nearest_substation(loaded, sql_tool):
    bbox = sql_tool(SP.feeder_geometry, SP.FeederGeometryInput,
                    {"feeder_mrid": IEEE13_ROOT, "region_code": "IEEE13", "detail": "summary"})["data"]["bbox"]
    lat, lon = (bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2
    near = sql_tool(SP.equipment_near, SP.EquipmentNearInput,
                    {"lat": lat, "lon": lon, "radius_m": 2000, "region_code": "IEEE13"})["data"]
    dist = [f["properties"]["distance_m"] for f in near["features"]]
    assert near["viz"] == "map" and dist == sorted(dist) and len(dist) == 35
    assert all(f["properties"]["feeder_name"] == "IEEE13 Head" for f in near["features"])
    subs = sql_tool(SP.nearest_substation, SP.NearestSubstationInput, {"lat": lat, "lon": lon, "region_code": "IEEE13"})
    p = subs["data"]["features"][0]["properties"]
    assert p["name"] == "IEEE13 Substation" and p["feeders"] == 1 and p["voltages_kv"]
