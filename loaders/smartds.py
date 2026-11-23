# SPDX-License-Identifier: Apache-2.0
"""NREL SMART-DS (OEDI) substations -> CIM, plus load time series into measurement_value and Parquet.

    python -m loaders.smartds --region P1R --substation p1rhs0_1247 --download
    python -m loaders.smartds --region P1R --substation p1rhs0_1247 --timeseries 2026-10-30 --max-profiles 40

The substation's DSS (all its feeders) is compiled through a generated master that keeps the network and replaces
the 35,040-point yearly load shapes with one-point stubs, so the network model loads in seconds. Each load keeps
its shape name in attrs.profile; --timeseries downloads up to --max-profiles of the referenced kW/kvar profiles,
writes that day's 15-minute samples (source 2, AMI) through the TelemetryWriter, and archives the day to Parquet.
The SMART-DS profiles are 2018; a date maps to the same month and day of 2018.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from feederline_mcp.adapters.base import Sample
from feederline_mcp.adapters.local import LocalDataSource

from .common import LoadReport, analyze, database_url, rebuild_edges, write_rows
from .dss_cim import DssCimLoader, Source

log = logging.getLogger("loaders.smartds")

S3 = "https://oedi-data-lake.s3.amazonaws.com"
PREFIX = "SMART-DS/v1.0/2018/{city}/{region}"
DATA_DIR = Path(os.environ.get("SMARTDS_DIR", "data/smartds")).resolve()
CITY = {"P": "SFO"}  # region prefix -> city; SFO regions are P1R..P39U
SOURCE_AMI = 2
STEPS_PER_DAY = 96

KEEP = re.compile(r"^\s*(clear|new\s+circuit|redirect|set\s+voltagebases|calcvoltagebases)", re.I)
LOADSHAPE = re.compile(r"new\s+loadshape\.(\S+)(.*)", re.I)
SHAPE_FILE = re.compile(r"(?<![a-z])(q?mult)\s*=\s*\(\s*file\s*=\s*([^)\s]+)\s*\)", re.I)

REGION_LOADS = """
SELECT ce.mrid, ce.name, ec.p_fixed, ec.q_fixed, ce.attrs->>'profile' AS profile,
       mp.name AS p_tag, mq.name AS q_tag
FROM conducting_equipment ce
JOIN energy_consumer ec ON ec.mrid = ce.mrid
JOIN measurement mp ON mp.power_system_resource_mrid = ce.mrid AND mp.measurement_type = 'ThreePhaseActivePower'
JOIN measurement mq ON mq.power_system_resource_mrid = ce.mrid AND mq.measurement_type = 'ThreePhaseReactivePower'
WHERE ce.region_code = %(region)s AND ce.gis_oid LIKE %(oid)s
"""


def city(region: str) -> str:
    return CITY.get(region[0].upper(), "SFO")


def substation_dir(region: str, substation: str) -> Path:
    return DATA_DIR / region / substation


# --- download ----------------------------------------------------------------------------------------------------
def list_keys(prefix: str) -> list[str]:
    keys, token = [], None
    while True:
        q = f"{S3}/?list-type=2&prefix={urllib.parse.quote(prefix)}"
        if token:
            q += f"&continuation-token={urllib.parse.quote(token)}"
        with urllib.request.urlopen(q, timeout=60) as r:
            xml = r.read().decode()
        keys += re.findall(r"<Key>([^<]*)</Key>", xml)
        m = re.search(r"<NextContinuationToken>([^<]*)", xml)
        if not m:
            return keys
        token = m.group(1)


def fetch(key: str, dest: Path) -> Path:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        urllib.request.urlretrieve(f"{S3}/{urllib.parse.quote(key)}", tmp)
        tmp.replace(dest)
    return dest


def download_substation(region: str, substation: str) -> int:
    prefix = f"{PREFIX.format(city=city(region), region=region)}/scenarios/base_timeseries/opendss/{substation}/"
    keys = [k for k in list_keys(prefix) if "/analysis/" not in k]
    out = substation_dir(region, substation)
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(lambda k: fetch(k, out / k[len(prefix):]), keys))
    return len(keys)


# --- generated master ---------------------------------------------------------------------------------------------
def shapes(sub_dir: Path) -> dict[str, dict[str, str]]:
    """Load shape name -> {'mult': profile file, 'qmult': profile file} from every LoadShapes.dss."""
    out: dict[str, dict[str, str]] = {}
    for f in sub_dir.rglob("LoadShapes.dss"):
        for line in f.read_text().splitlines():
            m = LOADSHAPE.match(line.strip())
            if m:
                out[m.group(1).lower()] = {k.lower(): Path(v).name for k, v in SHAPE_FILE.findall(m.group(2))}
    return out


def write_master(sub_dir: Path) -> Path:
    """_cim_master.dss: the substation network with one-point load shapes; _cim_buscoords.dss: all coordinates."""
    stub = sub_dir / "_cim_loadshapes.dss"
    stub.write_text("".join(f"New Loadshape.{n} npts=1 interval=1 mult=[1] qmult=[1]\n" for n in sorted(shapes(sub_dir))))
    lines = []
    for line in (sub_dir / "Master.dss").read_text().splitlines():
        if not KEEP.match(line):
            continue
        if re.match(r"\s*redirect\s+\S*loadshapes\.dss", line, re.I):
            if "_cim_loadshapes" not in "".join(lines):
                lines.append(f"Redirect {stub.name}")
            continue
        lines.append(line)
    master = sub_dir / "_cim_master.dss"
    master.write_text("\n".join(lines) + "\n")
    coords = sub_dir / "_cim_buscoords.dss"
    coords.write_text("".join(f.read_text() + "\n" for f in sorted(sub_dir.rglob("Buscoords.dss"))))
    return master


def source(region: str, substation: str) -> Source:
    d = substation_dir(region, substation)
    return Source(key=f"smartds:{region}:{substation}", region=region, label=substation,
                  master=write_master(d), coords=d / "_cim_buscoords.dss", lonlat=True,
                  tag_prefix=f"{region}_{substation}".upper())


def load(region: str, substation: str, url: str | None = None) -> LoadReport:
    ld = DssCimLoader(source(region, substation))
    rows = ld.build()
    fixture = substation_dir(region, substation).parent / f"{substation}_solution.json"
    fixture.write_text(json.dumps(ld.m.fixture(substation, ld.buses)))
    with psycopg.connect(url or database_url()) as conn:
        with conn.transaction():
            write_rows(conn, rows)
            ld.report.counts["network_edges"] = rebuild_edges(conn, region, grid_tier="Distribution")
        analyze(conn)
    return ld.report


# --- time series -------------------------------------------------------------------------------------------------
def read_profile(path: Path) -> list[float]:
    return [float(x) for x in path.read_text().split()]


def profile_window(day: date) -> slice:
    d = day.replace(year=2018) if not (day.month == 2 and day.day == 29) else date(2018, 2, 28)
    start = (d.timetuple().tm_yday - 1) * STEPS_PER_DAY
    return slice(start, start + STEPS_PER_DAY)


def ingest_day(region: str, substation: str, day: date, max_profiles: int, url: str | None = None) -> dict:
    url = url or database_url()
    with psycopg.connect(url, row_factory=dict_row) as conn:
        loads = conn.execute(REGION_LOADS, {"region": region, "oid": f"smartds:{region}:{substation}:%"}).fetchall()
    shape_files = shapes(substation_dir(region, substation))
    usage = Counter(r["profile"].lower() for r in loads if r["profile"])
    chosen = [s for s, _ in usage.most_common(max_profiles) if s in shape_files]
    prof_dir = DATA_DIR / region / "profiles"
    prefix = f"{PREFIX.format(city=city(region), region=region)}/profiles/"
    files = sorted({f for s in chosen for f in shape_files[s].values()})
    with ThreadPoolExecutor(16) as ex:
        list(ex.map(lambda f: fetch(prefix + f, prof_dir / f), files))
    window = profile_window(day)
    curves = {s: {k: read_profile(prof_dir / f)[window] for k, f in shape_files[s].items()} for s in chosen}
    start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    samples, covered = [], 0
    for r in loads:
        c = curves.get((r["profile"] or "").lower())
        if not c:
            continue
        covered += 1
        for i in range(STEPS_PER_DAY):
            ts = start + timedelta(minutes=15 * i)
            samples.append(Sample(r["p_tag"], ts, float(r["p_fixed"]) * c["mult"][i], 0, SOURCE_AMI))
            if "qmult" in c:
                samples.append(Sample(r["q_tag"], ts, float(r["q_fixed"]) * c["qmult"][i], 0, SOURCE_AMI))
    from ingest.archive import archive
    from ingest.writer import TelemetryWriter

    ds = LocalDataSource(url)
    try:
        writer = TelemetryWriter(ds)
        written = sum(writer.write_batch(samples[i:i + 20_000]).written for i in range(0, len(samples), 20_000))
    finally:
        ds.close()
    with psycopg.connect(url) as conn:
        path, n = archive(conn, day, Path(os.environ.get("HISTORY_DIR", "data/parquet")))
    return {"loads": len(loads), "loads_with_profile": covered, "profiles": len(chosen), "samples": written,
            "archive": str(path), "archived_rows": n}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="P1R")
    ap.add_argument("--substation", default="p1rhs0_1247")
    ap.add_argument("--download", action="store_true", help="fetch the substation's DSS files from OEDI first")
    ap.add_argument("--skip-model", action="store_true", help="only run --timeseries")
    ap.add_argument("--timeseries", type=date.fromisoformat, help="ingest this day's load profiles")
    ap.add_argument("--max-profiles", type=int, default=40, help="most-used profiles to fetch (~1.4 MB each pair)")
    args = ap.parse_args()
    if args.download or not (substation_dir(args.region, args.substation) / "Master.dss").exists():
        log.info("downloaded %d files", download_substation(args.region, args.substation))
    if not args.skip_model:
        for line in load(args.region, args.substation).lines():
            log.info(line)
    if args.timeseries:
        log.info(json.dumps(ingest_day(args.region, args.substation, args.timeseries, args.max_profiles)))


if __name__ == "__main__":
    main()
