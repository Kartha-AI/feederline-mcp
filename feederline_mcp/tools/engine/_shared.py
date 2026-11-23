# SPDX-License-Identifier: Apache-2.0
"""Helpers shared by the phase-2 engine tools: element lookup in the session and SQL-family calls in-process."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ... import engine
from ...sessions import Session
from ...studies import ToolError
from .._runner import Call, Services
from ..sql import queries as sql_queries


def sql_call(services: Services, s: Session) -> Call:
    """The SQL family's call context for this session's principal (no extra lock: same process, same thread)."""
    return Call(services.ds, services.sink, s.principal, s.id, services.areas, services.cache)


def resolve_element(names: dict[str, tuple[UUID, str]], ref: str) -> tuple[str, UUID, str]:
    """An equipment mRID, human name or DSS name in the loaded circuit -> (dss name, mRID, human name)."""
    r = ref.strip().lower()
    hits = [(dss, m, h) for dss, (m, h) in names.items()
            if not dss.startswith("cn_") and "." not in dss and r in (str(m), dss, (h or "").lower())]
    if not hits:
        raise ToolError("not_found", f"{ref!r} is not an element of the loaded feeder")
    if len(hits) > 1:
        raise ToolError("ambiguous", f"{ref!r} names {len(hits)} elements; pass an mRID",
                        candidates=[{"mrid": str(m), "name": h, "cim_class": None} for _, m, h in hits[:5]])
    return hits[0]


def classes(services: Services, s: Session, mrids: list[Any]) -> dict[Any, str]:
    if not mrids:
        return {}
    rows = services.ds.query(sql_queries.EQUIPMENT_CLASSES, {"mrids": list(mrids)}, s.principal.region_codes)
    return {r["mrid"]: r["cim_class"] for r in rows}


def bus_min_pu(ctx) -> dict[str, float]:
    out: dict[str, float] = {}
    for n in engine.node_voltages(ctx):
        if n.kv_base > 0 and n.node in (1, 2, 3) and n.pu > 0.01:
            out[n.bus] = min(out.get(n.bus, 9.9), n.pu)
    return out


PHASE = {1: "A", 2: "B", 3: "C"}
