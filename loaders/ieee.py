# SPDX-License-Identifier: Apache-2.0
"""IEEE 13/34/123 test feeders: DSS masters -> CIM rows, SCADA registry and latest values.

The DSS-to-CIM walk is loaders.dss_cim; this module names the feeders, writes the solved original circuit to
tests/fixtures/ieee<N>_solution.json and loads the rows (region IEEE<N>, grid tier Distribution).
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import psycopg

from .common import LoadReport, analyze, database_url, rebuild_edges, write_rows
from .dss_cim import DssCimLoader, DssModel, Source, layout  # noqa: F401  (layout re-exported for tests)

log = logging.getLogger("loaders.ieee")

ROOT = Path(__file__).resolve().parent.parent
FEEDER_DIR = ROOT / "tests" / "fixtures" / "feeders"
FIXTURE_DIR = ROOT / "tests" / "fixtures"

# master, coordinates, metres per coordinate unit, lon/lat of coordinate (0, 0)
FEEDERS = {
    13: ("ieee13/IEEE13Nodeckt.dss", "ieee13/IEEE13Node_BusXY.csv", 1.0, (-117.20, 32.80)),
    34: ("ieee34/ieee34Mod1.dss", "ieee34/IEEE34_BusXY.csv", 0.5, (-117.10, 32.80)),
    123: ("ieee123/IEEE123Master.dss", "ieee123/BusCoords.dat", 0.3048, (-117.00, 32.80)),
}


def source(n: int) -> Source:
    master, coords, scale, origin = FEEDERS[n]
    return Source(key=f"ieee{n}", region=f"IEEE{n}", label=f"IEEE{n}", master=FEEDER_DIR / master,
                  coords=FEEDER_DIR / coords, coord_scale_m=scale, origin=origin)


def IeeeLoader(n: int) -> DssCimLoader:  # noqa: N802  (kept as the loader's public constructor)
    return DssCimLoader(source(n))


def write_fixture(n: int, model: DssModel | None = None) -> Path:
    s = source(n)
    model = model or DssModel(s.master, s.coords)
    path = FIXTURE_DIR / f"ieee{n}_solution.json"
    path.write_text(json.dumps(model.fixture(f"IEEE{n}"), indent=1, sort_keys=True))
    return path


def load(n: int, url: str | None = None) -> LoadReport:
    ld = IeeeLoader(n)
    rows = ld.build()
    write_fixture(n, ld.m)
    with psycopg.connect(url or database_url()) as conn:
        with conn.transaction():
            write_rows(conn, rows)
            ld.report.counts["network_edges"] = rebuild_edges(conn, ld.region, grid_tier="Distribution")
        analyze(conn)
    return ld.report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feeders", nargs="+", type=int, default=[13, 34, 123], choices=sorted(FEEDERS))
    ap.add_argument("--fixtures-only", action="store_true")
    args = ap.parse_args()
    for n in args.feeders:
        if args.fixtures_only:
            log.info("fixture %s", write_fixture(n))
            continue
        for line in load(n).lines():
            log.info(line)


if __name__ == "__main__":
    main()
