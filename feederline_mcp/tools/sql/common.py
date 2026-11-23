# SPDX-License-Identifier: Apache-2.0
"""Shared inputs, gates and feeder attribution for the SQL tool family (no session, no engine)."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ...adapters.base import Principal
from ...compiler import scope
from ...shapes import Detail
from ...studies import ToolError
from .._runner import Call, Output, Services, invoke
from . import queries
from .resolve import resolve_mrid

REGION = r"^[A-Za-z0-9_\-]{1,64}$"
Ref = Annotated[str, Field(min_length=1, max_length=200)]
Case = Literal["operational", "planning"]
SWITCH_CLASSES = ("Breaker", "Recloser", "LoadBreakSwitch", "Disconnector", "Fuse")
PROTECTIVE = ("Fuse", "Recloser", "Breaker")


class RegionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region_code: str = Field(pattern=REGION)
    request_id: str | None = Field(default=None, max_length=128)  # accepted for symmetry; SQL tools keep no cache


class SizedInput(RegionInput):
    detail: Detail = "aggregated"
    limit: int = Field(default=500, ge=1, le=5000)


def rows(c: Call, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    return c.ds.query(sql, params, c.principal.region_codes)


def gate(principal: Principal, region_code: str) -> None:
    if not principal.can_see(region_code):
        raise ToolError("forbidden", f"region {region_code} is not granted to {principal.name}")


def resolve(c: Call, ref: str | UUID, region_code: str, cim_class: str | None = None) -> UUID:
    """A reference in a granted region -> mRID. forbidden when it exists only in an ungranted region."""
    gate(c.principal, region_code)
    mrid = resolve_mrid(c.ds, c.principal, ref, cim_class, region_code)
    seen = rows(c, queries.VISIBLE_REGION, {"mrid": mrid})
    if not seen:
        hidden = rows(c, queries.EDGE_REGION, {"mrid": mrid})
        if hidden and not c.principal.can_see(hidden[0]["region_code"]):
            raise ToolError("forbidden", f"{mrid} is in a region not granted to {c.principal.name}")
        raise ToolError("not_found", f"equipment {ref} not found in {region_code}")
    if seen[0]["region_code"] != region_code:
        raise ToolError("invalid_input", f"{ref} is in region {seen[0]['region_code']}, not {region_code}")
    return mrid


def anchor(c: Call, ref: str | UUID, region_code: str, case: str = "operational") -> scope.Anchor:
    """Resolve a reference and orient it as a trace anchor; forbidden when it lives in an ungranted region."""
    mrid = resolve(c, ref, region_code)
    try:
        a = scope.anchor(c.ds, c.principal, mrid, case)
    except scope.ScopeError:
        hidden = rows(c, queries.EDGE_REGION, {"mrid": mrid})
        if hidden and not c.principal.can_see(hidden[0]["region_code"]):
            raise ToolError("forbidden", f"{mrid} is in a region not granted to {c.principal.name}") from None
        raise ToolError("not_found", f"equipment {ref} not found in {region_code}") from None
    if a.region_code != region_code:
        if not c.principal.can_see(a.region_code):
            raise ToolError("forbidden", f"{a.name} is in a region not granted to {c.principal.name}")
        raise ToolError("invalid_input", f"{a.name} is in region {a.region_code}, not {region_code}")
    return a


def feeder_anchor(c: Call, ref: str | UUID, region_code: str) -> scope.Anchor:
    a = anchor(c, ref, region_code)
    if not a.is_root:
        raise ToolError("not_found", f"{a.name} is not a feeder head (a head breaker or the voltage level holding one)")
    return a


class Feeders:
    """Feeder heads of one region and node -> feeder attribution (nearest head through closed switches)."""

    def __init__(self, c: Call, region_code: str):
        self.c = c
        self.region = region_code
        self.heads = scope.roots(c.ds, c.principal, region_code)
        self.by_mrid = {h["mrid"]: h for h in self.heads}
        self.by_node = {h["load_node"]: h for h in self.heads}
        self._memo: dict[tuple[str, int], dict[str, Any] | None] = {}

    @property
    def own_edges(self) -> list[int]:
        return [h["own_edge"] for h in self.heads]

    def of_nodes(self, nodes: Iterable[int], case: str = "operational") -> dict[int, dict[str, Any] | None]:
        nodes = [n for n in set(nodes) if n is not None]
        todo = [n for n in nodes if (case, n) not in self._memo]
        if todo and self.heads:
            found = {r["node"]: self.by_node.get(r["start_vid"]) for r in rows(self.c, queries.MEMBERSHIP[case], {
                "region": self.region, "exclude": self.own_edges, "roots": list(self.by_node), "nodes": todo})}
            for n in todo:
                self._memo[(case, n)] = found.get(n)
        return {n: self._memo.get((case, n)) for n in nodes}

    def of_equipment(self, nodes_by_mrid: Mapping[Any, Iterable[int]], case: str = "operational") -> dict[Any, dict | None]:
        flat = self.of_nodes([n for ns in nodes_by_mrid.values() for n in ns], case)
        out: dict[Any, dict | None] = {}
        for m, ns in nodes_by_mrid.items():
            if m in self.by_mrid:
                out[m] = self.by_mrid[m]
                continue
            out[m] = next((flat[n] for n in ns if flat.get(n)), None)
        return out


def feeder_fields(head: Mapping[str, Any] | None) -> dict[str, Any]:
    return {"feeder_mrid": str(head["mrid"]) if head else None, "feeder_name": head["name"] if head else None}


def unit(symbol: str | None, multiplier: str | None) -> str:
    sym = "" if symbol in (None, "none") else symbol
    mul = "" if multiplier in (None, "none") else multiplier
    return f"{mul}{sym}"


def num(v: Any, digits: int = 3) -> float | None:
    return None if v is None else round(float(v), digits)


def keyword_params(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Let a tool take JSON argument names that are Python keywords (from): declare `from_: Annotated[str,
    Field(alias="from")]`; FastMCP validates by alias and calls back with `from=`, which this renames."""
    sig = inspect.signature(fn, eval_str=True)

    @functools.wraps(fn)
    async def wrapper(**kwargs: Any) -> Any:
        for k in [k for k in kwargs if k + "_" in sig.parameters and k not in sig.parameters]:
            kwargs[k + "_"] = kwargs.pop(k)
        return await fn(**kwargs)

    del wrapper.__wrapped__
    wrapper.__signature__ = sig  # type: ignore[attr-defined]
    wrapper.__annotations__ = {**{k: v.annotation for k, v in sig.parameters.items()}, "return": sig.return_annotation}
    return wrapper


async def run(services: Services, ctx: Any, tool: str, input_cls: type[BaseModel], raw: dict[str, Any],
              body: Any) -> dict[str, Any]:
    """A SQL tool call: no session lock, no engine context; the envelope's model is null."""
    return await invoke(services, ctx, tool, input_cls, {k: v for k, v in raw.items() if v is not None}, body,
                        needs_model=False, needs_session=False)


__all__ = ["Call", "Output", "Feeders", "RegionInput", "SizedInput", "rows", "gate", "resolve", "anchor",
           "feeder_anchor", "feeder_fields", "unit", "num", "run", "keyword_params", "Ref", "Case", "PROTECTIVE", "SWITCH_CLASSES"]
