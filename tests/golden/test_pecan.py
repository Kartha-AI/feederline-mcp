# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest

from loaders import pecan
from tests.conftest import LOADER_URL

pytestmark = pytest.mark.db
# Synthetic households in the Dataport 15-minute export format (no Pecan Street data is distributed).
CSV = Path(__file__).resolve().parents[1] / "fixtures" / "pecan_sample.csv"
COUNT = """
SELECT count(*) AS n, count(DISTINCT v.measurement_mrid) AS m FROM measurement_value v
JOIN measurement me ON me.mrid = v.measurement_mrid
WHERE me.region_code = 'IEEE13' AND v.source = 2 AND v.ts >= '2026-10-27 05:00+00' AND v.ts < '2026-10-28 05:00+00'
"""


def test_households_use_gross_consumption():
    hh = pecan.read_households(CSV)
    assert set(hh) == {"9001", "9002"} and all(len(s) == 96 for s in hh.values())
    assert all(kw > 0 for s in hh.values() for _, kw in s)
    assert hh["9001"][0][0].utcoffset().total_seconds() == -5 * 3600


def test_scaling_keeps_mean_and_power_factor():
    consumers = [{"p_fixed": 100, "q_fixed": 50, "p_tag": "P", "q_tag": "Q"}]
    hh = {"a": [(None, 1.0), (None, 3.0)]}
    s = pecan.samples_for(consumers, hh)
    p = [x.value for x in s if x.tag == "P"]
    q = [x.value for x in s if x.tag == "Q"]
    assert sum(p) / len(p) == pytest.approx(100) and q == [v / 2 for v in p]


def test_load_writes_every_consumer_and_is_idempotent(loaded, ds, tmp_path, monkeypatch):
    monkeypatch.setenv("HISTORY_DIR", str(tmp_path))
    pecan.load(CSV, "IEEE13", LOADER_URL)
    again = pecan.load(CSV, "IEEE13", LOADER_URL)
    assert again["samples"] == 0
    row = ds.query(COUNT, {}, ["*"])[0]
    assert row == {"n": 15 * 2 * 96, "m": 30}
    assert len(again["archives"]) == 2 and all(Path(a).exists() for a in again["archives"])
