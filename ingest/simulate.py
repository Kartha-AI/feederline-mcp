# SPDX-License-Identifier: Apache-2.0
"""Simulator: a diurnal curve with noise per measurement at its scan interval, source=6 (Simulator).

Live:      python -m ingest.simulate --scan-seconds 10
Backfill:  python -m ingest.simulate --backfill 2026-10-30 --step-minutes 15
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import random
import time
from datetime import UTC, date, datetime, timedelta

from feederline_mcp.adapters.base import ALL_REGIONS, Sample
from feederline_mcp.adapters.local import LocalDataSource

from .writer import TelemetryWriter

log = logging.getLogger("ingest.simulate")

SOURCE_SIMULATOR = 6
NOISE = 0.03

BASES = """
SELECT rs.name AS tag, m.measurement_type, coalesce(a.normal_value, d.normal_value) AS normal_value
FROM remote_source rs
JOIN measurement m ON m.mrid = rs.measurement_mrid
LEFT JOIN analog a ON a.mrid = m.mrid
LEFT JOIN discrete d ON d.mrid = m.mrid
"""


def diurnal(ts: datetime) -> float:
    """Residential-shaped multiplier, ~0.55 overnight, peaks ~1.15 at 19:00 UTC-local."""
    h = ts.hour + ts.minute / 60
    return 0.85 + 0.2 * math.sin((h - 13) / 24 * 2 * math.pi) + 0.1 * math.exp(-((h - 19) ** 2) / 4)


class Simulator:
    def __init__(self, ds: LocalDataSource, seed: int | None = None):
        self.writer = TelemetryWriter(ds)
        self.bases = {r["tag"]: r for r in ds.query(BASES, {}, [ALL_REGIONS])}
        self.rng = random.Random(seed)

    def sample(self, tag: str, ts: datetime) -> Sample | None:
        b = self.bases.get(tag)
        if b is None or b["normal_value"] is None:
            return None
        base, mtype = float(b["normal_value"]), b["measurement_type"]
        k = diurnal(ts)
        if mtype in ("ThreePhaseActivePower", "ThreePhaseReactivePower"):
            value = base * k * (1 + self.rng.gauss(0, NOISE))
        elif mtype == "LineToLineVoltage":
            value = base * (1 - 0.01 * (k - 0.85)) * (1 + self.rng.gauss(0, 0.002))
        elif mtype == "SwitchPosition":
            value = base
        elif mtype == "TapPosition":
            value = base
        else:
            value = base * (1 + self.rng.gauss(0, NOISE))
        return Sample(tag=tag, ts=ts, value=value, quality=0, source=SOURCE_SIMULATOR)

    def tick(self, ts: datetime) -> int:
        samples = [s for t in self.bases if (s := self.sample(t, ts)) is not None]
        return self.writer.write_batch(samples).written

    def backfill(self, day: date, step: timedelta) -> int:
        start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        n, ts = 0, start
        while ts < start + timedelta(days=1):
            n += self.tick(ts)
            ts += step
        return n


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan-seconds", type=float, default=10)
    ap.add_argument("--backfill", type=date.fromisoformat)
    ap.add_argument("--step-minutes", type=float, default=15)
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()
    ds = LocalDataSource(os.environ.get("DATABASE_URL", "postgresql://grid_write:grid_write@localhost:5433/grid"))
    sim = Simulator(ds, args.seed)
    if args.backfill:
        log.info("backfill %s: %d samples", args.backfill, sim.backfill(args.backfill, timedelta(minutes=args.step_minutes)))
        return
    while True:
        sim.tick(datetime.now(UTC))
        time.sleep(args.scan_seconds)


if __name__ == "__main__":
    main()
