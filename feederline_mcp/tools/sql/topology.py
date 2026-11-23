# SPDX-License-Identifier: Apache-2.0
"""Topology tools: what is fed through a device, what feeds it, paths, isolation points, ties, model connectivity."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID

from mcp.server.fastmcp import Context
from pydantic import model_validator

from ...compiler import scope
from ...shapes import Hop, PathData, TableData, TreeData, TreeNode, cap, columns, dump, model_size
from ...studies import Envelope, ToolError
from . import queries
from .common import (
    PROTECTIVE,
    SWITCH_CLASSES,
    Call,
    Case,
    Feeders,
    Output,
    Ref,
    RegionInput,
    SizedInput,
    anchor,
    feeder_anchor,
    feeder_fields,
    rows,
    run,
)

AGGREGATED_CLASSES = (*SWITCH_CLASSES, "PowerTransformer")
MAX_ISOLATION_SWITCHES = 50
EDGE_BYTES = 90  # each kept node also adds one {from, to} entry to edges


# --- the in-memory tree every topology and spatial tool builds on ---------------------------------------------
@dataclass
class Item:
    mrid: UUID
    name: str
    cim_class: str
    parent: UUID | None  # None: hangs from the anchor
    depth: int
    km_from_root: float
    nodes: list[int]
    kind: str  # edge (traversed) | attached (one-terminal) | boundary (switch at the edge of reach) | loop
    open: bool | None = None
    normal_open: bool | None = None
    open_ts: Any = None
    length_m: float = 0.0
    customers: int = 0
    kw: float = 0.0
    children: list[UUID] = field(default_factory=list)
    customers_down: int = 0
    km_down: float = 0.0
    kw_down: float = 0.0


@dataclass
class Tree:
    anchor: scope.Anchor
    case: str
    feeder: dict[str, Any] | None
    items: dict[UUID, Item]
    node_km: dict[int, float]
    node_cn: dict[int, UUID]
    top: list[UUID] = field(default_factory=list)  # items hanging from the anchor
    customers: int = 0
    km: float = 0.0
    kw: float = 0.0
    own: dict[str, Any] = field(default_factory=dict)  # the anchor's own load/length

    def mrids(self) -> list[UUID]:
        return [self.anchor.mrid, *self.items]

    def ancestors(self, mrid: UUID):
        p = self.items[mrid].parent
        while p is not None:
            yield self.items[p]
            p = self.items[p].parent

    def subtree(self, mrid: UUID | None):
        stack = list(self.top if mrid is None else self.items[mrid].children)
        while stack:
            m = stack.pop()
            yield self.items[m]
            stack.extend(self.items[m].children)


def _keep(cim_class: str, hits: list[bool]) -> bool:
    """The compiler's scope rule: every terminal reached, or a switch with any terminal reached."""
    return all(hits) or (any(hits) and cim_class in SWITCH_CLASSES)


def build_tree(c: Call, a: scope.Anchor, case: str, feeder: dict[str, Any] | None = None,
               exclude: list[int] | None = None) -> Tree:
    """Everything fed through anchor `a` under `case` switch states, as a parent-linked tree with subtree totals."""
    t = Tree(a, case, feeder, {}, {}, {})
    if len(a.nodes) < 2:  # one-terminal equipment feeds nothing but itself
        if a.cim_class == "EnergyConsumer":
            ec = rows(c, queries.DESCRIBE_SUBCLASS["EnergyConsumer"], {"mrid": a.mrid})
            if ec:
                t.own = {"customers": int(ec[0]["customer_count"] or 0), "kw": float(ec[0]["p_fixed_kw"] or 0)}
                t.customers, t.kw = t.own["customers"], t.own["kw"]
        return t
    params = {"region": a.region_code, "exclude": exclude if exclude is not None else [a.own_edge],
              "start": a.start_node}
    reach_edge: dict[int, UUID | None] = {a.start_node: None}
    depth_of: dict[UUID | None, int] = {None: 0}
    for r in rows(c, queries.TRACE_DOWNSTREAM[case], params):
        t.node_km[r["node"]] = float(r["agg_cost"]) / 1000
        if r["cn_mrid"] is not None:
            t.node_cn[r["node"]] = r["cn_mrid"]
        if r["edge"] is None or r["edge"] < 0:
            continue
        parent = reach_edge.get(r["pred"])
        m = r["equipment_mrid"]
        it = Item(m, r["name"] or str(m), r["cim_class"], parent, int(r["depth"]), t.node_km[r["node"]],
                  [r["pred"], r["node"]], "edge", r["open"], r["normal_open"], r["open_ts"],
                  float(r["length"] or 0) if r["cim_class"] == "ACLineSegment" else 0.0)
        t.items[m] = it
        reach_edge[r["node"]] = m
        depth_of[m] = it.depth
    t.node_km.setdefault(a.start_node, 0.0)
    extra = rows(c, queries.TRACE_EXTRA, {"region": a.region_code, "nodes": list(t.node_km),
                                          "skip": [a.mrid, *t.items]})
    for r in extra:
        nodes, hits = list(r["nodes"]), list(r["hits"])
        if not _keep(r["cim_class"], hits):
            continue
        hang = min((n for n, h in zip(nodes, hits, strict=True) if h), key=lambda n: t.node_km.get(n, 0.0))
        parent = reach_edge.get(hang)
        is_open = bool(r["normal_open"] if case == "planning" else r["open"]) if r["cim_class"] in SWITCH_CLASSES else False
        kind = "attached" if len(nodes) == 1 else ("boundary" if (not all(hits) or is_open) else "loop")
        t.items[r["mrid"]] = Item(
            r["mrid"], r["name"], r["cim_class"], parent, depth_of.get(parent, 0) + 1, t.node_km.get(hang, 0.0),
            nodes, kind, r["open"], r["normal_open"], r["open_ts"],
            float(r["length"] or 0) if r["cim_class"] == "ACLineSegment" else 0.0,
            int(r["customer_count"] or 0) if r["cim_class"] == "EnergyConsumer" else 0,
            float(r["p_fixed"] or 0) if r["cim_class"] == "EnergyConsumer" else 0.0)
    for m, it in t.items.items():
        (t.items[it.parent].children if it.parent is not None else t.top).append(m)
    for it in sorted(t.items.values(), key=lambda i: -i.depth):
        it.customers_down += it.customers
        it.km_down += it.length_m / 1000
        it.kw_down += it.kw
        if it.parent is not None:
            p = t.items[it.parent]
            p.customers_down += it.customers_down
            p.km_down += it.km_down
            p.kw_down += it.kw_down
    for m in t.top:
        it = t.items[m]
        t.customers += it.customers_down
        t.km += it.km_down
        t.kw += it.kw_down
    return t


def feeder_tree(c: Call, head: dict[str, Any], region_code: str, case: str) -> Tree:
    a = scope.anchor(c.ds, c.principal, head["mrid"], case)
    return build_tree(c, a, case, head)


def tree_resource(mrid: UUID, case: str) -> str:
    return f"grid://feeder/{mrid}/tree?case={quote(case)}"


def _node(t: Tree, it: Item, parent: UUID, depth: int, **more: Any) -> TreeNode:
    extra: dict[str, Any] = dict(more)
    if it.kind != "edge":
        extra["kind"] = it.kind
    if it.cim_class in SWITCH_CLASSES:
        extra["normal_open"] = it.normal_open
    return TreeNode(mrid=it.mrid, name=it.name, cim_class=it.cim_class, depth=depth, parent=parent,
                    open=it.open if it.cim_class in SWITCH_CLASSES else None, km_from_root=round(it.km_from_root, 3),
                    customers_downstream=it.customers_down, km_downstream=round(it.km_down, 3),
                    kw_nameplate_downstream=round(it.kw_down, 2), **extra)


def summary_of(t: Tree) -> dict[str, Any]:
    counts = Counter(it.cim_class for it in t.items.values())
    return {
        "case": t.case, "start": {"mrid": str(t.anchor.mrid), "name": t.anchor.name, "cim_class": t.anchor.cim_class},
        **feeder_fields(t.feeder), "counts": dict(sorted(counts.items())), "equipment": len(t.items),
        "km": round(t.km, 3), "customers": t.customers, "kw_nameplate": round(t.kw, 2),
        "open_boundaries": sum(1 for it in t.items.values() if it.kind == "boundary"),
    }


def tree_data(t: Tree, detail: str, limit: int, resource: str | None) -> TreeData:
    root = {"mrid": str(t.anchor.mrid), "name": t.anchor.name, "cim_class": t.anchor.cim_class,
            **feeder_fields(t.feeder)}
    data = TreeData(root=root, summary=summary_of(t))
    if detail == "summary":
        return data
    nodes: list[TreeNode] = []
    order = sorted(t.items.values(), key=lambda i: (i.km_from_root, i.depth, i.name))
    if detail == "full":
        nodes = [_node(t, it, it.parent or t.anchor.mrid, it.depth) for it in order]
    else:
        keep = {it.mrid for it in order if it.cim_class in AGGREGATED_CLASSES}
        depth: dict[UUID, int] = {}
        for it in sorted((i for i in order if i.mrid in keep), key=lambda i: i.depth):
            parent = next((p.mrid for p in t.ancestors(it.mrid) if p.mrid in keep), None)
            depth[it.mrid] = depth[parent] + 1 if parent is not None else 1
            nodes.append(_node(t, it, parent or t.anchor.mrid, depth[it.mrid]))
        nodes.sort(key=lambda n: (n.km_from_root, n.depth, n.name))
    kept, truncated = cap(nodes, limit, size=lambda n: model_size(n) + EDGE_BYTES)
    data.nodes = kept
    data.edges = [{"from": str(n.parent), "to": str(n.mrid)} for n in kept]
    data.truncated = truncated
    data.resource = resource if truncated else None
    return data


# --- upstream paths ---------------------------------------------------------------------------------------------
@dataclass
class Path:
    edges: list[dict[str, Any]]  # PATH rows in order, edge >= 0
    start: int
    end: int
    cost: float


def shortest(c: Call, region: str, case: str, starts: list[int], ends: list[int], exclude: list[int]) -> Path | None:
    shared = set(starts) & set(ends)
    if shared:
        n = next(iter(shared))
        return Path([], n, n, 0.0)
    best: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for r in rows(c, queries.PATH[case], {"region": region, "exclude": exclude, "starts": starts, "ends": ends}):
        best.setdefault((r["start_vid"], r["end_vid"]), []).append(r)
    if not best:
        return None
    (s, e), path = min(best.items(), key=lambda kv: max(float(r["agg_cost"]) for r in kv[1]))
    return Path([r for r in path if r["edge"] is not None and r["edge"] >= 0], s, e,
                max(float(r["agg_cost"]) for r in path))


def _hop(seq: int, mrid: UUID, name: str, cim_class: str, is_open: bool | None, km: float, **extra: Any) -> Hop:
    return Hop(seq=seq, mrid=mrid, name=name or str(mrid), cim_class=cim_class, protective=cim_class in PROTECTIVE,
               open=is_open if cim_class in SWITCH_CLASSES else None, km=round(km, 3), **extra)


def hops_of(c: Call, start: scope.Anchor, path: Path, end: dict[str, Any] | None) -> tuple[list[Hop], dict[str, float]]:
    """Ordered hops start -> ... -> end with cumulative km; series r/x (ohm) summed over the segments between."""
    hops = [_hop(0, start.mrid, start.name, start.cim_class, None, 0.0)]
    seen = {start.mrid}
    km = r_ohm = x_ohm = 0.0
    for r in path.edges:
        m = r["equipment_mrid"]
        if m in seen:
            continue
        seen.add(m)
        if r["cim_class"] == "ACLineSegment":
            km += float(r["length"] or 0) / 1000
            r_ohm += float(r["r"] or 0)
            x_ohm += float(r["x"] or 0)
        hops.append(_hop(len(hops), m, r["name"], r["cim_class"], r["open"], km))
    if end is not None and end["mrid"] not in seen:
        hops.append(_hop(len(hops), end["mrid"], end["name"], end["cim_class"], end.get("open"), km))
    return hops, {"km": round(km, 3), "r_ohm": round(r_ohm, 5), "x_ohm": round(x_ohm, 5)}


def upstream(c: Call, a: scope.Anchor, case: str, f: Feeders) -> tuple[PathData, dict[str, Any] | None]:
    frm = {"mrid": str(a.mrid), "name": a.name, "cim_class": a.cim_class}
    if a.is_root:
        head = f.by_mrid.get(a.mrid)
        data = PathData(**{"from": frm}, to={"mrid": str(a.mrid), "name": a.name, "cim_class": a.cim_class},
                        hops=[_hop(0, a.mrid, a.name, a.cim_class, None, 0.0)],
                        summary={"connected": True, "case": case, "km": 0.0, "r_ohm": 0.0, "x_ohm": 0.0,
                                 **feeder_fields(head), "protective": [a.name]})
        return data, head
    if not f.heads:
        raise ToolError("not_found", f"region {a.region_code} has no feeder heads")
    path = shortest(c, a.region_code, case, a.nodes[:2], list(f.by_node), f.own_edges)
    if path is None:
        data = PathData(**{"from": frm}, to=None, hops=[_hop(0, a.mrid, a.name, a.cim_class, None, 0.0)],
                        summary={"connected": False, "case": case, "reason": f"no path to a feeder head under {case} "
                                 "switch states", **feeder_fields(None)})
        return data, None
    head = f.by_node[path.end]
    end = {"mrid": head["mrid"], "name": head["name"], "cim_class": "Breaker", "open": False}
    hops, sums = hops_of(c, a, path, end)
    to = {"mrid": str(head["mrid"]), "name": head["name"], "cim_class": "Breaker"}
    summary = {"connected": True, "case": case, **sums, **feeder_fields(head), "substation_name": head["substation_name"],
               "protective": [h.name for h in hops[1:] if h.protective],
               "note": "r_ohm/x_ohm are a series sum of segment impedances, not a fault level"}
    return PathData(**{"from": frm}, to=to, hops=hops, summary=summary), head


# --- isolation --------------------------------------------------------------------------------------------------
@dataclass
class Isolation:
    upstream: Item | None  # None: the feeder head itself
    section: list[Item]
    downstream: list[Item]  # switches bounding the section below
    customers_isolated: int
    customers_in_section: int
    km_section: float


def isolation(t: Tree, device: UUID) -> Isolation:
    """Opening the first switch upstream of `device` and every switch bounding its section below it."""
    up: Item | None = None
    if device != t.anchor.mrid:
        up = next((p for p in t.ancestors(device) if p.cim_class in SWITCH_CLASSES), None)
    section: list[Item] = []
    down: list[Item] = []
    stack = list(t.top if up is None else up.children)
    while stack:
        it = t.items[stack.pop()]
        if it.cim_class in SWITCH_CLASSES and it.mrid != device:
            down.append(it)
            continue
        section.append(it)
        stack.extend(it.children)
    isolated = t.customers if up is None else up.customers_down
    return Isolation(up, section, sorted(down, key=lambda i: i.km_from_root), isolated,
                     sum(i.customers for i in section), sum(i.length_m for i in section) / 1000)


def device_tree(c: Call, ref: str, region: str, case: str, f: Feeders) -> tuple[scope.Anchor, Tree]:
    """The feeder tree holding a device (the device itself when it heads a feeder)."""
    a = anchor(c, ref, region, case)
    if a.is_root:
        head = f.by_mrid.get(a.mrid)
    else:
        found = f.of_nodes(a.nodes, case)
        head = next((found[n] for n in a.nodes if found.get(n)), None)
    if head is None:
        raise ToolError("not_found", f"{a.name} is not energised from any feeder head under {case} switch states")
    t = feeder_tree(c, head, region, case)
    if a.mrid != head["mrid"] and a.mrid not in t.items:
        raise ToolError("not_found", f"{a.name} is not in the {case} trace of {head['name']}")
    return a, t


# --- tools ------------------------------------------------------------------------------------------------------
class TraceDownstreamInput(SizedInput):
    mrid: Ref
    case: Case = "operational"


def trace_downstream(c: Call, args: TraceDownstreamInput) -> Output:
    a = anchor(c, args.mrid, args.region_code, args.case)
    f = Feeders(c, args.region_code)
    if a.is_root:
        head = f.by_mrid.get(a.mrid)
    else:
        found = f.of_nodes(a.nodes, args.case)
        head = next((found[n] for n in a.nodes if found.get(n)), None)
    t = build_tree(c, a, args.case, head, None if a.is_root else [a.own_edge, *f.own_edges])
    data = tree_data(t, args.detail, args.limit, tree_resource(a.mrid, args.case))
    warnings = [] if head else [f"{a.name} is not energised from a feeder head under {args.case} switch states"]
    return Output(dump(data), warnings)


class TraceUpstreamInput(RegionInput):
    mrid: Ref
    case: Case = "operational"


def trace_upstream(c: Call, args: TraceUpstreamInput) -> Output:
    a = anchor(c, args.mrid, args.region_code, args.case)
    data, _ = upstream(c, a, args.case, Feeders(c, args.region_code))
    return Output(dump(data))


class FindPathInput(RegionInput):
    from_mrid: Ref
    to_mrid: Ref
    case: Case = "operational"


def find_path(c: Call, args: FindPathInput) -> Output:
    a = anchor(c, args.from_mrid, args.region_code, args.case)
    b = anchor(c, args.to_mrid, args.region_code, args.case)
    frm = {"mrid": str(a.mrid), "name": a.name, "cim_class": a.cim_class}
    to = {"mrid": str(b.mrid), "name": b.name, "cim_class": b.cim_class}
    end = {"mrid": b.mrid, "name": b.name, "cim_class": b.cim_class}
    path = shortest(c, args.region_code, args.case, a.nodes, b.nodes, [])
    if path is not None:
        hops, sums = hops_of(c, a, path, end)
        summary = {"connected": True, "case": args.case, **sums, "blocked_by": None,
                   "protective": [h.name for h in hops if h.protective]}
        return Output(dump(PathData(**{"from": frm}, to=to, hops=hops, summary=summary)))
    wired = shortest(c, args.region_code, "all", a.nodes, b.nodes, [])
    if wired is None:
        summary = {"connected": False, "case": args.case, "blocked_by": None, "reason": "no wired path in the model"}
        return Output(dump(PathData(**{"from": frm}, to=to, summary=summary)))
    hops, sums = hops_of(c, a, wired, end)
    key = "normal_open" if args.case == "planning" else "open"
    by_mrid = {r["equipment_mrid"]: r for r in wired.edges}
    blocked = next((h for h in hops if h.cim_class in SWITCH_CLASSES and by_mrid.get(h.mrid, {}).get(key)), None)
    summary = {"connected": False, "case": args.case, **sums,
               "blocked_by": {"mrid": str(blocked.mrid), "name": blocked.name, "cim_class": blocked.cim_class}
               if blocked else None,
               "reason": f"the wired path crosses a switch that is open under {args.case} states"}
    return Output(dump(PathData(**{"from": frm}, to=to, hops=hops, summary=summary)))


class IsolateInput(RegionInput):
    mrid: Ref
    case: Case = "operational"


def isolate(c: Call, args: IsolateInput) -> Output:
    f = Feeders(c, args.region_code)
    a, t = device_tree(c, args.mrid, args.region_code, args.case, f)
    iso = isolation(t, a.mrid)
    up_mrid = iso.upstream.mrid if iso.upstream else t.anchor.mrid
    up_name = iso.upstream.name if iso.upstream else t.anchor.name
    up_class = iso.upstream.cim_class if iso.upstream else t.anchor.cim_class
    up_node = TreeNode(mrid=up_mrid, name=up_name, cim_class=up_class, depth=0, parent=None,
                       open=iso.upstream.open if iso.upstream else False,
                       km_from_root=round(iso.upstream.km_from_root if iso.upstream else 0.0, 3),
                       customers_downstream=iso.customers_isolated,
                       km_downstream=round(iso.upstream.km_down if iso.upstream else t.km, 3),
                       kw_nameplate_downstream=round(iso.upstream.kw_down if iso.upstream else t.kw, 2),
                       role="upstream")
    nodes = [up_node] + [_node(t, s, up_mrid, 1, role="downstream") for s in iso.downstream[:MAX_ISOLATION_SWITCHES]]
    restorable = sum(s.customers_down for s in iso.downstream)
    summary = {
        "case": args.case, "device": {"mrid": str(a.mrid), "name": a.name, "cim_class": a.cim_class},
        **feeder_fields(t.feeder), "switches_to_open": [n.name for n in nodes],
        "customers_isolated": iso.customers_isolated, "customers_in_section": iso.customers_in_section,
        "customers_restorable_downstream": restorable, "km_section": round(iso.km_section, 3),
        "section_equipment": len(iso.section),
    }
    warnings = []
    if iso.upstream is None:
        warnings.append(f"no switch between {a.name} and the feeder head; isolation opens the head breaker")
    if len(iso.downstream) > MAX_ISOLATION_SWITCHES:
        warnings.append(f"{len(iso.downstream) - MAX_ISOLATION_SWITCHES} more downstream switches not listed")
    root = {"mrid": str(up_mrid), "name": up_name, "cim_class": up_class, **feeder_fields(t.feeder)}
    data = TreeData(root=root, nodes=nodes, edges=[{"from": str(up_mrid), "to": str(n.mrid)} for n in nodes[1:]],
                    summary=summary)
    return Output(dump(data), warnings)


class TieOptionsInput(RegionInput):
    feeder_mrid: Ref | None = None
    section_root_mrid: Ref | None = None

    @model_validator(mode="after")
    def one_of(self):
        if not (self.feeder_mrid or self.section_root_mrid):
            raise ValueError("pass feeder_mrid or section_root_mrid")
        return self


def tie_options(c: Call, args: TieOptionsInput) -> Output:
    f = Feeders(c, args.region_code)
    if args.section_root_mrid:
        sec, t = device_tree(c, args.section_root_mrid, args.region_code, "planning", f)
        within = None if sec.mrid == t.anchor.mrid else sec.mrid
        root = {"mrid": str(sec.mrid), "name": sec.name, "cim_class": sec.cim_class, **feeder_fields(t.feeder)}
        kw = round(t.kw if within is None else t.items[within].kw_down, 2)
    else:
        a = feeder_anchor(c, args.feeder_mrid, args.region_code)
        t = feeder_tree(c, f.by_mrid[a.mrid], args.region_code, "planning")
        within = None
        root = {"mrid": str(a.mrid), "name": a.name, "cim_class": a.cim_class, **feeder_fields(t.feeder)}
        kw = None
    candidates = [it for it in t.subtree(within) if it.kind == "boundary" and it.normal_open]
    far = {it.mrid: [n for n in it.nodes if n not in t.node_km] for it in candidates}
    other = f.of_nodes([n for ns in far.values() for n in ns], "planning")
    nodes: list[TreeNode] = []
    for it in candidates:
        head = next((other[n] for n in far[it.mrid] if other.get(n)), None)
        if head is None or (t.feeder and head["mrid"] == t.feeder["mrid"]):
            continue
        nodes.append(_node(t, it, UUID(root["mrid"]), 1, open_ts=it.open_ts.isoformat() if it.open_ts else None,
                           other_side={**feeder_fields(head), "substation_name": head["substation_name"]},
                           kw_nameplate_transferred=kw))
    summary = {"ties": len(nodes), "section_kw_nameplate": kw, **feeder_fields(t.feeder),
               "normally_open_boundaries": len(candidates)}
    data = TreeData(root=root, nodes=nodes, edges=[{"from": root["mrid"], "to": str(n.mrid)} for n in nodes],
                    summary=summary)
    return Output(dump(data))


class ConnectivityReportInput(RegionInput):
    feeder_mrid: Ref


def _finding(kind: str, severity: str, where: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": kind, "severity": severity, "scope": where, "count": len(items),
            "mrids": [str(i["mrid"]) for i in items[:50]], "names": [i["name"] for i in items[:50]]}


def connectivity_report(c: Call, args: ConnectivityReportInput) -> Output:
    a = feeder_anchor(c, args.feeder_mrid, args.region_code)
    f = Feeders(c, args.region_code)
    planned = feeder_tree(c, f.by_mrid[a.mrid], args.region_code, "planning")
    live = feeder_tree(c, f.by_mrid[a.mrid], args.region_code, "operational")
    scope_ids = planned.mrids()
    comps: dict[int, set[int]] = {}
    for r in rows(c, queries.COMPONENTS["all"], {"region": args.region_code, "exclude": []}):
        comps.setdefault(r["component"], set()).add(r["node"])
    head_nodes = set(f.by_node)
    island_nodes = sorted({n for nodes in comps.values() if not nodes & head_nodes for n in nodes})
    islands = rows(c, queries.COMPONENT_EQUIPMENT, {"region": args.region_code, "nodes": island_nodes}) \
        if island_nodes else []
    dead = [{"mrid": it.mrid, "name": it.name} for m, it in planned.items.items()
            if m not in live.items and it.kind != "boundary"]
    findings = [
        _finding("island", "error", "region", islands),
        _finding("orphan_terminal", "error", "region",
                 rows(c, queries.ORPHAN_TERMINALS, {"region": args.region_code})),
        _finding("dangling_node", "info", "feeder", rows(c, queries.DANGLING_NODES, {"scope": scope_ids})),
        _finding("switch_without_state_time", "warning", "feeder",
                 rows(c, queries.SWITCHES_NO_TS, {"scope": scope_ids})),
        _finding("segment_without_length", "warning", "feeder",
                 rows(c, queries.SEGMENTS_NO_LENGTH, {"scope": scope_ids})),
        _finding("deenergised_by_open_switch", "warning", "feeder", dead),
    ]
    data = TableData(
        columns=columns(("kind", "Finding"), ("severity", "Severity"), ("scope", "Scope"), ("count", "Count"),
                        ("names", "Examples")),
        rows=findings,
        summary={**feeder_fields(f.by_mrid[a.mrid]), "islands": len([1 for n in comps.values() if not n & head_nodes]),
                 "equipment_in_feeder": len(planned.items), "findings": sum(1 for r in findings if r["count"])})
    return Output(dump(data))


def register(mcp, services) -> None:
    @mcp.tool(name="trace_downstream", structured_output=True)
    async def trace_downstream_tool(ctx: Context, mrid: str, region_code: str, case: Case = "operational",
                               detail: Literal["summary", "aggregated", "full"] = "aggregated",
                               limit: int = 500) -> Envelope:
        """Everything fed through this device (any equipment, mRID or name) under the current (operational) or normal
        (planning) switch states: equipment counts, km of line, customers and nameplate kW downstream. Returns a
        tree (data.viz = "tree"); detail=summary returns totals only, aggregated returns switches and transformers
        with the totals under each, full returns every device."""
        raw = {"mrid": mrid, "region_code": region_code, "case": case, "detail": detail, "limit": limit}
        return await run(services, ctx, "trace_downstream", TraceDownstreamInput, raw, trace_downstream)

    @mcp.tool(name="trace_upstream", structured_output=True)
    async def trace_upstream_tool(ctx: Context, mrid: str, region_code: str, case: Case = "operational") -> Envelope:
        """The path from a device back to its feeder head under current or normal switch states: each hop in order
        with cumulative km and protective devices (fuse, recloser, breaker) flagged, plus the series sum of segment
        resistance and reactance in ohm (an approximation, not a fault level). Returns a path (data.viz = "path")."""
        raw = {"mrid": mrid, "region_code": region_code, "case": case}
        return await run(services, ctx, "trace_upstream", TraceUpstreamInput, raw, trace_upstream)

    @mcp.tool(name="find_path", structured_output=True)
    async def find_path_tool(ctx: Context, from_mrid: str, to_mrid: str, region_code: str,
                        case: Case = "operational") -> Envelope:
        """Whether two devices are connected under current or normal switch states, and the equipment between them
        with cumulative km. When they are not, reports the first open switch on the wired path as blocked_by.
        Returns a path (data.viz = "path")."""
        raw = {"from_mrid": from_mrid, "to_mrid": to_mrid, "region_code": region_code, "case": case}
        return await run(services, ctx, "find_path", FindPathInput, raw, find_path)

    @mcp.tool(name="isolate", structured_output=True)
    async def isolate_tool(ctx: Context, mrid: str, region_code: str, case: Case = "operational") -> Envelope:
        """The switches to open to isolate a device: the first switch upstream and every switch bounding its section
        below, with customers isolated, customers inside the section and customers restorable from below. Returns a
        tree (data.viz = "tree") rooted at the upstream switch."""
        raw = {"mrid": mrid, "region_code": region_code, "case": case}
        return await run(services, ctx, "isolate", IsolateInput, raw, isolate)

    @mcp.tool(name="tie_options", structured_output=True)
    async def tie_options_tool(ctx: Context, region_code: str, feeder_mrid: str | None = None,
                          section_root_mrid: str | None = None) -> Envelope:
        """Normally-open tie switches from a feeder (or from the section below a device) to other feeders, with the
        other side's feeder and substation and, for a section, the nameplate kW it would transfer. Returns a tree
        (data.viz = "tree")."""
        raw = {"region_code": region_code, "feeder_mrid": feeder_mrid, "section_root_mrid": section_root_mrid}
        return await run(services, ctx, "tie_options", TieOptionsInput, raw, tie_options)

    @mcp.tool(name="connectivity_report", structured_output=True)
    async def connectivity_report_tool(ctx: Context, feeder_mrid: str, region_code: str) -> Envelope:
        """Model connectivity defects for a feeder and its region: islands not connected to any feeder head, orphan
        terminals, dangling nodes, switches without a state timestamp, segments without length, and equipment
        de-energised by currently open switches. Returns a table (data.viz = "table") of findings with counts and
        example devices."""
        raw = {"feeder_mrid": feeder_mrid, "region_code": region_code}
        return await run(services, ctx, "connectivity_report", ConnectivityReportInput, raw,
                         connectivity_report)
