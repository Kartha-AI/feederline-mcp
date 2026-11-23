# SPDX-License-Identifier: Apache-2.0
"""compare_cases: a baseline case against the session circuit or the baseline with actions applied."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field, model_validator

from ... import engine
from ...compiler import ScopeError, compile_feeder
from ...sessions import Session
from ...shapes import TableData, columns, dump
from ...studies import Envelope, ToolError
from .._runner import Output, Services, invoke
from .capacity import max_loading
from .set_control import Action, apply, plan

TOP_BUSES = 10
METRICS = (("converged", "Converged", None), ("min_v_pu", "Minimum voltage", "pu"), ("max_v_pu", "Maximum voltage", "pu"),
           ("losses_kw", "Losses", "kW"), ("losses_kvar", "Losses", "kvar"), ("total_load_kw", "Load", "kW"),
           ("max_loading_pct", "Maximum line loading", "%"), ("overloaded_lines", "Lines above 100%", None),
           ("voltage_violations", "Bus-phases outside 0.95-1.05 pu", None))


class CompareCasesInput(BaseModel):
    baseline: Literal["current", "operational", "planning"] = "planning"
    actions: list[Action] = Field(default_factory=list, max_length=20)
    request_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def something_to_compare(self):
        if self.baseline == "current" and not self.actions:
            raise ValueError("baseline=current needs actions to compare against")
        return self


def measure(ctx) -> tuple[dict[str, Any], dict[tuple[str, int], float]]:
    res = engine.solve(ctx)
    if not res.converged:
        return {"converged": False}, {}
    volts = {(n.bus, n.node): n.pu for n in engine.node_voltages(ctx) if n.kv_base > 0 and n.node in (1, 2, 3)}
    live = [v for v in volts.values() if v > 0.01]
    tot = engine.totals(ctx)
    loads = engine.line_loadings(ctx)
    return {"converged": True, "min_v_pu": round(min(live), 5) if live else None,
            "max_v_pu": round(max(live), 5) if live else None, "losses_kw": round(tot["losses_kw"], 3),
            "losses_kvar": round(tot["losses_kvar"], 3), "total_load_kw": round(tot["total_load_kw"], 3),
            "max_loading_pct": round(max_loading(ctx), 1),
            "overloaded_lines": sum(1 for b in loads if b["loading_pct"] > 100),
            "voltage_violations": sum(1 for v in live if not 0.95 <= v <= 1.05)}, volts


def execute(services: Services, s: Session, args: CompareCasesInput) -> Output:
    current, current_v = measure(s.dss)
    names = dict(s.names)
    with services.store.sweep(s):
        if args.baseline != "current":
            try:
                compiled = compile_feeder(services.ds, s.principal, s.model.root_mrid, args.baseline, s.model.region_code)
            except ScopeError as e:
                raise ToolError("not_found", str(e)) from None
            engine.run(s.dss, compiled.commands)
            names.update(compiled.names)
        base, base_v = measure(s.dss)
        if args.actions:
            planned, _ = plan(s.dss, names, args.actions)
            apply(s.dss, planned)
            mod, mod_v = measure(s.dss)
            modified_label = f"{args.baseline} + {len(args.actions)} actions"
        else:
            mod, mod_v = current, current_v
            modified_label = "session circuit" + (" (modified)" if s.model.modified else "")
    rows = []
    for key, label, unit in METRICS:
        b, m = base.get(key), mod.get(key)
        delta = round(m - b, 5) if isinstance(b, int | float) and isinstance(m, int | float) and not isinstance(b, bool) \
            else None
        rows.append({"metric": key, "label": label, "unit": unit, "baseline": b, "modified": m, "delta": delta})
    changed = sorted(((k, mod_v[k] - base_v[k]) for k in base_v.keys() & mod_v.keys()), key=lambda kv: -abs(kv[1]))
    buses = [{"bus": b, "mrid": str(names[b][0]) if b in names else None, "name": names.get(b, (None, b))[1],
              "phase": "ABC"[n - 1], "baseline_v_pu": round(base_v[(b, n)], 5), "modified_v_pu": round(mod_v[(b, n)], 5),
              "delta_v_pu": round(d, 5)} for (b, n), d in changed[:TOP_BUSES]]
    data = TableData(columns=columns(("label", "Metric"), ("unit", "Unit"), ("baseline", "Baseline"),
                                     ("modified", "Modified"), ("delta", "Change")),
                     rows=rows, buses_changed=buses,
                     summary={"baseline": args.baseline, "modified": modified_label,
                              "buses_compared": len(base_v.keys() & mod_v.keys())})
    warnings = []
    if not base.get("converged") or not mod.get("converged"):
        warnings.append("a case did not converge; its metrics are omitted")
    return Output(dump(data), warnings, record=True)


def register(mcp, services: Services) -> None:
    @mcp.tool(name="compare_cases", structured_output=True)
    async def compare_cases_tool(ctx: Context, baseline: Literal["current", "operational", "planning"] = "planning",
                                 actions: list[Action] | None = None, request_id: str | None = None) -> Envelope:
        """Compare a baseline (the loaded feeder recompiled for the operational or planning case, or the current
        session circuit) against either the session circuit or the baseline with control actions applied: minimum
        and maximum voltage (pu), losses (kW, kvar), load (kW), maximum line loading (%), overloaded lines and
        voltage violations, plus the 10 bus-phases whose voltage changes most. The session circuit is restored
        afterwards. Returns a table (data.viz = "table")."""
        raw = {"baseline": baseline, "actions": [a.model_dump() if isinstance(a, BaseModel) else a
                                                 for a in actions or []], "request_id": request_id}
        return await invoke(services, ctx, "compare_cases", CompareCasesInput, raw, lambda s, a: execute(services, s, a))
