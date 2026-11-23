# SPDX-License-Identifier: Apache-2.0
"""Engine calls on one dss_python context: load commands, solve, read results. No DB, no client text."""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dss import DSS

ENGINE = "dss_python"

_libm = ctypes.CDLL(ctypes.util.find_library("m") or None)
_thread = threading.local()


def prepare_thread() -> None:
    """Put this thread's FPU in non-stop mode once. On threads it did not create, the engine's runtime enables FP
    traps on first entry (SIGFPE on Linux, SIGILL on macOS arm64); the main thread is already masked."""
    if getattr(_thread, "ready", False):
        return
    _ = DSS.Version  # first engine entry on this thread: the runtime sets its trap mask here, so mask after it
    _thread.env = ctypes.create_string_buffer(256)  # fenv_t, opaque and smaller than this on every platform
    _libm.feholdexcept(_thread.env)
    _thread.ready = True


def version() -> str:
    import dss

    return f"{ENGINE} {dss.__version__}"


def new_context():
    prepare_thread()
    ctx = DSS.NewContext()
    ctx.AllowForms = False
    return ctx


def run(ctx, commands: Iterable[str]) -> None:
    prepare_thread()
    for c in commands:
        ctx.Text.Command = c


class NotConverged(Exception):
    def __init__(self, iterations: int, max_error: float):
        super().__init__(f"power flow did not converge after {iterations} iterations (max error {max_error:.3g})")
        self.iterations = iterations
        self.max_error = max_error


@dataclass
class SolveResult:
    converged: bool
    iterations: int
    max_error: float


def solve(ctx, max_iterations: int = 100, control_mode: str = "static", mode: str = "snapshot") -> SolveResult:
    run(ctx, [f"Set mode={mode}", f"Set maxiterations={int(max_iterations)}", f"Set controlmode={control_mode}"])
    sol = ctx.ActiveCircuit.Solution
    sol.Solve()
    return SolveResult(bool(sol.Converged), int(sol.Iterations), float(sol.Tolerance))


def converged_or_raise(ctx, res: SolveResult) -> None:
    if not res.converged:
        raise NotConverged(res.iterations, ctx.ActiveCircuit.Solution.Tolerance)


@dataclass
class NodeVoltage:
    bus: str
    node: int
    pu: float
    volts: float
    kv_base: float


def node_voltages(ctx) -> list[NodeVoltage]:
    ckt = ctx.ActiveCircuit
    out = []
    for name in ckt.AllBusNames:
        ckt.SetActiveBus(name)
        b = ckt.ActiveBus
        pu, v = list(b.puVmagAngle), list(b.VMagAngle)
        for i, n in enumerate(b.Nodes):
            out.append(NodeVoltage(name, int(n), pu[2 * i], v[2 * i], b.kVBase))
    return out


def totals(ctx) -> dict[str, float]:
    ckt = ctx.ActiveCircuit
    p, q = ckt.TotalPower
    loss = ckt.Losses
    return {"total_load_kw": -p, "total_load_kvar": -q, "losses_kw": loss[0] / 1000, "losses_kvar": loss[1] / 1000}


def line_loadings(ctx) -> list[dict[str, Any]]:
    """Max phase current / NormAmps per Line (switches included)."""
    ckt = ctx.ActiveCircuit
    L = ckt.Lines
    out = []
    i = L.First
    while i > 0:
        e = ckt.ActiveCktElement
        amps = e.NormalAmps
        mags = list(e.CurrentsMagAng)[0::2]
        nc = e.NumConductors
        imax = max(mags[:nc]) if mags else 0.0
        out.append({"dss_name": L.Name, "amps": imax, "normamps": amps,
                    "loading_pct": 100 * imax / amps if amps > 0 else 0.0, "is_switch": bool(L.IsSwitch)})
        i = L.Next
    return out


def head_power(ctx) -> tuple[float, float]:
    p, q = ctx.ActiveCircuit.TotalPower
    return -p, -q


def bus_exists(ctx, bus: str) -> bool:
    return ctx.ActiveCircuit.SetActiveBus(bus) >= 0


def bus_info(ctx, bus: str) -> dict[str, Any]:
    ckt = ctx.ActiveCircuit
    ckt.SetActiveBus(bus)
    b = ckt.ActiveBus
    return {"nodes": [int(n) for n in b.Nodes], "kv_base_ll": b.kVBase * math.sqrt(3)}


def add_der(ctx, name: str, bus: str, nodes: list[int], kv_ll: float, kw: float, der_type: str) -> None:
    phases = len(nodes)
    kv = kv_ll if phases > 1 else kv_ll / math.sqrt(3)
    b = bus + "".join(f".{n}" for n in nodes)
    if der_type == "pv":
        run(ctx, [f"New PVSystem.{name} bus1={b} phases={phases} kv={kv:.6g} kva={kw * 1.1:.6g} pmpp={kw:.6g}"
                  " irradiance=1 pf=1"])
    else:  # storage discharging at rated power
        run(ctx, [f"New Storage.{name} bus1={b} phases={phases} kv={kv:.6g} kwrated={kw:.6g} kwhrated={4 * kw:.6g}"
                  f" kwhstored={4 * kw:.6g} dispmode=external state=discharging %discharge=100"])


def set_der_kw(ctx, name: str, kw: float, der_type: str) -> None:
    if der_type == "pv":
        run(ctx, [f"Edit PVSystem.{name} pmpp={kw:.6g} kva={kw * 1.1:.6g}"])
    else:
        run(ctx, [f"Edit Storage.{name} kwrated={kw:.6g} kwhrated={4 * kw:.6g} kwhstored={4 * kw:.6g}"])


def remove_element(ctx, cls: str, name: str) -> None:
    run(ctx, [f"Disable {cls}.{name}"])


OPEN_FILE = "Open.dss"


def open_terminals(ctx) -> list[str]:
    """'Open <element> <terminal>' for every open terminal on a line (switch states Save Circuit does not keep)."""
    ckt = ctx.ActiveCircuit
    L = ckt.Lines
    out = []
    i = L.First
    while i > 0:
        e = ckt.ActiveCktElement
        for t in (1, 2):
            if e.IsOpen(t, 0):
                out.append(f"Open Line.{L.Name} {t}")
        i = L.Next
    return out


def save_circuit(ctx, directory: str) -> None:
    run(ctx, [f'Save Circuit Dir="{directory}"'])
    Path(directory, OPEN_FILE).write_text("".join(c + "\n" for c in open_terminals(ctx)))


def redirect(ctx, master: str) -> None:
    # Save Circuit writes "Set VoltageBases" with CalcVoltageBases commented out and drops open terminals.
    run(ctx, ["Clear", f'Redirect "{master}"'])
    opens = Path(master).with_name(OPEN_FILE)
    if opens.exists():
        run(ctx, [c for c in opens.read_text().splitlines() if c.startswith("Open Line.")])
    run(ctx, ["CalcVoltageBases"])


def set_load_kw(ctx, dss_load: str, kw: float, kvar: float) -> None:
    ckt = ctx.ActiveCircuit
    ckt.Loads.Name = dss_load
    ckt.Loads.kW = kw
    ckt.Loads.kvar = kvar


# --- phase 2 readers ------------------------------------------------------------------------------------------
LENGTH_M = {0: 1.0, 1: 1609.344, 2: 304.8, 3: 1000.0, 4: 1.0, 5: 0.3048, 6: 0.0254, 7: 0.01, 8: 0.001}


def lines(ctx) -> list[dict[str, Any]]:
    """Every Line with per-conductor currents (A) and terminal-1 powers (kW, kvar), losses (kW, kvar), length (m)."""
    ckt = ctx.ActiveCircuit
    L = ckt.Lines
    out = []
    i = L.First
    while i > 0:
        e = ckt.ActiveCktElement
        nc = e.NumConductors
        mags = list(e.CurrentsMagAng)[0::2]
        pw = list(e.Powers)
        loss = list(e.Losses)
        buses = [b.split(".")[0] for b in e.BusNames]
        amps = mags[:nc]
        out.append({
            "dss_name": L.Name, "bus1": buses[0], "bus2": buses[1] if len(buses) > 1 else buses[0],
            "nodes": [int(n) for n in list(e.NodeOrder)[:nc]], "amps": amps,
            "kw": pw[0:2 * nc:2], "kvar": pw[1:2 * nc:2], "normamps": e.NormalAmps,
            "loading_pct": 100 * max(amps, default=0.0) / e.NormalAmps if e.NormalAmps > 0 else 0.0,
            "losses_kw": loss[0] / 1000, "losses_kvar": loss[1] / 1000,
            "length_m": L.Length * LENGTH_M.get(int(L.Units), 1.0), "is_switch": bool(L.IsSwitch),
            "open": bool(e.IsOpen(1, 0) or e.IsOpen(2, 0)),
        })
        i = L.Next
    return out


def element_currents(ctx, full_name: str) -> list[float]:
    """Terminal-1 current magnitude per conductor (A) of an element such as 'Line.x' or 'Fault.study'."""
    ckt = ctx.ActiveCircuit
    if ckt.SetActiveElement(full_name) < 0:
        return []
    e = ckt.ActiveCktElement
    return list(e.CurrentsMagAng)[0:2 * e.NumConductors:2]


def element_nodes(ctx, full_name: str) -> list[int]:
    ckt = ctx.ActiveCircuit
    if ckt.SetActiveElement(full_name) < 0:
        return []
    e = ckt.ActiveCktElement
    return [int(n) for n in list(e.NodeOrder)[:e.NumConductors]]


def source_powers(ctx) -> tuple[list[int], list[float], list[float]]:
    """Feeder-head (Vsource) power by conductor, as delivered into the feeder (kW, kvar)."""
    ckt = ctx.ActiveCircuit
    ckt.SetActiveElement("Vsource.source")
    e = ckt.ActiveCktElement
    nc = e.NumConductors
    pw = list(e.Powers)
    return [int(n) for n in list(e.NodeOrder)[:nc]], [-x for x in pw[0:2 * nc:2]], [-x for x in pw[1:2 * nc:2]]


def regcontrols(ctx) -> list[dict[str, Any]]:
    ckt = ctx.ActiveCircuit
    R = ckt.RegControls
    out = []
    i = R.First
    while i > 0:
        out.append({"name": R.Name, "transformer": R.Transformer, "winding": R.Winding, "tap_winding": R.TapWinding,
                    "tap_number": int(R.TapNumber), "band_v": R.ForwardBand, "vreg_v": R.ForwardVreg})
        i = R.Next
    return out


def element_exists(ctx, full_name: str) -> bool:
    return ctx.ActiveCircuit.SetActiveElement(full_name) >= 0


def is_switch_line(ctx, dss_name: str) -> bool:
    L = ctx.ActiveCircuit.Lines
    L.Name = dss_name
    return L.Name.lower() == dss_name.lower() and bool(L.IsSwitch)


def capacitor_steps(ctx, dss_name: str) -> int:
    C = ctx.ActiveCircuit.Capacitors
    C.Name = dss_name
    return int(C.NumSteps) if C.Name.lower() == dss_name.lower() else 0


def bus_isc(ctx, bus: str) -> dict[int, float]:
    """Bolted short-circuit current magnitude (A) per node from the last faultstudy solve."""
    ckt = ctx.ActiveCircuit
    ckt.SetActiveBus(bus)
    b = ckt.ActiveBus
    isc = list(b.Isc)
    return {int(n): math.hypot(isc[2 * k], isc[2 * k + 1]) for k, n in enumerate(b.Nodes) if 2 * k + 1 < len(isc)}
