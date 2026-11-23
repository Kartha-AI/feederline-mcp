# SPDX-License-Identifier: Apache-2.0
"""HIFLD-schema substations and transmission lines -> CIM (transmission tier).

    python -m loaders.hifld --state CA

Features are paged as GeoJSON from ArcGIS REST into staging_hifld_substations / staging_hifld_lines (no GDAL
needed), then merged in SQL: one substation + location per point, one voltage level and connectivity node per
(substation, line voltage), and one ACLineSegment per line part whose two ends snap to different substations within
500 m. Ends that do not snap are reported, never guessed. mRIDs are uuid5(NAMESPACE, "hifld:<kind>:<id>"), computed
in SQL with the same namespace the Python loaders use. region_code is "<STATE>-<County>" of the (first) substation.

HIFLD Open was retired in 2025; the defaults point at public mirrors with the HIFLD schema (California Energy
Commission substations carrying HIFLD_IDs, and the HIFLD US transmission lines layer).
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import psycopg

from .common import NAMESPACE, LoadReport, analyze, database_url, rebuild_edges

log = logging.getLogger("loaders.hifld")

PAGE = 2000
SNAP_M = 500


@dataclass(frozen=True)
class StateSource:
    substations_url: str
    bbox: tuple[float, float, float, float]  # lon/lat envelope for the lines query
    fields: dict[str, str]  # staging column -> substation feature property


LINES_URL = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
             "US_Electric_Power_Transmission_Lines/FeatureServer/0")
STATES = {
    "CA": StateSource(
        "https://services.arcgis.com/hVnyNvwbpFFPDV5j/arcgis/rest/services/CA_Substations_Final/FeatureServer/16",
        (-124.5, 32.5, -114.1, 42.05),
        {"id": "HIFLD_ID", "name": "Name", "owner": "Owner", "state": "STATE", "county": "COUNTY",
         "max_voltage": "Max_Voltag", "status": "STATUS"},
    ),
}

INSERT_SUB = """
INSERT INTO staging_hifld_substations (id, name, owner, state, county, max_voltage, status, props, geom)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_Force2D(ST_GeomFromGeoJSON(%s)), 4326))
ON CONFLICT (id) DO NOTHING
"""
INSERT_LINE = """
INSERT INTO staging_hifld_lines (id, owner, voltage, volt_class, type, status, props, geom)
VALUES (%s, %s, %s, %s, %s, %s, %s, ST_Multi(ST_SetSRID(ST_Force2D(ST_GeomFromGeoJSON(%s)), 4326)))
ON CONFLICT (id) DO NOTHING
"""

# Region code from state and county: "CA-Alameda".
REGION_SQL = ("s.state || '-' || regexp_replace(regexp_replace(coalesce(s.county, 'Unknown'), ' County$', ''), "
              "'[^A-Za-z0-9]+', '', 'g')")

MERGE = f"""
CREATE TEMP TABLE hifld_sub ON COMMIT DROP AS
SELECT s.*, {REGION_SQL} AS region_code,
       uuid_generate_v5(%(ns)s, 'hifld:substation:' || s.id) AS sub_mrid,
       uuid_generate_v5(%(ns)s, 'hifld:location:sub:' || s.id) AS loc_mrid
FROM staging_hifld_substations s
WHERE coalesce(upper(s.status), 'IN SERVICE') IN ('IN SERVICE', 'ACTIVE', '');

CREATE TEMP TABLE hifld_part ON COMMIT DROP AS
SELECT l.id || '-' || d.path[1] AS part_id, l.owner, nullif(greatest(l.voltage, 0), 0) AS kv, l.volt_class,
       d.geom AS geom,
       (SELECT h.id FROM hifld_sub h WHERE ST_DWithin(h.geom::geography, ST_StartPoint(d.geom)::geography, %(snap)s)
        ORDER BY h.geom <-> ST_StartPoint(d.geom) LIMIT 1) AS sub1,
       (SELECT h.id FROM hifld_sub h WHERE ST_DWithin(h.geom::geography, ST_EndPoint(d.geom)::geography, %(snap)s)
        ORDER BY h.geom <-> ST_EndPoint(d.geom) LIMIT 1) AS sub2
FROM staging_hifld_lines l, LATERAL ST_Dump(l.geom) d
WHERE coalesce(upper(l.status), 'IN SERVICE') = 'IN SERVICE';

CREATE TEMP TABLE hifld_ok ON COMMIT DROP AS
SELECT p.*, uuid_generate_v5(%(ns)s, 'hifld:line:' || p.part_id) AS ce_mrid
FROM hifld_part p WHERE p.sub1 IS NOT NULL AND p.sub2 IS NOT NULL AND p.sub1 <> p.sub2;

-- (substation, kV) pairs: every snapped line end plus each substation's own maximum voltage
CREATE TEMP TABLE hifld_level ON COMMIT DROP AS
SELECT DISTINCT x.sub_id, x.kv FROM (
    SELECT sub1 AS sub_id, kv FROM hifld_ok UNION SELECT sub2, kv FROM hifld_ok
    UNION SELECT id, nullif(greatest(max_voltage, 0), 0) FROM hifld_sub
) x;

INSERT INTO base_voltage (mrid, nominal_voltage)
SELECT DISTINCT uuid_generate_v5(%(ns)s, 'cim:base_voltage:' || to_char(kv::numeric(8,3), 'FM999990.000')), kv::numeric(8,3)
FROM hifld_level WHERE kv IS NOT NULL
ON CONFLICT DO NOTHING;

INSERT INTO location (mrid, name, geom)
SELECT loc_mrid, name, geom FROM hifld_sub
ON CONFLICT (mrid) DO UPDATE SET name = EXCLUDED.name, geom = EXCLUDED.geom;

INSERT INTO substation (mrid, name, description, gis_oid, region_code, location_mrid)
SELECT sub_mrid, coalesce(nullif(trim(name), ''), 'Substation ' || id), owner, 'hifld:substation:' || id, region_code, loc_mrid
FROM hifld_sub
ON CONFLICT (mrid) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description,
    region_code = EXCLUDED.region_code, updated_at = now();

INSERT INTO voltage_level (mrid, name, substation_mrid, base_voltage_mrid)
SELECT uuid_generate_v5(%(ns)s, 'hifld:voltage_level:' || l.sub_id || ':' || l.kv), h.name || ' ' || l.kv || ' kV',
       h.sub_mrid, uuid_generate_v5(%(ns)s, 'cim:base_voltage:' || to_char(l.kv::numeric(8,3), 'FM999990.000'))
FROM hifld_level l JOIN hifld_sub h ON h.id = l.sub_id WHERE l.kv IS NOT NULL
ON CONFLICT (mrid) DO UPDATE SET name = EXCLUDED.name;

INSERT INTO connectivity_node (mrid, name, connectivity_node_container_mrid, region_code)
SELECT uuid_generate_v5(%(ns)s, 'hifld:connectivity_node:' || l.sub_id || ':' || coalesce(l.kv::text, 'na')),
       coalesce(h.name, h.id) || ' ' || coalesce(l.kv::text || ' kV', 'bus'),
       CASE WHEN l.kv IS NOT NULL THEN uuid_generate_v5(%(ns)s, 'hifld:voltage_level:' || l.sub_id || ':' || l.kv) END,
       h.region_code
FROM hifld_level l JOIN hifld_sub h ON h.id = l.sub_id
ON CONFLICT (mrid) DO UPDATE SET name = EXCLUDED.name, region_code = EXCLUDED.region_code;

INSERT INTO location (mrid, name, geom)
SELECT uuid_generate_v5(%(ns)s, 'hifld:location:line:' || part_id), part_id, geom FROM hifld_ok
ON CONFLICT (mrid) DO UPDATE SET geom = EXCLUDED.geom;

INSERT INTO conducting_equipment (mrid, cim_class, name, description, gis_oid, region_code, base_voltage_mrid,
                                  location_mrid, attrs)
SELECT o.ce_mrid, 'ACLineSegment',
       coalesce(nullif(o.owner, 'NOT AVAILABLE'), 'Line') || ' ' || coalesce(o.kv::text || ' kV ', '') || o.part_id,
       o.volt_class, 'hifld:line:' || o.part_id, h.region_code,
       CASE WHEN o.kv IS NOT NULL THEN uuid_generate_v5(%(ns)s, 'cim:base_voltage:' || to_char(o.kv::numeric(8,3), 'FM999990.000')) END,
       uuid_generate_v5(%(ns)s, 'hifld:location:line:' || o.part_id),
       jsonb_build_object('source', 'hifld', 'volt_class', o.volt_class)
FROM hifld_ok o JOIN hifld_sub h ON h.id = o.sub1
ON CONFLICT (mrid) DO UPDATE SET name = EXCLUDED.name, region_code = EXCLUDED.region_code,
    base_voltage_mrid = EXCLUDED.base_voltage_mrid, attrs = EXCLUDED.attrs, updated_at = now();

INSERT INTO ac_line_segment (mrid, length)
SELECT ce_mrid, round(ST_Length(geom::geography)::numeric, 2) FROM hifld_ok
ON CONFLICT (mrid) DO UPDATE SET length = EXCLUDED.length;

INSERT INTO terminal (mrid, conducting_equipment_mrid, connectivity_node_mrid, sequence_number, phases)
SELECT uuid_generate_v5(%(ns)s, 'hifld:terminal:' || o.part_id || ':' || t.seq), o.ce_mrid,
       uuid_generate_v5(%(ns)s, 'hifld:connectivity_node:' || CASE t.seq WHEN 1 THEN o.sub1 ELSE o.sub2 END
                        || ':' || coalesce(o.kv::text, 'na')),
       t.seq, 'ABC'
FROM hifld_ok o CROSS JOIN (VALUES (1), (2)) t(seq)
ON CONFLICT (mrid) DO UPDATE SET connectivity_node_mrid = EXCLUDED.connectivity_node_mrid;
"""

REPORT = """
SELECT (SELECT count(*) FROM hifld_sub) AS substations,
       (SELECT count(*) FROM hifld_part) AS line_parts,
       (SELECT count(*) FROM hifld_ok) AS snapped,
       (SELECT count(*) FROM hifld_part WHERE sub1 IS NULL OR sub2 IS NULL) AS unmatched_ends,
       (SELECT count(*) FROM hifld_part WHERE sub1 IS NOT NULL AND sub1 = sub2) AS self_loops,
       (SELECT count(*) FROM hifld_ok WHERE kv IS NULL) AS unknown_voltage,
       (SELECT array_agg(DISTINCT h.region_code) FROM hifld_sub h) AS regions
"""


def pages(url: str, extra: dict[str, str] | None = None) -> Iterator[dict]:
    base = {"where": "1=1", "outFields": "*", "f": "geojson", "outSR": "4326", "orderByFields": "OBJECTID",
            **(extra or {})}
    count_q = urllib.parse.urlencode({**base, "returnCountOnly": "true", "f": "json"})
    with urllib.request.urlopen(f"{url}/query?{count_q}", timeout=120) as r:
        total = json.load(r)["count"]

    def page(offset: int) -> list[dict]:
        q = urllib.parse.urlencode({**base, "resultOffset": offset, "resultRecordCount": PAGE})
        with urllib.request.urlopen(f"{url}/query?{q}", timeout=120) as r:
            return json.load(r)["features"]

    with ThreadPoolExecutor(6) as ex:
        for feats in ex.map(page, range(0, total, PAGE)):
            yield from feats


def stage(conn: psycopg.Connection, state: str, src: StateSource, lines_url: str) -> dict[str, int]:
    f = src.fields
    subs, lines = [], []
    for ft in pages(src.substations_url):
        p, g = ft["properties"], ft.get("geometry")
        if not g:
            continue
        ident = str(p.get(f["id"]) or f"oid{p.get('OBJECTID')}")
        subs.append((ident, p.get(f["name"]), p.get(f["owner"]), p.get(f["state"]) or state, p.get(f["county"]),
                     p.get(f["max_voltage"]), p.get(f["status"]), json.dumps(p), json.dumps(g)))
    env = {"geometry": ",".join(map(str, src.bbox)), "geometryType": "esriGeometryEnvelope", "inSR": "4326",
           "spatialRel": "esriSpatialRelIntersects"}
    for ft in pages(lines_url, env):
        p, g = ft["properties"], ft.get("geometry")
        if not g:
            continue
        lines.append((str(p.get("ID") or p.get("OBJECTID")), p.get("OWNER"), p.get("VOLTAGE"), p.get("VOLT_CLASS"),
                      p.get("TYPE"), p.get("STATUS"), json.dumps(p), json.dumps(g)))
    with conn.cursor() as cur:
        cur.execute("TRUNCATE staging_hifld_substations, staging_hifld_lines")
        cur.executemany(INSERT_SUB, subs)
        cur.executemany(INSERT_LINE, lines)
    return {"staged_substations": len(subs), "staged_lines": len(lines)}


def merge(conn: psycopg.Connection) -> dict:
    with conn.cursor() as cur:
        for stmt in filter(str.strip, MERGE.split(";\n")):  # one statement per execute (server-side binding)
            cur.execute(stmt, {"ns": NAMESPACE, "snap": SNAP_M})
        cur.execute(REPORT)
        cols = [d.name for d in cur.description]
        report = dict(zip(cols, cur.fetchone(), strict=True))
    edges = sum(rebuild_edges(conn, region, grid_tier="Transmission") for region in report["regions"] or [])
    report["network_edges"] = edges
    report["regions"] = len(report["regions"] or [])
    return report


def load(state: str, url: str | None = None, lines_url: str = LINES_URL) -> LoadReport:
    src = STATES[state.upper()]
    rep = LoadReport(f"hifld:{state.upper()}")
    with psycopg.connect(url or database_url()) as conn:
        with conn.transaction():
            rep.counts.update(stage(conn, state.upper(), src, lines_url))
            m = merge(conn)
        analyze(conn)
    rep.counts.update({k: v for k, v in m.items() if k not in ("unmatched_ends", "self_loops", "unknown_voltage")})
    rep.defaults["unknown_voltage"] = m["unknown_voltage"]
    rep.unmatched = [f"{m['unmatched_ends']} line parts with an end > {SNAP_M} m from any substation",
                     f"{m['self_loops']} line parts with both ends at one substation"]
    return rep


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="CA", choices=sorted(STATES))
    ap.add_argument("--lines-url", default=LINES_URL)
    args = ap.parse_args()
    for line in load(args.state, lines_url=args.lines_url).lines():
        log.info(line)


if __name__ == "__main__":
    main()
