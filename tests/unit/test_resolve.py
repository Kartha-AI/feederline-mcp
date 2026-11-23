# SPDX-License-Identifier: Apache-2.0
"""resolve_mrid precedence and ambiguity, the shared switch predicate, and the tree aggregation pass."""

from uuid import UUID, uuid4

import pytest

from feederline_mcp.adapters.base import Principal
from feederline_mcp.compiler import scope
from feederline_mcp.studies import ToolError
from feederline_mcp.tools._runner import AreaCache, Call
from feederline_mcp.tools.sql import queries
from feederline_mcp.tools.sql.resolve import resolve_mrid
from feederline_mcp.tools.sql.topology import build_tree, isolation

P = Principal("t", ["*"])


class FakeDS:
    rollup_table = "measurement_value_15m"
    source_name = "fake"

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def query(self, sql, params, regions):
        self.calls.append(sql)
        for key, rows in self.answers.items():
            if sql == key:
                return rows(params) if callable(rows) else rows
        return []


def cand(stage, name, cls="EnergyConsumer"):
    return {"mrid": uuid4(), "name": name, "cim_class": cls, "region_code": "R", "stage": stage, "score": 1.0}


def test_uuid_passes_through_without_a_query():
    ds = FakeDS({})
    u = uuid4()
    assert resolve_mrid(ds, P, str(u)) == u and ds.calls == []


@pytest.mark.parametrize("stages", [[1, 2, 3, 4], [2, 3, 4], [3, 4], [4]])
def test_first_stage_with_hits_wins(stages):
    # RESOLVE orders by stage: gis_oid (1) > tag (2) > exact name (3) > trigram (4)
    rows = [cand(s, f"hit{s}") for s in stages]
    assert resolve_mrid(FakeDS({queries.RESOLVE: rows}), P, "x") == rows[0]["mrid"]


def test_several_hits_at_the_first_stage_are_ambiguous():
    rows = [cand(3, "671"), cand(3, "671", "ACLineSegment"), cand(4, "6711")]
    with pytest.raises(ToolError) as e:
        resolve_mrid(FakeDS({queries.RESOLVE: rows}), P, "671")
    assert e.value.code == "ambiguous" and len(e.value.extra["candidates"]) == 2


def test_no_hit_is_not_found():
    with pytest.raises(ToolError) as e:
        resolve_mrid(FakeDS({queries.RESOLVE: []}), P, "nothing")
    assert e.value.code == "not_found"


def test_edges_sql_renders_each_switch_predicate():
    assert "NOT COALESCE(s.open, false)" in scope.edges_sql("operational")
    assert "NOT COALESCE(s.normal_open, false)" in scope.edges_sql("planning")
    assert scope.edges_sql("all").endswith("AND true")
    assert "s.mrid IS NULL" in scope.edges_sql("section")
    for case in ("operational", "planning"):  # the compiler scope and the SQL tools share the text
        assert scope.edges_sql(case) in scope.SCOPE[case] and scope.edges_sql(case) in queries.TRACE_DOWNSTREAM[case]
    with pytest.raises(scope.ScopeError):
        scope.edges_sql("bogus")


# Hand-built feeder: head -(A 1000 m)- n2 -(S switch)- n3 -(B 500 m)- n4 ; n2 -(C 200 m)- n5
# loads: L1 at n4 (2 customers, 10 kW), L2 at n5 (1, 5 kW), L3 at n3 (1, 7 kW). Seven items.
IDS = {k: UUID(int=i + 1) for i, k in enumerate(["head", "A", "S", "B", "C", "L1", "L2", "L3"])}


def _edge(seq, depth, pred, node, name, cls, length, agg):
    return {"seq": seq, "depth": depth, "pred": pred, "node": node, "edge": seq, "agg_cost": agg,
            "equipment_mrid": IDS[name], "source": pred, "target": node, "name": name, "cim_class": cls,
            "open": False if cls == "LoadBreakSwitch" else None, "normal_open": False if cls == "LoadBreakSwitch" else None,
            "open_ts": None, "length": length, "cn_mrid": None}


def _load(name, node, customers, kw):
    return {"mrid": IDS[name], "name": name, "cim_class": "EnergyConsumer", "open": None, "normal_open": None,
            "open_ts": None, "length": None, "customer_count": customers, "p_fixed": kw, "nodes": [node], "hits": [True],
            "phases": "ABC"}


def fake_tree():
    down = [{"seq": 0, "depth": 0, "pred": 1, "node": 1, "edge": -1, "agg_cost": 0, "equipment_mrid": None,
             "source": None, "target": None, "name": None, "cim_class": None, "open": None, "normal_open": None,
             "open_ts": None, "length": None, "cn_mrid": None},
            _edge(1, 1, 1, 2, "A", "ACLineSegment", 1000, 1000), _edge(2, 2, 2, 3, "S", "LoadBreakSwitch", None, 1001),
            _edge(3, 2, 2, 5, "C", "ACLineSegment", 200, 1200), _edge(4, 3, 3, 4, "B", "ACLineSegment", 500, 1501)]
    extra = [_load("L1", 4, 2, 10), _load("L2", 5, 1, 5), _load("L3", 3, 1, 7)]
    ds = FakeDS({queries.TRACE_DOWNSTREAM["operational"]: down, queries.TRACE_EXTRA: extra})
    a = scope.Anchor(IDS["head"], "head", "Breaker", "R", 99, [0, 1], [uuid4(), uuid4()], True, 1)
    return build_tree(Call(ds, None, P, "s", AreaCache(), {}), a, "operational")


def test_tree_aggregation_pass():
    t = fake_tree()
    assert len(t.items) == 7 and t.top == [IDS["A"]]
    s = t.items[IDS["S"]]
    assert (s.customers_down, round(s.km_down, 3), s.kw_down) == (3, 0.5, 17.0)
    a = t.items[IDS["A"]]
    assert (a.customers_down, round(a.km_down, 3), a.kw_down) == (4, 1.7, 22.0)
    assert (t.customers, round(t.km, 3), t.kw) == (4, 1.7, 22.0)
    assert t.items[IDS["L1"]].parent == IDS["B"] and t.items[IDS["L3"]].parent == IDS["S"]
    assert [p.mrid for p in t.ancestors(IDS["L1"])] == [IDS["B"], IDS["S"], IDS["A"]]


def test_isolation_from_the_tree():
    t = fake_tree()
    iso = isolation(t, IDS["B"])
    assert iso.upstream.mrid == IDS["S"] and iso.downstream == [] and iso.customers_isolated == 3
    head = isolation(t, IDS["C"])
    assert head.upstream is None and [d.mrid for d in head.downstream] == [IDS["S"]]
    assert head.customers_isolated == 4 and head.customers_in_section == 1
