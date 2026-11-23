# SPDX-License-Identifier: Apache-2.0
"""Telemetry tools over measurement_value_latest and the 15-minute rollup only (never raw measurement_value).

The rollup has no row-level security of its own, so every rollup read is keyed by measurement mRIDs first selected
from `measurement` under the caller's regions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context
from pydantic import Field, model_validator

from ...shapes import Series, TableData, TimeseriesData, cap, columns, dump
from ...studies import Envelope
from . import queries
from .common import (
    Call,
    Feeders,
    Output,
    Ref,
    RegionInput,
    feeder_anchor,
    feeder_fields,
    gate,
    keyword_params,
    num,
    resolve,
    rows,
    run,
    unit,
)
from .resolve import resolve_measurement
from .topology import feeder_tree

BUCKET = timedelta(minutes=15)
DEFAULT_SCAN_S = 900.0
MAX_HISTORY_DAYS = 7
MAX_PEAK_DAYS = 92
QUALITY_BITS = ("questionable", "invalid", "substituted", "old", "out_of_range", "blocked", "test")
SOURCES = {1: "SCADA", 2: "AMI", 3: "PMU", 4: "Estimator", 5: "Manual", 6: "Simulator"}


def quality_flags(q: int | None) -> list[str]:
    """IEC 61850 quality bitmask -> flag names (empty for 0 = good)."""
    return [] if not q else [name for bit, name in enumerate(QUALITY_BITS) if q & (1 << bit)]


def reading_columns():
    return columns(("equipment_name", "Equipment"), ("name", "Measurement"), ("measurement_type", "Type"),
                   ("phases", "Phases"), ("value", "Value"), ("unit", "Unit"), ("quality_flags", "Quality"),
                   ("source_name", "Source"), ("ts", "Time"), ("age_s", "Age", "s"), ("stale", "Stale"))


def reading_row(r: dict[str, Any]) -> dict[str, Any]:
    """The latest_readings row shape (describe_equipment.measurements uses the same)."""
    scan = float(r["scan_interval"] or DEFAULT_SCAN_S)
    age = float(r["age_s"]) if r["age_s"] is not None else None
    return {"mrid": str(r["mrid"]), "name": r["name"], "cim_class": r["cim_class"],
            "equipment_mrid": str(r["equipment_mrid"]), "equipment_name": r["equipment_name"],
            "measurement_type": r["measurement_type"], "phases": r["phases"], "tag": r["tag"],
            "value": num(r["value"], 4), "unit": unit(r["unit_symbol"], r["unit_multiplier"]),
            "quality": r["quality"], "quality_flags": quality_flags(r["quality"]), "source": r["source"],
            "source_name": SOURCES.get(r["source"]) if r["source"] else None,
            "ts": r["ts"].isoformat() if r["ts"] else None, "age_s": round(age, 1) if age is not None else None,
            "stale": age is None or age > 2 * scan}


def quality_stats(scan_s: float, start: datetime, end: datetime, received: int, bad: int,
                  bucket_epochs: list[float], latest_ts: datetime | None, now: datetime) -> dict[str, Any]:
    """Expected vs received samples, bad share, longest gap (s) and staleness for one measurement over a window.
    A gap is time not covered by any 15-minute rollup bucket, counting from the window start to its end."""
    expected = (end - start).total_seconds() / scan_s
    edges = [start.timestamp(), *bucket_epochs, end.timestamp()]
    gaps = [edges[1] - edges[0]] + [b - a - BUCKET.total_seconds() for a, b in zip(edges[1:-1], edges[2:-1], strict=False)]
    gaps.append(edges[-1] - (edges[-2] + BUCKET.total_seconds()) if bucket_epochs else 0.0)
    longest = max([g for g in gaps if g > 0], default=0.0)
    return {"expected": int(round(expected)), "received": int(received),
            "completeness": round(received / expected, 4) if expected else None,
            "bad_share": round(bad / received, 4) if received else None, "longest_gap_s": round(longest),
            "stale_now": latest_ts is None or (now - latest_ts).total_seconds() > 2 * scan_s}


def _scope(c: Call, feeder_ref: str, region: str) -> tuple[Any, list[Any]]:
    a = feeder_anchor(c, feeder_ref, region)
    head = Feeders(c, region).by_mrid[a.mrid]
    return head, feeder_tree(c, head, region, "operational").mrids()


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    start = datetime.combine(d, datetime.min.time(), tzinfo=UTC)
    return start, start + timedelta(days=1)


# --- latest_readings ------------------------------------------------------------------------------------------
class LatestReadingsInput(RegionInput):
    mrid: Ref | None = None
    feeder_mrid: Ref | None = None
    measurement_type: Literal["ThreePhaseActivePower", "ThreePhaseReactivePower", "LineToLineVoltage", "LineCurrent",
                              "Frequency", "SwitchPosition", "TapPosition"] | None = None
    limit: int = Field(default=500, ge=1, le=5000)

    @model_validator(mode="after")
    def one_of(self):
        if bool(self.mrid) == bool(self.feeder_mrid):
            raise ValueError("pass exactly one of mrid or feeder_mrid")
        return self


def latest_readings(c: Call, args: LatestReadingsInput) -> Output:
    if args.feeder_mrid:
        head, psrs = _scope(c, args.feeder_mrid, args.region_code)
    else:
        m = resolve(c, args.mrid, args.region_code)
        head, psrs = None, [m]
    found = [reading_row(r) for r in rows(c, queries.LATEST_READINGS, {"psrs": psrs, "mtype": args.measurement_type})]
    if head:
        for r in found:
            r.update(feeder_fields(head))
    kept, truncated = cap(found, args.limit)
    data = TableData(columns=reading_columns(), rows=kept, truncated=truncated,
                     summary={"measurements": len(found), "stale": sum(1 for r in found if r["stale"]),
                              "bad_quality": sum(1 for r in found if r["quality"]), **feeder_fields(head)})
    return Output(dump(data))


# --- measurement_history --------------------------------------------------------------------------------------
class MeasurementHistoryInput(RegionInput):
    measurement_mrids: list[Ref] = Field(min_length=1, max_length=12)
    start: datetime = Field(alias="from")
    end: datetime = Field(alias="to")
    stat: Literal["avg", "min", "max", "last"] = "avg"

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def window(self):
        if self.start.tzinfo is None:
            self.start = self.start.replace(tzinfo=UTC)
        if self.end.tzinfo is None:
            self.end = self.end.replace(tzinfo=UTC)
        if self.end <= self.start:
            raise ValueError("to must be after from")
        if self.end - self.start > timedelta(days=MAX_HISTORY_DAYS):
            raise ValueError(f"window longer than {MAX_HISTORY_DAYS} days; use a job for longer history")
        return self


def measurement_history(c: Call, args: MeasurementHistoryInput) -> Output:
    gate(c.principal, args.region_code)
    ids = list(dict.fromkeys(resolve_measurement(c.ds, c.principal, r, args.region_code) for r in args.measurement_mrids))
    meta = {m["mrid"]: m for m in rows(c, queries.MEASUREMENTS, {"mrids": ids})}
    sql = queries.rollup(queries.MEASUREMENT_HISTORY, c.ds.rollup_table)
    start = args.start - (args.start - datetime(2000, 1, 1, tzinfo=UTC)) % BUCKET
    ts = []
    t = start
    while t < args.end:
        ts.append(t)
        t += BUCKET
    index = {x: i for i, x in enumerate(ts)}
    values: dict[Any, list[float | None]] = {m: [None] * len(ts) for m in meta}
    bad = 0
    for r in rows(c, sql, {"mrids": list(meta), "start": start, "end": args.end}):
        i = index.get(r["bucket"])
        if i is not None:
            values[r["measurement_mrid"]][i] = num(r[f"{args.stat}_value"], 4)
            bad += int(r["bad_samples"] or 0)
    series = [Series(name=f"{meta[m]['equipment_name']} {meta[m]['name']}", mrid=m,
                     unit=unit(meta[m]["unit_symbol"], meta[m]["unit_multiplier"]), values=values[m]) for m in meta]
    data = TimeseriesData(interval="15m", ts=[x.isoformat() for x in ts], series=series,
                          summary={"stat": args.stat, "buckets": len(ts), "bad_samples": bad,
                                   "missing": {str(m): sum(1 for v in values[m] if v is None) for m in meta}})
    return Output(dump(data))


# --- load_profile ---------------------------------------------------------------------------------------------
class LoadProfileInput(RegionInput):
    feeder_mrid: Ref
    date: date


SERIES = (("head_p", "Head P", "kW"), ("head_q", "Head Q", "kvar"), ("load_p", "Measured load P", "kW"),
          ("der_p", "DER P", "kW"))


def load_profile_data(c: Call, feeder_ref: str, region: str, day: date) -> TimeseriesData:
    head, scope_ids = _scope(c, feeder_ref, region)
    start, end = _day_bounds(day)
    ts = [start + i * BUCKET for i in range(96)]
    vals: dict[str, list[float | None]] = {k: [None] * 96 for k, _, _ in SERIES}
    sql = queries.rollup(queries.LOAD_PROFILE, c.ds.rollup_table)
    for r in rows(c, sql, {"root": head["mrid"], "scope": scope_ids, "start": start, "end": end}):
        i = int((r["bucket"] - start) / BUCKET)
        if 0 <= i < 96:
            vals[r["series"]][i] = round(float(r["value"]), 3)
    series = [Series(name=label, mrid=head["mrid"] if k.startswith("head") else None, unit=u, values=vals[k])
              for k, label, u in SERIES]
    present = [k for k, _, _ in SERIES if any(v is not None for v in vals[k])]
    return TimeseriesData(interval="15m", ts=[t.isoformat() for t in ts], series=series,
                          summary={**feeder_fields(head), "date": day.isoformat(), "buckets": 96,
                                   "series_with_data": present,
                                   "peak_head_kw": max((v for v in vals["head_p"] if v is not None), default=None)})


def load_profile(c: Call, args: LoadProfileInput) -> Output:
    data = load_profile_data(c, args.feeder_mrid, args.region_code, args.date)
    warnings = [] if data.summary["series_with_data"] else [f"no rollup data for {args.date.isoformat()}"]
    return Output(dump(data), warnings)


# --- peak_load ------------------------------------------------------------------------------------------------
class PeakLoadInput(RegionInput):
    feeder_mrid: Ref
    start: date = Field(alias="from")
    end: date = Field(alias="to")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @model_validator(mode="after")
    def window(self):
        if self.end < self.start:
            raise ValueError("to must not be before from")
        if (self.end - self.start).days >= MAX_PEAK_DAYS:
            raise ValueError(f"window longer than {MAX_PEAK_DAYS} days")
        return self


def peak_load(c: Call, args: PeakLoadInput) -> Output:
    a = feeder_anchor(c, args.feeder_mrid, args.region_code)
    head = Feeders(c, args.region_code).by_mrid[a.mrid]
    mids = [r["mrid"] for r in rows(c, queries.HEAD_P_MEASUREMENTS, {"root": a.mrid})]
    start, _ = _day_bounds(args.start)
    _, end = _day_bounds(args.end)
    by_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    if mids:
        for r in rows(c, queries.rollup(queries.HEAD_P_BUCKETS, c.ds.rollup_table),
                      {"mrids": mids, "start": start, "end": end}):
            by_day[r["bucket"].astimezone(UTC).date()].append((r["bucket"], float(r["kw"])))
    out = []
    for d in sorted(by_day):
        pts = by_day[d]
        peak_ts, peak = max(pts, key=lambda p: p[1])
        mean = sum(p[1] for p in pts) / len(pts)
        out.append({"mrid": str(a.mrid), "name": a.name, "cim_class": a.cim_class, **feeder_fields(head),
                    "date": d.isoformat(), "peak_kw": round(peak, 2), "peak_bucket": peak_ts.isoformat(),
                    "min_kw": round(min(p[1] for p in pts), 2),
                    "load_factor": round(mean / peak, 4) if peak > 0 else None, "buckets": len(pts)})
    warnings = [] if mids else [f"{a.name} has no ThreePhaseActivePower measurement"]
    data = TableData(columns=columns(("date", "Date"), ("peak_kw", "Peak", "kW"), ("peak_bucket", "Peak at"),
                                     ("min_kw", "Minimum", "kW"), ("load_factor", "Load factor"),
                                     ("buckets", "Buckets")),
                     rows=out, summary={**feeder_fields(head), "days": len(out),
                                        "peak_kw": max((r["peak_kw"] for r in out), default=None)})
    return Output(dump(data), warnings)


# --- limit_exceedances ----------------------------------------------------------------------------------------
class LimitExceedancesInput(RegionInput):
    feeder_mrid: Ref | None = None
    limit: int = Field(default=500, ge=1, le=5000)


def limit_exceedances(c: Call, args: LimitExceedancesInput) -> Output:
    gate(c.principal, args.region_code)
    head, scope_ids = _scope(c, args.feeder_mrid, args.region_code) if args.feeder_mrid else (None, None)
    out = []
    for r in rows(c, queries.LIMIT_EXCEEDANCES, {"region": args.region_code, "scope": scope_ids}):
        low = r["min_value"] is not None and r["value"] < r["min_value"]
        lim = r["min_value"] if low else r["max_value"]
        out.append({"mrid": str(r["mrid"]), "name": r["name"], "cim_class": r["cim_class"],
                    "equipment_mrid": str(r["equipment_mrid"]), "equipment_name": r["equipment_name"],
                    **feeder_fields(head), "measurement_type": r["measurement_type"], "phases": r["phases"],
                    "value": num(r["value"], 4), "unit": unit(r["unit_symbol"], r["unit_multiplier"]),
                    "limit_kind": "min" if low else "max", "limit_value": num(lim, 4),
                    "margin": num(r["value"] - lim, 4), "ts": r["ts"].isoformat(), "age_s": round(float(r["age_s"]), 1)})
    kept, truncated = cap(out, args.limit)
    data = TableData(columns=columns(("equipment_name", "Equipment"), ("name", "Measurement"), ("value", "Value"),
                                     ("unit", "Unit"), ("limit_kind", "Limit"), ("limit_value", "Limit value"),
                                     ("margin", "Margin"), ("age_s", "Age", "s")),
                     rows=kept, truncated=truncated, summary={"exceedances": len(out), **feeder_fields(head)})
    return Output(dump(data))


# --- telemetry_quality ----------------------------------------------------------------------------------------
class TelemetryQualityInput(RegionInput):
    feeder_mrid: Ref
    days: int = Field(default=7, ge=1, le=31)
    limit: int = Field(default=500, ge=1, le=5000)


def telemetry_quality(c: Call, args: TelemetryQualityInput) -> Output:
    head, scope_ids = _scope(c, args.feeder_mrid, args.region_code)
    now = datetime.now(UTC)
    end = now - (now - datetime(2000, 1, 1, tzinfo=UTC)) % BUCKET
    start = end - timedelta(days=args.days)
    sql = queries.rollup(queries.TELEMETRY_QUALITY, c.ds.rollup_table)
    out = []
    for r in rows(c, sql, {"scope": scope_ids, "start": start, "end": end, "default_scan": DEFAULT_SCAN_S}):
        st = quality_stats(float(r["scan_interval"]), start, end, int(r["received"]), int(r["bad"]),
                           list(r["buckets"] or []), r["latest_ts"], now)
        out.append({"mrid": str(r["mrid"]), "name": r["name"], "cim_class": r["cim_class"],
                    "equipment_mrid": str(r["equipment_mrid"]), "equipment_name": r["equipment_name"],
                    **feeder_fields(head), "measurement_type": r["measurement_type"],
                    "scan_interval_s": float(r["scan_interval"]), **st})
    out.sort(key=lambda x: (x["completeness"] if x["completeness"] is not None else 0, x["name"]))
    kept, truncated = cap(out, args.limit)
    data = TableData(
        columns=columns(("equipment_name", "Equipment"), ("name", "Measurement"), ("expected", "Expected"),
                        ("received", "Received"), ("completeness", "Completeness"), ("bad_share", "Bad share"),
                        ("longest_gap_s", "Longest gap", "s"), ("stale_now", "Stale now")),
        rows=kept, truncated=truncated,
        summary={**feeder_fields(head), "measurements": len(out), "window_start": start.isoformat(),
                 "window_end": end.isoformat(),
                 "below_95pct": sum(1 for x in out if (x["completeness"] or 0) < 0.95),
                 "stale_now": sum(1 for x in out if x["stale_now"])})
    return Output(dump(data))


def register(mcp, services) -> None:
    @mcp.tool(name="latest_readings", structured_output=True)
    async def latest_readings_tool(ctx: Context, region_code: str, mrid: str | None = None,
                                   feeder_mrid: str | None = None, measurement_type: str | None = None,
                                   limit: int = 500) -> Envelope:
        """Latest value of every measurement on one device, or on every device of a feeder: value and unit, IEC 61850
        quality flags, source, timestamp, age (s) and whether it is stale (older than two scan intervals). Returns a
        table (data.viz = "table")."""
        raw = {"region_code": region_code, "mrid": mrid, "feeder_mrid": feeder_mrid,
               "measurement_type": measurement_type, "limit": limit}
        return await run(services, ctx, "latest_readings", LatestReadingsInput, raw, latest_readings)

    @mcp.tool(name="measurement_history", structured_output=True)
    @keyword_params
    async def measurement_history_tool(ctx: Context, measurement_mrids: list[str], region_code: str,
                                       from_: Annotated[str, Field(alias="from")], to: str,
                                       stat: Literal["avg", "min", "max", "last"] = "avg") -> Envelope:
        """15-minute history (avg, min, max or last per bucket) of up to 12 measurements, by mRID, tag or name, over
        at most 7 days; null where a bucket has no data. Returns a time series (data.viz = "timeseries")."""
        raw = {"measurement_mrids": measurement_mrids, "region_code": region_code, "from": from_, "to": to,
               "stat": stat}
        return await run(services, ctx, "measurement_history", MeasurementHistoryInput, raw, measurement_history)

    @mcp.tool(name="load_profile", structured_output=True)
    async def load_profile_tool(ctx: Context, feeder_mrid: str, date: str, region_code: str) -> Envelope:
        """A feeder's day in 96 fifteen-minute buckets (UTC): feeder-head P (kW) and Q (kvar), the sum of measured load
        P (kW) and DER P (kW). Returns a time series (data.viz = "timeseries")."""
        raw = {"feeder_mrid": feeder_mrid, "date": date, "region_code": region_code}
        return await run(services, ctx, "load_profile", LoadProfileInput, raw, load_profile)

    @mcp.tool(name="peak_load", structured_output=True)
    @keyword_params
    async def peak_load_tool(ctx: Context, feeder_mrid: str, region_code: str,
                             from_: Annotated[str, Field(alias="from")], to: str) -> Envelope:
        """Daily peak and minimum feeder-head demand (kW, 15-minute averages), the bucket of the peak and the load
        factor (mean/peak) for each day in a date range of at most 92 days. Returns a table (data.viz = "table")."""
        raw = {"feeder_mrid": feeder_mrid, "region_code": region_code, "from": from_, "to": to}
        return await run(services, ctx, "peak_load", PeakLoadInput, raw, peak_load)

    @mcp.tool(name="limit_exceedances", structured_output=True)
    async def limit_exceedances_tool(ctx: Context, region_code: str, feeder_mrid: str | None = None,
                                     limit: int = 500) -> Envelope:
        """Measurements whose latest value is outside their analog min/max limits, in a feeder or a whole region, with
        the limit crossed and the margin. Returns a table (data.viz = "table")."""
        raw = {"region_code": region_code, "feeder_mrid": feeder_mrid, "limit": limit}
        return await run(services, ctx, "limit_exceedances", LimitExceedancesInput, raw, limit_exceedances)

    @mcp.tool(name="telemetry_quality", structured_output=True)
    async def telemetry_quality_tool(ctx: Context, feeder_mrid: str, region_code: str, days: int = 7,
                                     limit: int = 500) -> Envelope:
        """Telemetry completeness for every measurement on a feeder over the last `days` days: samples expected from
        the scan interval vs received, share of bad-quality samples, longest gap (s) and whether it is stale now,
        least complete first. Returns a table (data.viz = "table")."""
        raw = {"feeder_mrid": feeder_mrid, "region_code": region_code, "days": days, "limit": limit}
        return await run(services, ctx, "telemetry_quality", TelemetryQualityInput, raw, telemetry_quality)
