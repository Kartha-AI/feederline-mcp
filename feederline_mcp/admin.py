# SPDX-License-Identifier: Apache-2.0
"""feederline-mcp-admin: database and data administration for the feederline-mcp stack.

Runs inside the server image (the npm CLI calls it through `docker compose run --rm admin ...`) and from a source
checkout (the Makefile calls it with `uv run`). Every subcommand reuses the loader and ingest modules.

Connection strings (each falls back to the local compose default):
  ADMIN_DATABASE_URL   owner of the schema: migrations, rollup refresh, status
  LOADER_DATABASE_URL  the loader login (grid_load)
  WRITER_DATABASE_URL  the telemetry writer login (grid_write)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

from . import __version__

log = logging.getLogger("feederline_mcp.admin")

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = Path(os.environ.get("FEEDERLINE_DB_DIR", ROOT / "db"))


def _url(var: str, user: str) -> str:
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5433")
    return os.environ.get(var) or f"postgresql://{user}:{user}@{host}:{port}/grid"


def admin_url() -> str:
    return _url("ADMIN_DATABASE_URL", "grid")


def loader_url() -> str:
    return _url("LOADER_DATABASE_URL", "grid_load")


def writer_url() -> str:
    return _url("WRITER_DATABASE_URL", "grid_write")


def sqitch_target(url: str) -> str:
    """postgresql://u:p@h:port/db -> db:pg://u:p@h:port/db (sqitch's URI form)."""
    u = urlparse(url)
    return f"db:pg://{u.netloc}{u.path}"


# --- migrate ---------------------------------------------------------------------------------------------------

def migrate() -> None:
    url = admin_url()
    subprocess.run(["sqitch", "deploy", sqitch_target(url)], cwd=DB_DIR / "sqitch", check=True)
    subprocess.run(["psql", url, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(DB_DIR / "local" / "logins.sql")], check=True)
    log.info("migrations deployed: %s", migration_version(url))


def migration_version(url: str) -> str | None:
    with psycopg.connect(url) as conn:
        try:
            row = conn.execute("SELECT change FROM sqitch.changes ORDER BY committed_at DESC LIMIT 1").fetchone()
        except psycopg.Error:
            return None
    return row[0] if row else None


# --- load ------------------------------------------------------------------------------------------------------

def load_ieee(feeders: list[int]) -> None:
    from loaders import ieee

    for n in feeders:
        for line in ieee.load(n, loader_url()).lines():
            log.info(line)


def load_smartds(region: str, substation: str, day: date | None, max_profiles: int) -> None:
    os.environ["DATABASE_URL"] = loader_url()
    from loaders import smartds

    if not (smartds.substation_dir(region, substation) / "Master.dss").exists():
        log.info("downloaded %d files", smartds.download_substation(region, substation))
    for line in smartds.load(region, substation, loader_url()).lines():
        log.info(line)
    if day:
        log.info(json.dumps(smartds.ingest_day(region, substation, day, max_profiles, loader_url()), default=str))
        refresh_rollups()


def load_hifld(state: str) -> None:
    os.environ["DATABASE_URL"] = loader_url()
    from loaders import hifld

    for line in hifld.load(state, loader_url()).lines():
        log.info(line)


# --- telemetry -------------------------------------------------------------------------------------------------

def refresh_rollups() -> None:
    with psycopg.connect(admin_url(), autocommit=True) as conn:
        conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY measurement_value_15m")
    log.info("rollups refreshed")


def simulate(backfill_days: int, seed: int, step_minutes: float) -> int:
    """Deterministic demo telemetry for the `backfill_days` days before today (UTC), then a rollup refresh."""
    from feederline_mcp.adapters.local import LocalDataSource
    from ingest.simulate import Simulator

    today = datetime.now(UTC).date()
    days = [today - timedelta(days=i) for i in range(backfill_days, 0, -1)]
    with psycopg.connect(writer_url(), autocommit=True) as conn:
        for d in days:
            conn.execute("SELECT ensure_measurement_value_partition(%s)", (d,))
    ds = LocalDataSource(writer_url())
    total = 0
    try:
        sim = Simulator(ds, seed)
        for d in days:
            n = sim.backfill(d, timedelta(minutes=step_minutes))
            total += n
            log.info("backfill %s: %d samples", d, n)
    finally:
        ds.close()
    refresh_rollups()
    return total


# --- status ----------------------------------------------------------------------------------------------------

FEEDERS = """
SELECT ce.mrid::text AS root_mrid, ce.name, ce.region_code, s.name AS substation, bv.nominal_voltage::float AS kv
FROM conducting_equipment ce
JOIN voltage_level vl ON vl.mrid = ce.equipment_container_mrid
LEFT JOIN substation s ON s.mrid = vl.substation_mrid
LEFT JOIN base_voltage bv ON bv.mrid = ce.base_voltage_mrid
WHERE ce.cim_class IN ('Breaker', 'Recloser') AND ce.valid_to IS NULL
ORDER BY ce.region_code, ce.name
"""
TELEMETRY_RANGE = "SELECT min(bucket) AS first, max(bucket) AS last FROM measurement_value_15m"


def status() -> dict:
    url = admin_url()
    out: dict = {"version": __version__, "migration": None, "feeders": [], "telemetry": None}
    with psycopg.connect(url, row_factory=dict_row) as conn:
        try:
            row = conn.execute("SELECT change FROM sqitch.changes ORDER BY committed_at DESC LIMIT 1").fetchone()
            out["migration"] = row["change"] if row else None
        except psycopg.Error:
            conn.rollback()
            return out
        out["feeders"] = conn.execute(FEEDERS).fetchall()
        rng = conn.execute(TELEMETRY_RANGE).fetchone()
        if rng and rng["first"]:
            out["telemetry"] = {"first": rng["first"].isoformat(), "last": rng["last"].isoformat()}
    return out


# --- CLI -------------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s", stream=sys.stderr)
    ap = argparse.ArgumentParser(prog="feederline-mcp-admin", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="deploy the sqitch migrations and the local login users")

    p_load = sub.add_parser("load", help="load a public dataset as CIM")
    lsub = p_load.add_subparsers(dest="dataset", required=True)
    p_ieee = lsub.add_parser("ieee", help="IEEE 13/34/123 test feeders")
    p_ieee.add_argument("--feeders", nargs="+", type=int, default=[13, 34, 123], choices=[13, 34, 123])
    p_sds = lsub.add_parser("smartds", help="an NREL SMART-DS substation (downloaded from OEDI)")
    p_sds.add_argument("--region", default="P1R")
    p_sds.add_argument("--substation", default="p1rhs0_1247")
    p_sds.add_argument("--date", type=date.fromisoformat, help="also ingest this day's load profiles")
    p_sds.add_argument("--max-profiles", type=int, default=40)
    p_hifld = lsub.add_parser("hifld", help="HIFLD-schema substations and transmission lines")
    p_hifld.add_argument("--state", default="CA")

    p_sim = sub.add_parser("simulate", help="deterministic demo telemetry for the last N days, then a rollup refresh")
    p_sim.add_argument("--backfill-days", type=int, default=30)
    p_sim.add_argument("--seed", type=int, default=42)
    p_sim.add_argument("--step-minutes", type=float, default=15)

    sub.add_parser("refresh-rollups", help="REFRESH MATERIALIZED VIEW CONCURRENTLY measurement_value_15m")

    p_status = sub.add_parser("status", help="migration, feeders loaded and telemetry range")
    p_status.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "migrate":
        migrate()
    elif args.cmd == "load":
        if args.dataset == "ieee":
            load_ieee(args.feeders)
        elif args.dataset == "smartds":
            load_smartds(args.region, args.substation, args.date, args.max_profiles)
        else:
            load_hifld(args.state)
    elif args.cmd == "simulate":
        log.info("simulated %d samples", simulate(args.backfill_days, args.seed, args.step_minutes))
    elif args.cmd == "refresh-rollups":
        refresh_rollups()
    elif args.cmd == "status":
        st = status()
        if args.json:
            sys.stdout.write(json.dumps(st, default=str) + "\n")
        else:
            sys.stdout.write(f"feederline-mcp {st['version']}  migration: {st['migration']}" + "\n")
            for f in st["feeders"]:
                sys.stdout.write(f"  {f['region_code']:<10} {f['name']:<24} {f['root_mrid']}  {f['kv']} kV" + "\n")
            t = st["telemetry"]
            sys.stdout.write((f"  telemetry: {t['first']} .. {t['last']}" if t else "  telemetry: none") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
