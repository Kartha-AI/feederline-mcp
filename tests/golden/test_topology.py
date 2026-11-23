# SPDX-License-Identifier: Apache-2.0
"""Topology tools against the loaded IEEE feeders: traces agree with the compiler scope, paths, isolation, RLS."""

from contextlib import contextmanager

import psycopg
import pytest

from feederline_mcp.adapters.base import Principal
from feederline_mcp.compiler.scope import trace
from feederline_mcp.tools.sql import topology as T
from loaders.common import mrid
from tests.conftest import LOADER_URL

pytestmark = pytest.mark.db
ALL = Principal("tester", ["*"])
ROOT = {n: mrid(f"ieee{n}", "breaker", "head") for n in (13, 34, 123)}


@contextmanager
def switches_open(names, region, normal_too=False):
    """Open switches in the CIM (as the loader role) for one test, then put every column back."""
    with psycopg.connect(LOADER_URL, autocommit=True) as conn:
        q = ("SELECT s.mrid, s.open, s.normal_open, s.open_ts FROM switch s JOIN conducting_equipment ce USING (mrid) "
             "WHERE ce.region_code = %s AND ce.name = ANY(%s)")
        before = conn.execute(q, (region, names)).fetchall()
        assert len(before) == len(names)
        for m, *_ in before:
            conn.execute("UPDATE switch SET open = true, normal_open = normal_open OR %s WHERE mrid = %s", (normal_too, m))
        try:
            yield
        finally:
            for m, op, no, ts in before:
                conn.execute("UPDATE switch SET open = %s, normal_open = %s, open_ts = %s WHERE mrid = %s", (op, no, ts, m))


def tree_ids(env):
    return {env["data"]["root"]["mrid"]} | {n["mrid"] for n in env["data"]["nodes"]}


@pytest.mark.parametrize("n", [13, 34, 123])
@pytest.mark.parametrize("case", ["operational", "planning"])
def test_trace_downstream_equals_compiler_scope(loaded, ds, sql_tool, n, case):
    env = sql_tool(T.trace_downstream, T.TraceDownstreamInput,
                   {"mrid": str(ROOT[n]), "region_code": f"IEEE{n}", "case": case, "detail": "full", "limit": 5000})
    assert env["success"] and env["data"]["viz"] == "tree" and env["model"] is None and env["meta"]["engine"] is None
    assert tree_ids(env) == {str(m) for m in trace(ds, ALL, ROOT[n], case, f"IEEE{n}").mrids}
    assert env["data"]["summary"]["customers"] == sum(1 for x in env["data"]["nodes"] if x["cim_class"] == "EnergyConsumer")


def test_normally_open_ties_are_boundaries_not_traversed(loaded, ds, sql_tool):
    with switches_open(["SW7", "SW8"], "IEEE123", normal_too=True):
        for case in ("operational", "planning"):
            env = sql_tool(T.trace_downstream, T.TraceDownstreamInput,
                           {"mrid": str(ROOT[123]), "region_code": "IEEE123", "case": case, "detail": "full",
                            "limit": 5000})
            assert tree_ids(env) == {str(m) for m in trace(ds, ALL, ROOT[123], case, "IEEE123").mrids}
            ties = [x for x in env["data"]["nodes"] if x["name"] in ("SW7", "SW8")]
            assert len(ties) == 2 and all(t["open"] and t["kind"] == "boundary" for t in ties)
            assert all(t["customers_downstream"] == 0 for t in ties)


def test_detail_levels(loaded, sql_tool):
    raw = {"mrid": str(ROOT[123]), "region_code": "IEEE123"}
    s = sql_tool(T.trace_downstream, T.TraceDownstreamInput, {**raw, "detail": "summary"})["data"]
    a = sql_tool(T.trace_downstream, T.TraceDownstreamInput, raw)["data"]
    assert s["nodes"] == [] and s["summary"]["customers"] == 91
    assert {x["cim_class"] for x in a["nodes"]} <= {"LoadBreakSwitch", "Breaker", "PowerTransformer", "Fuse",
                                                    "Recloser", "Disconnector"}
    assert sum(1 for x in a["nodes"] if x["cim_class"] == "LoadBreakSwitch") == 8
    capped = sql_tool(T.trace_downstream, T.TraceDownstreamInput, {**raw, "detail": "full", "limit": 10})["data"]
    assert len(capped["nodes"]) == 10 and capped["truncated"] and capped["resource"].startswith("grid://feeder/")


def test_trace_upstream_from_load_671_ends_at_head_breaker(loaded, sql_tool):
    env = sql_tool(T.trace_upstream, T.TraceUpstreamInput, {"mrid": "671", "region_code": "IEEE13"})
    d = env["data"]
    assert env["success"] and d["viz"] == "path"
    names = [h["name"] for h in d["hops"]]
    assert names[0] == "671" and names[-1] == "IEEE13 Head"
    assert names[1:4] == ["670671", "632670", "650632"]
    assert [h["name"] for h in d["hops"] if h["protective"]] == ["IEEE13 Head"]
    assert d["summary"]["feeder_name"] == "IEEE13 Head" and d["summary"]["km"] == pytest.approx(1.219, abs=0.01)
    assert d["summary"]["r_ohm"] > 0 and d["summary"]["x_ohm"] > 0


def test_find_path_across_an_open_switch(loaded, sql_tool):
    raw = {"from_mrid": "671", "to_mrid": "675A", "region_code": "IEEE13"}
    ok = sql_tool(T.find_path, T.FindPathInput, raw)["data"]
    assert ok["summary"]["connected"] and "671692" in [h["name"] for h in ok["hops"]]
    with switches_open(["671692"], "IEEE13"):
        blocked = sql_tool(T.find_path, T.FindPathInput, raw)["data"]
    assert blocked["summary"]["connected"] is False
    assert blocked["summary"]["blocked_by"]["name"] == "671692"


def test_isolate_and_ties(loaded, sql_tool):
    iso = sql_tool(T.isolate, T.IsolateInput, {"mrid": "692675", "region_code": "IEEE13"})["data"]
    assert iso["viz"] == "tree" and iso["nodes"][0]["name"] == "671692" and iso["nodes"][0]["role"] == "upstream"
    assert iso["summary"]["customers_isolated"] == 4
    head = sql_tool(T.isolate, T.IsolateInput, {"mrid": "632670", "region_code": "IEEE13"})
    assert head["warnings"] and head["data"]["summary"]["customers_isolated"] == 15
    ties = sql_tool(T.tie_options, T.TieOptionsInput, {"feeder_mrid": "IEEE123 Head", "region_code": "IEEE123"})
    assert ties["success"] and ties["data"]["viz"] == "tree" and ties["data"]["summary"]["ties"] == 0


def test_connectivity_report(loaded, sql_tool):
    env = sql_tool(T.connectivity_report, T.ConnectivityReportInput, {"feeder_mrid": str(ROOT[13]),
                                                                      "region_code": "IEEE13"})
    rows = {r["kind"]: r for r in env["data"]["rows"]}
    assert env["data"]["viz"] == "table" and rows["island"]["count"] == 0 and rows["orphan_terminal"]["count"] == 0
    with switches_open(["671692"], "IEEE13"):
        dead = sql_tool(T.connectivity_report, T.ConnectivityReportInput,
                        {"feeder_mrid": str(ROOT[13]), "region_code": "IEEE13"})["data"]["rows"]
    assert next(r for r in dead if r["kind"] == "deenergised_by_open_switch")["count"] >= 5


def test_region_isolation(loaded, ds, sql_tool):
    a = Principal("a", ["IEEE13"])
    other = str(ROOT[123])
    for raw in ({"mrid": other, "region_code": "IEEE13"}, {"mrid": other, "region_code": "IEEE123"}):
        for fn, cls in ((T.trace_downstream, T.TraceDownstreamInput), (T.trace_upstream, T.TraceUpstreamInput),
                        (T.isolate, T.IsolateInput)):
            env = sql_tool(fn, cls, raw, principal=a)
            assert env["success"] is False and env["errors"][0]["code"] == "forbidden", (fn.__name__, raw)
    mine = sql_tool(T.trace_downstream, T.TraceDownstreamInput,
                    {"mrid": str(ROOT[13]), "region_code": "IEEE13", "detail": "full"}, principal=a)
    regions = ds.query("SELECT DISTINCT region_code FROM conducting_equipment WHERE mrid = ANY(%(m)s::uuid[])",
                       {"m": list(tree_ids(mine))}, ["*"])
    assert [r["region_code"] for r in regions] == ["IEEE13"]
