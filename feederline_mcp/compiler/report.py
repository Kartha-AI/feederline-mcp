# SPDX-License-Identifier: Apache-2.0
"""Load seeding, compile report, warnings and the ModelVersion."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..adapters.base import ModelVersion
from .emit import LoadSeed
from .validate import DEFAULT_SCAN_S, GOOD_QUALITY, fresh, stale_switches  # noqa: F401  (shared predicates)

RATIO_BOUNDS = (0.05, 5.0)


@dataclass
class Head:
    p_kw: float | None = None
    q_kvar: float | None = None
    v_kv: float | None = None
    ts: datetime | None = None

    @classmethod
    def from_rows(cls, rows: Sequence[Mapping[str, Any]], now: datetime) -> Head:
        h = cls()
        for r in rows:
            if not fresh(r["value"], r["ts"], r["quality"], r["scan_interval"], now):
                continue
            if r["measurement_type"] == "ThreePhaseActivePower":
                h.p_kw = float(r["value"])
            elif r["measurement_type"] == "ThreePhaseReactivePower":
                h.q_kvar = float(r["value"])
            elif r["measurement_type"] == "LineToLineVoltage":
                h.v_kv = float(r["value"])
            h.ts = max(filter(None, (h.ts, r["ts"])))
        return h


@dataclass
class Seeding:
    loads: list[LoadSeed]
    measured: int = 0
    allocated: int = 0
    defaulted: int = 0
    ratio: float | None = None
    telemetry_ts: datetime | None = None
    warnings: list[str] = field(default_factory=list)


def seed_loads(loads: Sequence[Mapping[str, Any]], head: Head, der_kw: float, case: str, now: datetime) -> Seeding:
    """Measured P/Q when good and fresh; else p_fixed x allocation ratio; else p_fixed (planning / no head)."""
    s = Seeding(loads=[])
    measured, unmeasured = [], []
    for r in loads:
        if case == "operational" and fresh(r["p_value"], r["p_ts"], r["p_quality"], r["p_scan"], now):
            measured.append(r)
        else:
            unmeasured.append(r)
    for r in measured:
        q = float(r["q_value"]) if fresh(r["q_value"], r["q_ts"], r["q_quality"], r["p_scan"], now) else float(r["q_fixed"] or 0)
        s.loads.append(LoadSeed(r, float(r["p_value"]), q, 1, "measured"))
        s.telemetry_ts = max(filter(None, (s.telemetry_ts, r["p_ts"])))
    s.measured = len(measured)

    fixed = sum(float(r["p_fixed"] or 0) for r in unmeasured)
    if case == "operational" and unmeasured and head.p_kw is not None and fixed > 0:
        # Head P = loads + losses - DER, so the unmeasured share is head + DER - measured loads.
        ratio = (head.p_kw + der_kw - sum(ls.kw for ls in s.loads)) / fixed
        if RATIO_BOUNDS[0] <= ratio <= RATIO_BOUNDS[1]:
            s.ratio = ratio
        else:
            s.warnings.append(f"allocation ratio {ratio:.2f} outside {RATIO_BOUNDS}; unmeasured loads use p_fixed")
    for r in unmeasured:
        attrs = r.get("attrs") or {}
        p, q = float(r["p_fixed"] or 0), float(r["q_fixed"] or 0)
        if s.ratio is not None:
            s.loads.append(LoadSeed(r, p * s.ratio, q * s.ratio, 1, "allocated"))
            s.allocated += 1
        else:
            s.loads.append(LoadSeed(r, p, q, int(attrs.get("model", 1)), "defaulted"))
            s.defaulted += 1
    if case == "operational" and s.defaulted:
        s.warnings.append(f"{s.defaulted} loads use nameplate p_fixed (no fresh measurement or feeder-head allocation)")
    if head.ts:
        s.telemetry_ts = max(filter(None, (s.telemetry_ts, head.ts)))
    return s


def model_version(
    *, root: Mapping[str, Any], case: str, region_code: str, principal: str, counts: dict[str, int],
    rows: Sequence[Sequence[Mapping[str, Any]]], seeding: Seeding, defaulted_impedances: int, stale: int,
    warnings: list[str],
) -> ModelVersion:
    stamps = [r["updated_at"] for group in rows for r in group if r.get("updated_at")]
    cim_updated_at = max(stamps + [root["updated_at"]])
    return ModelVersion(
        model_id=uuid.uuid4(), root_mrid=root["mrid"], root_name=root["name"], case_kind=case, region_code=region_code,
        cim_updated_at=cim_updated_at, telemetry_ts=seeding.telemetry_ts, counts=counts,
        loads_measured=seeding.measured, loads_allocated=seeding.allocated, loads_defaulted=seeding.defaulted,
        defaulted_impedances=defaulted_impedances, stale_switches=stale, allocation_ratio=seeding.ratio,
        principal=principal, warnings=warnings,
    )
