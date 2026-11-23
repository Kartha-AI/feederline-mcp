# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ... import engine
from ...compiler import ScopeError, compile_feeder
from ...sessions import Session
from ...studies import ToolError, bus_kv_sanity
from .._runner import Output, Services, invoke

log = logging.getLogger(__name__)
REGION = r"^[A-Za-z0-9_\-]{1,64}$"


class LoadFeederInput(BaseModel):
    root_mrid: UUID
    case: Literal["operational", "planning"] = "operational"
    region_code: str = Field(pattern=REGION)
    request_id: str | None = Field(default=None, max_length=128)


def execute(services: Services, s: Session, args: LoadFeederInput) -> Output:
    try:
        compiled = compile_feeder(services.ds, s.principal, args.root_mrid, args.case, args.region_code)
    except ScopeError as e:
        raise ToolError("not_found", str(e)) from None
    ctx = engine.new_context()
    try:
        engine.run(ctx, compiled.commands)
    except Exception as e:
        raise ToolError("compile_failed", f"engine rejected the compiled feeder: {e}") from None
    compiled.model.warnings += bus_kv_sanity(ctx, compiled.bus_nominal, compiled.names)
    compiled.release()
    s.set_model(compiled, ctx)
    try:
        services.store.persist(s)
    except Exception as e:  # the study still works on this replica; it just cannot move
        log.exception("snapshot persist failed")
        compiled.model.warnings.append(f"session snapshot not persisted ({type(e).__name__}); no failover for it")
    services.submit(services.sink.write_model, compiled.model)
    report = compiled.model.report()
    report.pop("principal", None)
    warnings = report.pop("warnings")
    report["voltage_bases_kv"] = compiled.voltage_bases
    report["buses"] = sum(1 for k in compiled.names if k.startswith("cn_"))
    return Output(report, warnings)


def register(mcp, services: Services) -> None:
    @mcp.tool()
    async def load_feeder(ctx: Context, root_mrid: UUID, region_code: str,
                          case: Literal["operational", "planning"] = "operational", request_id: str | None = None) -> dict:
        """Compile the feeder below a head breaker (or voltage level) from the CIM model into a fresh OpenDSS circuit
        for this session. operational: live switch states and measured loads (kW/kvar), planning: normal switch states
        and nameplate loads. Returns the compile report: equipment counts by CIM class, measured/allocated/defaulted
        loads, allocation ratio, defaulted impedances, stale switches, voltage bases (kV)."""
        raw = {"root_mrid": root_mrid, "case": case, "region_code": region_code, "request_id": request_id}
        return await invoke(services, ctx, "load_feeder", LoadFeederInput, raw,
                            lambda s, a: execute(services, s, a), needs_model=False)
