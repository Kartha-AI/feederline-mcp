# SPDX-License-Identifier: Apache-2.0
"""phase_balance: per-phase power at the feeder head and the most imbalanced three-phase segments."""

from __future__ import annotations

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ... import engine
from ...sessions import Session
from ...shapes import TableData, columns, dump
from ...studies import Envelope
from .._runner import Output, Services, invoke
from ._shared import PHASE, classes
from .voltages import ensure_solved

WORST = 10
MIN_MEAN_KW = 1.0


class PhaseBalanceInput(BaseModel):
    request_id: str | None = Field(default=None, max_length=128)


def imbalance(kw: dict[str, float]) -> float | None:
    """Maximum deviation from the phase mean, as % of the mean (NEMA definition) over phases A, B, C."""
    vals = [kw.get(p) for p in "ABC"]
    if any(v is None for v in vals):
        return None
    mean = sum(vals) / 3
    return 100 * max(abs(v - mean) for v in vals) / mean if mean > 0 else None


def execute(services: Services, s: Session, args: PhaseBalanceInput) -> Output:
    ensure_solved(s)
    nodes, kw, _ = engine.source_powers(s.dss)
    head = {PHASE[n]: k for n, k in zip(nodes, kw, strict=False) if n in PHASE}
    root = s.model
    rows = [{"mrid": str(root.root_mrid), "name": root.root_name, "cim_class": "Breaker", "location": "feeder head",
             **{f"kw_{p.lower()}": round(head.get(p, 0.0), 2) for p in "ABC"},
             "imbalance_pct": round(imbalance(head) or 0.0, 2)}]
    segs = []
    for ln in engine.lines(s.dss):
        if ln["is_switch"] or ln["dss_name"] not in s.names:
            continue
        ph = {PHASE[n]: k for n, k in zip(ln["nodes"], ln["kw"], strict=False) if n in PHASE}
        if len(ph) == 3 and sum(abs(v) for v in ph.values()) / 3 >= MIN_MEAN_KW:
            pct = imbalance({p: abs(v) for p, v in ph.items()})
            if pct is not None:
                segs.append((pct, ln, ph))
    segs.sort(key=lambda x: -x[0])
    cls = classes(services, s, [s.names[ln["dss_name"]][0] for _, ln, _ in segs[:WORST]])
    for pct, ln, ph in segs[:WORST]:
        mrid, name = s.names[ln["dss_name"]]
        rows.append({"mrid": str(mrid), "name": name, "cim_class": cls.get(mrid, "ACLineSegment"), "location": "segment",
                     **{f"kw_{p.lower()}": round(ph[p], 2) for p in "ABC"}, "imbalance_pct": round(pct, 2)})
    data = TableData(columns=columns(("name", "Location"), ("kw_a", "Phase A", "kW"), ("kw_b", "Phase B", "kW"),
                                     ("kw_c", "Phase C", "kW"), ("imbalance_pct", "Imbalance", "%")),
                     rows=rows, summary={"head_imbalance_pct": rows[0]["imbalance_pct"],
                                         "three_phase_segments": len(segs)})
    return Output(dump(data))


def register(mcp, services: Services) -> None:
    @mcp.tool(name="phase_balance", structured_output=True)
    async def phase_balance_tool(ctx: Context, request_id: str | None = None) -> Envelope:
        """Per-phase active power (kW) at the feeder head and on the 10 most imbalanced three-phase segments of the
        solved feeder, with imbalance as the maximum deviation from the phase mean (% of mean). Returns a table
        (data.viz = "table")."""
        return await invoke(services, ctx, "phase_balance", PhaseBalanceInput, {"request_id": request_id},
                            lambda s, a: execute(services, s, a))
