# SPDX-License-Identifier: Apache-2.0
from datetime import UTC, datetime, timedelta

from feederline_mcp.tools.sql.telemetry import quality_flags, quality_stats

T0 = datetime(2026, 10, 20, tzinfo=UTC)


def test_quality_flags():
    assert quality_flags(0) == [] and quality_flags(None) == []
    assert quality_flags(0b1000011) == ["questionable", "invalid", "test"]


def test_complete_day_has_no_gap():
    buckets = [(T0 + timedelta(minutes=15 * i)).timestamp() for i in range(96)]
    st = quality_stats(900, T0, T0 + timedelta(days=1), 96, 0, buckets, T0 + timedelta(days=1), T0 + timedelta(days=1))
    assert st == {"expected": 96, "received": 96, "completeness": 1.0, "bad_share": 0.0, "longest_gap_s": 0,
                  "stale_now": False}


def test_gap_in_the_middle_and_at_the_end():
    buckets = [(T0 + timedelta(minutes=15 * i)).timestamp() for i in list(range(40)) + list(range(48, 90))]
    st = quality_stats(900, T0, T0 + timedelta(days=1), 82, 4, buckets, None, T0 + timedelta(days=2))
    assert st["longest_gap_s"] == 8 * 900 and st["bad_share"] == round(4 / 82, 4) and st["stale_now"]
    empty = quality_stats(60, T0, T0 + timedelta(hours=1), 0, 0, [], None, T0)
    assert empty["longest_gap_s"] == 3600 and empty["expected"] == 60 and empty["completeness"] == 0
