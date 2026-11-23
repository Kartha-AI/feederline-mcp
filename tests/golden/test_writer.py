# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime, timedelta

import pytest

from feederline_mcp.adapters.base import Sample
from feederline_mcp.adapters.local import LocalDataSource
from ingest.writer import TelemetryWriter
from tests.conftest import WRITER_URL

pytestmark = pytest.mark.db
TAG = "IEEE13_671692_POS"
STATE = """
SELECT s.open, s.open_ts FROM switch s JOIN measurement m ON m.power_system_resource_mrid = s.mrid
WHERE m.name = %(tag)s
"""


@pytest.fixture
def writer(loaded):
    ds = LocalDataSource(WRITER_URL)
    yield TelemetryWriter(ds), ds
    ds.close()


def test_switch_open_follows_last_sample_by_ts(writer):
    w, ds = writer
    t = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=5)
    # out of order in the batch: the later sample (open) wins
    rep = w.write_batch([Sample(TAG, t + timedelta(seconds=2), 0), Sample(TAG, t + timedelta(seconds=1), 1)])
    assert rep.written == 2 and not rep.unknown
    row = ds.query(STATE, {"tag": TAG}, ["*"])[0]
    assert row["open"] is True and row["open_ts"] == t + timedelta(seconds=2)
    # an older sample never rolls the state back
    w.write_batch([Sample(TAG, t, 1)])
    assert ds.query(STATE, {"tag": TAG}, ["*"])[0]["open"] is True
    w.write_batch([Sample(TAG, t + timedelta(seconds=3), 1)])
    assert ds.query(STATE, {"tag": TAG}, ["*"])[0]["open"] is False


def test_writer_is_idempotent_and_reports_unknown_tags(writer):
    w, ds = writer
    t = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
    batch = [Sample("IEEE13_671_P", t, 1100.0), Sample("NO_SUCH_TAG", t, 1.0)]
    first = w.write_batch(batch)
    second = w.write_batch(batch)
    assert first.written == 1 and second.written == 0
    assert [s.tag for s in second.unknown] == ["NO_SUCH_TAG"]
