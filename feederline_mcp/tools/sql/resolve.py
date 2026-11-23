# SPDX-License-Identifier: Apache-2.0
"""Human references -> mRIDs: a UUID, gis_oid, SCADA tag, exact name, or a close name (trigram > 0.6)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ...adapters.base import DataSource, Principal
from ...studies import ToolError
from . import queries

MAX_CANDIDATES = 5


def _uuid(ref: str) -> UUID | None:
    try:
        return UUID(ref.strip())
    except (ValueError, AttributeError):
        return None


def _candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"mrid": str(r["mrid"]), "name": r["name"], "cim_class": r["cim_class"], "region_code": r["region_code"]}
            for r in rows[:MAX_CANDIDATES]]


def resolve_mrid(ds: DataSource, principal: Principal, ref: str | UUID, cim_class: str | None = None,
                 region_code: str | None = None) -> UUID:
    """UUID string -> UUID. Else exact gis_oid, exact remote_source.name (tag), exact name, then trigram > 0.6.
    One hit -> UUID. Zero -> not_found. Several at the first stage with hits -> ambiguous (candidates[:5])."""
    if isinstance(ref, UUID):
        return ref
    u = _uuid(ref)
    if u is not None:
        return u
    ref = ref.strip()
    if not ref:
        raise ToolError("invalid_input", "empty reference")
    rows = ds.query(queries.RESOLVE, {"ref": ref, "cls": cim_class, "region": region_code}, principal.region_codes)
    if not rows:
        where = f" in {region_code}" if region_code else ""
        raise ToolError("not_found", f"no equipment matches {ref!r}{where}")
    first = [r for r in rows if r["stage"] == rows[0]["stage"]]
    unique = {r["mrid"]: r for r in first}
    if len(unique) > 1:
        raise ToolError("ambiguous", f"{ref!r} matches {len(unique)} objects; pass an mRID or a more specific name",
                        candidates=_candidates(list(unique.values())))
    return first[0]["mrid"]


def resolve_measurement(ds: DataSource, principal: Principal, ref: str, region_code: str | None = None) -> UUID:
    """A measurement by mRID, tag (remote_source.name) or exact measurement name."""
    rows = ds.query(queries.RESOLVE_MEASUREMENT, {"ref": ref.strip(), "region": region_code}, principal.region_codes)
    if not rows:
        raise ToolError("not_found", f"no measurement matches {ref!r}")
    first = {r["mrid"]: r for r in rows if r["stage"] == rows[0]["stage"]}
    if len(first) > 1:
        raise ToolError("ambiguous", f"{ref!r} matches {len(first)} measurements",
                        candidates=[{"mrid": str(r["mrid"]), "name": r["name"], "cim_class": "Measurement",
                                     "region_code": r["region_code"]} for r in list(first.values())[:MAX_CANDIDATES]])
    return rows[0]["mrid"]
