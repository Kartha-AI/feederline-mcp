# SPDX-License-Identifier: Apache-2.0
"""Governance tools: what changed in a feeder's model since a time or a study, and its data-quality findings."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from mcp.server.fastmcp import Context
from pydantic import Field

from ...compiler import scope
from ...compiler.validate import findings
from ...shapes import TableData, cap, columns, dump
from ...studies import Envelope, ToolError
from . import queries
from .common import Call, Case, Feeders, Output, Ref, RegionInput, feeder_anchor, feeder_fields, rows, run
from .topology import feeder_tree

RECENT_STUDIES = 500


class ModelChangesInput(RegionInput):
    feeder_mrid: Ref
    since: str = Field(min_length=1, max_length=64)  # ISO timestamp, or a study_id / model_id
    limit: int = Field(default=500, ge=1, le=5000)


def since_time(c: Call, root: UUID, since: str) -> tuple[datetime, str]:
    """An ISO timestamp as is; a study_id or model_id -> the CIM high-water mark that study's model used."""
    try:
        ref = UUID(since)
    except ValueError:
        try:
            t = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise ToolError("invalid_input", "since must be an ISO timestamp, a study_id or a model_id") from None
        return (t if t.tzinfo else t.replace(tzinfo=UTC)), "timestamp"
    for r in c.sink.recent(root, RECENT_STUDIES):
        if ref in (r.get("study_id"), r.get("model_id")) or since in (str(r.get("study_id")), str(r.get("model_id"))):
            t = r["cim_updated_at"]
            t = datetime.fromisoformat(t) if isinstance(t, str) else t
            return (t if t.tzinfo else t.replace(tzinfo=UTC)), "study"
    raise ToolError("not_found", f"no study or model {since} among this feeder's last {RECENT_STUDIES} studies")


def model_changes(c: Call, args: ModelChangesInput) -> Output:
    a = feeder_anchor(c, args.feeder_mrid, args.region_code)
    head = Feeders(c, args.region_code).by_mrid[a.mrid]
    t = feeder_tree(c, head, args.region_code, "planning")
    since, kind = since_time(c, a.mrid, args.since)
    found = rows(c, queries.MODEL_CHANGES, {"region": args.region_code, "scope": t.mrids(),
                                            "nodes": list(t.node_km), "since": since})
    out = [{"mrid": str(r["mrid"]), "name": r["name"], "cim_class": r["cim_class"], **feeder_fields(head),
            "change": r["change"], "updated_at": r["updated_at"].isoformat(),
            "valid_to": r["valid_to"].isoformat() if r["valid_to"] else None} for r in found]
    kept, truncated = cap(out, args.limit)
    data = TableData(columns=columns(("name", "Name"), ("cim_class", "Class"), ("change", "Change"),
                                     ("updated_at", "Updated"), ("valid_to", "Retired")),
                     rows=kept, truncated=truncated,
                     summary={**feeder_fields(head), "since": since.isoformat(), "since_kind": kind,
                              "updated": sum(1 for r in out if r["change"] == "updated"),
                              "retired": sum(1 for r in out if r["change"] == "retired")})
    return Output(dump(data))


class ValidateFeederInput(RegionInput):
    feeder_mrid: Ref
    case: Case = "operational"


def validate_feeder(c: Call, args: ValidateFeederInput) -> Output:
    a = feeder_anchor(c, args.feeder_mrid, args.region_code)
    try:
        sc = scope.trace(c.ds, c.principal, a.mrid, args.case, args.region_code)
    except scope.ScopeError as e:
        raise ToolError("not_found", str(e)) from None
    head = Feeders(c, args.region_code).by_mrid.get(a.mrid)
    found = findings(c.ds, c.principal, sc.mrids, sc.root, args.case)
    data = TableData(
        columns=columns(("kind", "Finding"), ("severity", "Severity"), ("count", "Count"), ("names", "Examples")),
        rows=[f.row() for f in found],
        summary={**feeder_fields(head), "case": args.case, "equipment": len(sc.mrids),
                 "errors": sum(len(f.items) for f in found if f.severity == "error"),
                 "warnings": sum(len(f.items) for f in found if f.severity == "warning")})
    return Output(dump(data))


def register(mcp, services) -> None:
    @mcp.tool(name="model_changes", structured_output=True)
    async def model_changes_tool(ctx: Context, feeder_mrid: str, since: str, region_code: str,
                                 limit: int = 500) -> Envelope:
        """Equipment in a feeder updated or retired since an ISO timestamp, or since the CIM state a given study_id
        (or model_id) was computed on. Returns a table (data.viz = "table")."""
        raw = {"feeder_mrid": feeder_mrid, "since": since, "region_code": region_code, "limit": limit}
        return await run(services, ctx, "model_changes", ModelChangesInput, raw, model_changes)

    @mcp.tool(name="validate_feeder", structured_output=True)
    async def validate_feeder_tool(ctx: Context, feeder_mrid: str, region_code: str,
                                   case: Literal["operational", "planning"] = "operational") -> Envelope:
        """Data-quality findings for a feeder without compiling it, counted exactly as load_feeder's compile report
        counts them: defaulted impedances, loads on nameplate values, missing base voltages and transformer terminals,
        stale or untimed switch states, stale load measurements, devices without location. Returns a table
        (data.viz = "table") of findings with counts and example devices."""
        raw = {"feeder_mrid": feeder_mrid, "region_code": region_code, "case": case}
        return await run(services, ctx, "validate_feeder", ValidateFeederInput, raw, validate_feeder)
