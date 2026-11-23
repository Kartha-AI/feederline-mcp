# SPDX-License-Identifier: Apache-2.0
"""regulator_status: modelled regulator taps against the measured tap positions."""

from __future__ import annotations

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ... import engine
from ...sessions import Session
from ...shapes import TableData, columns, dump
from ...studies import Envelope
from .._runner import Output, Services, invoke
from ..sql import queries as sql_queries
from ..sql.common import rows as sql_rows
from ..sql.telemetry import reading_row
from ._shared import sql_call
from .voltages import ensure_solved


class RegulatorStatusInput(BaseModel):
    request_id: str | None = Field(default=None, max_length=128)


def execute(services: Services, s: Session, args: RegulatorStatusInput) -> Output:
    ensure_solved(s)
    regs = engine.regcontrols(s.dss)
    mrids = [s.names[r["transformer"]][0] for r in regs if r["transformer"] in s.names]
    measured = {}
    if mrids:
        for r in sql_rows(sql_call(services, s), sql_queries.LATEST_READINGS, {"psrs": mrids, "mtype": "TapPosition"}):
            measured.setdefault(r["equipment_mrid"], reading_row(r))
    out = []
    for r in regs:
        mrid, name = s.names.get(r["transformer"], (None, r["transformer"]))
        m = measured.get(mrid)
        mt = round(m["value"]) if m and m["value"] is not None else None
        out.append({"mrid": str(mrid) if mrid else None, "name": name, "cim_class": "PowerTransformer",
                    "tap_number": r["tap_number"], "vreg_v": round(r["vreg_v"], 2), "band_v": round(r["band_v"], 2),
                    "winding": r["tap_winding"], "measured_tap": mt,
                    "delta_taps": r["tap_number"] - mt if mt is not None else None,
                    "measured_ts": m["ts"] if m else None, "measured_stale": m["stale"] if m else None})
    data = TableData(columns=columns(("name", "Regulator"), ("tap_number", "Model tap"), ("measured_tap", "Measured tap"),
                                     ("delta_taps", "Difference"), ("vreg_v", "Set point", "V"), ("band_v", "Band", "V"),
                                     ("measured_stale", "Measurement stale")),
                     rows=out, summary={"regulators": len(out),
                                        "differing": sum(1 for r in out if r["delta_taps"]),
                                        "without_measurement": sum(1 for r in out if r["measured_tap"] is None)})
    return Output(dump(data))


def register(mcp, services: Services) -> None:
    @mcp.tool(name="regulator_status", structured_output=True)
    async def regulator_status_tool(ctx: Context, request_id: str | None = None) -> Envelope:
        """Each voltage regulator on the solved feeder: modelled tap position (steps from neutral), voltage set point
        and band (V on the 120 V base), the latest measured tap position and the difference in taps. Returns a table
        (data.viz = "table")."""
        return await invoke(services, ctx, "regulator_status", RegulatorStatusInput, {"request_id": request_id},
                            lambda s, a: execute(services, s, a))
