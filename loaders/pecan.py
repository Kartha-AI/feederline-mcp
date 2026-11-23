# SPDX-License-Identifier: Apache-2.0
"""Pecan Street Dataport 15-minute CSV -> measurement_value (source 2, AMI), scaled onto a region's consumers.

    python -m loaders.pecan --file 15minute_data_austin.csv --region IEEE123

Expects the Dataport 15-minute export: `dataid`, `local_15min` (timestamp with UTC offset) and kW columns. The
consumers carry no DER, so the series is the household's gross consumption: `use` when exported, else `grid`
(net) plus `solar` and `solar2`. Each consumer in the region is assigned a household
(round robin over dataids, deterministic by consumer name) and its P series is the household's shape scaled so
its mean equals the consumer's p_fixed; Q keeps the consumer's nameplate power factor. Samples go through the
TelemetryWriter (newest wins in measurement_value_latest), then each covered day is archived to Parquet.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from feederline_mcp.adapters.base import Sample
from feederline_mcp.adapters.local import LocalDataSource

from .common import database_url

log = logging.getLogger("loaders.pecan")

SOURCE_AMI = 2
CONSUMERS = """
SELECT ce.name, ec.p_fixed, ec.q_fixed, mp.name AS p_tag, mq.name AS q_tag
FROM conducting_equipment ce
JOIN energy_consumer ec ON ec.mrid = ce.mrid
JOIN measurement mp ON mp.power_system_resource_mrid = ce.mrid AND mp.measurement_type = 'ThreePhaseActivePower'
JOIN measurement mq ON mq.power_system_resource_mrid = ce.mrid AND mq.measurement_type = 'ThreePhaseReactivePower'
WHERE ce.region_code = %(region)s AND ec.p_fixed > 0
ORDER BY ce.name
"""


def _num(row: dict, col: str) -> float | None:
    raw = (row.get(col) or "").strip()
    return float(raw) if raw else None


def gross_kw(row: dict, cols: list[str]) -> float | None:
    if "use" in cols:
        return _num(row, "use")
    grid = _num(row, "grid")
    if grid is None:
        return None
    return grid + sum(_num(row, c) or 0.0 for c in ("solar", "solar2") if c in cols)


def read_households(path: Path) -> dict[str, list[tuple[datetime, float]]]:
    """dataid -> [(ts, kW)] sorted by time; rows without a usable kW value are skipped."""
    out: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        for row in reader:
            kw = gross_kw(row, cols)
            if kw is None:
                continue
            ts = datetime.fromisoformat(row["local_15min"].strip().replace(" ", "T"))
            if ts.tzinfo is None:
                raise ValueError("local_15min must carry a UTC offset (Dataport exports do)")
            out[row["dataid"].strip()].append((ts, kw))
    for series in out.values():
        series.sort()
    return dict(out)


def samples_for(consumers: list[dict], households: dict[str, list[tuple[datetime, float]]]) -> list[Sample]:
    ids = sorted(households)
    out: list[Sample] = []
    for i, c in enumerate(consumers):
        series = households[ids[i % len(ids)]]
        mean = sum(v for _, v in series) / len(series)
        if mean <= 0:
            continue
        p_fixed, q_fixed = float(c["p_fixed"]), float(c["q_fixed"] or 0)
        k = p_fixed / mean
        for ts, kw in series:
            p = kw * k
            out.append(Sample(c["p_tag"], ts, p, 0, SOURCE_AMI))
            out.append(Sample(c["q_tag"], ts, p * q_fixed / p_fixed, 0, SOURCE_AMI))
    return out


def load(path: Path, region: str, url: str | None = None, archive_days: bool = True) -> dict:
    url = url or database_url()
    households = read_households(path)
    if not households:
        raise ValueError(f"{path}: no household rows")
    with psycopg.connect(url, row_factory=dict_row) as conn:
        consumers = conn.execute(CONSUMERS, {"region": region}).fetchall()
    if not consumers:
        raise ValueError(f"region {region} has no energy consumers with p_fixed")
    samples = samples_for(consumers, households)
    from ingest.writer import TelemetryWriter

    ds = LocalDataSource(url)
    try:
        writer = TelemetryWriter(ds)
        written = sum(writer.write_batch(samples[i:i + 20_000]).written for i in range(0, len(samples), 20_000))
    finally:
        ds.close()
    days = sorted({s.ts.astimezone(UTC).date() for s in samples})
    archived = []
    if archive_days:
        from ingest.archive import archive

        with psycopg.connect(url) as conn:
            for d in days:
                archived.append(str(archive(conn, d, Path(os.environ.get("HISTORY_DIR", "data/parquet")))[0]))
    return {"households": len(households), "consumers": len(consumers), "samples": written,
            "days": [d.isoformat() for d in days], "archives": archived}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", type=Path, required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--no-archive", action="store_true")
    args = ap.parse_args()
    log.info(json.dumps(load(args.file, args.region, archive_days=not args.no_archive)))


if __name__ == "__main__":
    main()
