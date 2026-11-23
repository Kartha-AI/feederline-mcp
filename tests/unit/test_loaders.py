# SPDX-License-Identifier: Apache-2.0
import uuid

from loaders.common import NAMESPACE, mrid
from loaders.ieee import IeeeLoader, layout


def test_mrid_is_uuid5_of_source_kind_id():
    assert mrid("ieee13", "line", "650632") == uuid.uuid5(NAMESPACE, "ieee13:line:650632")
    assert mrid("ieee13", "line", "650632") != mrid("ieee34", "line", "650632")


def test_ieee13_rows_are_deterministic():
    a, b = IeeeLoader(13).build(), IeeeLoader(13).build()
    for table in ("conducting_equipment", "terminal", "connectivity_node", "power_transformer_end", "measurement"):
        ids_a = sorted(str(r.get("mrid", r.get("measurement_mrid"))) for r in a.tables[table])
        ids_b = sorted(str(r.get("mrid", r.get("measurement_mrid"))) for r in b.tables[table])
        assert ids_a == ids_b and len(set(ids_a)) == len(ids_a)


def test_ieee13_cim_shape():
    ld = IeeeLoader(13)
    rows = ld.build()
    c = rows.counts()
    assert (c["ACLineSegment"], c["LoadBreakSwitch"], c["Breaker"], c["PowerTransformer"], c["EnergyConsumer"],
            c["LinearShuntCompensator"]) == (11, 1, 1, 5, 15, 2)
    regs = [r for r in rows.tables["conducting_equipment"] if "regulator" in r["attrs"]]
    assert len(regs) == 3
    terminals = rows.tables["terminal"]
    assert {t["phases"] for t in terminals} <= {"ABC", "AB", "BC", "AC", "A", "B", "C"}
    # every terminal points at a known node
    nodes = {n["mrid"] for n in rows.tables["connectivity_node"]}
    assert all(t["connectivity_node_mrid"] in nodes for t in terminals)
    # every location is valid WKT in the 4326 envelope
    assert all(r["geom"].startswith(("POINT(-117", "LINESTRING(-117")) for r in rows.tables["location"])


def test_layout_places_missing_buses_next_to_neighbours():
    out = layout({"a": (0.0, 0.0), "b": None, "c": None}, {"a": {"b"}, "b": {"a", "c"}, "c": {"b"}})
    assert out["b"] != out["a"] and out["c"] != out["b"]
