# SPDX-License-Identifier: Apache-2.0
"""set_control: switch, tap and capacitor actions on the session's in-memory circuit (never on switch.open)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ... import engine
from ...compiler import emit
from ...sessions import Session
from ...shapes import TableData, columns, dump
from ...studies import Envelope, ToolError, converged
from .._runner import Output, Services, invoke
from ._shared import resolve_element
from .power_flow import voltage_range

log = logging.getLogger(__name__)
MAX_ACTIONS = 20
MAX_TAP = 16


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mrid: str = Field(min_length=1, max_length=200)  # mRID or name of a switch, regulator transformer or capacitor
    kind: Literal["open", "close", "tap", "cap"]
    value: float | None = None  # tap: steps from neutral (-16..16); cap: 1 on, 0 off

    @model_validator(mode="after")
    def valued(self):
        if self.kind == "tap":
            if self.value is None or self.value != int(self.value) or abs(self.value) > MAX_TAP:
                raise ValueError(f"tap needs an integer value in -{MAX_TAP}..{MAX_TAP}")
        if self.kind == "cap" and self.value not in (0, 1, None):
            raise ValueError("cap value is 1 (on) or 0 (off)")
        return self


class SetControlInput(BaseModel):
    actions: list[Action] = Field(min_length=1, max_length=MAX_ACTIONS)
    max_iterations: int = Field(default=100, ge=1, le=1000)
    request_id: str | None = Field(default=None, max_length=128)


@dataclass
class Planned:
    action: Action
    dss: str
    mrid: object
    name: str
    cim_class: str
    commands: list[str]


def plan(ctx, names, actions: list[Action]) -> tuple[list[Planned], bool]:
    """Resolve and validate every action before any is applied; True when taps or capacitors are set by hand."""
    regs = {r["transformer"].lower(): r for r in engine.regcontrols(ctx)}
    out, manual = [], False
    for a in actions:
        dss, mrid, name = resolve_element(names, a.mrid)
        if a.kind in ("open", "close"):
            if not engine.is_switch_line(ctx, dss):
                raise ToolError("invalid_input", f"{name} is not a switch")
            cmds = emit.open_switch(dss) if a.kind == "open" else emit.close_switch(dss)
            cls = "Switch"
        elif a.kind == "tap":
            if not engine.element_exists(ctx, f"Transformer.{dss}"):
                raise ToolError("invalid_input", f"{name} is not a transformer")
            reg = regs.get(dss.lower())
            cmds = emit.set_tap(dss, int(a.value), reg["tap_winding"] if reg else 2)
            cls, manual = "PowerTransformer", True
        else:
            steps = engine.capacitor_steps(ctx, dss)
            if not steps:
                raise ToolError("invalid_input", f"{name} is not a capacitor")
            cmds = emit.set_capacitor(dss, a.value != 0, steps)
            cls, manual = "LinearShuntCompensator", True
        out.append(Planned(a, dss, mrid, name, cls, cmds))
    return out, manual


def apply(ctx, planned: list[Planned]) -> None:
    for p in planned:
        engine.run(ctx, p.commands)


def execute(services: Services, s: Session, args: SetControlInput) -> Output:
    planned, manual = plan(s.dss, s.names, args.actions)
    apply(s.dss, planned)
    s.model.modified = True
    s.results.clear()
    mode = "off" if manual else "static"
    res = engine.solve(s.dss, args.max_iterations, mode)
    s.solved = res.converged
    try:
        services.store.persist(s)
    except Exception:  # the change still holds on this replica
        log.exception("snapshot persist after set_control failed")
    converged(s.dss, res)
    vmin, vmax = voltage_range(s.dss)
    tot = engine.totals(s.dss)
    rows = [{"mrid": str(p.mrid), "name": p.name, "cim_class": p.cim_class, "kind": p.action.kind,
             "value": p.action.value, "applied": True} for p in planned]
    data = TableData(
        columns=columns(("name", "Device"), ("kind", "Action"), ("value", "Value"), ("applied", "Applied")),
        rows=rows,
        summary={"converged": True, "iterations": res.iterations, "control_mode": mode,
                 "min_v_pu": round(vmin, 5), "max_v_pu": round(vmax, 5), "losses_kw": round(tot["losses_kw"], 3),
                 "total_load_kw": round(tot["total_load_kw"], 3), "modified": True})
    warnings = ["regulator and capacitor controls frozen (control_mode=off) after a manual tap or capacitor action"] \
        if manual else []
    warnings.append("in-memory circuit only; the CIM model and switch.open are unchanged")
    return Output(dump(data), warnings, record=True)


def register(mcp, services: Services) -> None:
    @mcp.tool(name="set_control", structured_output=True)
    async def set_control_tool(ctx: Context, actions: list[Action], max_iterations: int = 100,
                               request_id: str | None = None) -> Envelope:
        """Apply up to 20 control actions to the loaded feeder's in-memory circuit (open or close a switch, set a
        regulator tap in steps from neutral, switch a capacitor on or off), re-solve and report convergence, min/max
        voltage (pu), losses (kW) and load (kW). The CIM model is not changed; every later result is marked
        modified. Returns a table (data.viz = "table") of the actions."""
        raw = {"actions": [a.model_dump() if isinstance(a, BaseModel) else a for a in actions],
               "max_iterations": max_iterations, "request_id": request_id}
        return await invoke(services, ctx, "set_control", SetControlInput, raw, lambda s, a: execute(services, s, a))
