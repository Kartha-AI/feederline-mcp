# Exploring the server with the MCP Inspector

A hands-on guide to the feederline-mcp server: what the grid model is, what each tool does, what to type, and what comes
back. Every example uses the IEEE 13-bus test feeder, which is small enough to read every line of output.

## Start

```bash
npx -y @kartha-energy/feederline-mcp up        # if the stack is not running (loads the IEEE feeders on first run)
npx -y @kartha-energy/feederline-mcp inspect   # opens the Inspector pointed at http://127.0.0.1:8000/mcp with your token
```

From source, the equivalents are `make up`, `make load-ieee` and `make inspect` (which uses the static `dev-token`).

Click **Connect**. The left tabs are **Resources**, **Prompts** and **Tools**. In the Tools tab, pick a tool, fill the
form, press **Run Tool**. List and object fields take JSON.

One Inspector connection is one MCP session. Engine tools remember the loaded feeder for that session; reconnecting
starts a fresh session and you must run `load_feeder` again.

---

## The grid in plain terms

An electric distribution system is a tree. Power enters at the top and branches out to customers.

```
Region (IEEE13)                     a security boundary: who may see which part of the grid
 └─ Substation (IEEE13 Substation)  where high-voltage transmission is stepped down for local distribution
     └─ Feeder head (IEEE13 Head)   the breaker at the start of one feeder; the "root" of the tree
         ├─ Line segments           wires between poles or junctions (have length and resistance)
         ├─ Regulators (REG1..3)    transformers that nudge voltage up or down in small steps ("taps")
         ├─ Transformers (XFM1)     step voltage down again, e.g. to a building
         ├─ Switches (671692)       can be opened to cut a section off, or closed to reconnect it
         ├─ Capacitors (CAP1, CAP2) support voltage on long or heavily loaded feeders
         └─ Loads (671, 675A, ...)  customers, or groups of customers, drawing power
```

Terms that appear in tool inputs and outputs:

| Term | Meaning |
| --- | --- |
| **Region** (`region_code`) | A slice of the grid a user is allowed to see. Every tool asks for it; you cannot read a region you are not granted. Locally you see all of them. |
| **Feeder** | Everything downstream of one feeder-head breaker. Most tools work on one feeder. |
| **Root / feeder_mrid** | The ID of the feeder-head breaker. Passing it means "this whole feeder". |
| **mRID** | The permanent unique ID (a UUID) of every piece of equipment. Tools that take `mrid` also accept a name such as `REG1` or `671692`. |
| **Bus / node** | A junction where equipment connects (a pole top, a busbar). Names in IEEE 13 are numbers: `650`, `671`, `675`. |
| **Phase (A, B, C)** | Three-phase power runs on three wires. Some laterals carry only one or two phases. |
| **Per unit (pu)** | Voltage as a fraction of its nominal value. 1.00 is perfect; utilities keep customers between 0.95 and 1.05 (ANSI Range A). |
| **kW / kvar** | Real power (does work) and reactive power (needed by motors and magnetic equipment, but not consumed). |
| **Loading %** | Current on a wire as a percent of its rating. Over 100 % means overheating. |
| **Losses** | Power lost as heat in the wires. It grows with the square of the current. |
| **Case: operational / planning** | `operational` uses switches as they are right now (from SCADA); `planning` uses the as-designed normal positions. |
| **SCADA / telemetry** | Live measurements sent from the field: power, voltage, switch positions, tap positions. |

### Two families of tools

- **Database tools** answer from the grid records in Postgres: what is connected to what, where it is, what the meters
  read. Each call stands alone and is fast. Their results carry `"model": null`.
- **Engine tools** build an electrical simulation (OpenDSS) of one feeder and solve it: voltages, currents, losses,
  "what if" changes. You first `load_feeder`; later calls in the same session use that simulation. Nothing they do is
  written back to the grid records.

### What every result looks like

```json
{ "success": true,            // false if the tool failed or a power flow did not converge
  "data": { ... },            // the answer; database tools name a chart type in data.viz
  "model": { ... },           // which feeder simulation was used (engine tools only)
  "warnings": [ ... ],        // data-quality notes, e.g. stale switch states; not errors
  "errors": null,             // a code and message when success is false
  "meta": { "duration_ms": 41, "study_id": "...", ... } }
```

`data.viz` tells a client how to draw the result:

| `viz` | Shape |
| --- | --- |
| `table` | `columns` plus `rows` |
| `tree` | nested nodes (feeder hierarchy) |
| `path` | an ordered list of hops |
| `map` | GeoJSON features with coordinates |
| `timeseries` | one or more series of time/value points |
| `profile` | voltage against distance from the substation |

If a result is too large, it is cut off (`truncated: true`) and `resource` names a `grid://` address that holds the
whole thing.

---

## IDs to use

| Feeder | `region_code` | Root ID |
| --- | --- | --- |
| IEEE 13 | `IEEE13` | `c990acfa-4c84-5788-be92-53b20464fbd8` |
| IEEE 34 | `IEEE34` | `378451c2-afdb-524f-a587-9808c081df60` |
| IEEE 123 | `IEEE123` | `1dfeb41b-bf91-52f3-b6af-7b400b93c5b8` |
| SMART-DS P1R (17k nodes) | `P1R` | `d0c64dbe-07ab-5e7c-a7c8-20a79abe57ae` |

Useful IEEE 13 names: switch `671692`; regulators `REG1`, `REG2`, `REG3`; capacitors `CAP1`, `CAP2`; transformer
`XFM1`; buses `650`, `632`, `634`, `671`, `675`, `680`, `652`; loads `611`, `652`, `671`, `675A`, `692`.

Telemetry for IEEE 13 covers **2026-10-20 to 2026-11-11**. Dates outside that range return nothing.

In the examples, "root" means `c990acfa-4c84-5788-be92-53b20464fbd8`.

---

## 1. Resources (read-only documents)

Resources are documents you open rather than actions you run. Open them from the **Resources** tab.

| Address | What it shows |
| --- | --- |
| `grid://feeders` | Every feeder root: name, region, substation, voltage. The starting point for finding IDs. |
| `grid://feeder/c990acfa-4c84-5788-be92-53b20464fbd8/summary` | How many of each equipment type the feeder has. |
| `grid://feeder/c990acfa-4c84-5788-be92-53b20464fbd8/geojson` | The feeder as a map (lines and points with coordinates). |
| `grid://feeder/c990acfa-4c84-5788-be92-53b20464fbd8/tree` | The feeder as a tree from the head down. |
| `grid://feeder/c990acfa-4c84-5788-be92-53b20464fbd8/profile/2026-11-10` | That day's load curve at the feeder head. |
| `grid://session/current` | The feeder simulation loaded in your session (empty until `load_feeder`). |
| `grid://studies/recent?feeder=c990acfa-4c84-5788-be92-53b20464fbd8` | Recent saved studies on the feeder. |

**Prompts** tab: `daily_health_sweep` (region_code `IEEE13`) and `hosting_capacity_request` produce ready-made
instructions an AI assistant would follow. The Inspector only shows the text.

---

## 2. Database tools

Each one stands alone. Run them in any order.

### Finding equipment

**`search_equipment`**: find equipment by name, GIS ID or SCADA tag, like a search box.
- `query`: text to look for, e.g. `67`
- `region_code`: `IEEE13`
- `cim_class` (optional): limit to one type, e.g. `EnergyConsumer` (loads), `PowerTransformer`, `LoadBreakSwitch`
- `limit`: maximum matches
- Shows a table of matches, best first, with feeder, substation and a score (1.0 exact ID or tag, 0.9 exact name,
  lower for partial matches).

**`describe_equipment`**: everything known about one device.
- `mrid`: `REG1`
- `region_code`: `IEEE13`
- Shows its type and ratings, its container (substation, line), its feeder, the wiring (terminals and what else
  connects there), transformer windings, location, and every meter on it with the latest value, how old the value is,
  and whether it is stale.

### Following the wires

**`trace_downstream`**: everything fed from a point, as a tree.
- `mrid`: root (the whole feeder) or any device, e.g. `671692`
- `region_code`: `IEEE13`
- `case`: `operational` (switches as they are now) or `planning` (as designed)
- `detail`: `summary` (counts only), `aggregated` (default), `full` (every node)
- Shows the tree below that point with load, customer counts and length totals on each branch.

**`trace_upstream`**: the route from a device back to the substation.
- `mrid`: `675A`
- `region_code`: `IEEE13`
- Shows each hop toward the source with cumulative distance (km), the protective devices on the way (fuses, reclosers,
  breakers) flagged, and the total line impedance back to the source.

**`find_path`**: the electrical route between any two points.
- `from_mrid`: `652`
- `to_mrid`: `675A`
- Shows the ordered hops between them and their distances.

**`isolate`**: "if this switch or device is opened, who loses power?"
- `mrid`: `671692`
- Shows the section that goes dark, how many customers are cut off, and how many could be picked up again through a
  tie switch from a neighbouring feeder.

**`tie_options`**: normally-open switches that link this feeder to another, i.e. backup supply routes.
- `region_code`: `IEEE13`; `feeder_mrid`: root (or `section_root_mrid` for one section)
- Shows each tie with the feeder on its far side. IEEE 13 has none, so the list is empty.

**`connectivity_report`**: checks the wiring records for mistakes.
- `feeder_mrid`: root
- Shows islands (equipment connected to nothing upstream), dangling ends, switches with no recorded state time,
  segments with no length, and equipment with no location.

### Where things are

**`equipment_near`**: equipment within a radius of a map point.
- `lat`, `lon`: a point; take one from `feeder_geometry` output
- `radius_m`: search distance in metres
- Shows a map of the matches, nearest first, each with its distance.

**`nearest_substation`**: the closest substations to a point.
- `lat`, `lon`; `k`: how many
- Shows the substations with distances.

**`affected_by_area`**: what lies inside a drawn area (storm cell, wildfire perimeter, flood zone).
- `polygon`: a GeoJSON polygon, e.g. `{"type":"Polygon","coordinates":[[[lon,lat],[lon,lat],[lon,lat],[lon,lat]]]}`
- Shows equipment and customers inside, by feeder. The full list is stored under `grid://area/{id}`.

**`feeder_geometry`**: the feeder drawn as a map.
- `feeder_mrid`: root
- `detail`: how much to include; `simplify_m`: how much to smooth line shapes (metres)
- Shows GeoJSON lines and points.

### Meter readings

**`latest_readings`**: the newest value of every meter.
- `region_code`: `IEEE13`; optional `feeder_mrid` (root), `mrid` (one device) or `measurement_type`
  (`ThreePhaseActivePower`, `LineToLineVoltage`, `TapPosition`, `SwitchPosition`, ...)
- Shows a table of meters with value, unit, time, quality, age in seconds and a stale flag.

**`measurement_history`**: one or more meters over time.
- `measurement_mrids`: a JSON list of meter IDs (take them from `latest_readings`), e.g. `["..."]`
- `from`, `to`: e.g. `2026-11-09`, `2026-11-11` (at most 7 days)
- `stat`: `avg`, `min`, `max` or `last` for each 15-minute bucket
- Shows a time series per meter.

**`load_profile`**: the feeder's total demand through one day.
- `feeder_mrid`: root; `date`: `2026-11-10`
- Shows the head kW curve in 15-minute steps.

**`peak_load`**: the highest demand in a period and when it happened.
- `feeder_mrid`: root; `from`: `2026-10-20`; `to`: `2026-11-11`
- Shows the peak kW and its timestamp.

**`limit_exceedances`**: meters currently outside their allowed range.
- `region_code`: `IEEE13`; optional `feeder_mrid`
- Shows each meter, its value, the limit it broke, and by how much.

**`telemetry_quality`**: how trustworthy the meter data is.
- `feeder_mrid`: root; `days`: `30`
- Shows per-meter completeness (samples received against expected) and the share flagged bad.

### Record keeping

**`model_changes`**: what changed in the grid records since a date.
- `feeder_mrid`: root; `since`: `2026-10-01`
- Shows equipment added or edited, with timestamps.

**`validate_feeder`**: is the feeder's data good enough to simulate?
- `feeder_mrid`: root; `case`: `operational`
- Shows findings: loads with no voltage, wires using default impedance, stale switch states, missing locations.

---

## 3. Engine tools

Run `load_feeder` first. The rest use that simulation and must run in the same Inspector connection. Changes made by
`set_control` live only in the simulation; the database is never touched.

**`load_feeder`**: build the simulation of one feeder from the database.
- `root_mrid`: root
- `region_code`: `IEEE13`
- `case`: `operational` (today's switch positions and live loads) or `planning` (design positions)
- Shows a compile report: how many lines, transformers, loads were built; how many loads used live meter values,
  how many were estimated, how many fell back to defaults; any stale switches.

**`run_power_flow`**: solve the circuit: work out the voltage at every point and the current in every wire.
- `max_iterations`: solver attempt limit (default 100)
- `control_mode`: `static` lets regulators and capacitors react; `off` freezes them
- Shows whether it converged, the lowest and highest voltage (pu), total load (kW), losses (kW and kvar), and any
  overloaded wires.

**`check_voltages`**: list every point outside the voltage limits.
- `min_pu`: `0.95`; `max_pu`: `1.05`; `limit`: maximum rows
- Shows each bus and phase that is too low or too high, the worst one, and whether the feeder is compliant.

**`regulator_status`**: what the voltage regulators are doing.
- Shows each regulator's tap position in the simulation, its voltage setpoint and band, the latest tap reported by
  SCADA, and the difference between the two.

**`phase_balance`**: are the three phases carrying similar load?
- Shows kW on phases A, B and C at the head and on the 10 most uneven segments, with imbalance (%). Uneven phases
  waste capacity and heat motors.

**`losses_by_segment`**: which wires waste the most power.
- `top_n`: `10`
- Shows the worst segments with kW lost, loading, length, losses per km, and share of the feeder's total losses.

**`line_loading`**: how full each wire is.
- `threshold_pct`: show only lines above this loading, e.g. `50`
- `as`: `table` or `map`
- Shows lines with loading % (current against rating).

**`fault_study`**: how much current flows if a short circuit happens at a point. Used to check that fuses and breakers
are sized to trip.
- `bus`: `671`
- `fault_type`: `3ph` (all three phases shorted, the most severe), `SLG` (one phase to ground, the most common),
  `LL`, `LLG`
- `impedance_ohm`: resistance of the fault itself; `0` is a bolted fault
- Shows fault current per phase, voltage sag at the bus, and the protective devices between the bus and the substation.

**`analyze_capacity`**: hosting capacity: how much solar or battery could be connected at one point before something
breaks.
- `bus_mrid`: `675`
- `der_type`: `pv` (solar) or `storage`
- `step_kw`: `250`; `max_kw`: `3000`. The tool adds DER in steps up to the maximum.
- `constraints`: which limits to enforce: `overvoltage`, `thermal` (wire overload), `reverse_flow` (power flowing back
  into the substation)
- Shows the hosting capacity (kW), which limit stopped it, and the curve of voltage and loading at each step. The
  simulation is restored afterwards.

**`optimize_der`**: the best place to put a given amount of solar or storage.
- `capacity_kw`: `500`; `der_type`: `pv`
- `objective`: `losses` (cut wasted power) or `voltage` (raise the lowest voltage)
- `candidate_buses` (optional): JSON list of buses to try; default is all three-phase buses
- Shows candidate buses ranked, with losses and minimum voltage for each, and the recommended one.

**`set_control`**: change switches, taps or capacitors in the simulation (what-if).
- `actions`: a JSON list of changes:
  ```json
  [{"mrid":"671692","kind":"open"},{"mrid":"CAP1","kind":"cap","value":0}]
  ```
  - `kind`: `open` / `close` (switch), `tap` (regulator; `value` is steps from neutral, -16 to 16), `cap` (capacitor;
    `value` 1 on, 0 off)
- Shows what was applied and the new solved result. The session is now marked "modified"; run `run_power_flow` or
  `check_voltages` again to see the effect.

**`compare_cases`**: side-by-side before and after.
- `baseline`: `current` (the simulation as it stands), `operational` or `planning`
- `actions`: optional changes to apply to the comparison case, e.g. `[{"mrid":"REG1","kind":"tap","value":4}]`
- Shows both cases with differences in voltages, losses and loading.

**`run_timeseries`**: run the simulation through a day using recorded loads.
- `date`: `2026-11-10`; `interval`: `15m` or `1h`; `source`: `rollups` (15-minute averages) or `ami` (smart meters)
- Shows per step the min and max voltage and worst loading, plus energy lost over the day (kWh).

**`create_visualization`**: a chart of the latest results.
- `plot`: `voltage_profile` (voltage against distance), `loading`, `capacity_curve` (after `analyze_capacity`),
  `timeseries` (after `run_timeseries`)
- `top_n`: how many items to label
- `as`: `png` (an image) or `data` (the numbers, for a client to draw)

---

## Suggested walkthrough

1. Read `grid://feeders` to see what exists.
2. `trace_downstream` on root to see the tree; `describe_equipment` on `REG1`.
3. `latest_readings` (feeder_mrid root) and `load_profile` (`2026-11-10`) for live data.
4. `isolate` on `671692`: who would lose power.
5. `load_feeder` on root, then `run_power_flow`, then `check_voltages`.
6. `phase_balance` and `losses_by_segment` to find weak spots.
7. `analyze_capacity` at `675` for a solar hosting limit.
8. `set_control` to open `671692`, then `check_voltages` again: the loads beyond the switch drop out.
9. `compare_cases` with a `REG1` tap change against `operational`.
10. `create_visualization` with `voltage_profile`, `as` = `png`.
