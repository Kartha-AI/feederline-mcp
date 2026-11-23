# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field, model_validator

from ... import engine
from ...sessions import Session
from ...studies import converged, resolve_bus
from .._runner import Output, Services, invoke
from .power_flow import voltage_range

MAX_STEPS = 20
OVERVOLTAGE_PU = 1.05
Constraint = Literal["overvoltage", "thermal", "reverse_flow"]


class AnalyzeCapacityInput(BaseModel):
    bus_mrid: str = Field(min_length=1, max_length=128)
    der_type: Literal["pv", "storage"] = "pv"
    step_kw: float = Field(default=250, gt=0)
    max_kw: float = Field(default=5000, gt=0)
    constraints: list[Constraint] = Field(default_factory=lambda: ["overvoltage", "thermal", "reverse_flow"], min_length=1)
    request_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def bounded(self):
        if self.max_kw / self.step_kw > MAX_STEPS + 1e-9:
            raise ValueError(f"max_kw/step_kw exceeds {MAX_STEPS} solves; run larger sweeps as a job")
        return self


def max_loading(ctx) -> float:
    return max((b["loading_pct"] for b in engine.line_loadings(ctx)), default=0.0)


def execute(services: Services, s: Session, args: AnalyzeCapacityInput) -> Output:
    bus = resolve_bus(s.names, args.bus_mrid)
    res = engine.solve(s.dss)
    converged(s.dss, res)
    s.solved = True
    _, base_vmax = voltage_range(s.dss)
    base_loading = max_loading(s.dss)
    info = engine.bus_info(s.dss, bus)
    nodes = [n for n in info["nodes"] if n in (1, 2, 3)]
    warnings = []
    if base_vmax > OVERVOLTAGE_PU:
        warnings.append(f"base case already peaks at {base_vmax:.4f} pu; overvoltage counts only DER-driven rise")
    if base_loading > 100:
        warnings.append(f"base case already loads a line to {base_loading:.0f}%; thermal counts only DER-driven increase")

    curve, hosting, binding = [], 0.0, None
    steps = [args.step_kw * (i + 1) for i in range(int(round(args.max_kw / args.step_kw)))]
    with services.store.sweep(s):
        engine.add_der(s.dss, "hc_der", bus, nodes, info["kv_base_ll"], steps[0], args.der_type)
        for kw in steps:
            engine.set_der_kw(s.dss, "hc_der", kw, args.der_type)
            r = engine.solve(s.dss)
            if not r.converged:
                binding = "non_convergence"
                break
            _, vmax = voltage_range(s.dss)
            loading = max_loading(s.dss)
            head_kw, _ = engine.head_power(s.dss)
            curve.append({"kw": kw, "max_v_pu": round(vmax, 5), "max_loading_pct": round(loading, 1),
                          "head_kw": round(head_kw, 1)})
            hit = None
            if "overvoltage" in args.constraints and vmax > OVERVOLTAGE_PU and vmax > base_vmax + 1e-4:
                hit = "overvoltage"
            elif "thermal" in args.constraints and loading > 100 and loading > base_loading + 0.1:
                hit = "thermal"
            elif "reverse_flow" in args.constraints and head_kw < 0:
                hit = "reverse_flow"
            if hit:
                binding = hit
                break
            hosting = kw
    name = s.names.get(bus, (None, bus))[1]
    data = {"bus": name, "bus_mrid": str(s.names[bus][0]), "der_type": args.der_type, "hosting_capacity_kw": hosting,
            "binding_constraint": binding, "curve": curve}
    if binding is None:
        warnings.append(f"no constraint bound up to {args.max_kw:g} kW; hosting capacity is at least that")
    s.results["analyze_capacity"] = data
    return Output(data, warnings, record=True)


def register(mcp, services: Services) -> None:
    @mcp.tool()
    async def analyze_capacity(ctx: Context, bus_mrid: str, der_type: Literal["pv", "storage"] = "pv",
                               step_kw: float = 250, max_kw: float = 5000,
                               constraints: list[Constraint] | None = None, request_id: str | None = None) -> dict:
        """Hosting capacity at one bus (mRID or bus name): add PV or discharging storage in step_kw increments up to
        max_kw (at most 20 solves) and stop at the first DER-driven violation of the chosen constraints (overvoltage
        above 1.05 pu, line loading above 100%, reverse flow at the feeder head). Returns hosting capacity (kW), the
        binding constraint and the curve of max voltage (pu) and max loading (%) per step. The circuit is restored
        afterwards."""
        raw = {"bus_mrid": bus_mrid, "der_type": der_type, "step_kw": step_kw, "max_kw": max_kw,
               "request_id": request_id, **({"constraints": constraints} if constraints else {})}
        return await invoke(services, ctx, "analyze_capacity", AnalyzeCapacityInput, raw,
                            lambda s, a: execute(services, s, a))
