# SPDX-License-Identifier: Apache-2.0
"""line_loading: lines above a loading threshold as a table, or the feeder map re-coloured by loading."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field

from ... import engine
from ...compiler import emit
from ...sessions import Session
from ...shapes import MapData, TableData, cap, columns, dump, model_size
from ...studies import Envelope
from .._runner import Output, Services, invoke
from ..sql.common import keyword_params
from ..sql.spatial import feeder_geometry_data
from ._shared import bus_min_pu, classes, sql_call
from .voltages import ensure_solved


class LineLoadingInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    threshold_pct: float = Field(default=80, ge=0, le=1000)
    as_: Literal["table", "map"] = Field(default="table", alias="as")
    limit: int = Field(default=500, ge=1, le=5000)
    request_id: str | None = Field(default=None, max_length=128)


def loading_map(services: Services, s: Session, limit: int = 500) -> MapData:
    """feeder_geometry (aggregated: segments and devices) with loading_pct and v_pu filled from the solved circuit."""
    ensure_solved(s)
    by_name = {ln["dss_name"]: ln for ln in engine.lines(s.dss)}
    vmin = bus_min_pu(s.dss)
    full = feeder_geometry_data(sql_call(services, s), s.model.root_mrid, s.model.region_code, 5.0,
                                ("segments", "devices"), s.model.case_kind)
    filled = 0
    for f in full.features:
        ln = by_name.get(emit.dss_name(f.properties["mrid"]))
        if ln is None:
            continue
        f.properties["loading_pct"] = round(ln["loading_pct"], 1)
        pus = [vmin[b] for b in (ln["bus1"], ln["bus2"]) if b in vmin]
        f.properties["v_pu"] = round(min(pus), 5) if pus else None
        filled += 1
    data = MapData(bbox=full.bbox, summary={**full.summary, "filled": filled}, features=[])
    data.features, data.truncated = cap(full.features, limit, size=model_size)
    return data


def loading_table(services: Services, s: Session, threshold: float) -> TableData:
    ensure_solved(s)
    over = sorted((ln for ln in engine.lines(s.dss) if ln["loading_pct"] >= threshold and ln["dss_name"] in s.names),
                  key=lambda ln: -ln["loading_pct"])
    cls = classes(services, s, [s.names[ln["dss_name"]][0] for ln in over])
    rows: list[dict[str, Any]] = []
    for ln in over:
        mrid, name = s.names[ln["dss_name"]]
        rows.append({"mrid": str(mrid), "name": name, "cim_class": cls.get(mrid, "ACLineSegment"),
                     "loading_pct": round(ln["loading_pct"], 1), "amps": round(max(ln["amps"], default=0.0), 1),
                     "normamps": round(ln["normamps"], 1)})
    return TableData(columns=columns(("name", "Line"), ("cim_class", "Class"), ("loading_pct", "Loading", "%"),
                                     ("amps", "Current", "A"), ("normamps", "Rating", "A")),
                     rows=rows, summary={"threshold_pct": threshold, "lines": len(rows),
                                         "max_loading_pct": rows[0]["loading_pct"] if rows else None})


def execute(services: Services, s: Session, args: LineLoadingInput) -> Output:
    if args.as_ == "map":
        data = loading_map(services, s, args.limit)
        warnings = ["map truncated to the size budget; no resource carries engine values"] if data.truncated else []
        return Output(dump(data), warnings)
    return Output(dump(loading_table(services, s, args.threshold_pct)))


def register(mcp, services: Services) -> None:
    @mcp.tool(name="line_loading", structured_output=True)
    @keyword_params
    async def line_loading_tool(ctx: Context, threshold_pct: float = 80,
                                as_: Annotated[Literal["table", "map"], Field(alias="as")] = "table", limit: int = 500,
                                request_id: str | None = None) -> Envelope:
        """Line loading on the solved feeder as % of normal amps. as=table lists lines at or above threshold_pct with
        current (A) and rating (A) (data.viz = "table"); as=map returns the feeder geometry with loading_pct and the
        lowest bus voltage v_pu (pu) filled on every line (data.viz = "map")."""
        raw = {"threshold_pct": threshold_pct, "as": as_, "limit": limit, "request_id": request_id}
        return await invoke(services, ctx, "line_loading", LineLoadingInput, raw, lambda s, a: execute(services, s, a))
