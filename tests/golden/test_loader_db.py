# SPDX-License-Identifier: Apache-2.0
import pytest

from loaders import ieee
from tests.conftest import LOADER_URL

pytestmark = pytest.mark.db

COUNTS = """
SELECT (SELECT count(*) FROM conducting_equipment) AS ce, (SELECT count(*) FROM terminal) AS t,
       (SELECT count(*) FROM connectivity_node) AS cn, (SELECT count(*) FROM measurement) AS m,
       (SELECT count(*) FROM network_edges) AS e
"""
ORPHANS = """
SELECT count(*) AS n FROM terminal t LEFT JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
WHERE cn.mrid IS NULL
"""


def test_loader_is_idempotent(loaded, ds):
    before = ds.query(COUNTS, {}, ["*"])[0]
    ieee.load(13, LOADER_URL)
    assert ds.query(COUNTS, {}, ["*"])[0] == before


def test_network_edges_built_and_no_orphans(loaded, ds):
    edges = ds.query("SELECT region_code, count(*) AS n FROM network_edges GROUP BY 1 ORDER BY 1", {}, ["*"])
    counts = {r["region_code"]: r["n"] for r in edges}
    assert {k: counts.get(k) for k in ("IEEE123", "IEEE13", "IEEE34")} == {"IEEE123": 135, "IEEE13": 18, "IEEE34": 41}
    assert ds.query(ORPHANS, {}, ["*"])[0]["n"] == 0
