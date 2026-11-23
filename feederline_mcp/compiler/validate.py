# SPDX-License-Identifier: Apache-2.0
"""Default and missing-data predicates, shared by the compile report and validate_feeder.

The compiler counts defaulted impedances, defaulted loads and stale switches through these functions, and
validate_feeder runs the same functions over the same scoped rows without compiling, so the two always agree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from ..adapters.base import DataSource, Principal
from . import queries

GOOD_QUALITY = {0, 1, 4}  # good, questionable, substituted
DEFAULT_SCAN_S = 900.0
_PHASES = {"ABC": 3, "AB": 2, "BC": 2, "AC": 2, "A": 1, "B": 1, "C": 1, "s1": 1, "s2": 1, "s12": 2}


def fresh(value: Any, ts: datetime | None, quality: int | None, scan_s: Any, now: datetime) -> bool:
    """A measurement usable for seeding: good quality and younger than two scan intervals."""
    if value is None or ts is None or quality not in GOOD_QUALITY:
        return False
    return now - ts <= timedelta(seconds=2 * float(scan_s or DEFAULT_SCAN_S))


def has_phase_impedance(row: Mapping[str, Any]) -> bool:
    pz = (row.get("attrs") or {}).get("phase_impedance")
    n = _PHASES.get(row.get("phases1") or "ABC", 3)
    return bool(pz) and len(pz["r"]) == n * n


def segment_defaulted(row: Mapping[str, Any]) -> bool:
    """No phase impedance matrix and no CIM r/x: the compiler falls back to a conductor-type impedance."""
    return not has_phase_impedance(row) and (row.get("r") is None or row.get("x") is None)


def switch_stale(row: Mapping[str, Any], now: datetime) -> bool:
    """Switch state never timestamped, or older than two scan intervals of its position measurement."""
    ts = row.get("open_ts")
    return ts is None or now - ts > timedelta(seconds=2 * float(row.get("scan_interval") or DEFAULT_SCAN_S))


def stale_switches(switches: Sequence[Mapping[str, Any]], now: datetime) -> int:
    return sum(1 for r in switches if switch_stale(r, now))


@dataclass
class Finding:
    kind: str
    severity: str  # error | warning | info
    items: list[dict[str, Any]] = field(default_factory=list)

    def row(self, limit: int = 50) -> dict[str, Any]:
        return {"kind": self.kind, "severity": self.severity, "count": len(self.items),
                "mrids": [str(i["mrid"]) for i in self.items[:limit]], "names": [i["name"] for i in self.items[:limit]]}


def _ids(rows: Sequence[Mapping[str, Any]], cim_class: str | None = None) -> list[dict[str, Any]]:
    return [{"mrid": r["mrid"], "name": r["name"], "cim_class": r.get("cim_class", cim_class)} for r in rows]


def findings(ds: DataSource, principal: Principal, scope: list[UUID], root: Mapping[str, Any], case: str,
             now: datetime | None = None) -> list[Finding]:
    """Every predicate over a feeder scope, counting exactly what compile_feeder would count."""
    from .report import Head, seed_loads  # report imports emit, which imports this module

    now = now or datetime.now(UTC)
    regions = principal.region_codes
    p = {"scope": scope}
    segments = ds.query(queries.SEGMENTS, p, regions)
    switches = [s for s in ds.query(queries.SWITCHES, p, regions) if s["mrid"] != root["mrid"]]
    loads = ds.query(queries.LOADS, p, regions)
    ders = ds.query(queries.DER, p, regions)
    head = Head.from_rows(ds.query(queries.HEAD_MEASUREMENTS, {"root": root["mrid"]}, regions), now) \
        if case == "operational" else Head()
    der_kw = sum(float(d["p_value"]) for d in ders
                 if case == "operational" and fresh(d["p_value"], d["p_ts"], d["p_quality"], d["p_scan"], now))
    seeding = seed_loads(loads, head, der_kw, case, now)
    out = [
        Finding("defaulted_impedance", "warning", _ids([s for s in segments if segment_defaulted(s)], "ACLineSegment")),
        Finding("load_defaulted", "warning" if case == "operational" else "info",
                _ids([ls.row for ls in seeding.loads if ls.how == "defaulted"], "EnergyConsumer")),
        Finding("load_without_base_voltage", "error", _ids(ds.query(queries.LOADS_WITHOUT_BASE_VOLTAGE, p, regions))),
        Finding("transformer_end_without_terminal", "error", _ids(ds.query(queries.ENDS_WITHOUT_TERMINAL, p, regions))),
        Finding("no_location", "info", _ids(ds.query(queries.WITHOUT_LOCATION, p, regions))),
    ]
    if case == "operational":
        out += [
            Finding("switch_without_state_time", "warning",
                    _ids([s for s in switches if s["open_ts"] is None])),
            Finding("stale_switch", "warning", _ids([s for s in switches if switch_stale(s, now)])),
            Finding("stale_measured_load", "warning", _ids(
                [r for r in loads if r["p_value"] is not None
                 and not fresh(r["p_value"], r["p_ts"], r["p_quality"], r["p_scan"], now)], "EnergyConsumer")),
        ]
    return out
