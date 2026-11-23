# SPDX-License-Identifier: Apache-2.0
"""The result envelope and the validators every solve passes through."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from . import engine
from .adapters.base import ModelVersion

HEAD_BALANCE_TOL = 0.15
TRANSFORMER_OVERLOAD_X = 10.0
BUS_KV_TOL = 0.10


class ModelInfo(BaseModel):
    root_mrid: str
    root_name: str
    case_kind: str
    cim_updated_at: str
    telemetry_ts: str | None = None
    buses: int = 0
    loads: int = 0
    loads_measured: int = 0
    modified: bool = False  # set_control changed the in-memory circuit


class Meta(BaseModel):
    session_id: str
    study_id: str | None = None
    engine: str | None  # None for SQL-family tools (no engine context)
    duration_ms: int
    source: str


class ErrorInfo(BaseModel):
    model_config = ConfigDict(extra="allow")
    code: str
    message: str


class Envelope(BaseModel):
    success: bool
    data: dict[str, Any] | None = None
    model: ModelInfo | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[ErrorInfo] | None = None
    meta: Meta


class ToolError(Exception):
    """A failure that becomes success=false with a typed error code."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.extra = extra


def model_info(m: ModelVersion | None, buses: int) -> ModelInfo | None:
    if m is None:
        return None
    return ModelInfo(
        root_mrid=str(m.root_mrid), root_name=m.root_name, case_kind=m.case_kind,
        cim_updated_at=m.cim_updated_at.isoformat(), telemetry_ts=m.telemetry_ts.isoformat() if m.telemetry_ts else None,
        buses=buses, loads=m.loads_measured + m.loads_allocated + m.loads_defaulted, loads_measured=m.loads_measured,
        modified=m.modified,
    )


def envelope(*, success: bool, meta: Meta, data: dict[str, Any] | None = None, model: ModelInfo | None = None,
             warnings: list[str] | None = None, errors: list[ErrorInfo] | None = None) -> Envelope:
    return Envelope(success=success, data=data if success else None, model=model, warnings=warnings or [],
                    errors=errors, meta=meta)


# --- validators --------------------------------------------------------------------------------------------------

def converged(ctx, res: engine.SolveResult) -> None:
    if not res.converged:
        raise ToolError("not_converged", "power flow did not converge", iterations=res.iterations,
                        max_error=ctx.ActiveCircuit.Solution.Tolerance)


def resolve_bus(names: Mapping[str, tuple[UUID, str]], ref: str) -> str:
    """mRID, human name or DSS bus name -> DSS bus name (cn_xxxxxxxx)."""
    ref_l = ref.strip().lower()
    for dss, (mrid, human) in names.items():
        if not dss.startswith("cn_"):
            continue
        if ref_l in (str(mrid), dss, (human or "").lower()):
            return dss
    raise ToolError("bus_not_found", f"bus {ref!r} is not in the loaded feeder")


def head_balance(modelled_kw: float, measured_kw: float | None) -> list[str]:
    if measured_kw is None or abs(measured_kw) < 1e-6:
        return []
    err = abs(modelled_kw - measured_kw) / abs(measured_kw)
    if err > HEAD_BALANCE_TOL:
        return [f"feeder-head balance: modelled {modelled_kw:.0f} kW vs measured {measured_kw:.0f} kW ({err:.0%})"]
    return []


def transformer_sanity(ctx, names: Mapping[str, tuple[UUID, str]]) -> list[str]:
    """Load through a transformer above 10x its rating points at a CIM rating or load error."""
    ckt = ctx.ActiveCircuit
    T = ckt.Transformers
    out = []
    i = T.First
    while i > 0:
        e = ckt.ActiveCktElement
        T.Wdg = 1
        kva = T.kVA
        pw = list(e.Powers)
        nc = e.NumConductors
        s = abs(complex(sum(pw[0:2 * nc:2]), sum(pw[1:2 * nc:2])))
        if kva > 0 and s > TRANSFORMER_OVERLOAD_X * kva:
            out.append(f"transformer {names.get(T.Name, (None, T.Name))[1]} carries {s:.0f} kVA, >10x its {kva:.0f} kVA rating")
        i = T.Next
    return out


def bus_kv_sanity(ctx, bus_nominal: Mapping[str, float], names: Mapping[str, tuple[UUID, str]]) -> list[str]:
    """Engine-assigned bus base vs the CIM base voltage of the equipment on it."""
    bad = []
    for b, nominal in bus_nominal.items():
        if not nominal or not engine.bus_exists(ctx, b):
            continue
        kv = engine.bus_info(ctx, b)["kv_base_ll"]
        if kv > 0 and abs(kv - nominal) / nominal > BUS_KV_TOL:
            bad.append(names.get(b, (None, b))[1])
    if bad:
        return [f"{len(bad)} buses have an engine voltage base >10% from their CIM nominal: {', '.join(bad[:10])}"]
    return []
