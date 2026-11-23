# SPDX-License-Identifier: Apache-2.0
"""Each result shape round-trips its spec example; the size cap honours limit and byte budget."""

import json

import pytest

from feederline_mcp import shapes

U1, U2 = "c990acfa-4c84-5788-be92-53b20464fbd8", "d20ccdc1-9d7f-557f-913e-10e7a2f9a632"
EXAMPLES = {
    "map": {"features": [{"geometry": {"type": "Point", "coordinates": [-117.2, 32.8]},
                          "properties": {"mrid": U1, "name": "671", "cim_class": "EnergyConsumer", "loading_pct": None}}],
            "bbox": [-117.2, 32.8, -117.2, 32.8], "summary": {"features": 1}},
    "tree": {"root": {"mrid": U1, "name": "IEEE13 Head", "cim_class": "Breaker"},
             "nodes": [{"mrid": U2, "name": "671692", "cim_class": "LoadBreakSwitch", "depth": 1, "parent": U1,
                        "open": False, "km_from_root": 1.2, "customers_downstream": 4, "km_downstream": 0.15,
                        "kw_nameplate_downstream": 1013.0, "normal_open": False}],
             "edges": [{"from": U1, "to": U2}], "summary": {"customers": 15}},
    "path": {"from": {"mrid": U2, "name": "671", "cim_class": "EnergyConsumer"},
             "to": {"mrid": U1, "name": "IEEE13 Head", "cim_class": "Breaker"},
             "hops": [{"seq": 0, "mrid": U2, "name": "671", "cim_class": "EnergyConsumer", "protective": False, "km": 0},
                      {"seq": 1, "mrid": U1, "name": "IEEE13 Head", "cim_class": "Breaker", "protective": True,
                       "open": False, "km": 1.219}],
             "summary": {"connected": True, "r_ohm": 0.14}},
    "timeseries": {"interval": "15m", "ts": ["2026-10-29T00:00:00+00:00", "2026-10-29T00:15:00+00:00"],
                   "series": [{"name": "Head P", "mrid": U1, "unit": "kW", "values": [1200.5, None]}]},
    "table": {"columns": [{"key": "name", "label": "Name"}, {"key": "peak_kw", "label": "Peak", "unit": "kW"}],
              "rows": [{"mrid": U1, "name": "IEEE13 Head", "cim_class": "Breaker", "peak_kw": 3466.0}]},
    "profile": {"x_unit": "km", "y_unit": "pu",
                "points": [{"bus": "cn_d20ccdc1", "mrid": U2, "name": "671", "distance_km": 1.22, "phase": "A",
                            "value": 0.98}]},
}


@pytest.mark.parametrize("viz", sorted(EXAMPLES))
def test_round_trip(viz):
    cls = shapes.SHAPES[viz]
    m = cls.model_validate(EXAMPLES[viz])
    d = shapes.dump(m)
    assert d["viz"] == viz and d["truncated"] is False and d["resource"] is None
    assert shapes.dump(cls.model_validate(json.loads(json.dumps(d)))) == d


def test_path_serialises_from_under_its_json_name():
    d = shapes.dump(shapes.PathData.model_validate(EXAMPLES["path"]))
    assert "from" in d and "from_" not in d


def test_cap_by_limit_and_budget():
    items = [{"x": "a" * 100} for _ in range(10)]
    assert shapes.cap(items, 20) == (items, False)
    assert shapes.cap(items, 3) == (items[:3], True)
    kept, cut = shapes.cap(items, 10, budget=350)
    assert cut and len(kept) == 3


def test_bbox_of_nested_coordinates():
    f = shapes.Feature(geometry={"type": "LineString", "coordinates": [[1, 2], [3, -1]]}, properties={})
    assert shapes.bbox_of([f]) == [1, -1, 3, 2]
