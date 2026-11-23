# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from mcp.server.fastmcp import Context
from pydantic import BaseModel, Field

from ... import engine
from ...compiler import emit, queries
from ...sessions import Session
from ...studies import ToolError
from .._runner import Output, Services, invoke
from .capacity import max_loading
from .power_flow import voltage_range

MAX_STEPS = 96
STEP = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1)}


class TimeseriesInput(BaseModel):
    date: date
    interval: Literal["15m", "1h"] = "15m"
    source: Literal["rollups", "ami"] = "rollups"
    min_pu: float = Field(default=0.95, gt=0, lt=2)
    max_pu: float = Field(default=1.05, gt=0, lt=2)
    request_id: str | None = Field(default=None, max_length=128)


def load_profiles(services: Services, s: Session, args: TimeseriesInput) -> tuple[list[datetime], dict]:
    step = STEP[args.interval]
    start = datetime.combine(args.date, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)
    psrs = [ls.row["mrid"] for ls in s.compiled.load_seeds]
    if args.source == "rollups":
        sql = queries.ROLLUP_DAY.format(rollup=services.ds.rollup_table)
        rows = services.ds.query(sql, {"psrs": psrs, "start": start, "end": end}, s.principal.region_codes)
    else:
        rows = services.ds.query(queries.RAW_DAY, {"psrs": psrs, "start": start, "end": end, "step": step},
                                 s.principal.region_codes)
    # bucket -> load -> {P, Q}, averaging 15-minute rollups into hourly steps when asked
    acc: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for r in rows:
        k = start + ((r["bucket"] - start) // step) * step
        acc[k][r["power_system_resource_mrid"]][r["measurement_type"]].append(float(r["avg_value"]))
    steps = [start + i * step for i in range(int(timedelta(days=1) / step))][:MAX_STEPS]
    prof = {k: {psr: {t: sum(v) / len(v) for t, v in d.items()} for psr, d in loads.items()} for k, loads in acc.items()}
    return steps, prof


def execute(services: Services, s: Session, args: TimeseriesInput) -> Output:
    steps, prof = load_profiles(services, s, args)
    if not prof:
        raise ToolError("no_data", f"no {args.source} load data for {args.date.isoformat()}")
    hours = STEP[args.interval].total_seconds() / 3600
    # Only loads with data at some step are touched; the rest keep their compiled values all day.
    with_data = {psr for loads in prof.values() for psr in loads}
    seeds = {ls.row["mrid"]: ls for ls in s.compiled.load_seeds if ls.row["mrid"] in with_data}
    out = {"steps": [], "min_v_pu_by_step": [], "max_loading_by_step": [], "losses_kw_by_step": [],
           "violations_by_step": []}
    energy, missing_steps, failed = 0.0, 0, 0
    with services.store.sweep(s):
        for ts in steps:
            values = prof.get(ts)
            if not values:
                missing_steps += 1
            for psr, ls in seeds.items():
                v = (values or {}).get(psr, {})
                kw = v.get("ThreePhaseActivePower", ls.kw)
                kvar = v.get("ThreePhaseReactivePower", ls.kvar)
                engine.set_load_kw(s.dss, emit.dss_name(psr), kw, kvar)
            r = engine.solve(s.dss)
            out["steps"].append(ts.isoformat())
            if not r.converged:
                failed += 1
                for k in ("min_v_pu_by_step", "max_loading_by_step", "losses_kw_by_step", "violations_by_step"):
                    out[k].append(None)
                continue
            vmin, _ = voltage_range(s.dss)
            losses = engine.totals(s.dss)["losses_kw"]
            energy += losses * hours
            viol = sum(1 for n in engine.node_voltages(s.dss)
                       if n.kv_base > 0 and n.node in (1, 2, 3) and not args.min_pu <= n.pu <= args.max_pu)
            out["min_v_pu_by_step"].append(round(vmin, 5))
            out["max_loading_by_step"].append(round(max_loading(s.dss), 1))
            out["losses_kw_by_step"].append(round(losses, 3))
            out["violations_by_step"].append(viol)
    warnings = []
    if len(seeds) < len(s.compiled.load_seeds):
        warnings.append(f"{len(s.compiled.load_seeds) - len(seeds)} of {len(s.compiled.load_seeds)} loads have no "
                        f"{args.source} data for the day and stay at their compiled values")
    if missing_steps:
        warnings.append(f"{missing_steps} steps had no data; loads held at their compiled values")
    if failed:
        warnings.append(f"{failed} steps did not converge")
    data = {"date": args.date.isoformat(), "interval": args.interval, "source": args.source, **out,
            "energy_losses_kwh": round(energy, 3)}
    s.results["run_timeseries"] = data
    return Output(data, warnings, record=True)


def register(mcp, services: Services) -> None:
    @mcp.tool()
    async def run_timeseries(ctx: Context, date: str, interval: Literal["15m", "1h"] = "15m",
                             source: Literal["rollups", "ami"] = "rollups", request_id: str | None = None) -> dict:
        """Daily time-series power flow (UTC day, at most 96 steps) driving every load from its 15-minute rollups or
        raw AMI samples. Returns per-step minimum voltage (pu), maximum line loading (%), losses (kW) and voltage
        violation counts, plus energy losses for the day (kWh). The circuit is restored afterwards."""
        raw = {"date": date, "interval": interval, "source": source, "request_id": request_id}
        return await invoke(services, ctx, "run_timeseries", TimeseriesInput, raw, lambda s, a: execute(services, s, a))
