# SPDX-License-Identifier: Apache-2.0
"""The adapter seam: the only interfaces tools/ and compiler/ may depend on."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

ALL_REGIONS = "*"


@dataclass(frozen=True)
class Principal:
    name: str
    region_codes: list[str]

    def can_see(self, region_code: str) -> bool:
        return ALL_REGIONS in self.region_codes or region_code in self.region_codes


@dataclass
class ModelVersion:
    model_id: UUID
    root_mrid: UUID
    root_name: str
    case_kind: str
    region_code: str
    cim_updated_at: datetime
    telemetry_ts: datetime | None
    counts: dict[str, int]
    loads_measured: int
    loads_allocated: int
    loads_defaulted: int
    defaulted_impedances: int
    stale_switches: int
    allocation_ratio: float | None
    principal: str = ""
    warnings: list[str] = field(default_factory=list)
    modified: bool = False  # the session circuit was changed by set_control after compile

    def report(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("model_id", "root_mrid"):
            d[k] = str(d[k])
        for k in ("cim_updated_at", "telemetry_ts"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass
class StudyResult:
    study_id: UUID
    model_id: UUID
    tool: str
    inputs: dict[str, Any]
    data: dict[str, Any]
    warnings: list[str]
    duration_ms: int


@dataclass(frozen=True)
class Sample:
    tag: str
    ts: datetime
    value: float
    quality: int = 0
    source: int = 1


class TxCursor(Protocol):
    def copy_rows(self, table: str, rows: Iterable[tuple], on_conflict: str = "DO NOTHING") -> int: ...
    def upsert_latest(self, latest: Mapping[UUID, tuple[datetime, float, int, int]]) -> None: ...
    def upsert_switch_open(self, switches: Mapping[UUID, tuple[datetime, bool]]) -> None: ...


class DataSource(Protocol):
    rollup_table: str  # 'measurement_value_15m'
    source_name: str  # adapter name reported in meta.source, e.g. 'local'

    def query(self, sql: str, params: Mapping[str, Any], region_codes: list[str]) -> list[dict]: ...
    def load_tag_map(self) -> dict[str, tuple[UUID, str, UUID | None]]: ...  # tag -> (meas mrid, type, switch mrid)
    def tx(self) -> AbstractContextManager[TxCursor]: ...


class StudySink(Protocol):
    def write_model(self, m: ModelVersion) -> None: ...
    def write_result(self, r: StudyResult) -> None: ...  # called async, never blocks the tool response
    def recent(self, root_mrid: UUID, limit: int = 20) -> list[dict]: ...


class SnapshotMirror(Protocol):
    """Shares a session's snapshot directory between replicas that do not share a filesystem."""

    def push(self, session_id: str, local_dir: Path) -> None: ...
    def pull(self, session_id: str, local_dir: Path) -> bool: ...  # True when the session existed remotely


class AuthError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Auth(Protocol):
    def principal(self, request: Any) -> Principal: ...  # raise AuthError(401|403)
