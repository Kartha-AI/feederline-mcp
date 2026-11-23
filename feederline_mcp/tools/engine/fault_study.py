# SPDX-License-Identifier: Apache-2.0
"""fault_study: bolted short-circuit current at a bus, and the current through each protective device upstream for a
fault of a given type and impedance. The circuit is restored afterwards."""

from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ... import engine
from ...compiler import emit, scope
from ...compiler.scope import ScopeError
from ...sessions import Session
from ...shapes import TableData, columns, dump
from ...studies import Envelope, ToolError, resolve_bus
from .._runner import Output, Services, invoke
from ..sql.common import Feeders
from ..sql.topology import upstream
from ._shared import PHASE, sql_call

FaultType = Literal["3ph", "slg", "ll"]


class FaultStudyInput(BaseModel):
    bus: str = Field(min_length=1, max_length=200)
    fault_type: FaultType = "3ph"
    impedance_ohm: float = Field(default=0.0, ge=0, le=1000)
    request_id: str | None = Field(default=None, max_length=128)


def protective_chain(services: Services, s: Session, bus: str) -> list[tuple[str, object, str, str]]:
    """(dss name, mRID, name, CIM class) of the protective devices between the bus and the feeder head."""
    feeding = next((ln for ln in engine.lines(s.dss) if ln["bus2"] == bus), None) \
        or next((ln for ln in engine.lines(s.dss) if ln["bus1"] == bus), None)
    if feeding is None or feeding["dss_name"] not in s.names:
        return []
    c = sql_call(services, s)
    try:
        a = scope.anchor(c.ds, c.principal, s.names[feeding["dss_name"]][0], s.model.case_kind)
    except ScopeError:
        return []
    path, _ = upstream(c, a, s.model.case_kind, Feeders(c, s.model.region_code))
    out = []
    for h in path.hops:
        dss = emit.dss_name(h.mrid)
        if h.protective and dss in s.names:
            out.append((dss, h.mrid, h.name, h.cim_class))
    return out


def execute(services: Services, s: Session, args: FaultStudyInput) -> Output:
    bus = resolve_bus(s.names, args.bus)
    nodes = [n for n in engine.bus_info(s.dss, bus)["nodes"] if n in PHASE]
    if not nodes:
        raise ToolError("invalid_input", f"bus {args.bus} has no phase nodes")
    if args.fault_type == "ll" and len(nodes) < 2:
        raise ToolError("invalid_input", f"bus {args.bus} has one phase; a line-to-line fault needs two")
    chain = protective_chain(services, s, bus)
    bus_mrid, bus_name = s.names[bus]
    rows = []
    with services.store.sweep(s):
        engine.run(s.dss, ["Set mode=faultstudy"])
        s.dss.ActiveCircuit.Solution.Solve()
        for n, amps in sorted(engine.bus_isc(s.dss, bus).items()):
            if n in PHASE:
                rows.append({"mrid": str(bus_mrid), "name": bus_name, "cim_class": "ConnectivityNode",
                             "kind": "bolted_isc", "phase": PHASE[n], "current_a": round(amps, 1)})
        engine.run(s.dss, emit.fault(bus, args.fault_type, args.impedance_ohm, nodes))
        res = engine.solve(s.dss, 100, "off", "direct")
        if not res.converged:
            raise ToolError("not_converged", "fault solution did not converge", iterations=res.iterations)
        fault_nodes = engine.element_nodes(s.dss, f"Fault.{emit.FAULT_NAME}")
        for n, amps in zip(fault_nodes, engine.element_currents(s.dss, f"Fault.{emit.FAULT_NAME}"), strict=False):
            if n in PHASE:
                rows.append({"mrid": str(bus_mrid), "name": bus_name, "cim_class": "ConnectivityNode",
                             "kind": "fault_current", "phase": PHASE[n], "current_a": round(amps, 1)})
        for dss, mrid, name, cls in chain:
            for n, amps in zip(engine.element_nodes(s.dss, f"Line.{dss}"),
                               engine.element_currents(s.dss, f"Line.{dss}"), strict=False):
                if n in PHASE:
                    rows.append({"mrid": str(mrid), "name": name, "cim_class": cls, "kind": "through_current",
                                 "phase": PHASE[n], "current_a": round(amps, 1)})
    fault_a = [r["current_a"] for r in rows if r["kind"] == "fault_current"]
    data = TableData(
        columns=columns(("name", "Location"), ("kind", "Quantity"), ("phase", "Phase"), ("current_a", "Current", "A")),
        rows=rows,
        summary={"bus": bus_name, "bus_mrid": str(bus_mrid), "fault_type": args.fault_type,
                 "impedance_ohm": args.impedance_ohm, "max_fault_current_a": max(fault_a, default=None),
                 "protective_devices": [name for _, _, name, _ in chain]})
    warnings = [] if chain else ["no protective device found between the bus and the feeder head"]
    if s.model.modified:
        warnings.append("computed on the modified in-memory circuit (set_control)")
    return Output(dump(data), warnings, record=True)


def register(mcp, services: Services) -> None:
    @mcp.tool(name="fault_study", structured_output=True)
    async def fault_study_tool(ctx: Context, bus: str, fault_type: FaultType = "3ph", impedance_ohm: float = 0.0,
                               request_id: str | None = None) -> Envelope:
        """Fault currents at a bus (mRID or name) of the loaded feeder: the bolted short-circuit current per phase
        (A), the current into a 3ph, slg or ll fault through impedance_ohm (ohm), and the current (A) through each
        protective device (fuse, recloser, breaker) between the bus and the feeder head. The circuit is restored
        afterwards. Returns a table (data.viz = "table")."""
        raw = {"bus": bus, "fault_type": fault_type, "impedance_ohm": impedance_ohm, "request_id": request_id}
        return await invoke(services, ctx, "fault_study", FaultStudyInput, raw, lambda s, a: execute(services, s, a))
