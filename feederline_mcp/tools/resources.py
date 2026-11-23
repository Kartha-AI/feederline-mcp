# SPDX-License-Identifier: Apache-2.0
"""Resources (grid://...) and prompts."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

from mcp.server.fastmcp import Context

from ..compiler import ScopeError, queries
from ..compiler.scope import trace
from ..shapes import dump
from ..studies import ToolError
from ._runner import Call, Services, principal_of, session_id
from .sql import queries as sql_queries
from .sql.common import anchor as sql_anchor
from .sql.spatial import SIMPLIFY_STEPS, feeder_geometry_data
from .sql.telemetry import load_profile_data
from .sql.topology import build_tree, tree_data

INCLUDES = ("segments", "devices", "loads")


def _json(obj: Any) -> str:
    return json.dumps(obj, default=str, indent=1)


def _query(q: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(q.lstrip("?")).items()}


def feeders(services: Services, ctx: Context, region: str | None) -> str:
    p = principal_of(ctx)
    rows = services.ds.query(queries.FEEDERS, {"region": region}, p.region_codes)
    return _json([{"root_mrid": r["mrid"], "name": r["name"], "region_code": r["region_code"],
                   "substation": r["substation"], "nominal_kv": r["nominal_voltage"]} for r in rows])


def feeder_summary(services: Services, ctx: Context, mrid: str) -> str:
    p = principal_of(ctx)
    try:
        root_mrid = UUID(mrid)
        root = services.ds.query(queries.ROOT, {"root": root_mrid}, p.region_codes)
        if not root:
            return _json({"error": "not_found", "root_mrid": mrid})
        sc = trace(services.ds, p, root_mrid, "operational", root[0]["region_code"])
    except (ValueError, ScopeError) as e:
        return _json({"error": "not_found", "message": str(e)})
    counts = {r["cim_class"]: r["n"] for r in services.ds.query(queries.SCOPE_COUNTS, {"scope": sc.mrids}, p.region_codes)}
    return _json({"root_mrid": mrid, "name": sc.root["name"], "region_code": sc.root["region_code"], "counts": counts})


def session_current(services: Services, ctx: Context) -> str:
    s = services.store.get(session_id(ctx))
    if s is None or s.model is None:
        return _json({"session_id": session_id(ctx), "model": None})
    return _json({"session_id": s.id, "principal": s.principal.name, "model": s.model.report(),
                  "history": [{"model_id": m.model_id, "root_name": m.root_name, "case_kind": m.case_kind}
                              for m in s.history],
                  "tool_log": s.tool_log[-50:]})


def studies_recent(services: Services, ctx: Context, feeder: str) -> str:
    p = principal_of(ctx)
    try:
        root = UUID(feeder)
    except ValueError:
        return _json({"error": "invalid_input", "message": "feeder must be a root mRID"})
    if not services.ds.query(queries.ROOT, {"root": root}, p.region_codes):
        return _json({"error": "not_found", "root_mrid": feeder})
    return _json(services.sink.recent(root, 20))


def substations_near(services: Services, ctx: Context, q: dict[str, str]) -> str:
    p = principal_of(ctx)
    try:
        lat, lon = float(q["lat"]), float(q["lon"])
        km = min(float(q.get("km", 25)), 200.0)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180 and km > 0):
            raise ValueError
    except (KeyError, ValueError):
        return _json({"error": "invalid_input", "message": "need lat, lon (degrees) and km (0-200)"})
    params = {"lat": lat, "lon": lon, "radius_m": km * 1000, "limit": 50}
    subs = services.ds.query(queries.SUBSTATIONS_NEAR, params, p.region_codes)
    segs = services.ds.query(queries.SEGMENTS_NEAR, params, p.region_codes)
    return _json({"radius_km": km, "substations": [{k: r[k] for k in ("mrid", "name", "region_code", "km", "max_kv")}
                                                   for r in subs],
                  "segments_by_voltage": segs})


def _call(services: Services, ctx: Context) -> Call:
    return Call(services.ds, services.sink, principal_of(ctx), session_id(ctx), services.areas, services.cache)


def _error(e: Exception) -> str:
    if isinstance(e, ToolError):
        return _json({"error": e.code, "message": str(e), **e.extra})
    return _json({"error": "invalid_input", "message": str(e)})


def _region_of(c: Call, mrid: str) -> str:
    """A visible equipment's region (RLS hides the rest)."""
    rows = c.ds.query(queries.ANCHOR, {"mrid": UUID(mrid)}, c.principal.region_codes) or \
        c.ds.query(queries.VOLTAGE_LEVEL_HEAD, {"root": UUID(mrid)}, c.principal.region_codes)
    if not rows:
        raise ToolError("not_found", f"{mrid} not found (or not visible)")
    if "region_code" not in rows[0]:
        rows = c.ds.query(queries.ANCHOR, {"mrid": rows[0]["mrid"]}, c.principal.region_codes)
    return rows[0]["region_code"]


def feeder_geojson(services: Services, ctx: Context, mrid: str, q: dict[str, str]) -> str:
    """Whole-feeder MapData, cached per (feeder, simplify_m, include) and the region's model/switch version."""
    c = _call(services, ctx)
    try:
        simplify = float(q.get("simplify_m", 5))
        if simplify not in SIMPLIFY_STEPS:
            raise ValueError(f"simplify_m must be one of {', '.join(f'{x:g}' for x in SIMPLIFY_STEPS)}")
        include = tuple(i for i in INCLUDES if i in (q.get("include") or ",".join(INCLUDES)).split(","))
        if not include:
            raise ValueError(f"include is a comma list of {', '.join(INCLUDES)}")
        region = _region_of(c, mrid)
        head = sql_anchor(c, mrid, region)  # RLS check before any cache hit
        v = c.ds.query(sql_queries.REGION_VERSION, {"region": region}, c.principal.region_codes)[0]
        key = ("geojson", head.mrid, simplify, include, v["cim"], v["switches"])
        if key not in services.cache:
            for k in [k for k in services.cache if k[:2] == ("geojson", head.mrid)]:
                services.cache.pop(k, None)
            services.cache[key] = dump(feeder_geometry_data(c, head.mrid, region, simplify, include))
        return _json(services.cache[key])
    except (ToolError, ValueError) as e:
        return _error(e)


def feeder_tree_resource(services: Services, ctx: Context, mrid: str, q: dict[str, str]) -> str:
    """Full downstream tree from any equipment; not cached (switch states change between calls)."""
    c = _call(services, ctx)
    try:
        case = q.get("case", "operational")
        if case not in ("operational", "planning"):
            raise ValueError("case is operational or planning")
        region = _region_of(c, mrid)
        a = sql_anchor(c, mrid, region, case)
        return _json(dump(tree_data(build_tree(c, a, case), "full", 10**9, None)))
    except (ToolError, ValueError) as e:
        return _error(e)


def feeder_profile(services: Services, ctx: Context, mrid: str, day: str) -> str:
    """One day's load_profile; cached once the day is over."""
    from datetime import UTC, date, datetime

    c = _call(services, ctx)
    try:
        d = date.fromisoformat(day)
        region = _region_of(c, mrid)
        head = sql_anchor(c, mrid, region)
        key = ("profile", head.mrid, d)
        if key in services.cache:
            return _json(services.cache[key])
        data = dump(load_profile_data(c, head.mrid, region, d))
        if d < datetime.now(UTC).date():
            services.cache[key] = data
        return _json(data)
    except (ToolError, ValueError) as e:
        return _error(e)


def area(services: Services, ctx: Context, area_id: str) -> str:
    hit = services.areas.get(session_id(ctx), area_id)
    if hit is None:
        return _json({"error": "not_found", "message": f"no affected_by_area result {area_id} in this session"})
    return _json(hit)


def register(mcp, services: Services) -> None:
    # Parameterless resources are static in FastMCP; the request context comes from get_context().
    @mcp.resource("grid://feeders", mime_type="application/json")
    def feeders_all() -> str:
        """Feeder heads (root mRIDs) visible to the caller."""
        return feeders(services, mcp.get_context(), None)

    # grid://feeders?region=XYZ  (FastMCP templates match {q} against the literal query string)
    @mcp.resource("grid://feeders{q}", mime_type="application/json")
    def feeders_by_region(q: str, ctx: Context) -> str:
        """Feeder heads in one region: grid://feeders?region=<code>."""
        return feeders(services, ctx, _query(q).get("region"))

    @mcp.resource("grid://feeder/{mrid}/summary", mime_type="application/json")
    def summary(mrid: str, ctx: Context) -> str:
        """Equipment counts by CIM class for the feeder below a root mRID (operational switch states)."""
        return feeder_summary(services, ctx, mrid)

    @mcp.resource("grid://session/current", mime_type="application/json")
    def current() -> str:
        """The model loaded in this MCP session, earlier models and the tool log."""
        return session_current(services, mcp.get_context())

    @mcp.resource("grid://studies/recent{q}", mime_type="application/json")
    def recent(q: str, ctx: Context) -> str:
        """Recent study results for a feeder: grid://studies/recent?feeder=<root mRID>."""
        return studies_recent(services, ctx, _query(q).get("feeder", ""))

    @mcp.resource("grid://substations/near{q}", mime_type="application/json")
    def near(q: str, ctx: Context) -> str:
        """Substations (nearest first, distance in km, max kV) and line segments by voltage within a radius:
        grid://substations/near?lat=<deg>&lon=<deg>&km=<radius, max 200>."""
        return substations_near(services, ctx, _query(q))

    @mcp.resource("grid://feeder/{mrid}/geojson", mime_type="application/geo+json")
    def geojson(mrid: str, ctx: Context) -> str:
        """The whole feeder as a GeoJSON FeatureCollection (MapData), lines simplified to 5 m."""
        return feeder_geojson(services, ctx, mrid, {})

    @mcp.resource("grid://feeder/{mrid}/geojson{q}", mime_type="application/geo+json")
    def geojson_q(mrid: str, q: str, ctx: Context) -> str:
        """The whole feeder as GeoJSON: ?simplify_m=1|5|25&include=segments,devices,loads."""
        return feeder_geojson(services, ctx, mrid, _query(q))

    @mcp.resource("grid://feeder/{mrid}/tree", mime_type="application/json")
    def tree(mrid: str, ctx: Context) -> str:
        """Everything downstream of a device as a full tree (TreeData), current switch states."""
        return feeder_tree_resource(services, ctx, mrid, {})

    @mcp.resource("grid://feeder/{mrid}/tree{q}", mime_type="application/json")
    def tree_q(mrid: str, q: str, ctx: Context) -> str:
        """Everything downstream of a device as a full tree: ?case=operational|planning."""
        return feeder_tree_resource(services, ctx, mrid, _query(q))

    @mcp.resource("grid://feeder/{mrid}/profile/{day}", mime_type="application/json")
    def profile(mrid: str, day: str, ctx: Context) -> str:
        """A feeder's 96-bucket load profile for one UTC date (YYYY-MM-DD) as TimeseriesData."""
        return feeder_profile(services, ctx, mrid, day)

    @mcp.resource("grid://area/{area_id}", mime_type="application/geo+json")
    def area_result(area_id: str, ctx: Context) -> str:
        """The last affected_by_area result for a polygon in this session, uncapped (MapData)."""
        return area(services, ctx, area_id)

    @mcp.prompt()
    def hosting_capacity_request(root_mrid: str, region_code: str, bus: str, der_type: str = "pv") -> str:
        """Hosting capacity study at one bus."""
        return (f"Load feeder {root_mrid} (region {region_code}, operational case), run a power flow, then run "
                f"analyze_capacity at bus {bus} for {der_type}. Report the hosting capacity in kW, the binding "
                f"constraint, and any compile warnings that limit confidence in the result.")

    @mcp.prompt()
    def daily_health_sweep(region_code: str) -> str:
        """Voltage and loading health check across a region's feeders."""
        return (f"Read grid://feeders?region={region_code}. For each feeder: load_feeder (operational), "
                f"run_power_flow, check_voltages. Summarise per feeder: convergence, min/max voltage (pu), "
                f"overloaded lines, voltage violations and compile warnings, worst feeders first.")
