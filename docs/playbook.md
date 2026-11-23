# Claude playbook: learning the grid with feederline-mcp

A learning guide for exploring the feederline-mcp server from Claude Desktop. It is the companion to the
[Inspector guide](../tests/inspector.md): the Inspector guide tells you which form fields to fill, this playbook tells you
what to ask Claude, what the answer means, and what concept each step teaches.

Worked examples marked **Seen in session** are real results from one IEEE 123 session. Fresh installs may differ,
because demo telemetry is simulated. Prompts for IEEE 13 follow the Inspector guide and were not run for this
playbook, so your numbers will differ.

---

## Before you start

You need Docker and Node.js 18+.

```bash
npx -y @kartha-energy/feederline-mcp up       # starts the stack, loads IEEE 13/34/123 and 30 days of demo telemetry
npx -y @kartha-energy/feederline-mcp config   # prints the Claude Desktop settings
```

Add the printed `feederline-mcp` entry to Claude Desktop's config (or run `config --write-desktop`), then restart
Claude Desktop. The server's 34 tools appear in the tools menu. Connection details for Claude Desktop and Claude Code
are in the [README](../README.md#quick-start).

`npx -y @kartha-energy/feederline-mcp status` shows the loaded feeders and the telemetry date range. Add
`up --smartds` to also load the SMART-DS P1R feeder used at the end of the learning path.

---

## 1. How working in Claude differs from the Inspector

| In the Inspector | In Claude Desktop |
| --- | --- |
| You pick a tool and fill a form. | You ask in plain language; Claude picks the tool and fills the inputs. |
| You read raw JSON. | Claude reads the JSON and gives you the finding, a table, a chart or a map. |
| One connection is one session. | One chat is one session. The loaded feeder is remembered for the chat. |
| You must know the tool name. | Naming the tool is optional. "run_power_flow with defaults" and "solve the feeder" both work. |
| Nothing is interpreted. | Claude flags oddities, but check its reasoning against the numbers it shows you. |

Three habits make Claude sessions go well:

1. **Name the feeder once, early.** Give the region and root ID (or just "IEEE 13"). Engine tools need `load_feeder`
   first, and a new chat starts with nothing loaded.
2. **Say which device or bus you mean.** "Describe the equipment" or "analyze capacity" without a target forces Claude
   to guess. In the session it guessed the feeder head both times, which is a weak place to test hosting capacity.
3. **Ask for the form you want.** "As a table", "on the map", "plot it". Claude draws charts and maps itself from the
   tool's data; the server's PNG output is not shown in the chat.

---

## 2. Concepts and definitions

### 2.1 The grid is a tree

Power enters at the top and branches out to customers.

```
Region                    a security boundary: who may see which part of the grid
 └─ Substation            where transmission voltage is stepped down for local distribution
     └─ Feeder head       the breaker at the start of one feeder; the root of the tree
         ├─ Line segments wires between junctions; they have length, impedance and a current rating
         ├─ Regulators    transformers that move voltage up or down in small steps (taps)
         ├─ Transformers  step voltage down again, for example to a building
         ├─ Switches      open to cut a section off, close to reconnect it
         ├─ Capacitors    supply reactive power locally, which lifts voltage
         └─ Loads         customers, or groups of customers, drawing power
```

Why the tree matters: almost every question is either "what is below this point?" (downstream) or "how does power
get to this point?" (upstream). A closed tie switch turns the tree into a loop, which changes how current divides and
is usually not the normal operating state.

### 2.2 Identity and location

| Term | Definition | Why you care |
| --- | --- | --- |
| **Region** (`region_code`) | A slice of the grid a user is allowed to see. | Every tool asks for it. |
| **Feeder** | Everything downstream of one feeder-head breaker. | Most tools work on one feeder. |
| **Root / feeder mRID** | The ID of the feeder-head breaker. | Passing it means "this whole feeder". |
| **mRID** | The permanent unique ID (a UUID) of a piece of equipment. | Names such as `REG1` also work where a tool takes `mrid`. |
| **Bus / node** | A junction where equipment connects. | Voltages are reported per bus and phase. |
| **CIM class** | The equipment type in the data model: `ACLineSegment`, `Breaker`, `EnergyConsumer` (load), `LinearShuntCompensator` (capacitor), `LoadBreakSwitch`, `PowerTransformer`. | Used to filter searches and read compile reports. |
| **Terminal** | A connection point on a device. A breaker has two. | `describe_equipment` shows what else sits on each terminal. |

### 2.3 Electrical quantities

| Term | Definition | Rule of thumb |
| --- | --- | --- |
| **Phase (A, B, C)** | Three-phase power runs on three wires. Laterals may carry one or two. | Uneven phases waste capacity. |
| **Nominal kV** | The design voltage of a section. | IEEE 123 runs at 4.16 kV with 0.48 kV on a transformer secondary. |
| **Per unit (pu)** | Voltage as a fraction of nominal. | 1.00 is nominal. Keep customers between 0.95 and 1.05 (ANSI Range A). |
| **kW** | Real power; does work. | What customers are billed for. |
| **kvar** | Reactive power; needed by motors and magnetic equipment, not consumed. | Capacitors supply it locally. |
| **Loading %** | Current on a wire as a percent of its rating. | Over 100 % means overheating, if the rating is right. |
| **Losses** | Power lost as heat in the wires. | Grows with the square of the current. |
| **Tap** | One step of a regulator, from -16 to +16 around neutral. | Each step moves voltage by a small fixed amount. |

### 2.4 Data and study terms

| Term | Definition |
| --- | --- |
| **Case: operational** | Switches as they are now and measured loads, from SCADA. |
| **Case: planning** | As-designed normal switch positions and nameplate loads. |
| **SCADA / telemetry** | Live measurements from the field: power, voltage, switch and tap positions. |
| **Stale** | A reading older than expected (more than two scan intervals for switches). The value may no longer be true. |
| **Measured / allocated / defaulted loads** | How each load got its value at compile time: from its own meter, scaled from the feeder-head reading, or from nameplate. |
| **Snapshot power flow** | Solve the circuit for one instant: voltage at every bus, current in every wire. |
| **Converged** | The solver found a consistent answer. If it did not, do not trust any number from that run. |
| **Hosting capacity** | How much solar or storage a point can take before a limit is broken. |
| **Binding constraint** | The limit that stopped the hosting-capacity sweep: overvoltage, thermal or reverse flow. |
| **Reverse flow** | Power flowing from the feeder back into the substation. |
| **Fault types** | `3ph` all three phases shorted (most severe), `SLG` one phase to ground (most common), `LL`, `LLG`. |
| **Tie switch** | A normally-open switch linking two feeders or two parts of one feeder; a backup supply route. |

### 2.5 Two families of tools

- **Database tools** answer from grid records: what is connected to what, where it is, what the meters read. Each
  call stands alone. Their results carry `"model": null`.
- **Engine tools** build an OpenDSS simulation of one feeder and solve it. You load the feeder first; later calls in
  the same chat use that simulation. Nothing is written back to the grid records.

A useful test: if the question is about *records or readings*, it is a database tool. If it is about *what the
physics would do*, it is an engine tool.

### 2.6 Reading a result

Every tool returns the same envelope. Ask Claude to "show the raw result" if you want to see it.

| Field | What to check |
| --- | --- |
| `success` | False if the tool failed or a power flow did not converge. |
| `data` | The answer. `data.viz` names the shape: `table`, `tree`, `path`, `map`, `timeseries`, `profile`. |
| `model` | Which simulation was used. `null` for database tools. `modified: true` means `set_control` has changed it. |
| `warnings` | Data-quality notes. Not errors, but they decide how far to trust the answer. |
| `errors` | Code and message when `success` is false. |
| `meta` | Duration, session ID and `study_id` for saved studies. |

Large results are saved in full and summarised; Claude works from the saved copy, so you can still ask for detail.

---

## 3. IDs to use

| Feeder | `region_code` | Root ID |
| --- | --- | --- |
| IEEE 13 | `IEEE13` | `c990acfa-4c84-5788-be92-53b20464fbd8` |
| IEEE 34 | `IEEE34` | `378451c2-afdb-524f-a587-9808c081df60` |
| IEEE 123 | `IEEE123` | `1dfeb41b-bf91-52f3-b6af-7b400b93c5b8` |
| SMART-DS P1R (17k nodes, `up --smartds`) | `P1R` | `d0c64dbe-07ab-5e7c-a7c8-20a79abe57ae` |

IEEE 13 names: switch `671692`; regulators `REG1`, `REG2`, `REG3`; capacitors `CAP1`, `CAP2`; transformer `XFM1`;
buses `650`, `632`, `634`, `671`, `675`, `680`, `652`; loads `611`, `652`, `671`, `675A`, `692`.

Demo telemetry covers the 30 days before you ran `up` (`status` shows the exact range). Dates outside that range
return nothing.

Start with IEEE 13: it is small enough to read every line. Move to IEEE 123 when you want regulators in series,
several capacitors and switches to play with. Leave P1R until you are comfortable, because results are large.

---

## 4. Learning path

Each lesson has a concept, a prompt to type into Claude, and what to look for in the answer.

### Lesson 1: Find your way around

**Concept:** the hierarchy of region, substation, feeder, equipment.

> List the feeders available, with region, substation and nominal voltage.

> Trace downstream from the IEEE 13 feeder head and show me the tree.

> Describe REG1 in IEEE13.

Look for: the equipment counts, which devices sit on each terminal, and the age of each measurement.

**Seen in session (IEEE 123):** the head is a closed three-phase breaker at 4.16 kV. Its second terminal connects to
node 150, shared with regulator REG1A. It carries four measurements (voltage, kW, kvar, switch position), all from
the estimator and all about 8 days old.

Lesson learned: `describe_equipment` describes *one device*. Ask for a device by name.

### Lesson 2: See the feeder on a map

**Concept:** geometry, three-phase mains versus single-phase laterals.

> Show me the map of IEEE 13, with switches, regulators and capacitors marked.

Look for: where the thick three-phase trunk runs, where laterals branch, and where voltage-support devices sit
relative to the far ends.

**Seen in session (IEEE 123):** 11.88 km of line, 118 segments, of which 60 are three-phase. All 8 switches were
reported closed and normally closed. In the standard IEEE 123 test case two of them are open, so the model may be
running with closed loops. This is exactly the kind of thing a map makes visible and a table hides.

### Lesson 3: Read the meters

**Concept:** telemetry, quality and staleness.

> Show the latest readings for IEEE 13 and flag anything stale.

> Plot the IEEE 13 load profile for yesterday.

> What was the peak load on IEEE 13 over the last 30 days, and when?

> How complete is the telemetry on IEEE 13 over the last 30 days?

Look for: the daily load shape, the gap between peak and minimum, and which meters you would not trust.

### Lesson 4: What happens if a switch opens

**Concept:** isolation, customers affected, backup supply.

> If switch 671692 opens, who loses power? Are there tie options to pick them back up?

Look for: the section that goes dark and the customer count. IEEE 13 has no ties, so nothing can be restored.

### Lesson 5: Build the simulation

**Concept:** compiling a model, operational versus planning, how loads get their values.

> Load the IEEE 13 feeder as an operational case and summarise the compile report.

Look for: counts by equipment type, how many loads were measured, allocated or defaulted, and the warnings.

**Seen in session (IEEE 123):** 133 buses, 118 line segments, 91 loads, 8 transformers, 8 switches, 4 capacitors,
1 breaker. All 91 loads were defaulted to nameplate, the source was set to 1.0 pu, and all 8 switches were stale.
So the "operational" case was really a nameplate case.

Follow-up worth asking:

> Reload it as a planning case and tell me what changed in the compile report.

### Lesson 6: Solve it

**Concept:** power flow, voltage limits, losses, overloads.

> Run a power flow with defaults. Then check voltages against 0.95 and 1.05 pu.

Look for: convergence first, then minimum and maximum voltage, losses as a share of load, and overloaded branches.

**Seen in session (IEEE 123):** converged in 3 iterations. Head load 3,615.3 kW; losses 96.1 kW and 192.5 kvar,
about 2.7 % of head load. Voltage ranged from 0.9792 pu to 1.0499 pu. Five trunk branches were overloaded, the worst
at 157.9 % (L115 and SW1).

Lesson learned: an overload is only as real as the rating behind it. The same 400 A rating appeared on the head
switch and on a single-phase lateral, which suggests a placeholder. Ask:

> Show the ratings and phase currents for the overloaded branches. Are the ratings real or defaults?

### Lesson 7: Voltage along the feeder

**Concept:** voltage drop with distance, what regulators and capacitors do to the profile.

> Plot the voltage profile by phase against distance from the head.

> Show regulator status: taps in the simulation against the latest SCADA taps.

Look for: the step up at each regulator, the downward slope between them, and how far apart the phases are.

**Seen in session (IEEE 123):** voltage stepped from 1.000 to 1.0375 pu right after the head regulator. Phase A
sagged fastest and bottomed out at 0.9792 pu at bus 65, about 1.29 km out. Phase B peaked at 1.0499 pu at bus 83,
about 1.68 km out, where a three-phase capacitor sits. From about 0.93 km there were two bands of nodes: one sagging
near 0.98 to 1.00 pu and one near 1.04 pu, consistent with a mid-feeder regulator boosting part of the feeder.

### Lesson 8: Find the weak spots

**Concept:** phase imbalance and where losses concentrate.

> Show phase balance at the head and the ten most uneven segments.

> Which ten segments lose the most power, and what share of total losses is each?

> Show line loading above 50 % on the map.

Look for: whether losses and loading point at the same few segments. They usually do, near the head.

### Lesson 9: Hosting capacity

**Concept:** how much solar a point can take, and which limit stops it.

> Analyze PV hosting capacity at bus 675 on IEEE 13, 250 kW steps up to 3,000 kW, all three constraints.

Look for: the capacity in kW, the binding constraint, and the curve of maximum voltage and loading per step.

**Seen in session (IEEE 123):** at the feeder head (bus 150) capacity was 3,500 kW, bound by reverse flow. The head
flow went negative at the 3,750 kW step. Maximum voltage stayed near 1.0500 pu and maximum loading stayed at 157.9 %
for every step.

Lessons learned:

- **Location decides the answer.** PV at the head only offsets head flow, so voltage and loading barely respond.
  Test a bus far from the head to see overvoltage or thermal limits bind.
- **Step size sets the resolution.** The true limit was near 3,615 kW, the feeder's own load; 3,500 kW was the last
  step that passed.
- **Load level matters.** Minimum load, not nameplate load, is what usually limits PV.
- **Pre-existing violations are not counted.** The 158 % overload was already there, so the thermal check counted
  only DER-driven increases.

Then let the tool choose the location:

> Where is the best place for 500 kW of PV on this feeder to cut losses? And to raise the lowest voltage?

### Lesson 10: What-if changes

**Concept:** controls, and comparing before and after.

> In the simulation, open switch 671692, then check voltages again.

> Compare the current case against one where REG1 is raised 4 taps.

> Switch CAP1 off and show me what happens to the lowest voltage and to losses.

Look for: `modified: true` in the model block after a change. Changes live only in the simulation. To get back to a
clean state, ask Claude to reload the feeder.

### Lesson 11: Faults

**Concept:** short-circuit current and protection.

> Run a bolted three-phase fault at bus 671 and list the protective devices back to the substation.

> Now a single-line-to-ground fault at the same bus. How do the currents compare?

Look for: fault current falling as you move away from the source, and SLG current below three-phase current.

### Lesson 12: A day in the life

**Concept:** time-series simulation with recorded loads.

> Run a time series for IEEE 13 for yesterday at 15-minute steps and plot minimum and maximum voltage through the day.

Look for: when the lowest voltage and worst loading occur, and the energy lost over the day in kWh.

### Lesson 13: Is the data good enough?

**Concept:** model validation before you trust any study.

> Validate the IEEE 13 feeder for an operational study and give me a connectivity report.

> What changed in the IEEE 13 model in the last 30 days?

Look for: islands, dangling ends, default impedances, stale switch states and missing locations. Run this lesson
*before* a study you intend to act on.

---

## 5. Prompt cheat sheet

### Database tools

| You want | Ask Claude | Tool |
| --- | --- | --- |
| Find a device | "Search IEEE13 for equipment matching 67" | `search_equipment` |
| Everything about a device | "Describe REG1 in IEEE13" | `describe_equipment` |
| What is fed from here | "Trace downstream from 671692" | `trace_downstream` |
| Route back to the source | "Trace upstream from load 675A" | `trace_upstream` |
| Route between two points | "Find the path from 652 to 675A" | `find_path` |
| Outage impact | "Who loses power if 671692 opens?" | `isolate` |
| Backup routes | "What tie options does this feeder have?" | `tie_options` |
| Wiring errors | "Give me a connectivity report" | `connectivity_report` |
| What is nearby | "What equipment is within 200 m of this point?" | `equipment_near` |
| Closest substations | "Nearest three substations to this point" | `nearest_substation` |
| Storm or fire impact | "What is inside this polygon?" | `affected_by_area` |
| The feeder as a map | "Show me the map" | `feeder_geometry` |
| Newest meter values | "Latest readings on this feeder" | `latest_readings` |
| A meter over time | "Plot these meters for the last 3 days" | `measurement_history` |
| Daily demand curve | "Load profile for yesterday" | `load_profile` |
| Highest demand | "Peak load between two dates" | `peak_load` |
| Meters out of range | "Any limit exceedances?" | `limit_exceedances` |
| Meter data quality | "Telemetry quality over 30 days" | `telemetry_quality` |
| Record changes | "What changed in the last 30 days?" | `model_changes` |
| Ready to simulate? | "Validate this feeder" | `validate_feeder` |

### Engine tools (load a feeder first)

| You want | Ask Claude | Tool |
| --- | --- | --- |
| Build the simulation | "Load IEEE 13, operational case" | `load_feeder` |
| Solve it | "Run a power flow" | `run_power_flow` |
| Voltage violations | "Check voltages against 0.95 and 1.05" | `check_voltages` |
| Regulator taps | "Regulator status" | `regulator_status` |
| Phase imbalance | "Phase balance" | `phase_balance` |
| Where losses are | "Top ten segments by losses" | `losses_by_segment` |
| How full the wires are | "Line loading above 50 %, on the map" | `line_loading` |
| Short-circuit current | "Three-phase fault at 671" | `fault_study` |
| Solar limit at a bus | "PV hosting capacity at 675" | `analyze_capacity` |
| Best DER location | "Best bus for 500 kW PV to cut losses" | `optimize_der` |
| Change a control | "Open 671692 in the simulation" | `set_control` |
| Before and after | "Compare against REG1 at +4 taps" | `compare_cases` |
| A full day | "Time series for yesterday" | `run_timeseries` |
| A chart | "Plot the voltage profile" | `create_visualization` |

---

## 6. Habits that keep you honest

- **Read the warnings before the result.** In the session every study ran on nameplate loads, a 1.0 pu source and
  stale switch states. The numbers were correct for that model and not necessarily for the field.
- **Check convergence first.** A run that did not converge has no usable numbers.
- **Question ratings.** Identical ratings across very different conductors usually mean defaults.
- **Question topology.** If every switch is closed, ask whether any should be normally open.
- **Name the bus for capacity and fault studies.** The answer is about that point, not the feeder.
- **Know which case you are in.** Operational with no fresh telemetry behaves like planning.
- **Remember the simulation is a sandbox.** `set_control` never touches the grid records. Reload to reset.
- **Ask Claude to show its working.** "Which numbers is that based on?" is always a fair question.

---

## 7. Check your understanding

1. Voltage at a bus reads 1.0375 pu on a 4.16 kV system. What is it in kV, and is it within ANSI Range A?
2. A compile report says 0 measured, 0 allocated, 91 defaulted loads. What does that tell you about a hosting-capacity
   result from that model?
3. Hosting capacity at the feeder head came out equal to roughly the feeder's own load, bound by reverse flow. Why,
   and why would a remote bus likely give a different binding constraint?
4. Losses were 96.1 kW on a 3,615.3 kW head load. If current doubled everywhere, roughly what would losses become?
5. Two buses at the same distance from the head show 0.98 pu and 1.04 pu. Name one piece of equipment that could
   explain it and the tool you would use to confirm.
6. A line shows 158 % loading. Name two checks to make before calling it a real overload.
7. Which family of tool answers "who loses power if this switch opens?", and why does it not need `load_feeder`?
8. After `set_control`, the model block shows `modified: true`. What has changed in the grid database?

**Answers**

1. About 4.316 kV (1.0375 × 4.16). Yes, it is between 0.95 and 1.05.
2. Every load is at nameplate, so the result reflects nameplate load. Minimum measured load would usually give a
   lower, more realistic limit.
3. PV at the head only cancels head flow; once it exceeds feeder load, power flows back. Voltage and line loading
   downstream hardly change. At a remote bus, injected power raises local voltage and loads the lines back toward
   the head, so overvoltage or thermal is more likely to bind.
4. About four times, roughly 384 kW, because losses grow with the square of current.
5. A regulator between the source and the higher-voltage bus (or a capacitor near it). Confirm with
   `regulator_status`, or `trace_upstream` from each bus.
6. Whether the rating is real or a default, and whether the topology (switch states) and loads behind the result are
   current.
7. A database tool (`isolate`). It answers from connectivity records, not from a solved circuit.
8. Nothing. Changes live only in the simulation for that session.
