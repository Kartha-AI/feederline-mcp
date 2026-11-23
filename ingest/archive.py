# SPDX-License-Identifier: Apache-2.0
"""Archive one day's partition to HISTORY_DIR/measurement_value/YYYY-MM-DD.parquet (written atomically)."""

from __future__ import annotations

import argparse
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq

from .rotate import archive_path

log = logging.getLogger("ingest.archive")

DAY = """
SELECT ts, measurement_mrid::text AS measurement_mrid, value, quality, source
FROM measurement_value WHERE ts >= %(start)s AND ts < %(end)s ORDER BY measurement_mrid, ts
"""
SCHEMA = pa.schema([("ts", pa.timestamp("us", tz="UTC")), ("measurement_mrid", pa.string()), ("value", pa.float64()),
                    ("quality", pa.int16()), ("source", pa.int16())])


def archive(conn: psycopg.Connection, d: date, history_dir: Path) -> tuple[Path, int]:
    start = datetime.combine(d, datetime.min.time(), tzinfo=UTC)
    with conn.cursor() as cur:
        cur.execute(DAY, {"start": start, "end": start + timedelta(days=1)})
        rows = cur.fetchall()
    cols = list(zip(*rows, strict=True)) if rows else [[] for _ in SCHEMA]
    table = pa.Table.from_arrays([pa.array(c, type=f.type) for c, f in zip(cols, SCHEMA, strict=True)], schema=SCHEMA)
    out = archive_path(history_dir, d)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp)
    tmp.replace(out)
    return out, len(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", type=date.fromisoformat, default=datetime.now(UTC).date() - timedelta(days=1))
    args = ap.parse_args()
    with psycopg.connect(os.environ.get("DATABASE_URL", "")) as conn:
        path, n = archive(conn, args.date, Path(os.environ.get("HISTORY_DIR", "data/parquet")))
    log.info("archived %d rows to %s", n, path)


if __name__ == "__main__":
    main()
