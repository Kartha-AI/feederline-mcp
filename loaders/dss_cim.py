# SPDX-License-Identifier: Apache-2.0
"""DSS -> CIM: walk a solved OpenDSS circuit and emit CIM rows, SCADA registry and latest values.

Shared by the IEEE and SMART-DS loaders. The engine parses the DSS (dss_python); the walk covers Lines (disabled
ones included), Transformers (2 and 3 windings), RegControls, Loads, Capacitors and Buses. mRIDs are
uuid5(NAMESPACE, "<source>:<kind>:<id>"). Full phase-impedance matrices, load model, winding node orders and
regulator settings are profile extras in conducting_equipment.attrs so a compile round-trips the original circuit.
Split-phase (120/240 V) secondaries use the CIM phase codes s1, s2, s12.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from dss import DSS

from .common import LoadReport, Rows, mrid

log = logging.getLogger("loaders.dss_cim")

# DSS LineUnits -> km
UNIT_KM = {0: 1.0, 1: 1.609344, 2: 0.3048, 3: 1.0, 4: 0.001, 5: 0.0003048, 6: 0.0000254, 7: 0.00001, 8: 0.000001}
PHASE_LETTERS = {1: "A", 2: "B", 3: "C"}
SPLIT_CODES = {(1,): "s1", (2,): "s2", (1, 2): "s12"}
QUALITY_GOOD = 0
SOURCE_ESTIMATOR = 4


@dataclass(frozen=True)
class Source:
    key: str  # mRID namespace prefix, e.g. "ieee13" or "smartds:P1R:p1rhs0_1247"
    region: str
    label: str  # human prefix for the substation, head breaker and tags
    master: Path
    coords: Path | None = None
    lonlat: bool = False  # coords already lon/lat (else local x/y scaled about origin)
    coord_scale_m: float = 1.0
    origin: tuple[float, float] = (-117.0, 32.8)
    grid_tier: str = "Distribution"
    seed_scan_s: int = 900
    tag_prefix: str | None = None  # remote_source.name prefix; defaults to region


def phase_code(nodes: list[int], split: bool = False) -> str:
    nz = tuple(sorted({n for n in nodes if n in PHASE_LETTERS}))
    if split and nz in SPLIT_CODES:
        return SPLIT_CODES[nz]
    return "".join(PHASE_LETTERS[n] for n in nz)


def lower_bus(name: str) -> str:
    return name.split(".")[0].lower()


def nodes_of(bus_spec: str, nphases: int) -> list[int]:
    parts = bus_spec.split(".")[1:]
    return [int(p) for p in parts] if parts else list(range(1, nphases + 1))


def permuted(vals, n: int, order, scale: float = 1.0) -> list[float]:
    """Phase matrix re-ordered to ascending phase (A, B, C), per km."""
    m = np.array(vals, dtype=float).reshape(n, n)[np.ix_(order, order)]
    return [round(float(x), 10) for x in (m * scale).flatten()]


def seq_from_matrix(z: np.ndarray) -> tuple[complex, complex]:
    """Positive- and zero-sequence impedance of a (total-ohm) phase matrix."""
    n = z.shape[0]
    zs = np.trace(z) / n
    zm = (z.sum() - np.trace(z)) / (n * (n - 1)) if n > 1 else 0
    return zs - zm, zs + (n - 1) * zm


def star_reactances(xhl: float, xht: float | None, xlt: float | None, windings: int) -> list[float]:
    """Short-circuit reactances (%) -> per-winding star equivalent (%); 2-winding keeps it all on winding 1."""
    if windings == 2:
        return [xhl, 0.0]
    return [(xhl + xht - xlt) / 2, (xhl + xlt - xht) / 2, (xht + xlt - xhl) / 2]


def names(collection) -> list[str]:
    """Element names of a class, disabled ones included (AllNames reports ['NONE'] for an empty class)."""
    return list(collection.AllNames) if collection.Count > 0 else []


def layout(coords: dict[str, tuple[float, float] | None], adj: dict[str, set[str]],
           step: float = 10.0) -> dict[str, tuple[float, float]]:
    """Fill buses without coordinates from their nearest placed neighbour (tree layout fallback)."""
    placed = {b: c for b, c in coords.items() if c is not None}
    if not placed and adj:
        placed[next(iter(adj))] = (0.0, 0.0)
    q = deque(placed)
    while q:
        b = q.popleft()
        k = 0
        for nb in sorted(adj.get(b, ())):
            if nb not in placed:
                k += 1
                x, y = placed[b]
                placed[nb] = (x + step * k, y - step)
                q.append(nb)
    for b in coords:
        placed.setdefault(b, (0.0, 0.0))
    return placed


class DssModel:
    """Solved original circuit, read through the engine."""

    def __init__(self, master: Path, coords: Path | None = None):
        self.dss = DSS.NewContext()
        self.dss.AllowForms = False
        self.dss.Text.Command = f'Compile "{master}"'
        self.dss.Text.Command = "Set ControlMode=STATIC"
        self.dss.Text.Command = "Set maxcontroliter=50"
        self.dss.Text.Command = "Solve"
        self.ckt = self.dss.ActiveCircuit
        if not self.ckt.Solution.Converged:
            raise RuntimeError(f"{master} did not converge")
        if coords is not None:
            self.dss.Text.Command = f'BusCoords "{coords}"'

    def terminal_nodes(self) -> list[list[int]]:
        """Per terminal, every conductor's node (0 included)."""
        e = self.ckt.ActiveCktElement
        order = [int(n) for n in e.NodeOrder]
        nc = e.NumConductors
        return [order[t * nc:(t + 1) * nc] for t in range(e.NumTerminals)]

    def bus_data(self) -> dict[str, dict[str, Any]]:
        out = {}
        for name in self.ckt.AllBusNames:
            self.ckt.SetActiveBus(name)
            b = self.ckt.ActiveBus
            pu = list(b.puVmagAngle)
            v = list(b.VMagAngle)
            nodes = [int(n) for n in b.Nodes]
            out[name] = {
                "kv_base_ln": b.kVBase,
                "x": b.x if b.Coorddefined else None,
                "y": b.y if b.Coorddefined else None,
                "nodes": {str(n): {"pu": pu[2 * i], "volts": v[2 * i]} for i, n in enumerate(nodes)},
            }
        return out

    def fixture(self, name: str, buses: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        sol = self.ckt.Solution
        p, q = self.ckt.TotalPower
        loss = self.ckt.Losses
        buses = buses or self.bus_data()
        return {
            "feeder": name,
            "engine": self.dss.Version.split(" revision")[0],
            "converged": bool(sol.Converged),
            "iterations": sol.Iterations,
            "total_load_kw": -p,
            "total_load_kvar": -q,
            "losses_kw": loss[0] / 1000,
            "losses_kvar": loss[1] / 1000,
            "buses": {k: {"kv_base_ln": v["kv_base_ln"], "nodes": v["nodes"]} for k, v in buses.items()},
        }


class DssCimLoader:
    def __init__(self, source: Source, model: DssModel | None = None):
        self.s = source
        self.src = source.key
        self.region = source.region
        self.tag = source.tag_prefix or source.region
        self.m = model or DssModel(source.master, source.coords)
        self.rows = Rows()
        self.report = LoadReport(self.src)
        self.now = datetime.now(UTC)
        self.vbases = sorted(float(v) for v in self.m.ckt.Settings.VoltageBases)
        self._bv: set = set()
        self.cn: dict[str, Any] = {}
        self.buses: dict[str, dict[str, Any]] = {}

    # --- helpers -------------------------------------------------------------------------------------------------
    def id(self, kind: str, ident: Any):
        return mrid(self.src, kind, ident)

    def nominal_kv(self, kv_ln: float) -> float:
        ll = kv_ln * math.sqrt(3)
        return min(self.vbases, key=lambda v: abs(v - ll)) if self.vbases else round(ll, 3)

    def base_voltage(self, kv: float):
        bv = mrid("cim", "base_voltage", f"{kv:.3f}")
        if bv not in self._bv:
            self._bv.add(bv)
            self.rows.add("base_voltage", mrid=bv, nominal_voltage=round(kv, 3))
        return bv

    def lonlat(self, xy: tuple[float, float]) -> tuple[float, float]:
        if self.s.lonlat:
            return xy
        lon0, lat0 = self.s.origin
        dx, dy = xy[0] * self.s.coord_scale_m, xy[1] * self.s.coord_scale_m
        return lon0 + dx / (111_320 * math.cos(math.radians(lat0))), lat0 + dy / 110_540

    def point(self, kind: str, ident: str, bus: str, name: str):
        lon, lat = self.lonlat(self.coords[bus])
        loc = self.id(f"location:{kind}", ident)
        self.rows.add("location", mrid=loc, name=name, geom=f"POINT({lon:.8f} {lat:.8f})")
        return loc

    def linestring(self, kind: str, ident: str, b1: str, b2: str, name: str):
        (x1, y1), (x2, y2) = self.lonlat(self.coords[b1]), self.lonlat(self.coords[b2])
        loc = self.id(f"location:{kind}", ident)
        self.rows.add("location", mrid=loc, name=name, geom=f"LINESTRING({x1:.8f} {y1:.8f}, {x2:.8f} {y2:.8f})")
        return loc

    def equipment(self, kind: str, ident: str, cim_class: str, name: str, bus_kv: float, location, attrs=None,
                  container=None, in_service: bool = True):
        ce = self.id(kind, ident)
        self.rows.add(
            "conducting_equipment", mrid=ce, cim_class=cim_class, name=name, description=f"{self.s.label} {kind} {ident}",
            gis_oid=f"{self.src}:{kind}:{ident}", region_code=self.region,
            equipment_container_mrid=container or self.line_mrid, base_voltage_mrid=self.base_voltage(bus_kv),
            location_mrid=location, in_service=in_service, attrs=attrs or {}, valid_from=self.now, valid_to=None,
        )
        return ce

    def terminal(self, ce, seq: int, bus: str, phases: str):
        t = mrid(self.src, "terminal", f"{ce}:{seq}")
        self.rows.add("terminal", mrid=t, conducting_equipment_mrid=ce, connectivity_node_mrid=self.cn[bus],
                      sequence_number=seq, phases=phases)
        return t

    def phases(self, bus: str, nodes: list[int]) -> str:
        return phase_code(nodes, bus in self.split)

    def measurement(self, psr, terminal, tag: str, mtype: str, unit: str, mult: str, value: float, phases="ABC",
                    discrete=False):
        m = self.id("measurement", tag)
        self.rows.add("measurement", mrid=m, name=tag, measurement_type=mtype, phases=phases, unit_symbol=unit,
                      unit_multiplier=mult, power_system_resource_mrid=psr, terminal_mrid=terminal,
                      cim_class="Discrete" if discrete else "Analog", region_code=self.region)
        if discrete:
            self.rows.add("discrete", mrid=m, min_value=0, max_value=1, normal_value=int(value),
                          value_aliases={"0": "open", "1": "closed"})
        else:
            self.rows.add("analog", mrid=m, min_value=None, max_value=None, normal_value=value, positive_flow_in=True)
        self.rows.add("remote_source", mrid=self.id("remote_source", tag), name=tag, measurement_mrid=m,
                      remote_unit_mrid=self.rtu, scan_interval=self.s.seed_scan_s, deadband=None, sensor_accuracy=None)
        self.rows.add("measurement_value_latest", measurement_mrid=m, ts=self.now, value=value,
                      quality=QUALITY_GOOD, source=SOURCE_ESTIMATOR)
        return m

    def tagname(self, name: str, suffix: str) -> str:
        return f"{self.tag}_{name.upper()}_{suffix}"

    # --- pre-scan ------------------------------------------------------------------------------------------------
    def _line_specs(self) -> list[dict[str, Any]]:
        """Every line (disabled included) with its buses, nodes and enabled flag."""
        ckt = self.m.ckt
        L = ckt.Lines
        out = []
        for name in names(L):
            L.Name = name
            e = ckt.ActiveCktElement
            n = L.Phases
            if e.Enabled:
                b1, b2 = (lower_bus(b) for b in e.BusNames)
                n1, n2 = (t[:n] for t in self.m.terminal_nodes())
            else:
                s1, s2 = e.Properties("bus1").Val, e.Properties("bus2").Val
                b1, b2 = lower_bus(s1), lower_bus(s2)
                n1, n2 = nodes_of(s1, n)[:n], nodes_of(s2, n)[:n]
            out.append({"name": name, "b1": b1, "b2": b2, "n1": n1, "n2": n2, "enabled": bool(e.Enabled)})
        return out

    def _transformer_specs(self) -> list[dict[str, Any]]:
        ckt = self.m.ckt
        T = ckt.Transformers
        out = []
        for name in names(T):
            T.Name = name
            e = ckt.ActiveCktElement
            if not e.Enabled:
                self.report.unmatched.append(f"Transformer.{name}: disabled, skipped")
                continue
            nw = T.NumWindings
            if nw not in (2, 3):
                self.report.unmatched.append(f"Transformer.{name}: {nw} windings, skipped")
                continue
            buses = [lower_bus(b) for b in e.BusNames]
            nodes = self.m.terminal_nodes()
            ends = []
            for w in range(1, nw + 1):
                T.Wdg = w
                ends.append({"kv": T.kV, "kva": T.kVA, "r_pct": T.R, "delta": T.IsDelta, "tap": T.Tap})
            out.append({"name": name, "buses": buses, "nodes": nodes, "ends": ends, "phases": e.NumPhases,
                        "xhl": T.Xhl, "xht": T.Xht if nw == 3 else None, "xlt": T.Xlt if nw == 3 else None,
                        "props": {p: float(e.Properties(p).Val) for p in
                                  ("ppm_antifloat", "%noloadloss", "%imag", "normhkva", "emerghkva")}})
        return out

    def _split_buses(self, lines: list[dict], transformers: list[dict]) -> set[str]:
        """Center-tap secondaries (1-phase, 3 windings) and everything reached from them by lines."""
        seeds = {t["buses"][w] for t in transformers if t["phases"] == 1 and len(t["ends"]) == 3 for w in (1, 2)}
        adj: dict[str, set[str]] = {}
        for ln in lines:
            adj.setdefault(ln["b1"], set()).add(ln["b2"])
            adj.setdefault(ln["b2"], set()).add(ln["b1"])
        split, q = set(seeds), deque(seeds)
        while q:
            b = q.popleft()
            for nb in adj.get(b, ()):
                if nb not in split:
                    split.add(nb)
                    q.append(nb)
        return split

    # --- walk ----------------------------------------------------------------------------------------------------
    def build(self) -> Rows:
        ckt = self.m.ckt
        self.buses = self.m.bus_data()
        self.bus_kv = {b: self.nominal_kv(d["kv_base_ln"]) for b, d in self.buses.items()}

        ckt.SetActiveElement("Vsource.source")
        vs = ckt.ActiveCktElement
        src_bus = lower_bus(vs.BusNames[0])
        src_node = f"{src_bus}_source"
        source_attrs = {
            "basekv": float(vs.Properties("basekv").Val), "pu": float(vs.Properties("pu").Val),
            "angle": float(vs.Properties("angle").Val),
            **{k.lower(): float(vs.Properties(k).Val) for k in ("R1", "X1", "R0", "X0")},
        }
        head_p, head_q = (-x for x in ckt.TotalPower)

        lines = self._line_specs()
        transformers = self._transformer_specs()
        self.split = self._split_buses(lines, transformers)

        adj: dict[str, set[str]] = {b: set() for b in self.buses}
        for ln in lines:
            adj.setdefault(ln["b1"], set()).add(ln["b2"])
            adj.setdefault(ln["b2"], set()).add(ln["b1"])
        for t in transformers:
            for a in t["buses"]:
                adj.setdefault(a, set()).update(x for x in t["buses"] if x != a)
        adj[src_node] = {src_bus}
        adj[src_bus].add(src_node)
        raw: dict[str, tuple[float, float] | None] = {}
        for b in adj:
            d = self.buses.get(b)
            raw[b] = (d["x"], d["y"]) if d and d["x"] is not None and (d["x"], d["y"]) != (0.0, 0.0) else None
        self.coords = layout(raw, adj, step=0.0001 if self.s.lonlat else 10.0)
        self.report.defaults["layout_coords"] = sum(1 for v in raw.values() if v is None)

        # buses only reachable through disabled elements take the kV of their neighbour
        for b in adj:
            if b not in self.bus_kv:
                nb = next((x for x in adj[b] if x in self.bus_kv), None)
                self.bus_kv[b] = self.bus_kv[nb] if nb else self.nominal_kv(source_attrs["basekv"] / math.sqrt(3))

        # containers
        sub = self.id("substation", "head")
        name = f"{self.s.label} Substation"
        self.rows.add("substation", mrid=sub, name=name, description=f"{self.s.label} source",
                      gis_oid=f"{self.src}:substation", region_code=self.region,
                      location_mrid=self.point("substation", "head", src_bus, name), valid_from=self.now, valid_to=None)
        vl = self.id("voltage_level", "head")
        self.rows.add("voltage_level", mrid=vl, name=f"{self.s.label} {source_attrs['basekv']:g} kV", substation_mrid=sub,
                      base_voltage_mrid=self.base_voltage(source_attrs["basekv"]))
        self.line_mrid = self.id("line", "feeder")
        self.rows.add("line", mrid=self.line_mrid, name=self.s.label, region_code=self.region)
        self.rtu = self.id("remote_unit", "rtu")
        self.rows.add("remote_unit", mrid=self.rtu, name=f"{self.s.label} RTU", remote_unit_type="RTU",
                      substation_mrid=sub, region_code=self.region)

        for b in [src_node, *adj.keys() - {src_node}]:
            self.cn[b] = self.id("connectivity_node", b)
            self.rows.add("connectivity_node", mrid=self.cn[b], name=f"{src_bus} (source)" if b == src_node else b,
                          connectivity_node_container_mrid=self.line_mrid, region_code=self.region)
        self.bus_kv[src_node] = self.bus_kv[src_bus]

        # feeder-head breaker: Vsource sits on terminal 1's node, the feeder hangs off terminal 2
        head = f"{self.s.label} Head"
        brk = self.equipment("breaker", "head", "Breaker", head, self.bus_kv[src_bus],
                             self.point("breaker", "head", src_bus, head), attrs={"source": source_attrs}, container=vl)
        self.rows.add("switch", mrid=brk, normal_open=False, open=False, open_ts=self.now, rated_current=None)
        self.terminal(brk, 1, src_node, "ABC")
        t2 = self.terminal(brk, 2, src_bus, "ABC")
        v_ll = float(np.mean([d["pu"] for d in self.buses[src_bus]["nodes"].values()])) * self.bus_kv[src_bus]
        self.measurement(brk, t2, f"{self.tag}_HEAD_P", "ThreePhaseActivePower", "W", "k", head_p)
        self.measurement(brk, t2, f"{self.tag}_HEAD_Q", "ThreePhaseReactivePower", "VAr", "k", head_q)
        self.measurement(brk, t2, f"{self.tag}_HEAD_V", "LineToLineVoltage", "V", "k", v_ll)
        self.measurement(brk, t2, f"{self.tag}_HEAD_POS", "SwitchPosition", "none", "none", 1, discrete=True)

        self._lines(lines)
        self._transformers(transformers)
        self._loads()
        self._capacitors()
        self.report.counts.update(self.rows.counts())
        return self.rows

    def _lines(self, specs: list[dict]) -> None:
        ckt = self.m.ckt
        L = ckt.Lines
        for sp in specs:
            name = sp["name"]
            L.Name = name
            e = ckt.ActiveCktElement
            b1, b2, n1, n2 = sp["b1"], sp["b2"], sp["n1"], sp["n2"]
            if sorted(n1) != sorted(n2):
                self.report.unmatched.append(f"Line.{name}: phases {n1} vs {n2}")
            order = np.argsort(n1)
            n = L.Phases
            km = L.Length * UNIT_KM[L.Units]
            per_unit_to_km = 1.0 / UNIT_KM[L.Units]
            r = permuted(L.Rmatrix, n, order, per_unit_to_km)
            x = permuted(L.Xmatrix, n, order, per_unit_to_km)
            c = permuted(L.Cmatrix, n, order, per_unit_to_km)
            z_total = (np.array(r) + 1j * np.array(x)).reshape(n, n) * km
            z1, z0 = seq_from_matrix(z_total)
            phases = self.phases(b1, n1)
            attrs = {"phase_impedance": {"r": r, "x": x, "c": c, "length_km": km}, "dss_name": f"Line.{name}"}
            loc = self.linestring("line", name, b1, b2, name)
            if L.IsSwitch or (not sp["enabled"] and "switch" in name.lower()):
                is_open = (not sp["enabled"]) or e.IsOpen(1, 0) or e.IsOpen(2, 0)
                ce = self.equipment("switch", name, "LoadBreakSwitch", name.upper(), self.bus_kv[b1], loc, attrs)
                self.rows.add("switch", mrid=ce, normal_open=is_open, open=is_open, open_ts=self.now,
                              rated_current=L.NormAmps)
                self.terminal(ce, 1, b1, phases)
                t2 = self.terminal(ce, 2, b2, self.phases(b2, n2))
                self.measurement(ce, t2, self.tagname(name, "POS"), "SwitchPosition", "none", "none",
                                 0 if is_open else 1, phases=phases, discrete=True)
            else:
                if not sp["enabled"]:
                    self.report.defaults["out_of_service_lines"] += 1
                ce = self.equipment("line", name, "ACLineSegment", name.upper(), self.bus_kv[b1], loc, attrs,
                                    in_service=sp["enabled"])
                self.rows.add("ac_line_segment", mrid=ce, length=round(km * 1000, 2), r=z1.real, x=z1.imag,
                              r0=z0.real, x0=z0.imag, rated_current=L.NormAmps, conductor_type=L.LineCode or None)
                self.terminal(ce, 1, b1, phases)
                self.terminal(ce, 2, b2, self.phases(b2, n2))

    def _transformers(self, specs: list[dict]) -> None:
        ckt = self.m.ckt
        T = ckt.Transformers
        regs: dict[str, dict[str, Any]] = {}
        R = ckt.RegControls
        for rc in names(R):
            R.Name = rc
            regs[R.Transformer.lower()] = {
                "name": rc, "winding": R.Winding, "vreg": R.ForwardVreg, "band": R.ForwardBand, "ptratio": R.PTratio,
                "ctprim": R.CTPrimary, "r": R.ForwardR, "x": R.ForwardX, "maxtapchange": R.MaxTapChange,
            }
        for sp in specs:
            name, buses, nodes, ends, nph = sp["name"], sp["buses"], sp["nodes"], sp["ends"], sp["phases"]
            props = sp["props"]
            attrs: dict[str, Any] = {"dss_name": f"Transformer.{name}", "phases": nph,
                                     "ppm_antifloat": props["ppm_antifloat"]}
            for key, prop in (("noloadloss_pct", "%noloadloss"), ("imag_pct", "%imag")):
                if props[prop]:
                    attrs[key] = props[prop]
            attrs["normhkva"], attrs["emerghkva"] = props["normhkva"], props["emerghkva"]
            # Only non-default winding wiring is recorded: a phase conductor on node 0 (center-tap secondaries).
            wiring = [tn if 0 in tn[:nph] else None for tn in nodes]
            if any(wiring):
                attrs["winding_nodes"] = wiring
            if name.lower() in regs:
                attrs["regulator"] = regs[name.lower()]
            if any(abs(en["tap"] - 1.0) > 1e-9 for en in ends) and "regulator" not in attrs:
                attrs["taps"] = [en["tap"] for en in ends]
            is_reg = "regulator" in attrs
            ce = self.equipment("transformer", name, "PowerTransformer", name.upper(), self.bus_kv[buses[0]],
                                self.point("transformer", name, buses[0], name), attrs)
            conn = ["D" if en["delta"] else "Y" for en in ends]
            vg = "Yy0" if is_reg else conn[0] + "".join(c.lower() for c in conn[1:])
            self.rows.add("power_transformer", mrid=ce, vector_group=vg)
            star = star_reactances(sp["xhl"], sp["xht"], sp["xlt"], len(ends))
            mva1 = ends[0]["kva"] / 1000
            for w, en in enumerate(ends, start=1):
                zbase = en["kv"] ** 2 / (en["kva"] / 1000)
                t = self.terminal(ce, w, buses[w - 1], self.phases(buses[w - 1], nodes[w - 1]))
                self.rows.add("power_transformer_end", mrid=self.id("transformer_end", f"{name}:{w}"),
                              power_transformer_mrid=ce, end_number=w, rated_u=en["kv"], rated_s=en["kva"] / 1000,
                              r=en["r_pct"] / 100 * zbase, x=star[w - 1] / 100 * en["kv"] ** 2 / mva1,
                              connection_kind=conn[w - 1], terminal_mrid=t)
            if is_reg:
                T.Name = name
                T.Wdg = attrs["regulator"]["winding"]
                tap_pos = round((T.Tap - 1.0) / 0.00625)
                self.measurement(ce, None, self.tagname(name, "TAP"), "TapPosition", "none", "none",
                                 tap_pos, phases=self.phases(buses[0], nodes[0]), discrete=True)

    def _loads(self) -> None:
        ckt = self.m.ckt
        Ld = ckt.Loads
        for name in names(Ld):
            Ld.Name = name
            e = ckt.ActiveCktElement
            if not e.Enabled:
                self.report.unmatched.append(f"Load.{name}: disabled, skipped")
                continue
            bus = lower_bus(e.BusNames[0])
            nodes = self.m.terminal_nodes()[0]
            phases = self.phases(bus, nodes)
            pw = list(e.Powers)
            p, q = sum(pw[0::2]), sum(pw[1::2])
            attrs = {"dss_name": f"Load.{name}", "kv": Ld.kV, "model": Ld.Model, "vminpu": Ld.Vminpu,
                     "vmaxpu": Ld.Vmaxpu, "dss_phases": e.NumPhases}
            profile = e.Properties("yearly").Val
            if profile:
                attrs["profile"] = profile
            ce = self.equipment("load", name, "EnergyConsumer", name.upper(), self.bus_kv[bus],
                                self.point("load", name, bus, name), attrs)
            self.rows.add("energy_consumer", mrid=ce, customer_count=1, p_fixed=round(Ld.kW, 4),
                          q_fixed=round(Ld.kvar, 4), phase_connection="D" if Ld.IsDelta else "Y")
            t = self.terminal(ce, 1, bus, phases)
            self.measurement(ce, t, self.tagname(name, "P"), "ThreePhaseActivePower", "W", "k", p, phases=phases)
            self.measurement(ce, t, self.tagname(name, "Q"), "ThreePhaseReactivePower", "VAr", "k", q, phases=phases)

    def _capacitors(self) -> None:
        ckt = self.m.ckt
        C = ckt.Capacitors
        for name in names(C):
            C.Name = name
            e = ckt.ActiveCktElement
            if not e.Enabled:
                continue
            bus = lower_bus(e.BusNames[0])
            phases = self.phases(bus, self.m.terminal_nodes()[0])
            attrs = {"dss_name": f"Capacitor.{name}", "kvar": C.kvar, "kv": C.kV, "conn": "D" if C.IsDelta else "Y",
                     "dss_phases": e.NumPhases}
            ce = self.equipment("capacitor", name, "LinearShuntCompensator", name.upper(), self.bus_kv[bus],
                                self.point("capacitor", name, bus, name), attrs)
            self.terminal(ce, 1, bus, phases)
