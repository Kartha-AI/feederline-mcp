# SPDX-License-Identifier: Apache-2.0
"""Spatial tools: what is near a point, the nearest substations, what an area outage takes out, feeder geometry."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field, field_validator

from ...shapes import Feature, MapData, bbox_of, cap, dump, model_size
from ...studies import Envelope, ToolError
from . import queries
from .common import (
    SWITCH_CLASSES,
    Call,
    Case,
    Feeders,
    Output,
    Ref,
    RegionInput,
    SizedInput,
    feeder_anchor,
    feeder_fields,
    gate,
    num,
    rows,
    run,
)
from .topology import Tree, feeder_tree

M_PER_DEG = 111_320.0
SIMPLIFY_STEPS = (1.0, 5.0, 25.0)  # the only tolerances the geojson resource caches
MAX_POLYGON_POINTS = 10_000
Include = Literal["segments", "devices", "loads"]


def tolerance_deg(simplify_m: float) -> float:
    """Metres -> degrees using a degree of latitude: never simplifies more than simplify_m in either axis."""
    return simplify_m / M_PER_DEG


def _include(cim_class: str) -> str:
    return "segments" if cim_class == "ACLineSegment" else "loads" if cim_class == "EnergyConsumer" else "devices"


def _feature(r: dict[str, Any], **props: Any) -> Feature:
    p = {"mrid": str(r["mrid"]), "name": r["name"], "cim_class": r["cim_class"], **props}
    return Feature(geometry=r["geometry"], properties=p)


# --- feeder_geometry ------------------------------------------------------------------------------------------
def feeder_geometry_data(c: Call, feeder_ref: str | UUID, region: str, simplify_m: float = 5.0,
                         include: tuple[str, ...] = ("segments", "devices", "loads"),
                         case: str = "operational") -> MapData:
    """The whole feeder as GeoJSON features, uncapped: every located device under normal or current switch states,
    `energised` when it is in the `case` trace. Engine fields (loading_pct, v_pu) are null."""
    a = feeder_anchor(c, feeder_ref, region)
    head = Feeders(c, region).by_mrid[a.mrid]
    live = feeder_tree(c, head, region, case)
    other = feeder_tree(c, head, region, "planning" if case == "operational" else "operational")
    energised = set(live.mrids())
    scope_ids = list(dict.fromkeys([*live.mrids(), *other.mrids()]))
    feats: list[Feature] = []
    counts: dict[str, int] = {}
    for r in rows(c, queries.FEEDER_GEOMETRY, {"scope": scope_ids, "tol": tolerance_deg(simplify_m)}):
        if _include(r["cim_class"]) not in include:
            continue
        props: dict[str, Any] = {**feeder_fields(head), "energised": r["mrid"] in energised, "phases": r["phases"]}
        if r["cim_class"] == "ACLineSegment":
            props.update(length_m=num(r["length_m"], 1), rated_a=num(r["rated_a"], 1))
        if r["cim_class"] in SWITCH_CLASSES:
            props.update(open=r["open"], normal_open=r["normal_open"], rated_a=num(r["rated_a"], 1))
        props.update(loading_pct=None, v_pu=None)
        feats.append(_feature(r, **props))
        counts[r["cim_class"]] = counts.get(r["cim_class"], 0) + 1
    box = rows(c, queries.FEEDER_BBOX, {"scope": scope_ids})
    bbox = [box[0][k] for k in ("xmin", "ymin", "xmax", "ymax")] if box and box[0]["xmin"] is not None else None
    located = sum(counts.values())
    summary = {**feeder_fields(head), "case": case, "features": located, "counts": dict(sorted(counts.items())),
               "equipment_in_scope": len(scope_ids), "energised": len(energised),
               "km": round(live.km, 3), "customers": live.customers, "simplify_m": simplify_m}
    return MapData(features=feats, bbox=bbox, summary=summary)


def geojson_resource(feeder: UUID, simplify_m: float, include: tuple[str, ...]) -> str:
    step = min(SIMPLIFY_STEPS, key=lambda s: abs(s - simplify_m))
    return f"grid://feeder/{feeder}/geojson?simplify_m={step:g}&include={','.join(include)}"


class FeederGeometryInput(SizedInput):
    feeder_mrid: Ref
    simplify_m: float = Field(default=5.0, ge=0, le=500)
    include: list[Include] = Field(default_factory=lambda: ["segments", "devices", "loads"], min_length=1)


def feeder_geometry(c: Call, args: FeederGeometryInput) -> Output:
    include = tuple(dict.fromkeys(args.include))
    if args.detail == "aggregated":
        include = tuple(i for i in include if i != "loads") or ("segments",)
    full = feeder_geometry_data(c, args.feeder_mrid, args.region_code, args.simplify_m, include)
    data = MapData(bbox=full.bbox, summary=full.summary, features=[])
    if args.detail != "summary":
        data.features, data.truncated = cap(full.features, args.limit, size=model_size)
        if data.truncated:
            data.resource = geojson_resource(UUID(full.summary["feeder_mrid"]), args.simplify_m, include)
    return Output(dump(data))


# --- equipment_near / nearest_substation ----------------------------------------------------------------------
class PointInput(RegionInput):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class EquipmentNearInput(PointInput):
    radius_m: float = Field(default=500, gt=0, le=25_000)
    cim_class: str | None = Field(default=None, pattern=r"^[A-Za-z]{1,40}$")
    limit: int = Field(default=100, ge=1, le=5000)


def equipment_near(c: Call, args: EquipmentNearInput) -> Output:
    gate(c.principal, args.region_code)
    found = rows(c, queries.EQUIPMENT_NEAR, {"lat": args.lat, "lon": args.lon, "radius_m": args.radius_m,
                                             "cls": args.cim_class, "region": args.region_code, "limit": args.limit})
    heads = Feeders(c, args.region_code).of_equipment({r["mrid"]: r["nodes"] or [] for r in found})
    feats = [_feature(r, **feeder_fields(heads.get(r["mrid"])), distance_m=round(float(r["distance_m"]), 1),
                      loading_pct=None, v_pu=None) for r in found]
    kept, truncated = cap(feats, args.limit, size=model_size)
    point = {"type": "Point", "coordinates": [args.lon, args.lat]}
    data = MapData(features=kept, bbox=bbox_of(kept), truncated=truncated,
                   summary={"center": point, "radius_m": args.radius_m, "found": len(found),
                            "by_class": {k: sum(1 for r in found if r["cim_class"] == k)
                                         for k in sorted({r["cim_class"] for r in found})}})
    return Output(dump(data))


class NearestSubstationInput(PointInput):
    k: int = Field(default=3, ge=1, le=20)


def nearest_substation(c: Call, args: NearestSubstationInput) -> Output:
    gate(c.principal, args.region_code)
    found = rows(c, queries.NEAREST_SUBSTATION, {"lat": args.lat, "lon": args.lon, "region": args.region_code,
                                                 "k": args.k})
    feats = [Feature(geometry=r["geometry"], properties={
        "mrid": str(r["mrid"]), "name": r["name"], "cim_class": "Substation", "region_code": r["region_code"],
        "distance_m": round(float(r["distance_m"]), 1), "voltages_kv": [float(v) for v in r["voltages_kv"] or []],
        "feeders": int(r["feeders"])}) for r in found]
    data = MapData(features=feats, bbox=bbox_of(feats),
                   summary={"center": {"type": "Point", "coordinates": [args.lon, args.lat]}, "found": len(feats),
                            "nearest_km": round(feats[0].properties["distance_m"] / 1000, 3) if feats else None})
    return Output(dump(data))


# --- affected_by_area -----------------------------------------------------------------------------------------
class Polygon(BaseModel):
    type: Literal["Polygon", "MultiPolygon"]
    coordinates: list[Any]

    @field_validator("coordinates")
    @classmethod
    def bounded(cls, v: list[Any]) -> list[Any]:
        n = 0

        def walk(x: Any) -> None:
            nonlocal n
            if isinstance(x, list) and x and isinstance(x[0], int | float):
                if len(x) < 2 or not all(isinstance(i, int | float) for i in x):
                    raise ValueError("positions are [lon, lat]")
                n += 1
            elif isinstance(x, list):
                for i in x:
                    walk(i)
            else:
                raise ValueError("coordinates must be nested arrays of numbers")

        walk(v)
        if n < 4 or n > MAX_POLYGON_POINTS:
            raise ValueError(f"polygon needs 4..{MAX_POLYGON_POINTS} positions")
        return v


class AffectedByAreaInput(SizedInput):
    polygon: Polygon
    case: Case = "operational"
    simplify_m: float = Field(default=5.0, ge=0, le=500)


def area_id(polygon: Polygon, region: str, case: str) -> str:
    canon = json.dumps({"p": polygon.model_dump(), "r": region, "c": case}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def _isolators(t: Tree, hit: list[UUID]) -> list[UUID | None]:
    """The first switch upstream of each hit segment (None: the feeder head), keeping only the outermost."""
    ups: set[UUID | None] = set()
    for m in hit:
        ups.add(next((p.mrid for p in t.ancestors(m) if p.cim_class in SWITCH_CLASSES), None))
    if None in ups:
        return [None]
    return [u for u in ups if not any(p.mrid in ups for p in t.ancestors(u))]


def affected_by_area(c: Call, args: AffectedByAreaInput) -> Output:
    gate(c.principal, args.region_code)
    poly = json.dumps(args.polygon.model_dump())
    hits = rows(c, queries.SEGMENTS_IN_AREA, {"polygon": poly, "region": args.region_code,
                                              "tol": tolerance_deg(args.simplify_m)})
    f = Feeders(c, args.region_code)
    heads = f.of_equipment({r["mrid"]: r["nodes"] or [] for r in hits}, args.case)
    by_feeder: dict[Any, list[dict[str, Any]]] = {}
    unfed = []
    for r in hits:
        h = heads.get(r["mrid"])
        if h is None:
            unfed.append(r)
        else:
            by_feeder.setdefault(h["mrid"], []).append(r)
    feeders_out, isolator_feats, seg_feats, downstream_ids = [], [], [], []
    total_customers = 0
    for fm, segs in by_feeder.items():
        head = f.by_mrid[fm]
        t = feeder_tree(c, head, args.region_code, args.case)
        in_tree = [r["mrid"] for r in segs if r["mrid"] in t.items]
        ups = _isolators(t, in_tree)
        lost = t.customers if ups == [None] else sum(t.items[u].customers_down for u in ups)
        total_customers += lost
        names = [head["name"] if u is None else t.items[u].name for u in ups]
        feeders_out.append({**feeder_fields(head), "substation_name": head["substation_name"], "segments": len(segs),
                            "km": round(sum(float(r["length_m"] or 0) for r in segs) / 1000, 3),
                            "customers_lost": lost, "isolate": names})
        for r in segs:
            seg_feats.append(_feature(r, **feeder_fields(head), role="in_area", length_m=num(r["length_m"], 1),
                                      loading_pct=None, v_pu=None))
        iso_ids = [u if u is not None else fm for u in ups]
        for loc in rows(c, queries.FEEDER_GEOMETRY, {"scope": iso_ids, "tol": tolerance_deg(args.simplify_m)}):
            isolator_feats.append(_feature(loc, **feeder_fields(head), role="isolate"))
        if args.detail == "full":
            for u in ups:
                downstream_ids += [i.mrid for i in t.subtree(u)]
    for r in unfed:
        seg_feats.append(_feature(r, **feeder_fields(None), role="in_area", length_m=num(r["length_m"], 1),
                                  loading_pct=None, v_pu=None))
    down_feats = []
    if downstream_ids:
        seen = {UUID(x.properties["mrid"]) for x in seg_feats}
        for loc in rows(c, queries.FEEDER_GEOMETRY, {"scope": downstream_ids, "tol": tolerance_deg(args.simplify_m)}):
            if loc["mrid"] not in seen:
                down_feats.append(_feature(loc, role="downstream"))
    aid = area_id(args.polygon, args.region_code, args.case)
    area = Feature(geometry=args.polygon.model_dump(), properties={"mrid": aid, "name": "Area", "cim_class": "Area"})
    summary = {"area_id": aid, "case": args.case, "feeders": feeders_out, "segments": len(hits),
               "km": round(sum(float(r["length_m"] or 0) for r in hits) / 1000, 3), "customers": total_customers,
               "segments_without_feeder": len(unfed)}
    everything = [area, *isolator_feats, *seg_feats, *down_feats] if hits else []
    full = MapData(features=everything, bbox=bbox_of(everything), summary=summary)
    c.areas.put(c.session_id, aid, dump(full))
    data = MapData(features=everything[:1], bbox=full.bbox, summary=summary)
    if args.detail != "summary":
        data.features, data.truncated = cap(everything, args.limit, size=model_size)
        data.resource = f"grid://area/{aid}" if data.truncated else None
    warnings = [f"{len(unfed)} segments in the area are not fed from any feeder head under {args.case} states"] \
        if unfed else []
    return Output(dump(data), warnings)


def register(mcp, services) -> None:
    @mcp.tool(name="equipment_near", structured_output=True)
    async def equipment_near_tool(ctx: Context, lat: float, lon: float, region_code: str, radius_m: float = 500,
                                  cim_class: str | None = None, limit: int = 100) -> Envelope:
        """Located equipment within radius_m metres (at most 25 km) of a point (WGS84 degrees), nearest first, with
        distance (m) and feeder. Returns a map (data.viz = "map")."""
        raw = {"lat": lat, "lon": lon, "region_code": region_code, "radius_m": radius_m, "cim_class": cim_class,
               "limit": limit}
        return await run(services, ctx, "equipment_near", EquipmentNearInput, raw, equipment_near)

    @mcp.tool(name="nearest_substation", structured_output=True)
    async def nearest_substation_tool(ctx: Context, lat: float, lon: float, region_code: str, k: int = 3) -> Envelope:
        """The k nearest substations to a point (WGS84 degrees) with distance (m), voltage levels (kV) and the number
        of feeder heads in each. Returns a map (data.viz = "map")."""
        raw = {"lat": lat, "lon": lon, "region_code": region_code, "k": k}
        return await run(services, ctx, "nearest_substation", NearestSubstationInput, raw, nearest_substation)

    @mcp.tool(name="affected_by_area", structured_output=True)
    async def affected_by_area_tool(ctx: Context, polygon: dict, region_code: str, case: Case = "operational",
                                    detail: Literal["summary", "aggregated", "full"] = "aggregated",
                                    simplify_m: float = 5.0, limit: int = 500) -> Envelope:
        """What an outage over an area (GeoJSON Polygon or MultiPolygon, WGS84) takes out: line segments inside it
        grouped by feeder, the switch to open upstream of each, and customers lost. Returns a map (data.viz = "map")
        with the area, the isolating switches and the segments; full adds everything downstream."""
        raw = {"polygon": polygon, "region_code": region_code, "case": case, "detail": detail,
               "simplify_m": simplify_m, "limit": limit}
        return await run(services, ctx, "affected_by_area", AffectedByAreaInput, raw, affected_by_area)

    @mcp.tool(name="feeder_geometry", structured_output=True)
    async def feeder_geometry_tool(ctx: Context, feeder_mrid: str, region_code: str,
                                   detail: Literal["summary", "aggregated", "full"] = "aggregated",
                                   simplify_m: float = 5.0, include: list[Include] | None = None,
                                   limit: int = 500) -> Envelope:
        """A feeder as GeoJSON features (WGS84): line segments, devices and loads with phases, length (m), ratings
        (A), switch states and whether each is energised now; loading_pct and v_pu stay null until an engine tool
        fills them. Lines are simplified to simplify_m metres. aggregated leaves out loads. Returns a map
        (data.viz = "map")."""
        raw = {"feeder_mrid": feeder_mrid, "region_code": region_code, "detail": detail, "simplify_m": simplify_m,
               "include": include, "limit": limit}
        return await run(services, ctx, "feeder_geometry", FeederGeometryInput, raw, feeder_geometry)


__all__ = ["feeder_geometry_data", "geojson_resource", "tolerance_deg", "ToolError"]
