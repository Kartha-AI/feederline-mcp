# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import io
from typing import Annotated, Literal

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from mcp.server.fastmcp import Context  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from ... import engine  # noqa: E402
from ...sessions import Session  # noqa: E402
from ...shapes import (  # noqa: E402
    ProfileData,
    ProfilePoint,
    Series,
    TableData,
    TimeseriesData,
    columns,
    dump,
)
from ...studies import ToolError  # noqa: E402
from .._runner import Output, Services, invoke  # noqa: E402
from ..sql.common import (
    Feeders,  # noqa: E402
    keyword_params,  # noqa: E402
)
from ..sql.topology import feeder_tree  # noqa: E402
from ._shared import sql_call  # noqa: E402
from .voltages import PHASE, ensure_solved  # noqa: E402

# Reference palette (validated categorical slots 1-3), light surface and text tokens.
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = {"A": "#2a78d6", "B": "#eb6834", "C": "#1baf7a"}
MARKERS = {"A": "o", "B": "s", "C": "^"}
LIMIT = "#d03b3b"  # status critical, reserved for limit lines
Plot = Literal["voltage_profile", "loading", "capacity_curve", "timeseries"]


class VisualizationInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    plot: Plot
    top_n: int = Field(default=20, ge=1, le=100)
    as_: Literal["png", "data"] = Field(default="png", alias="as")
    request_id: str | None = Field(default=None, max_length=128)


def _axes(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(INK_2)
    ax.tick_params(colors=INK_2, labelsize=8)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _figure(rows: int = 1, height: float = 4.2):
    fig, axes = plt.subplots(rows, 1, figsize=(9, height), sharex=rows > 1, squeeze=False)
    fig.patch.set_facecolor(SURFACE)
    for ax in axes[:, 0]:
        _axes(ax)
    return fig, list(axes[:, 0])


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=120, facecolor=SURFACE)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def voltage_profile(s: Session) -> tuple[str, str]:
    ensure_solved(s)
    nodes = [n for n in engine.node_voltages(s.dss) if n.kv_base > 0 and n.node in PHASE]
    buses = sorted({n.bus for n in nodes}, key=lambda b: s.names.get(b, (None, b))[1])
    x = {b: i for i, b in enumerate(buses)}
    fig, (ax,) = _figure()
    for ph in "ABC":
        pts = [(x[n.bus], n.pu) for n in nodes if PHASE[n.node] == ph]
        if pts:
            ax.scatter(*zip(*pts, strict=True), s=22, color=SERIES[ph], marker=MARKERS[ph], label=f"Phase {ph}",
                       edgecolors=SURFACE, linewidths=1, zorder=3)
    for lim in (0.95, 1.05):
        ax.axhline(lim, color=LIMIT, linewidth=1, linestyle="--", zorder=2)
        ax.annotate(f"{lim:.2f} pu", (len(buses) - 0.5, lim), color=INK_2, fontsize=8, va="bottom", ha="right")
    ax.set_ylabel("Voltage (pu)", color=INK_2, fontsize=9)
    ax.set_xlabel("Bus", color=INK_2, fontsize=9)
    step = max(1, len(buses) // 30)
    ax.set_xticks(range(0, len(buses), step), [s.names.get(b, (None, b))[1] for b in buses][::step], rotation=90)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK, ncols=3, loc="lower left")
    ax.set_title(f"{s.model.root_name}: node voltages", color=INK, fontsize=11, loc="left")
    vmin = min(n.pu for n in nodes)
    vmax = max(n.pu for n in nodes)
    return _png(fig), f"{s.model.root_name} node voltages by bus and phase: {vmin:.3f}-{vmax:.3f} pu; dashed lines at 0.95/1.05 pu."


def loading(s: Session, top_n: int) -> tuple[str, str]:
    ensure_solved(s)
    rows = sorted(engine.line_loadings(s.dss), key=lambda b: -b["loading_pct"])[:top_n]
    if not rows:
        raise ToolError("no_data", "no lines in the circuit")
    labels = [s.names.get(r["dss_name"], (None, r["dss_name"]))[1] for r in rows][::-1]
    vals = [r["loading_pct"] for r in rows][::-1]
    fig, (ax,) = _figure(height=max(3.0, 0.28 * len(rows) + 1.2))
    ax.barh(labels, vals, color=SERIES["A"], height=0.6, edgecolor=SURFACE, linewidth=2, zorder=3)
    ax.axvline(100, color=LIMIT, linewidth=1, linestyle="--", zorder=2)
    for i, v in enumerate(vals):
        ax.annotate(f"{v:.0f}%", (v, i), xytext=(4, 0), textcoords="offset points", va="center", fontsize=7, color=INK_2)
    ax.set_xlabel("Loading (% of normal amps)", color=INK_2, fontsize=9)
    ax.set_title(f"{s.model.root_name}: top {len(rows)} line loadings", color=INK, fontsize=11, loc="left")
    return _png(fig), f"Top {len(rows)} line loadings on {s.model.root_name}; highest {rows[0]['loading_pct']:.0f}% ({labels[-1]})."


def capacity_curve(s: Session) -> tuple[str, str]:
    d = s.results.get("analyze_capacity")
    if not d or not d["curve"]:
        raise ToolError("no_data", "run analyze_capacity first")
    kw = [c["kw"] for c in d["curve"]]
    fig, (a1, a2) = _figure(rows=2, height=5.4)
    a1.plot(kw, [c["max_v_pu"] for c in d["curve"]], color=SERIES["A"], linewidth=2, marker="o", markersize=5)
    a1.axhline(1.05, color=LIMIT, linewidth=1, linestyle="--")
    a1.set_ylabel("Max voltage (pu)", color=INK_2, fontsize=9)
    a2.plot(kw, [c["max_loading_pct"] for c in d["curve"]], color=SERIES["A"], linewidth=2, marker="o", markersize=5)
    a2.axhline(100, color=LIMIT, linewidth=1, linestyle="--")
    a2.set_ylabel("Max line loading (%)", color=INK_2, fontsize=9)
    a2.set_xlabel(f"{d['der_type'].upper()} at {d['bus']} (kW)", color=INK_2, fontsize=9)
    for ax in (a1, a2):
        ax.axvline(d["hosting_capacity_kw"], color=INK_2, linewidth=1, linestyle=":")
    a1.set_title(f"Hosting capacity at {d['bus']}: {d['hosting_capacity_kw']:g} kW", color=INK, fontsize=11, loc="left")
    return _png(fig), (f"Hosting capacity at {d['bus']} is {d['hosting_capacity_kw']:g} kW "
                       f"(binding: {d['binding_constraint'] or 'none within range'}).")


def timeseries(s: Session) -> tuple[str, str]:
    d = s.results.get("run_timeseries")
    if not d:
        raise ToolError("no_data", "run run_timeseries first")
    x = list(range(len(d["steps"])))
    labels = [t[11:16] for t in d["steps"]]
    fig, (a1, a2) = _figure(rows=2, height=5.4)
    a1.plot(x, [v if v is not None else float("nan") for v in d["min_v_pu_by_step"]], color=SERIES["A"], linewidth=2)
    a1.axhline(0.95, color=LIMIT, linewidth=1, linestyle="--")
    a1.set_ylabel("Min voltage (pu)", color=INK_2, fontsize=9)
    a2.plot(x, [v if v is not None else float("nan") for v in d["max_loading_by_step"]], color=SERIES["A"], linewidth=2)
    a2.axhline(100, color=LIMIT, linewidth=1, linestyle="--")
    a2.set_ylabel("Max line loading (%)", color=INK_2, fontsize=9)
    step = max(1, len(x) // 12)
    a2.set_xticks(x[::step], labels[::step])
    a2.set_xlabel("Time (UTC)", color=INK_2, fontsize=9)
    a1.set_title(f"{s.model.root_name} on {d['date']}", color=INK, fontsize=11, loc="left")
    mins = [v for v in d["min_v_pu_by_step"] if v is not None]
    return _png(fig), (f"{s.model.root_name} {d['date']}: lowest voltage {min(mins):.3f} pu, "
                       f"energy losses {d['energy_losses_kwh']:.0f} kWh.")


# --- as="data": the same plots as visualization-ready shapes --------------------------------------------------
def voltage_profile_data(services: Services, s: Session) -> ProfileData:
    """Bus-phase voltages (pu) against distance from the feeder head (km) along the CIM trace."""
    ensure_solved(s)
    c = sql_call(services, s)
    f = Feeders(c, s.model.region_code)
    head = f.by_mrid.get(s.model.root_mrid)
    if head is None:
        raise ToolError("not_found", f"{s.model.root_name} is no longer a feeder head in the model")
    t = feeder_tree(c, head, s.model.region_code, s.model.case_kind)
    km_by_cn = {cn: t.node_km[n] for n, cn in t.node_cn.items() if n in t.node_km}
    points = []
    for n in engine.node_voltages(s.dss):
        if n.kv_base <= 0 or n.node not in PHASE or n.bus not in s.names:
            continue
        cn, human = s.names[n.bus]
        if cn in km_by_cn:
            points.append(ProfilePoint(bus=n.bus, mrid=cn, name=human, distance_km=round(km_by_cn[cn], 4),
                                       phase=PHASE[n.node], value=round(n.pu, 5)))
    points.sort(key=lambda p: (p.distance_km, p.phase))
    vals = [p.value for p in points]
    return ProfileData(x_unit="km", y_unit="pu", points=points,
                       summary={"feeder_name": s.model.root_name, "min_v_pu": min(vals, default=None),
                                "max_v_pu": max(vals, default=None), "limits_pu": [0.95, 1.05]})


def capacity_curve_data(s: Session) -> TableData:
    d = s.results.get("analyze_capacity")
    if not d or not d["curve"]:
        raise ToolError("no_data", "run analyze_capacity first")
    return TableData(columns=columns(("kw", "DER", "kW"), ("max_v_pu", "Maximum voltage", "pu"),
                                     ("max_loading_pct", "Maximum line loading", "%"), ("head_kw", "Feeder head", "kW")),
                     rows=d["curve"], summary={k: d[k] for k in ("bus", "bus_mrid", "der_type", "hosting_capacity_kw",
                                                                 "binding_constraint")})


def timeseries_data(s: Session) -> TimeseriesData:
    d = s.results.get("run_timeseries")
    if not d:
        raise ToolError("no_data", "run run_timeseries first")
    series = [Series(name="Minimum voltage", unit="pu", values=d["min_v_pu_by_step"]),
              Series(name="Maximum line loading", unit="%", values=d["max_loading_by_step"]),
              Series(name="Losses", unit="kW", values=d["losses_kw_by_step"]),
              Series(name="Voltage violations", unit="count", values=d["violations_by_step"])]
    return TimeseriesData(interval=d["interval"], ts=d["steps"], series=series,
                          summary={"date": d["date"], "source": d["source"], "energy_losses_kwh": d["energy_losses_kwh"],
                                   "feeder_name": s.model.root_name})


def execute_data(services: Services, s: Session, args: VisualizationInput) -> Output:
    from .loading import loading_map

    if args.plot == "voltage_profile":
        return Output(dump(voltage_profile_data(services, s)))
    if args.plot == "loading":
        return Output(dump(loading_map(services, s)))
    if args.plot == "capacity_curve":
        return Output(dump(capacity_curve_data(s)))
    return Output(dump(timeseries_data(s)))


def execute(s: Session, args: VisualizationInput, services: Services | None = None) -> Output:
    if args.as_ == "data":
        if services is None:
            raise ToolError("internal", "data output needs the service context")
        return execute_data(services, s, args)
    if args.plot == "voltage_profile":
        png, caption = voltage_profile(s)
    elif args.plot == "loading":
        png, caption = loading(s, args.top_n)
    elif args.plot == "capacity_curve":
        png, caption = capacity_curve(s)
    else:
        png, caption = timeseries(s)
    return Output({"plot": args.plot, "image_png_base64": png, "caption": caption})


def register(mcp, services: Services) -> None:
    @mcp.tool()
    @keyword_params
    async def create_visualization(ctx: Context, plot: Plot, top_n: int = 20,
                                   as_: Annotated[Literal["png", "data"], Field(alias="as")] = "png",
                                   request_id: str | None = None) -> dict:
        """Render a PNG (base64) of the loaded feeder: voltage_profile (node voltages in pu by bus and phase with
        0.95/1.05 limits), loading (top_n lines by % of normal amps), capacity_curve (last analyze_capacity sweep) or
        timeseries (last run_timeseries day). Returns the image and a one-line caption. as=data returns the same plot
        as data instead: voltage_profile as a profile against distance from the head (km), loading as a map with
        loading_pct, capacity_curve as a table, timeseries as a time series."""
        raw = {"plot": plot, "top_n": top_n, "as": as_, "request_id": request_id}
        return await invoke(services, ctx, "create_visualization", VisualizationInput, raw,
                            lambda s, a: execute(s, a, services))
