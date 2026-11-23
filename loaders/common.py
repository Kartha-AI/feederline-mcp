# SPDX-License-Identifier: Apache-2.0
"""Shared loader plumbing: deterministic mRIDs, upserts by mrid, network_edges rebuild, ANALYZE, reports."""

from __future__ import annotations

import logging
import os
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://kartha.energy/cim")

# Two-terminal conducting equipment becomes pgRouting edges.
EDGE_CLASSES = ("ACLineSegment", "PowerTransformer", "Breaker", "Recloser", "LoadBreakSwitch", "Disconnector", "Fuse")
SWITCH_CLASSES = ("Breaker", "Recloser", "LoadBreakSwitch", "Disconnector", "Fuse")

MODEL_TABLES = (
    "location", "base_voltage", "substation", "voltage_level", "line", "connectivity_node", "conducting_equipment",
    "ac_line_segment", "switch", "power_transformer", "power_transformer_end", "energy_consumer", "terminal",
    "measurement", "analog", "discrete", "remote_unit", "remote_source", "measurement_value_latest", "network_edges",
)

REBUILD_EDGES_DELETE = "DELETE FROM network_edges WHERE region_code = %(region)s"

REBUILD_EDGES_INSERT = """
INSERT INTO network_edges (id, equipment_mrid, cim_class, source, target, cost, reverse_cost, region_code, grid_tier)
SELECT ce.routing_id, ce.mrid, ce.cim_class, cn1.routing_id, cn2.routing_id,
       greatest(coalesce(seg.length, 1), 1)::float8, greatest(coalesce(seg.length, 1), 1)::float8,
       ce.region_code, %(grid_tier)s
FROM conducting_equipment ce
JOIN terminal t1 ON t1.conducting_equipment_mrid = ce.mrid AND t1.sequence_number = 1
JOIN terminal t2 ON t2.conducting_equipment_mrid = ce.mrid AND t2.sequence_number = 2
JOIN connectivity_node cn1 ON cn1.mrid = t1.connectivity_node_mrid
JOIN connectivity_node cn2 ON cn2.mrid = t2.connectivity_node_mrid
LEFT JOIN ac_line_segment seg ON seg.mrid = ce.mrid
WHERE ce.region_code = %(region)s AND ce.valid_to IS NULL AND ce.in_service
  AND ce.cim_class = ANY(%(classes)s)
"""

# Columns the database generates; never written by loaders.
GENERATED = {"geom_proj", "routing_id"}


def mrid(source: str, kind: str, ident: Any) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{source}:{kind}:{ident}")


def database_url() -> str:
    """DATABASE_URL, defaulting to the local loader login."""
    return os.environ.get("DATABASE_URL", "postgresql://grid_load:grid_load@localhost:5433/grid")


@dataclass
class LoadReport:
    source: str
    counts: Counter = field(default_factory=Counter)
    unmatched: list[str] = field(default_factory=list)
    defaults: Counter = field(default_factory=Counter)

    def lines(self) -> list[str]:
        out = [f"[{self.source}] " + ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))]
        if self.defaults:
            out.append(f"[{self.source}] defaults: " + ", ".join(f"{k}={v}" for k, v in sorted(self.defaults.items())))
        if self.unmatched:
            out.append(f"[{self.source}] unmatched: {len(self.unmatched)} " + ", ".join(self.unmatched[:20]))
        return out


@dataclass
class Rows:
    """CIM rows by table, in insert (FK) order."""

    tables: dict[str, list[dict]] = field(default_factory=lambda: {t: [] for t in MODEL_TABLES if t != "network_edges"})

    def add(self, table: str, **row: Any) -> dict:
        self.tables[table].append(row)
        return row

    def counts(self) -> Counter:
        c = Counter({t: len(r) for t, r in self.tables.items() if r})
        for row in self.tables["conducting_equipment"]:
            c[row["cim_class"]] += 1
        return c


def _adapt(v: Any) -> Any:
    return Jsonb(v) if isinstance(v, dict | list) else v


def upsert(cur: psycopg.Cursor, table: str, rows: Sequence[Mapping[str, Any]], key: Sequence[str] = ("mrid",),
           geom_cols: Iterable[str] = ()) -> int:
    """INSERT ... ON CONFLICT (key) DO UPDATE for every non-key column. geom columns are WKT in, ST_GeomFromText."""
    if not rows:
        return 0
    cols = [c for c in rows[0] if c not in GENERATED]
    geom_cols = set(geom_cols)
    values = sql.SQL(", ").join(
        sql.SQL("ST_GeomFromText({}, 4326)").format(sql.Placeholder()) if c in geom_cols else sql.Placeholder()
        for c in cols
    )
    updates = [c for c in cols if c not in key]
    set_clause = sql.SQL(", ").join(
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c)) for c in updates
    )
    if table in ("conducting_equipment", "substation"):
        set_clause = sql.SQL(", ").join([set_clause, sql.SQL("updated_at = now()")])
    conflict = sql.SQL("DO UPDATE SET {}").format(set_clause) if updates else sql.SQL("DO NOTHING")
    stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) {}").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, cols)),
        values,
        sql.SQL(", ").join(map(sql.Identifier, key)),
        conflict,
    )
    cur.executemany(stmt, [tuple(_adapt(r[c]) for c in cols) for r in rows])
    return len(rows)


def write_rows(conn: psycopg.Connection, rows: Rows) -> None:
    keys = {
        "power_transformer_end": ("power_transformer_mrid", "end_number"),
        "measurement_value_latest": ("measurement_mrid",),
    }
    with conn.cursor() as cur:
        for table, data in rows.tables.items():
            if data:
                upsert(cur, table, data, key=keys.get(table, ("mrid",)), geom_cols=("geom",) if table == "location" else ())


def rebuild_edges(conn: psycopg.Connection, region: str, grid_tier: str) -> int:
    with conn.cursor() as cur:
        cur.execute(REBUILD_EDGES_DELETE, {"region": region})
        cur.execute(REBUILD_EDGES_INSERT, {"region": region, "grid_tier": grid_tier, "classes": list(EDGE_CLASSES)})
        return cur.rowcount


def analyze(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        for t in MODEL_TABLES:
            cur.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(t)))
