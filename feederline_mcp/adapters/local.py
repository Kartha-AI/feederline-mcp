# SPDX-License-Identifier: Apache-2.0
"""Local adapter: one Postgres (psycopg pool), the studies schema as sink, static bearer or no auth."""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .base import ALL_REGIONS, AuthError, ModelVersion, Principal, StudyResult

log = logging.getLogger(__name__)

SET_REGIONS = "SELECT set_config('app.region_codes', %(regions)s, true)"

TAG_MAP = """
SELECT rs.name AS tag, m.mrid AS measurement_mrid, m.measurement_type, sw.mrid AS switch_mrid
FROM remote_source rs
JOIN measurement m ON m.mrid = rs.measurement_mrid
LEFT JOIN switch sw ON sw.mrid = m.power_system_resource_mrid
"""

UPSERT_LATEST = """
INSERT INTO measurement_value_latest (measurement_mrid, ts, value, quality, source)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (measurement_mrid) DO UPDATE
SET ts = EXCLUDED.ts, value = EXCLUDED.value, quality = EXCLUDED.quality, source = EXCLUDED.source
WHERE measurement_value_latest.ts <= EXCLUDED.ts
"""

UPSERT_SWITCH_OPEN = """
UPDATE switch SET open = %s, open_ts = %s
WHERE mrid = %s AND (open_ts IS NULL OR open_ts <= %s)
"""

INSERT_MODEL = """
INSERT INTO studies.models (model_id, principal, root_mrid, root_name, case_kind, region_code,
                            cim_updated_at, telemetry_ts, compile_report)
VALUES (%(model_id)s, %(principal)s, %(root_mrid)s, %(root_name)s, %(case_kind)s, %(region_code)s,
        %(cim_updated_at)s, %(telemetry_ts)s, %(compile_report)s)
ON CONFLICT (model_id) DO NOTHING
"""

INSERT_RESULT = """
INSERT INTO studies.results (study_id, model_id, tool, inputs, data, warnings, duration_ms)
VALUES (%(study_id)s, %(model_id)s, %(tool)s, %(inputs)s, %(data)s, %(warnings)s, %(duration_ms)s)
ON CONFLICT (study_id) DO NOTHING
"""

RECENT = """
SELECT r.study_id, r.created_at, r.tool, r.inputs, r.data, r.warnings, r.duration_ms,
       m.model_id, m.root_mrid, m.root_name, m.case_kind, m.cim_updated_at
FROM studies.results r JOIN studies.models m USING (model_id)
WHERE m.root_mrid = %(root)s
ORDER BY r.created_at DESC LIMIT %(limit)s
"""


def regions_setting(region_codes: list[str]) -> str:
    return ALL_REGIONS if ALL_REGIONS in region_codes else ",".join(region_codes)


class LocalTxCursor:
    def __init__(self, cur):
        self.cur = cur

    def copy_rows(self, table: str, rows: Iterable[tuple], on_conflict: str = "DO NOTHING") -> int:
        if table != "measurement_value" or on_conflict != "DO NOTHING":
            raise ValueError("copy_rows supports measurement_value with ON CONFLICT DO NOTHING only")
        rows = list(rows)
        if not rows:
            return 0
        self.cur.execute("CREATE TEMP TABLE IF NOT EXISTS _mv_stage (LIKE measurement_value) ON COMMIT DELETE ROWS")
        with self.cur.copy("COPY _mv_stage (ts, measurement_mrid, value, quality, source) FROM STDIN") as cp:
            for r in rows:
                cp.write_row(r)
        self.cur.execute(
            "SELECT ensure_measurement_value_partition(d::date) FROM (SELECT DISTINCT ts::date AS d FROM _mv_stage) s"
        )
        self.cur.execute(
            "INSERT INTO measurement_value SELECT * FROM _mv_stage ON CONFLICT DO NOTHING"
        )
        return self.cur.rowcount

    def upsert_latest(self, latest: Mapping[UUID, tuple[datetime, float, int, int]]) -> None:
        if latest:
            self.cur.executemany(UPSERT_LATEST, [(m, *v) for m, v in latest.items()])

    def upsert_switch_open(self, switches: Mapping[UUID, tuple[datetime, bool]]) -> None:
        if switches:
            self.cur.executemany(UPSERT_SWITCH_OPEN, [(is_open, ts, m, ts) for m, (ts, is_open) in switches.items()])


class LocalDataSource:
    rollup_table = "measurement_value_15m"
    source_name = "local"

    def __init__(self, url: str, min_size: int = 1, max_size: int = 8):
        self.pool = ConnectionPool(url, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row}, open=True)

    def query(self, sql: str, params: Mapping[str, Any], region_codes: list[str]) -> list[dict]:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(SET_REGIONS, {"regions": regions_setting(region_codes)})
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def load_tag_map(self) -> dict[str, tuple[UUID, str, UUID | None]]:
        rows = self.query(TAG_MAP, {}, [ALL_REGIONS])
        return {r["tag"]: (r["measurement_mrid"], r["measurement_type"], r["switch_mrid"]) for r in rows}

    @contextmanager
    def tx(self) -> Iterator[LocalTxCursor]:
        with self.pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute(SET_REGIONS, {"regions": ALL_REGIONS})
            yield LocalTxCursor(cur)

    def close(self) -> None:
        self.pool.close()


class PostgresStudySink:
    def __init__(self, ds: LocalDataSource):
        self.pool = ds.pool

    def write_model(self, m: ModelVersion) -> None:
        params = {
            "model_id": m.model_id, "principal": m.principal or "unknown", "root_mrid": m.root_mrid,
            "root_name": m.root_name, "case_kind": m.case_kind, "region_code": m.region_code,
            "cim_updated_at": m.cim_updated_at, "telemetry_ts": m.telemetry_ts, "compile_report": Jsonb(m.report()),
        }
        with self.pool.connection() as conn:
            conn.execute(INSERT_MODEL, params)

    def write_result(self, r: StudyResult) -> None:
        params = {
            "study_id": r.study_id, "model_id": r.model_id, "tool": r.tool,
            "inputs": Jsonb(json.loads(json.dumps(r.inputs, default=str))),
            "data": Jsonb(json.loads(json.dumps(r.data, default=str))),
            "warnings": Jsonb(r.warnings), "duration_ms": r.duration_ms,
        }
        with self.pool.connection() as conn:
            conn.execute(INSERT_RESULT, params)

    def recent(self, root_mrid: UUID, limit: int = 20) -> list[dict]:
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(RECENT, {"root": root_mrid, "limit": limit})
            return cur.fetchall()


class BearerAuth:
    """Static bearer token; the local principal sees all regions unless MCP_REGIONS narrows it."""

    def __init__(self, token: str, regions: list[str] | None = None, name: str = "local"):
        if not token:
            raise ValueError("MCP_BEARER_TOKEN is required when MCP_AUTH is on")
        self.token = token
        self.regions = regions or [ALL_REGIONS]
        self.name = name

    def principal(self, request: Any) -> Principal:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AuthError(401, "missing bearer token")
        if not hmac.compare_digest(token.strip(), self.token):
            raise AuthError(403, "invalid bearer token")
        return Principal(self.name, list(self.regions))


class NoAuth:
    def __init__(self, regions: list[str] | None = None):
        self.regions = regions or [ALL_REGIONS]

    def principal(self, request: Any) -> Principal:
        return Principal("local", list(self.regions))



def build(settings: Any) -> tuple[LocalDataSource, PostgresStudySink, BearerAuth | NoAuth]:
    """Entry point `local` in the `feederline_mcp.adapters` group: one Postgres, studies schema, bearer or no auth."""
    ds = LocalDataSource(settings.database_url)
    auth = NoAuth(settings.regions) if settings.auth_mode == "off" else BearerAuth(settings.bearer_token, settings.regions)
    return ds, PostgresStudySink(ds), auth
