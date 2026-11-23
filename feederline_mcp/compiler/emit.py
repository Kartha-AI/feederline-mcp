# SPDX-License-Identifier: Apache-2.0
"""Rows -> DSS command strings. The only place DSS text is built; inputs are validated rows, never client text."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from . import conductors, validate

PHASE_NODES = {"A": "1", "B": "2", "C": "3"}
SPLIT_NODES = {"s1": "1", "s2": "2", "s12": "12"}  # CIM split-phase codes on 120/240 V secondaries
SEEDED_VMIN_PU, SEEDED_VMAX_PU = 0.5, 1.5
DEFAULT_SOURCE = {"pu": 1.02, "angle": 0.0, "mvasc3": 2000.0, "mvasc1": 2100.0}


def dss_name(mrid: UUID | str) -> str:
    return str(mrid).replace("-", "")


def node_list(phases: str) -> list[str]:
    if phases in SPLIT_NODES:
        return list(SPLIT_NODES[phases])
    return [PHASE_NODES[p] for p in phases]


def phase_count(phases: str) -> int:
    return len(node_list(phases))


def node_suffix(phases: str) -> str:
    return "".join("." + n for n in node_list(phases))


def bus_base(cn_mrid: UUID | str) -> str:
    return "cn_" + str(cn_mrid)[:8]


def bus(cn_mrid: UUID | str, phases: str) -> str:
    return bus_base(cn_mrid) + node_suffix(phases)


def fmt(v: float) -> str:
    return f"{float(v):.10g}"


def lower_triangle(flat: list[float], n: int) -> str:
    rows = [" ".join(fmt(flat[i * n + j]) for j in range(i + 1)) for i in range(n)]
    return "[" + " | ".join(rows) + "]"


@dataclass
class LoadSeed:
    """A load row after seeding: the kW/kvar the compiler decided to use and how."""

    row: Mapping[str, Any]
    kw: float
    kvar: float
    model: int
    how: str  # measured | allocated | defaulted


@dataclass
class Emitted:
    commands: list[str] = field(default_factory=list)
    names: dict[str, tuple[UUID, str]] = field(default_factory=dict)
    defaulted_impedances: int = 0
    voltage_bases: list[float] = field(default_factory=list)
    bus_names: dict[str, str] = field(default_factory=dict)  # str(cn mrid) -> DSS bus name, set by node_names()

    def add(self, cmd: str) -> None:
        self.commands.append(cmd)

    def bus_base(self, cn_mrid: UUID | str) -> str:
        return self.bus_names.get(str(cn_mrid)) or bus_base(cn_mrid)

    def bus(self, cn_mrid: UUID | str, phases: str) -> str:
        return self.bus_base(cn_mrid) + node_suffix(phases)


def circuit(root: Mapping[str, Any], pu: float, out: Emitted) -> None:
    src = {**DEFAULT_SOURCE, **(root.get("attrs") or {}).get("source", {})}
    kv = float(root["nominal_voltage"] or src.get("basekv", 12.47))
    name = dss_name(root["mrid"])
    cmd = f"New Circuit.{name} bus1={out.bus(root['source_cn'], 'ABC')} basekv={fmt(kv)} pu={fmt(pu)} angle={fmt(src['angle'])}"
    if all(k in src for k in ("r1", "x1", "r0", "x0")):
        cmd += f" r1={fmt(src['r1'])} x1={fmt(src['x1'])} r0={fmt(src['r0'])} x0={fmt(src['x0'])}"
    else:
        cmd += f" MVAsc3={fmt(src['mvasc3'])} MVAsc1={fmt(src['mvasc1'])}"
    out.add("Clear")
    out.add("Set DefaultBaseFrequency=60")
    out.add(cmd)
    out.names["vsource.source"] = (root["mrid"], f"{root['name']} source")


def _impedance(row: Mapping[str, Any], length_km: float) -> str:
    pz = (row.get("attrs") or {}).get("phase_impedance")
    n = phase_count(row["phases1"])
    if validate.has_phase_impedance(row):
        s = f" rmatrix={lower_triangle(pz['r'], n)} xmatrix={lower_triangle(pz['x'], n)}"
        if pz.get("c"):
            s += f" cmatrix={lower_triangle(pz['c'], n)}"
        return s
    return ""


def _sequence(flat: list[float], n: int) -> tuple[float, float]:
    """Positive- and zero-sequence values of a symmetric phase matrix (averaged self and mutual terms)."""
    self_ = sum(flat[i * n + i] for i in range(n)) / n
    mutual = (sum(flat) - self_ * n) / (n * (n - 1)) if n > 1 else 0.0
    return self_ - mutual, self_ + (n - 1) * mutual


def segment(row: Mapping[str, Any], out: Emitted) -> None:
    name = dss_name(row["mrid"])
    length_km = float(row["length"] or 0) / 1000 or 0.001
    n = phase_count(row["phases1"])
    cmd = (f"New Line.{name} phases={n} bus1={out.bus(row['cn1'], row['phases1'])} bus2={out.bus(row['cn2'], row['phases2'])}"
           f" length={fmt(length_km)} units=km")
    z = _impedance(row, length_km)
    if z:
        cmd += z
    elif not validate.segment_defaulted(row):
        r0 = row["r0"] if row["r0"] is not None else 3 * float(row["r"])
        x0 = row["x0"] if row["x0"] is not None else 3 * float(row["x"])
        cmd += (f" r1={fmt(float(row['r']) / length_km)} x1={fmt(float(row['x']) / length_km)}"
                f" r0={fmt(float(r0) / length_km)} x0={fmt(float(x0) / length_km)} c1=0 c0=0")
    else:
        c = conductors.lookup(row.get("conductor_type"))
        cmd += f" r1={fmt(c['r1'])} x1={fmt(c['x1'])} r0={fmt(c['r0'])} x0={fmt(c['x0'])} c1=0 c0=0"
        out.defaulted_impedances += 1
    if row.get("rated_current"):
        cmd += f" normamps={fmt(row['rated_current'])}"
    out.add(cmd)
    out.names[name] = (row["mrid"], row["name"])


def switch(row: Mapping[str, Any], is_open: bool, out: Emitted) -> None:
    name = dss_name(row["mrid"])
    n = phase_count(row["phases1"])
    pz = (row.get("attrs") or {}).get("phase_impedance")
    out.add(f"New Line.{name} phases={n} bus1={out.bus(row['cn1'], row['phases1'])} bus2={out.bus(row['cn2'], row['phases2'])} switch=y")
    # switch=y applies its own 1-ohm defaults at the end of the command, so the impedance goes in a separate Edit.
    # The engine ignores matrices on switch lines; sequence values are what it honours.
    if pz:
        r1, r0 = _sequence(pz["r"], n)
        x1, x0 = _sequence(pz["x"], n)
        c1, c0 = _sequence(pz["c"], n) if pz.get("c") else (0.0, 0.0)
        edit = (f"Edit Line.{name} length={fmt(pz['length_km'])} units=km r1={fmt(r1)} r0={fmt(r0)}"
                f" x1={fmt(x1)} x0={fmt(x0)} c1={fmt(c1)} c0={fmt(c0)}")
    else:
        edit = f"Edit Line.{name} r1=0.0001 r0=0.0001 x1=0 x0=0 c1=0 c0=0 length=1 units=km"
    # An unrated switch reports no loading rather than the engine's 400 A default.
    edit += f" normamps={fmt(row['rated_current'] or 0)} emergamps={fmt(row['rated_current'] or 0)}"
    out.add(edit)
    if is_open:
        out.add(f"Open Line.{name} 1")
    out.names[name] = (row["mrid"], row["name"])


TAP_STEP = 0.00625  # 5/8 % per step, 32-step regulator


def transformer(row: Mapping[str, Any], out: Emitted, tap_position: int | None = None) -> None:
    """2- or 3-winding transformer. End reactances are a star equivalent in ohms on each end's voltage and end 1's
    MVA; a regulator starts from tap_position (measured) when given, else neutral."""
    name = dss_name(row["mrid"])
    attrs = row.get("attrs") or {}
    ends = sorted(row["ends"], key=lambda e: e["end_number"])
    if len(ends) not in (2, 3):
        raise ValueError(f"transformer {row['name']} has {len(ends)} ends; 2 or 3 are supported")
    phases = int(attrs.get("phases") or phase_count(ends[0]["phases"]))
    wiring = attrs.get("winding_nodes") or [None] * len(ends)
    mva1 = float(ends[0]["rated_s"])
    buses, conns, kvs, kvas, rs, xs = [], [], [], [], [], []
    for e, nodes in zip(ends, wiring, strict=True):
        kv, mva = float(e["rated_u"]), float(e["rated_s"])
        buses.append(out.bus_base(e["cn"]) + "".join(f".{n}" for n in nodes) if nodes else out.bus(e["cn"], e["phases"]))
        conns.append("delta" if e["connection_kind"] == "D" else "wye")
        kvs.append(fmt(kv))
        kvas.append(fmt(mva * 1000))
        rs.append(fmt(float(e["r"] or 0) / (kv * kv / mva) * 100))
        xs.append(float(e["x"] or 0) / (kv * kv / mva1) * 100)
    cmd = (f"New Transformer.{name} phases={phases} windings={len(ends)} buses=[{' '.join(buses)}]"
           f" conns=[{' '.join(conns)}] kvs=[{' '.join(kvs)}] kvas=[{' '.join(kvas)}] %rs=[{' '.join(rs)}]")
    if len(ends) == 2:
        cmd += f" xhl={fmt(xs[0] + xs[1] or 0.01)}"
    else:
        cmd += f" xhl={fmt(xs[0] + xs[1])} xht={fmt(xs[0] + xs[2])} xlt={fmt(xs[1] + xs[2])}"
    for key, prop in (("ppm_antifloat", "ppm_antifloat"), ("noloadloss_pct", "%noloadloss"), ("imag_pct", "%imag"),
                      ("normhkva", "normhkva"), ("emerghkva", "emerghkva")):
        if attrs.get(key) is not None:
            cmd += f" {prop}={fmt(attrs[key])}"
    reg = attrs.get("regulator")
    if reg and tap_position is not None:
        taps = [1.0] * len(ends)
        taps[int(reg.get("winding", 2)) - 1] = 1.0 + TAP_STEP * tap_position
        cmd += f" taps=[{' '.join(fmt(t) for t in taps)}]"
    elif attrs.get("taps"):
        cmd += f" taps=[{' '.join(fmt(t) for t in attrs['taps'])}]"
    out.add(cmd)
    out.names[name] = (row["mrid"], row["name"])
    if reg:
        out.add(
            f"New RegControl.{name} transformer={name} winding={int(reg.get('winding', 2))} vreg={fmt(reg['vreg'])}"
            f" band={fmt(reg['band'])} ptratio={fmt(reg['ptratio'])} ctprim={fmt(reg['ctprim'])}"
            f" R={fmt(reg.get('r', 0))} X={fmt(reg.get('x', 0))}"
            + (f" maxtapchange={int(reg['maxtapchange'])}" if reg.get("maxtapchange") is not None else "")
        )


def _load_kv(row: Mapping[str, Any]) -> tuple[int, float]:
    """DSS phases and kV for a load (1-phase wye is rated L-N; delta and 3-phase are rated L-L)."""
    attrs = row.get("attrs") or {}
    phases, conn = row["phases"], row["phase_connection"] or "Y"
    n = 1 if (conn == "D" and phase_count(phases) == 2) else phase_count(phases)
    if attrs.get("kv"):
        return n, float(attrs["kv"])
    ll = float(row["nominal_voltage"])
    return n, (ll / math.sqrt(3) if (n == 1 and conn != "D") else ll)


def load(seed: LoadSeed, out: Emitted) -> None:
    row = seed.row
    attrs = row.get("attrs") or {}
    name = dss_name(row["mrid"])
    n, kv = _load_kv(row)
    conn = "delta" if row["phase_connection"] == "D" else "wye"
    cmd = (f"New Load.{name} bus1={out.bus(row['cn'], row['phases'])} phases={n} conn={conn} model={seed.model}"
           f" kv={fmt(kv)} kw={fmt(seed.kw)} kvar={fmt(seed.kvar)}")
    if seed.how == "defaulted":
        if attrs.get("vminpu") is not None:
            cmd += f" vminpu={fmt(attrs['vminpu'])}"
        if attrs.get("vmaxpu") is not None:
            cmd += f" vmaxpu={fmt(attrs['vmaxpu'])}"
    else:
        # Measured/allocated P is what the load draws at today's voltage: keep it constant-P over a wide band.
        cmd += f" vminpu={fmt(SEEDED_VMIN_PU)} vmaxpu={fmt(SEEDED_VMAX_PU)}"
    out.add(cmd)
    out.names[name] = (row["mrid"], row["name"])


def capacitor(row: Mapping[str, Any], out: Emitted) -> None:
    attrs = row.get("attrs") or {}
    name = dss_name(row["mrid"])
    n = phase_count(row["phases"])
    kv = attrs.get("kv") or (float(row["nominal_voltage"]) / (math.sqrt(3) if n == 1 else 1))
    conn = "delta" if attrs.get("conn") == "D" else "wye"
    out.add(f"New Capacitor.{name} bus1={out.bus(row['cn'], row['phases'])} phases={n} kvar={fmt(attrs.get('kvar', 0))}"
            f" kv={fmt(kv)} conn={conn}")
    out.names[name] = (row["mrid"], row["name"])


def der(row: Mapping[str, Any], out: Emitted) -> None:
    attrs = row.get("attrs") or {}
    name = dss_name(row["mrid"])
    n = phase_count(row["phases"])
    ll = float(row["nominal_voltage"])
    kv = ll / math.sqrt(3) if n == 1 else ll
    kw = float(attrs.get("rated_kw", 0))
    pf = float(attrs.get("pf", 1.0))
    if attrs.get("der_type", "pv") == "pv":
        out.add(f"New PVSystem.{name} bus1={out.bus(row['cn'], row['phases'])} phases={n} kv={fmt(kv)} kva={fmt(kw * 1.1)}"
                f" pmpp={fmt(kw)} irradiance=1 pf={fmt(pf)}")
        vv = attrs.get("volt_var")
        if vv:
            xs, ys = vv.get("v", [0.5, 0.92, 0.98, 1.02, 1.08, 1.5]), vv.get("q", [1, 1, 0, 0, -1, -1])
            out.add(f"New XYcurve.vv{name} npts={len(xs)} xarray=[{' '.join(map(fmt, xs))}] yarray=[{' '.join(map(fmt, ys))}]")
            out.add(f"New InvControl.{name} DERList=[PVSystem.{name}] mode=VOLTVAR vvc_curve1=vv{name} voltage_curvex_ref=rated")
    else:
        out.add(f"New Generator.{name} bus1={out.bus(row['cn'], row['phases'])} phases={n} kv={fmt(kv)} kw={fmt(kw)}"
                f" pf={fmt(pf)} model=1")
    out.names[name] = (row["mrid"], row["name"])


def voltage_bases(nominals: Iterable[float]) -> list[float]:
    out: list[float] = []
    for v in sorted({round(float(x), 3) for x in nominals if x}, reverse=True):
        if all(abs(v - b) / b > 0.01 for b in out):
            out.append(v)
    return out


def transformer_end_bases(transformers: Iterable[Mapping[str, Any]]) -> list[float]:
    vals = []
    for t in transformers:
        phases = int((t.get("attrs") or {}).get("phases") or 3)
        for e in t["ends"]:
            kv = float(e["rated_u"])
            vals.append(kv * math.sqrt(3) if phases == 1 and e["connection_kind"] != "D" else kv)
    return vals


def finish(bases: list[float], out: Emitted) -> None:
    out.voltage_bases = bases
    out.add(f"Set voltagebases=[{' '.join(fmt(b) for b in bases)}]")
    out.add("CalcVoltageBases")


def node_names(nodes: Iterable[Mapping[str, Any]], out: Emitted) -> None:
    """Bus names are 'cn_' + the first 8 hex of the node mRID; nodes whose prefix collides use the full hex."""
    nodes = list(nodes)
    groups: dict[str, set[str]] = {}
    for n in nodes:
        groups.setdefault(bus_base(n["mrid"]), set()).add(str(n["mrid"]))
    for n in nodes:
        b = bus_base(n["mrid"])
        if len(groups[b]) > 1:
            b = "cn_" + dss_name(n["mrid"])
        out.bus_names[str(n["mrid"])] = b
        out.names[b] = (n["mrid"], n["name"] or b)


# --- in-memory controls and faults (set_control, fault_study). Names are session DSS names, never client text. ---
FAULT_NAME = "study"


def _safe_name(dss: str) -> str:
    if not dss or not all(ch.isalnum() or ch == "_" for ch in dss):
        raise ValueError(f"not a DSS element name: {dss!r}")
    return dss


def open_switch(dss: str) -> list[str]:
    return [f"Open Line.{_safe_name(dss)} 1"]


def close_switch(dss: str) -> list[str]:
    n = _safe_name(dss)
    return [f"Close Line.{n} 1", f"Close Line.{n} 2"]


def set_tap(dss: str, tap: int, winding: int = 2) -> list[str]:
    """Regulator tap position in steps from neutral (5/8 % per step)."""
    return [f"Edit Transformer.{_safe_name(dss)} wdg={int(winding)} tap={fmt(1.0 + TAP_STEP * int(tap))}"]


def set_capacitor(dss: str, on: bool, steps: int = 1) -> list[str]:
    states = " ".join(["1" if on else "0"] * max(1, int(steps)))
    return [f"Edit Capacitor.{_safe_name(dss)} states=[{states}]"]


def fault(bus: str, kind: str, impedance_ohm: float, nodes: list[int]) -> list[str]:
    """A fault at a bus: 3ph (all phases to ground), slg (first phase to ground), ll (first two phases)."""
    b = _safe_name(bus)
    r = fmt(max(float(impedance_ohm), 1e-4))
    if kind == "3ph":
        ph = [n for n in nodes if n in (1, 2, 3)]
        return [f"New Fault.{FAULT_NAME} bus1={b}.{'.'.join(map(str, ph))} phases={len(ph)} r={r}"]
    if kind == "slg":
        return [f"New Fault.{FAULT_NAME} bus1={b}.{nodes[0]} phases=1 r={r}"]
    if kind == "ll":
        if len(nodes) < 2:
            raise ValueError("a line-to-line fault needs a bus with two phases")
        return [f"New Fault.{FAULT_NAME} bus1={b}.{nodes[0]} bus2={b}.{nodes[1]} phases=1 r={r}"]
    raise ValueError(f"unknown fault kind {kind!r}")


def remove_fault() -> list[str]:
    return [f"Disable Fault.{FAULT_NAME}"]
