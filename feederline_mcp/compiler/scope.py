# SPDX-License-Identifier: Apache-2.0
"""pgRouting feeder scope from a root (head breaker, or the voltage level containing one).

The edge set and its switch predicate live here once: the compiler's scope and the SQL tool family's traces both
render their pgRouting edge SQL through edges_sql()/edges_expr(), so a trace and a compile cannot disagree about
which switches are closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ..adapters.base import DataSource, Principal
from . import queries


class ScopeError(Exception):
    """Root missing, not visible to the principal, or not a feeder head."""


# operational: live switch.open; planning: normal_open. The two extra cases serve the topology tools:
# all: ignore switch state (is there any wired path at all?); section: never cross a switch (isolation sections).
SWITCH_PREDICATE = {
    "operational": "NOT COALESCE(s.open, false)",
    "planning": "NOT COALESCE(s.normal_open, false)",
    "all": "true",
    "section": "s.mrid IS NULL",
}
CASES = tuple(SWITCH_PREDICATE)

EDGES_TEMPLATE = (
    "SELECT e.id, e.source, e.target, e.cost, e.reverse_cost FROM network_edges e "
    "LEFT JOIN switch s ON s.mrid = e.equipment_mrid "
    "WHERE e.region_code = %%L AND e.grid_tier = 'Distribution' AND NOT (e.id = ANY(%%L::bigint[])) "
    "AND {switch_predicate}"
)


def edges_sql(case: str) -> str:
    """The pgRouting edge SQL over network_edges for one region, Distribution tier, through closed switches.

    operational: NOT COALESCE(s.open, false); planning: NOT COALESCE(s.normal_open, false). The two %%L slots are
    bound server-side by edges_expr(): the region_code and the array of edge ids never to traverse (a trace
    anchor's own edge, the feeder heads' edges)."""
    if case not in SWITCH_PREDICATE:
        raise ScopeError(f"unknown case {case!r}")
    return EDGES_TEMPLATE.format(switch_predicate=SWITCH_PREDICATE[case])


def edges_expr(case: str) -> str:
    """edges_sql(case) as a SQL text expression with bound params %(region)s and %(exclude)s (bigint[])."""
    return ("format($edges$" + edges_sql(case) + "$edges$, %(region)s::text, "
            "COALESCE(%(exclude)s::bigint[], '{}'::bigint[]))")


def render(template: str) -> dict[str, str]:
    """One query constant per case: template with {edges} replaced by that case's edge expression."""
    return {case: template.replace("{edges}", edges_expr(case)) for case in CASES}


SCOPE = render(queries.SCOPE_TEMPLATE)
ORIENT = render(queries.ORIENT_TEMPLATE)


@dataclass
class Scope:
    root: dict[str, Any]
    mrids: list[UUID]
    case: str


@dataclass
class Anchor:
    """Any equipment as the start of a trace."""

    mrid: UUID
    name: str
    cim_class: str
    region_code: str
    own_edge: int | None  # its network_edges id (two-terminal equipment); never traversed from it
    nodes: list[int]  # terminal routing ids in sequence order
    cns: list[UUID]  # terminal connectivity nodes in sequence order
    is_root: bool
    start_node: int  # load-side node: where a downstream trace starts


def resolve_root(ds: DataSource, principal: Principal, root_mrid: UUID) -> dict[str, Any]:
    rows = ds.query(queries.ROOT, {"root": root_mrid}, principal.region_codes)
    if not rows:
        head = ds.query(queries.VOLTAGE_LEVEL_HEAD, {"root": root_mrid}, principal.region_codes)
        if head:
            rows = ds.query(queries.ROOT, {"root": head[0]["mrid"]}, principal.region_codes)
    if not rows:
        raise ScopeError(f"root {root_mrid} not found (or not visible to this principal)")
    return rows[0]


def roots(ds: DataSource, principal: Principal, region_code: str) -> list[dict[str, Any]]:
    """Feeder heads of a region with their load-side node and own edge."""
    return ds.query(queries.ROOTS, {"region": region_code}, principal.region_codes)


def anchor(ds: DataSource, principal: Principal, mrid: UUID, case: str = "operational") -> Anchor:
    """Equipment (or a voltage level, meaning its head breaker) as a trace anchor, oriented load-side.

    Breaker heading a feeder -> terminal 2's node. Other two-terminal equipment -> whichever terminal node is
    farther from every feeder head under `case` (terminal 2 when neither reaches a head). One-terminal equipment ->
    its node (a downstream trace from it holds only itself)."""
    rows = ds.query(queries.ANCHOR, {"mrid": mrid}, principal.region_codes)
    if not rows:
        head = ds.query(queries.VOLTAGE_LEVEL_HEAD, {"root": mrid}, principal.region_codes)
        if head:
            rows = ds.query(queries.ANCHOR, {"mrid": head[0]["mrid"]}, principal.region_codes)
    if not rows:
        raise ScopeError(f"equipment {mrid} not found (or not visible to this principal)")
    r = rows[0]
    nodes, cns = list(r["nodes"]), list(r["cns"])
    own_edge = r["own_edge"] if len(nodes) >= 2 else None
    if r["is_root"] or len(nodes) == 1:
        start = nodes[1] if r["is_root"] else nodes[0]
    else:
        heads = roots(ds, principal, r["region_code"])
        head_nodes = [h["load_node"] for h in heads]
        dist: dict[int, float] = {n: 0.0 for n in nodes[:2] if n in head_nodes}
        if head_nodes:
            for d in ds.query(ORIENT[case], {"region": r["region_code"], "exclude": [h["own_edge"] for h in heads],
                                             "starts": nodes[:2], "roots": head_nodes}, principal.region_codes):
                dist[d["start_vid"]] = float(d["dist"])
        a, b = nodes[0], nodes[1]
        if a in dist and b in dist:
            start = a if dist[a] > dist[b] else b
        elif a in dist:
            start = b
        elif b in dist:
            start = a
        else:
            start = b
    return Anchor(r["mrid"], r["name"], r["cim_class"], r["region_code"], own_edge, nodes, cns, bool(r["is_root"]),
                  start)


def root_node(ds: DataSource, principal: Principal, root_mrid: UUID, case: str = "operational") -> tuple[int, int | None]:
    """(routing_id of the anchor's load-side connectivity node, routing_id of its own edge or None).
    Breaker -> terminal.sequence_number = 2's node; voltage_level -> its head breaker's; else ScopeError."""
    a = anchor(ds, principal, root_mrid, case)
    return a.start_node, a.own_edge


def trace(ds: DataSource, principal: Principal, root_mrid: UUID, case: str, region_code: str) -> Scope:
    if case not in ("operational", "planning"):
        raise ScopeError(f"unknown case {case!r}")
    if not principal.can_see(region_code):
        raise ScopeError(f"region {region_code} is not granted to {principal.name}")
    root = resolve_root(ds, principal, root_mrid)
    if root["region_code"] != region_code:
        raise ScopeError(f"root {root['name']} is in region {root['region_code']}, not {region_code}")
    rows = ds.query(
        SCOPE[case],
        {"region": region_code, "exclude": [root["root_edge"]], "start": root["load_routing_id"], "root": root["mrid"]},
        principal.region_codes,
    )
    return Scope(root=root, mrids=[r["mrid"] for r in rows], case=case)
