# SPDX-License-Identifier: Apache-2.0
"""CIM rows -> an OpenDSS circuit: scope, query, seed, emit, report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from ..adapters.base import DataSource, ModelVersion, Principal
from . import emit, queries
from .report import Head, fresh, model_version, seed_loads, stale_switches
from .scope import ScopeError, trace

__all__ = ["CompiledFeeder", "ScopeError", "compile_feeder"]


@dataclass
class CompiledFeeder:
    commands: list[str]
    names: dict[str, tuple[UUID, str]]
    model: ModelVersion
    voltage_bases: list[float]
    head: Head
    root: dict
    load_seeds: list[emit.LoadSeed]
    bus_nominal: dict[str, float]

    def release(self) -> None:
        """Drop what the session no longer needs once the engine holds the circuit (~20 MB on a 10k-node feeder)."""
        self.commands = []
        self.load_seeds = [emit.LoadSeed({"mrid": ls.row["mrid"], "name": ls.row["name"]}, ls.kw, ls.kvar, ls.model, ls.how)
                           for ls in self.load_seeds]


def compile_feeder(ds: DataSource, principal: Principal, root_mrid: UUID, case: str, region_code: str,
                   now: datetime | None = None) -> CompiledFeeder:
    now = now or datetime.now(UTC)
    regions = principal.region_codes
    sc = trace(ds, principal, root_mrid, case, region_code)
    p = {"scope": sc.mrids}
    root = sc.root
    nodes = ds.query(queries.NODES, p, regions)
    segments = ds.query(queries.SEGMENTS, p, regions)
    switches = ds.query(queries.SWITCHES, p, regions)
    transformers = ds.query(queries.TRANSFORMERS, p, regions)
    loads = ds.query(queries.LOADS, p, regions)
    caps = ds.query(queries.CAPACITORS, p, regions)
    ders = ds.query(queries.DER, p, regions)
    head_rows = ds.query(queries.HEAD_MEASUREMENTS, {"root": root["mrid"]}, regions)
    counts = {r["cim_class"]: r["n"] for r in ds.query(queries.SCOPE_COUNTS, p, regions)}

    warnings: list[str] = []
    head = Head.from_rows(head_rows, now) if case == "operational" else Head()
    der_kw = 0.0
    for d in ders:
        if case == "operational" and fresh(d["p_value"], d["p_ts"], d["p_quality"], d["p_scan"], now):
            der_kw += float(d["p_value"])
    seeding = seed_loads(loads, head, der_kw, case, now)
    warnings += seeding.warnings

    src = (root.get("attrs") or {}).get("source", {})
    nominal = float(root["nominal_voltage"] or src.get("basekv", 12.47))
    pu = head.v_kv / nominal if head.v_kv else float(src.get("pu", emit.DEFAULT_SOURCE["pu"]))
    if case == "operational" and head.v_kv is None:
        warnings.append(f"no fresh feeder-head voltage; source at {pu:.4f} pu")

    out = emit.Emitted()
    emit.node_names(nodes, out)
    emit.circuit(root, pu, out)
    for r in segments:
        emit.segment(r, out)
    root_row = None
    for r in switches:
        is_open = bool(r["normal_open"] if case == "planning" else r["open"])
        emit.switch(r, is_open and r["mrid"] != root["mrid"], out)
        if r["mrid"] == root["mrid"]:
            root_row = r
    if root_row is None:
        raise ScopeError(f"root {root['name']} has no switch row")
    for r in transformers:
        tap = None
        if case == "operational" and fresh(r["tap_value"], r["tap_ts"], r["tap_quality"], r["tap_scan"], now):
            tap = round(float(r["tap_value"]))
        emit.transformer(r, out, tap)
    for ls in seeding.loads:
        emit.load(ls, out)
    for r in caps:
        emit.capacitor(r, out)
    for r in ders:
        emit.der(r, out)
    bases = emit.voltage_bases(
        [nominal]
        + [r["nominal_voltage"] for g in (segments, switches, loads, caps, ders) for r in g if r.get("nominal_voltage")]
        + emit.transformer_end_bases(transformers)
    )
    emit.finish(bases, out)

    stale = stale_switches([s for s in switches if s["mrid"] != root["mrid"]], now) if case == "operational" else 0
    if stale:
        warnings.append(f"{stale} switches have stale state (> 2 scan intervals)")
    if out.defaulted_impedances:
        warnings.append(f"{out.defaulted_impedances} segments use conductor-type default impedances")

    model = model_version(
        root=root, case=case, region_code=region_code, principal=principal.name, counts=counts,
        rows=(segments, switches, transformers, loads, caps, ders), seeding=seeding,
        defaulted_impedances=out.defaulted_impedances, stale=stale, warnings=warnings,
    )
    bus_nominal: dict[str, float] = {}
    for r in (*segments, *switches):
        for cn in (r["cn1"], r["cn2"]):
            bus_nominal.setdefault(out.bus_base(cn), float(r["nominal_voltage"] or 0))
    for r in (*loads, *caps, *ders):
        bus_nominal.setdefault(out.bus_base(r["cn"]), float(r["nominal_voltage"] or 0))
    return CompiledFeeder(out.commands, out.names, model, bases, head, root, seeding.loads, bus_nominal)
