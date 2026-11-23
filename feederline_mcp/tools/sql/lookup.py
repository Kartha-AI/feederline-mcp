# SPDX-License-Identifier: Apache-2.0
"""Lookup tools: find equipment by name, gis_oid or tag; describe one device with its wiring and live readings."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context
from pydantic import Field

from ...shapes import TableData, columns, dump
from ...studies import Envelope, ToolError
from . import queries
from .common import Call, Feeders, Output, Ref, RegionInput, feeder_fields, gate, num, resolve, rows, run
from .telemetry import reading_columns, reading_row


def like_pattern(q: str) -> str:
    """A contains-pattern for LIKE with the client's text taken literally."""
    esc = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


class SearchEquipmentInput(RegionInput):
    query: str = Field(min_length=1, max_length=200)
    cim_class: str | None = Field(default=None, pattern=r"^[A-Za-z]{1,40}$")
    limit: int = Field(default=20, ge=1, le=200)


def search_equipment(c: Call, args: SearchEquipmentInput) -> Output:
    gate(c.principal, args.region_code)
    q = args.query.strip()
    hits = rows(c, queries.SEARCH_EQUIPMENT, {"q": q, "pat": like_pattern(q), "region": args.region_code,
                                              "cls": args.cim_class, "limit": args.limit})
    ctx = {r["mrid"]: r for r in rows(c, queries.CONTEXT, {"mrids": [h["mrid"] for h in hits]})} if hits else {}
    f = Feeders(c, args.region_code)
    heads = f.of_equipment({m: x["nodes"] or [] for m, x in ctx.items()})
    out = []
    for h in hits:
        head = heads.get(h["mrid"])
        out.append({"mrid": str(h["mrid"]), "name": h["name"], "cim_class": h["cim_class"], "gis_oid": h["gis_oid"],
                    "tag": h["tag"], **feeder_fields(head),
                    "substation_name": (ctx.get(h["mrid"]) or {}).get("substation_name")
                    or (head["substation_name"] if head else None),
                    "score": round(float(h["score"]), 3)})
    data = TableData(
        columns=columns(("name", "Name"), ("cim_class", "Class"), ("feeder_name", "Feeder"),
                        ("substation_name", "Substation"), ("gis_oid", "GIS id"), ("tag", "Tag"), ("score", "Score")),
        rows=out, summary={"query": q, "matches": len(out)})
    return Output(dump(data))


class DescribeEquipmentInput(RegionInput):
    mrid: Ref


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if isinstance(v, int | float | str | bool | dict | list) or v is None:
        return v
    return float(v) if hasattr(v, "__float__") else str(v)


def describe_equipment(c: Call, args: DescribeEquipmentInput) -> Output:
    mrid = resolve(c, args.mrid, args.region_code)
    hub = rows(c, queries.DESCRIBE_HUB, {"mrid": mrid})
    if not hub:
        raise ToolError("not_found", f"equipment {args.mrid} not found in {args.region_code}")
    h = hub[0]
    if h["region_code"] != args.region_code:
        raise ToolError("invalid_input", f"{h['name']} is in region {h['region_code']}, not {args.region_code}")
    sub_sql = queries.DESCRIBE_SUBCLASS.get(h["cim_class"])
    sub = rows(c, sub_sql, {"mrid": mrid}) if sub_sql else []
    terminals = rows(c, queries.DESCRIBE_TERMINALS, {"mrid": mrid})
    ends = rows(c, queries.DESCRIBE_ENDS, {"mrid": mrid}) if h["cim_class"] == "PowerTransformer" else []
    readings = rows(c, queries.LATEST_READINGS, {"psrs": [mrid], "mtype": None})
    f = Feeders(c, args.region_code)
    nodes = [n for n in rows(c, queries.CONTEXT, {"mrids": [mrid]})[0]["nodes"] or []]
    head = f.of_equipment({mrid: nodes}).get(mrid)
    row = {"mrid": str(h["mrid"]), "name": h["name"], "cim_class": h["cim_class"], **feeder_fields(head),
           "description": h["description"], "gis_oid": h["gis_oid"], "region_code": h["region_code"],
           "in_service": h["in_service"], "nominal_kv": num(h["nominal_kv"]),
           "substation_name": h["substation_name"] or (head["substation_name"] if head else None),
           "voltage_level_name": h["voltage_level_name"], "line_name": h["line_name"],
           "updated_at": _jsonable(h["updated_at"]), "valid_from": _jsonable(h["valid_from"]),
           "valid_to": _jsonable(h["valid_to"]), "attrs": h["attrs"] or {},
           **({k: _jsonable(v) for k, v in sub[0].items()} if sub else {})}
    container = {"line": {"mrid": str(h["line_mrid"]), "name": h["line_name"]} if h["line_mrid"] else None,
                 "voltage_level": {"mrid": str(h["voltage_level_mrid"]), "name": h["voltage_level_name"]}
                 if h["voltage_level_mrid"] else None,
                 "substation": {"mrid": str(h["substation_mrid"]), "name": h["substation_name"]}
                 if h["substation_mrid"] else None}
    data = TableData(
        columns=columns(("name", "Name"), ("cim_class", "Class"), ("feeder_name", "Feeder"),
                        ("substation_name", "Substation"), ("nominal_kv", "Nominal", "kV"), ("in_service", "In service")),
        rows=[row],
        terminals=[{"mrid": str(t["mrid"]), "sequence_number": t["sequence_number"], "phases": t["phases"],
                    "connectivity_node": {"mrid": str(t["cn_mrid"]), "name": t["cn_name"]} if t["cn_mrid"] else None,
                    "other_devices": t["others"]} for t in terminals],
        ends=[{k: _jsonable(v) for k, v in e.items()} for e in ends],
        measurements=[reading_row(r) for r in readings],
        measurement_columns=[col.model_dump() for col in reading_columns()],
        container=container,
        location={"mrid": str(h["location_mrid"]), "name": h["location_name"], "geometry": h["geometry"]}
        if h["location_mrid"] else None,
        summary={"terminals": len(terminals), "measurements": len(readings),
                 "stale_measurements": sum(1 for r in readings if reading_row(r)["stale"])})
    return Output(dump(data))


def register(mcp, services) -> None:
    @mcp.tool(name="search_equipment", structured_output=True)
    async def search_equipment_tool(ctx: Context, query: str, region_code: str, cim_class: str | None = None,
                                    limit: int = 20) -> Envelope:
        """Find equipment, substations, connectivity nodes or SCADA tags in a region by name, GIS id or tag, best match
        first (score 1.0 exact GIS id or tag, 0.9 exact name, else name similarity), with each hit's feeder and
        substation. Returns a table (data.viz = "table")."""
        raw = {"query": query, "region_code": region_code, "cim_class": cim_class, "limit": limit}
        return await run(services, ctx, "search_equipment", SearchEquipmentInput, raw, search_equipment)

    @mcp.tool(name="describe_equipment", structured_output=True)
    async def describe_equipment_tool(ctx: Context, mrid: str, region_code: str) -> Envelope:
        """One device in full: its CIM attributes and ratings, container (line, voltage level, substation), feeder,
        terminals with the connectivity node and the other devices on it, transformer ends, location geometry, and
        every measurement with its latest value, quality, age (s) and staleness. Returns a one-row table
        (data.viz = "table") with the nested detail beside it."""
        raw = {"mrid": mrid, "region_code": region_code}
        return await run(services, ctx, "describe_equipment", DescribeEquipmentInput, raw, describe_equipment)
