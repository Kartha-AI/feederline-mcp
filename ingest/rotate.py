# SPDX-License-Identifier: Apache-2.0
"""Partition rotation: create D+1, D+2; drop partitions older than HOT_DAYS only once their archive exists; ANALYZE."""

from __future__ import annotations

import argparse
import logging
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg import sql

log = logging.getLogger("ingest.rotate")

PARTITIONS = """
SELECT c.relname FROM pg_inherits i
JOIN pg_class c ON c.oid = i.inhrelid JOIN pg_class p ON p.oid = i.inhparent
WHERE p.relname = 'measurement_value'
"""
PART_RE = re.compile(r"^measurement_value_(\d{4})_(\d{2})_(\d{2})$")


def archive_path(history_dir: Path, d: date) -> Path:
    return history_dir / "measurement_value" / f"{d.isoformat()}.parquet"


def rotate(conn: psycopg.Connection, hot_days: int, history_dir: Path, today: date | None = None) -> dict:
    today = today or datetime.now(UTC).date()
    created, dropped, kept = [], [], []
    with conn.cursor() as cur:
        for i in (0, 1, 2):
            cur.execute("SELECT ensure_measurement_value_partition(%s)", (today + timedelta(days=i),))
            created.append(cur.fetchone()[0])
        cur.execute(PARTITIONS)
        for (name,) in cur.fetchall():
            m = PART_RE.match(name)
            if not m:
                continue
            d = date(int(m[1]), int(m[2]), int(m[3]))
            if d >= today - timedelta(days=hot_days):
                continue
            if archive_path(history_dir, d).exists():
                cur.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(name)))
                dropped.append(name)
            else:
                kept.append(name)
                log.warning("not dropping %s: archive %s missing", name, archive_path(history_dir, d))
        cur.execute("ANALYZE measurement_value")
    return {"created": created, "dropped": dropped, "kept_unarchived": kept}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hot-days", type=int, default=int(os.environ.get("HOT_DAYS", "14")))
    args = ap.parse_args()
    with psycopg.connect(os.environ.get("DATABASE_URL", ""), autocommit=True) as conn:
        log.info(rotate(conn, args.hot_days, Path(os.environ.get("HISTORY_DIR", "data/parquet"))))


if __name__ == "__main__":
    main()
