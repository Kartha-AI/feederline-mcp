# SPDX-License-Identifier: Apache-2.0
"""losses_by_segment: the line segments with the highest losses on the solved feeder."""

from __future__ import annotations

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ... import engine
from ...sessions import Session
from ...shapes import TableData, columns, dump
from ...studies import Envelope
from .._runner import Output, Services, invoke
from ._shared import classes
from .voltages import ensure_solved


class LossesInput(BaseModel):
    top_n: int = Field(default=20, ge=1, le=500)
    request_id: str | None = Field(default=None, max_length=128)


def execute(services: Services, s: Session, args: LossesInput) -> Output:
    ensure_solved(s)
    segs = [ln for ln in engine.lines(s.dss) if not ln["is_switch"] and ln["dss_name"] in s.names]
    segs.sort(key=lambda ln: -ln["losses_kw"])
    top = segs[: args.top_n]
    cls = classes(services, s, [s.names[ln["dss_name"]][0] for ln in top])
    total = engine.totals(s.dss)["losses_kw"]
    seg_total = sum(ln["losses_kw"] for ln in segs)
    rows = []
    for ln in top:
        mrid, name = s.names[ln["dss_name"]]
        km = ln["length_m"] / 1000
        rows.append({"mrid": str(mrid), "name": name, "cim_class": cls.get(mrid, "ACLineSegment"),
                     "losses_kw": round(ln["losses_kw"], 3), "losses_kvar": round(ln["losses_kvar"], 3),
                     "loading_pct": round(ln["loading_pct"], 1), "length_m": round(ln["length_m"], 1),
                     "losses_kw_per_km": round(ln["losses_kw"] / km, 3) if km > 0 else None,
                     "share_pct": round(100 * ln["losses_kw"] / total, 2) if total > 0 else None})
    data = TableData(columns=columns(("name", "Segment"), ("losses_kw", "Losses", "kW"), ("losses_kvar", "Losses", "kvar"),
                                     ("loading_pct", "Loading", "%"), ("length_m", "Length", "m"),
                                     ("losses_kw_per_km", "Losses per km", "kW/km"), ("share_pct", "Share of feeder", "%")),
                     rows=rows,
                     summary={"feeder_losses_kw": round(total, 3), "segment_losses_kw": round(seg_total, 3),
                              "top_n_share_pct": round(100 * sum(r["losses_kw"] for r in rows) / total, 2)
                              if total > 0 else None, "segments": len(segs)})
    return Output(dump(data))


def register(mcp, services: Services) -> None:
    @mcp.tool(name="losses_by_segment", structured_output=True)
    async def losses_by_segment_tool(ctx: Context, top_n: int = 20, request_id: str | None = None) -> Envelope:
        """The top_n line segments of the solved feeder by real-power losses (kW), with reactive losses (kvar),
        loading (%), length (m), losses per km and share of total feeder losses (%). Returns a table
        (data.viz = "table")."""
        return await invoke(services, ctx, "losses_by_segment", LossesInput, {"top_n": top_n, "request_id": request_id},
                            lambda s, a: execute(services, s, a))
