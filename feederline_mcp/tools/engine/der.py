# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ... import engine
from ...sessions import Session
from ...studies import ToolError, converged, resolve_bus
from .._runner import Output, Services, invoke
from .power_flow import voltage_range

MAX_CANDIDATES = 200
RANKED = 20


class OptimizeDerInput(BaseModel):
    capacity_kw: float = Field(gt=0, le=100_000)
    der_type: Literal["pv", "storage"] = "pv"
    candidate_buses: list[str] | None = Field(default=None, max_length=MAX_CANDIDATES)
    objective: Literal["losses", "voltage"] = "losses"
    request_id: str | None = Field(default=None, max_length=128)


def three_phase_buses(s: Session) -> list[str]:
    source = s.compiled.root["source_cn"]
    out = []
    for b, (mrid, _) in s.names.items():
        if b.startswith("cn_") and mrid != source and engine.bus_exists(s.dss, b):
            if {1, 2, 3} <= set(engine.bus_info(s.dss, b)["nodes"]):
                out.append(b)
    return sorted(out, key=lambda b: s.names[b][1])[:MAX_CANDIDATES]


def execute(services: Services, s: Session, args: OptimizeDerInput) -> Output:
    res = engine.solve(s.dss)
    converged(s.dss, res)
    s.solved = True
    base = engine.totals(s.dss)
    base_vmin, _ = voltage_range(s.dss)
    candidates = [resolve_bus(s.names, b) for b in args.candidate_buses] if args.candidate_buses else three_phase_buses(s)
    if not candidates:
        raise ToolError("no_candidates", "no candidate buses")
    rows, failed = [], 0
    with services.store.sweep(s):
        for i, b in enumerate(candidates):
            info = engine.bus_info(s.dss, b)
            nodes = [n for n in info["nodes"] if n in (1, 2, 3)]
            name = f"opt{i}"
            engine.add_der(s.dss, name, b, nodes, info["kv_base_ll"], args.capacity_kw, args.der_type)
            r = engine.solve(s.dss)
            if r.converged:
                vmin, vmax = voltage_range(s.dss)
                mrid, human = s.names[b]
                rows.append({"bus": b, "mrid": str(mrid), "name": human,
                             "losses_kw": round(engine.totals(s.dss)["losses_kw"], 3),
                             "min_v_pu": round(vmin, 5), "max_v_pu": round(vmax, 5)})
            else:
                failed += 1
            engine.remove_element(s.dss, "PVSystem" if args.der_type == "pv" else "Storage", name)
    if args.objective == "losses":
        rows.sort(key=lambda r: r["losses_kw"])
    else:
        rows.sort(key=lambda r: (-r["min_v_pu"], r["max_v_pu"]))
    warnings = [f"{failed} candidates did not converge"] if failed else []
    if rows and rows[0]["max_v_pu"] > 1.05:
        warnings.append(f"recommended placement peaks at {rows[0]['max_v_pu']:.4f} pu")
    data = {"objective": args.objective, "capacity_kw": args.capacity_kw, "der_type": args.der_type,
            "candidates": len(candidates), "base_losses_kw": round(base["losses_kw"], 3),
            "base_min_v_pu": round(base_vmin, 5), "ranked": rows[:RANKED], "recommended": rows[0] if rows else None}
    s.results["optimize_der"] = data
    return Output(data, warnings, record=True)


def register(mcp, services: Services) -> None:
    @mcp.tool()
    async def optimize_der(ctx: Context, capacity_kw: float, der_type: Literal["pv", "storage"] = "pv",
                           candidate_buses: list[str] | None = None, objective: Literal["losses", "voltage"] = "losses",
                           request_id: str | None = None) -> dict:
        """Rank candidate buses (default: every 3-phase bus, at most 200) for one DER of capacity_kw by feeder losses
        (kW, lowest first) or by minimum voltage (pu, highest first), one solve per candidate. The circuit is restored
        afterwards."""
        raw = {"capacity_kw": capacity_kw, "der_type": der_type, "candidate_buses": candidate_buses,
               "objective": objective, "request_id": request_id}
        return await invoke(services, ctx, "optimize_der", OptimizeDerInput, raw, lambda s, a: execute(services, s, a))
