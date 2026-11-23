# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field, model_validator

from ... import engine
from ...sessions import Session
from ...studies import converged
from .._runner import Output, Services, invoke

PHASE = {1: "A", 2: "B", 3: "C"}


class CheckVoltagesInput(BaseModel):
    min_pu: float = Field(default=0.95, gt=0, lt=2)
    max_pu: float = Field(default=1.05, gt=0, lt=2)
    limit: int = Field(default=200, ge=1, le=5000)
    request_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def ordered(self):
        if self.min_pu >= self.max_pu:
            raise ValueError("min_pu must be below max_pu")
        return self


def ensure_solved(s: Session) -> None:
    if not s.solved:
        res = engine.solve(s.dss)
        s.solved = res.converged
        converged(s.dss, res)


def execute(s: Session, args: CheckVoltagesInput) -> Output:
    ensure_solved(s)
    violations = []
    for n in engine.node_voltages(s.dss):
        if n.kv_base <= 0 or n.node not in PHASE:
            continue
        kind = "under" if n.pu < args.min_pu else "over" if n.pu > args.max_pu else None
        if kind:
            mrid, human = s.names.get(n.bus, (None, n.bus))
            violations.append({"bus": n.bus, "mrid": str(mrid) if mrid else None, "name": human, "phase": PHASE[n.node],
                               "v_pu": round(n.pu, 5), "kind": kind})
    violations.sort(key=lambda v: -max(args.min_pu - v["v_pu"], v["v_pu"] - args.max_pu))
    data = {"violations": violations[: args.limit], "count": len(violations),
            "worst": violations[0] if violations else None, "compliant": not violations}
    return Output(data, [] if len(violations) <= args.limit else [f"{len(violations) - args.limit} violations not listed"])


def register(mcp, services: Services) -> None:
    @mcp.tool()
    async def check_voltages(ctx: Context, min_pu: float = 0.95, max_pu: float = 1.05, limit: int = 200,
                             request_id: str | None = None) -> dict:
        """List bus/phase voltages outside [min_pu, max_pu] on the solved feeder (solving first if needed), worst
        first. Voltages are per unit of the bus voltage base."""
        raw = {"min_pu": min_pu, "max_pu": max_pu, "limit": limit, "request_id": request_id}
        return await invoke(services, ctx, "check_voltages", CheckVoltagesInput, raw, execute)
