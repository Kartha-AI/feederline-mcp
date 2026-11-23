# SPDX-License-Identifier: Apache-2.0
"""Telemetry tools on simulator data: IEEE 13 backfilled for one past UTC day, rollup refreshed, 96 buckets read."""

import os
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest

from feederline_mcp.adapters.base import Principal
from feederline_mcp.tools.sql import telemetry as TE
from loaders.common import mrid
from tests.conftest import WRITER_URL

pytestmark = pytest.mark.db
ADMIN_URL = os.environ.get("ADMIN_URL", "postgresql://grid:grid@localhost:5433/grid")
DAY = date(2026, 10, 20)
ROOT = str(mrid("ieee13", "breaker", "head"))


@pytest.fixture(scope="module")
def simulated(loaded):
    """Simulator samples (source 6) for every IEEE 13 measurement at 15-minute steps, then the rollup refresh."""
    from feederline_mcp.adapters.local import LocalDataSource
    from ingest.simulate import Simulator

    w = LocalDataSource(WRITER_URL)
    try:
        sim = Simulator(w, seed=7)
        sim.bases = {t: b for t, b in sim.bases.items() if t.startswith("IEEE13_")}
        sim.backfill(DAY, timedelta(minutes=15))
    finally:
        w.close()
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY measurement_value_15m")
    return DAY


def test_load_profile_has_96_buckets(simulated, sql_tool):
    env = sql_tool(TE.load_profile, TE.LoadProfileInput, {"feeder_mrid": ROOT, "region_code": "IEEE13",
                                                         "date": DAY.isoformat()})
    d = env["data"]
    assert env["success"] and d["viz"] == "timeseries" and len(d["ts"]) == 96 and d["interval"] == "15m"
    series = {s["name"]: s for s in d["series"]}
    assert len(series["Head P"]["values"]) == 96 and all(v is not None for v in series["Head P"]["values"])
    assert all(v is not None for v in series["Measured load P"]["values"])
    assert d["summary"]["peak_head_kw"] > 0 and set(d["summary"]["series_with_data"]) >= {"head_p", "head_q", "load_p"}


def test_measurement_history_and_peak_load(simulated, sql_tool):
    start = datetime.combine(DAY, datetime.min.time(), tzinfo=UTC)
    env = sql_tool(TE.measurement_history, TE.MeasurementHistoryInput,
                   {"measurement_mrids": ["IEEE13_671_P", "IEEE13_671_Q"], "region_code": "IEEE13",
                    "from": start.isoformat(), "to": (start + timedelta(days=1)).isoformat(), "stat": "max"})
    d = env["data"]
    assert env["success"] and len(d["ts"]) == 96 and len(d["series"]) == 2
    assert all(v is not None for s in d["series"] for v in s["values"]) and d["series"][0]["unit"] in ("kW", "kVAr")
    peak = sql_tool(TE.peak_load, TE.PeakLoadInput, {"feeder_mrid": ROOT, "region_code": "IEEE13",
                                                    "from": DAY.isoformat(), "to": DAY.isoformat()})["data"]
    row = peak["rows"][0]
    assert row["date"] == DAY.isoformat() and row["buckets"] == 96 and 0 < row["load_factor"] <= 1
    assert row["peak_kw"] >= row["min_kw"] > 0


def test_latest_readings_fresh_after_load(loaded, ds, sql_tool):
    env = sql_tool(TE.latest_readings, TE.LatestReadingsInput, {"feeder_mrid": ROOT, "region_code": "IEEE13"})
    n = ds.query("SELECT count(*) AS n FROM measurement WHERE region_code = 'IEEE13'", {}, ["*"])[0]["n"]
    rows = env["data"]["rows"]
    assert env["data"]["viz"] == "table" and len(rows) == n
    assert all(r["stale"] is False and r["quality_flags"] == [] for r in rows)
    assert all(r["feeder_name"] == "IEEE13 Head" and r["mrid"] and r["name"] and r["cim_class"] for r in rows)


def test_limits_and_quality(loaded, sql_tool):
    lim = sql_tool(TE.limit_exceedances, TE.LimitExceedancesInput, {"region_code": "IEEE13"})
    assert lim["success"] and lim["data"]["viz"] == "table"
    q = sql_tool(TE.telemetry_quality, TE.TelemetryQualityInput, {"feeder_mrid": ROOT, "region_code": "IEEE13",
                                                                 "days": 1})
    assert q["success"] and all({"expected", "received", "longest_gap_s", "stale_now"} <= set(r)
                                for r in q["data"]["rows"])


def test_region_b_measurements_are_forbidden(loaded, sql_tool):
    a = Principal("a", ["IEEE13"])
    env = sql_tool(TE.latest_readings, TE.LatestReadingsInput,
                   {"feeder_mrid": str(mrid("ieee123", "breaker", "head")), "region_code": "IEEE123"}, principal=a)
    assert env["errors"][0]["code"] == "forbidden"
