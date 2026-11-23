# SPDX-License-Identifier: Apache-2.0
"""Visualization-ready result shapes. A tool returns exactly one as envelope.data; data.viz names it.

Every shape carries `truncated` and `resource`: a result cut by `limit` or by the serialized-size budget sets
truncated=True and points `resource` at the grid:// URI holding the rest (None when no resource exists).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# A context window, not a browser: past this many serialized bytes of rows a result is truncated.
BUDGET_BYTES = 160_000

Viz = Literal["map", "tree", "path", "timeseries", "table", "profile"]
Detail = Literal["summary", "aggregated", "full"]


class _Shape(BaseModel):
    truncated: bool = False
    resource: str | None = None


# --- map ------------------------------------------------------------------------------------------------------
class Feature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: dict[str, Any]  # GeoJSON geometry, EPSG:4326
    properties: dict[str, Any]  # always mrid, name, cim_class; feeder_mrid/feeder_name when known


class MapData(_Shape):
    viz: Literal["map"] = "map"
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[Feature]
    bbox: list[float] | None = None
    summary: dict[str, Any] = Field(default_factory=dict)


# --- tree -----------------------------------------------------------------------------------------------------
class TreeNode(BaseModel):
    model_config = ConfigDict(extra="allow")  # role, loop, phases ... per tool
    mrid: UUID
    name: str
    cim_class: str
    depth: int
    parent: UUID | None
    open: bool | None = None  # switches only
    km_from_root: float
    customers_downstream: int = 0
    km_downstream: float = 0.0
    kw_nameplate_downstream: float = 0.0


class TreeData(_Shape):
    viz: Literal["tree"] = "tree"
    root: dict[str, Any]
    nodes: list[TreeNode] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)  # {"from": parent mrid, "to": child mrid}
    summary: dict[str, Any] = Field(default_factory=dict)


# --- path -----------------------------------------------------------------------------------------------------
class Hop(BaseModel):
    model_config = ConfigDict(extra="allow")
    seq: int
    mrid: UUID
    name: str
    cim_class: str
    protective: bool
    open: bool | None = None
    km: float  # cumulative from the start


class PathData(_Shape):
    viz: Literal["path"] = "path"
    from_: dict[str, Any] = Field(alias="from")
    to: dict[str, Any] | None
    hops: list[Hop] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


# --- timeseries -----------------------------------------------------------------------------------------------
class Series(BaseModel):
    name: str
    mrid: UUID | None = None
    unit: str
    values: list[float | None]


class TimeseriesData(_Shape):
    viz: Literal["timeseries"] = "timeseries"
    interval: str
    ts: list[str]
    series: list[Series]
    summary: dict[str, Any] = Field(default_factory=dict)


# --- table ----------------------------------------------------------------------------------------------------
class Column(BaseModel):
    key: str
    label: str
    unit: str | None = None


class TableData(_Shape):
    model_config = ConfigDict(extra="allow")  # nested detail (describe_equipment: terminals, ends ...)
    viz: Literal["table"] = "table"
    columns: list[Column]
    rows: list[dict[str, Any]]
    summary: dict[str, Any] = Field(default_factory=dict)


# --- profile --------------------------------------------------------------------------------------------------
class ProfilePoint(BaseModel):
    bus: str
    mrid: UUID | None
    name: str
    distance_km: float
    phase: str
    value: float


class ProfileData(_Shape):
    viz: Literal["profile"] = "profile"
    x_unit: str
    y_unit: str
    points: list[ProfilePoint]
    summary: dict[str, Any] = Field(default_factory=dict)


SHAPES: dict[str, type[_Shape]] = {"map": MapData, "tree": TreeData, "path": PathData, "timeseries": TimeseriesData,
                                   "table": TableData, "profile": ProfileData}


def dump(shape: _Shape) -> dict[str, Any]:
    """The JSON form a tool puts in envelope.data."""
    return shape.model_dump(mode="json", by_alias=True)


def columns(*spec: tuple[str, str] | tuple[str, str, str]) -> list[Column]:
    return [Column(key=c[0], label=c[1], unit=c[2] if len(c) > 2 else None) for c in spec]


def cap[T](items: Sequence[T], limit: int, budget: int = BUDGET_BYTES,
        size: Callable[[T], int] | None = None) -> tuple[list[T], bool]:
    """First items within `limit` and the serialized-size budget; True when anything was cut."""
    size = size or (lambda x: len(json.dumps(x, default=str, separators=(",", ":"))))
    out: list[T] = []
    used = 0
    for item in items[:limit]:
        used += size(item)
        if used > budget:
            return out, True
        out.append(item)
    return out, len(items) > limit


def model_size(m: BaseModel) -> int:
    return len(m.model_dump_json(by_alias=True))


def round_coords(geometry: dict[str, Any], digits: int = 6) -> dict[str, Any]:
    def r(c: Any) -> Any:
        return [r(x) for x in c] if isinstance(c, list) else round(c, digits)

    return {**geometry, "coordinates": r(geometry["coordinates"])} if "coordinates" in geometry else geometry


def bbox_of(features: Iterable[Feature]) -> list[float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def walk(c: Any) -> None:
        if c and isinstance(c[0], int | float):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for x in c:
                walk(x)

    for f in features:
        if "coordinates" in f.geometry:
            walk(f.geometry["coordinates"])
    return [min(xs), min(ys), max(xs), max(ys)] if xs else None
