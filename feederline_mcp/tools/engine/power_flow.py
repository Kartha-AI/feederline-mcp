# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ... import engine
from ...sessions import Session
from ...studies import converged, head_balance, transformer_sanity
from .._runner import Output, Services, invoke


class PowerFlowInput(BaseModel):
    mode: Literal["snapshot"] = "snapshot"
    max_iterations: int = Field(default=100, ge=1, le=1000)
    control_mode: Literal["static", "off"] = "static"
    request_id: str | None = Field(default=None, max_length=128)


def voltage_range(ctx) -> tuple[float, float]:
    pus = [n.pu for n in engine.node_voltages(ctx) if n.kv_base > 0 and n.pu > 0.01]
    return (min(pus), max(pus)) if pus else (0.0, 0.0)


def overloaded(ctx, names, threshold: float = 100.0) -> list[dict]:
    out = []
    for b in engine.line_loadings(ctx):
        if b["loading_pct"] > threshold:
            mrid, human = names.get(b["dss_name"], (None, b["dss_name"]))
            out.append({"name": human, "mrid": str(mrid) if mrid else None, "loading_pct": round(b["loading_pct"], 1)})
    return sorted(out, key=lambda x: -x["loading_pct"])


def execute(s: Session, args: PowerFlowInput) -> Output:
    res = engine.solve(s.dss, args.max_iterations, args.control_mode, args.mode)
    s.solved = res.converged
    converged(s.dss, res)
    vmin, vmax = voltage_range(s.dss)
    tot = engine.totals(s.dss)
    data = {
        "converged": True, "iterations": res.iterations, "min_v_pu": round(vmin, 5), "max_v_pu": round(vmax, 5),
        "losses_kw": round(tot["losses_kw"], 3), "losses_kvar": round(tot["losses_kvar"], 3),
        "total_load_kw": round(tot["total_load_kw"], 3), "overloaded_branches": overloaded(s.dss, s.names),
    }
    warnings = list(s.model.warnings)
    if s.model.case_kind == "operational":
        warnings += head_balance(tot["total_load_kw"], s.compiled.head.p_kw)
    warnings += transformer_sanity(s.dss, s.names)
    s.results["run_power_flow"] = data
    return Output(data, warnings, record=True)


def register(mcp, services: Services) -> None:
    @mcp.tool()
    async def run_power_flow(ctx: Context, mode: Literal["snapshot"] = "snapshot", max_iterations: int = 100,
                             control_mode: Literal["static", "off"] = "static", request_id: str | None = None) -> dict:
        """Solve a snapshot power flow on the loaded feeder. static: regulator and capacitor controls act; off: taps
        frozen. Returns convergence, iterations, min/max node voltage (pu), losses (kW, kvar), total load including
        losses at the feeder head (kW), and lines loaded above 100% of normal amps."""
        raw = {"mode": mode, "max_iterations": max_iterations, "control_mode": control_mode, "request_id": request_id}
        return await invoke(services, ctx, "run_power_flow", PowerFlowInput, raw, execute)
