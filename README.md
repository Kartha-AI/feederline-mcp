# feederline-mcp

Model Context Protocol (MCP) server for distribution power grid simulation, DER hosting capacity, and power flow analysis (powered by OpenDSS)

A CIM (IEC 61970-301) operational data store on PostgreSQL + PostGIS + pgRouting, plus a Model Context Protocol
server that compiles any distribution feeder from it into an in-process OpenDSS circuit, so AI agents such as Claude
can run power-flow, hosting-capacity, voltage, fault and time-series studies on a utility network model.

> **Public data only.** The local stack is for public datasets (IEEE test feeders, NREL SMART-DS, HIFLD-schema
> mirrors). Real utility network models and loading are CEII (Critical Energy/Electric Infrastructure Information)
> and must not be loaded into it.

## Quick start

You need **Docker** (Docker Desktop or Docker Engine with Compose v2) and **Node.js 18+**. Nothing else is installed on
the host: Postgres, PostGIS, pgRouting, OpenDSS and Python all run in containers.

```bash
npx -y @kartha-energy/feederline-mcp up       # pull images, start Postgres + server, migrate, load IEEE 13/34/123, seed demo telemetry
npx -y @kartha-energy/feederline-mcp config   # print the Claude Desktop and Claude Code connection settings
```

The server listens on `127.0.0.1` only, with a random bearer token generated on first run and kept in
`~/.feederline-mcp/config.json`.

### Connect Claude Desktop

Add this to `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`),
or run `npx -y @kartha-energy/feederline-mcp config --write-desktop`, which merges it in after writing a backup:

```json
{
  "mcpServers": {
    "feederline-mcp": {
      "command": "npx",
      "args": ["-y", "@kartha-energy/feederline-mcp", "connect"]
    }
  }
}
```

Restart Claude Desktop; the 34 tools appear in the tools menu. `connect` is a stdio bridge to the local server: it
reads the token from `~/.feederline-mcp/config.json` and starts the stack if it isn't running.

### Connect Claude Code

```bash
claude mcp add --transport http feederline-mcp http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer <token from: npx -y @kartha-energy/feederline-mcp config>"
```

Notes:

- The "custom connector" screen in Claude apps is for servers reachable over public HTTPS. For this local server, use
  the config file above.
- One Claude chat is one MCP session. A new chat starts with no feeder loaded, so call `load_feeder` again.
- Without the bridge, the community `mcp-remote` package works too; `config` prints that variant with your token.

## What you can ask

The [Claude playbook](docs/playbook.md) is a 13-lesson learning path for Claude Desktop: concepts, prompts to type,
what to look for in each answer, a prompt cheat sheet and a self-check quiz. A few prompts from it:

- "List the feeders available, with region, substation and nominal voltage."
- "Load the IEEE 123 feeder and run a power flow. What are the losses and the lowest voltage?"
- "Which buses on IEEE 123 are outside 0.95–1.05 pu? Show the worst ten."
- "What is the PV hosting capacity at bus 76 on IEEE 123, and which constraint binds first?"
- "What was the peak load at the IEEE 123 feeder head over the last 7 days, and when did it occur?"

## Tools

34 MCP tools in two families, every result in the same envelope (`success`, `data`, `model`, `warnings`, `errors`, `meta`):

- **Engine** (need `load_feeder` first): `load_feeder`, `run_power_flow`, `check_voltages`, `analyze_capacity`,
  `optimize_der`, `run_timeseries`, `create_visualization`, `set_control`, `compare_cases`, `fault_study`,
  `phase_balance`, `losses_by_segment`, `line_loading`, `regulator_status`.
- **SQL** (straight from Postgres, no session): lookup (`search_equipment`, `describe_equipment`), topology
  (`trace_downstream`, `trace_upstream`, `find_path`, `isolate`, `tie_options`, `connectivity_report`), spatial
  (`equipment_near`, `nearest_substation`, `affected_by_area`, `feeder_geometry`), telemetry (`latest_readings`,
  `measurement_history`, `load_profile`, `peak_load`, `limit_exceedances`, `telemetry_quality`), governance
  (`model_changes`, `validate_feeder`).

Results name their shape in `data.viz` (`map`, `tree`, `path`, `timeseries`, `table`, `profile`) so a client can draw
them directly. Large results are capped and point at a `grid://` resource holding the rest. Each tool's MCP
description (its docstring) states its inputs, outputs and units.

No tool writes to the network model, switch states or telemetry. `set_control` changes only the in-memory circuit of
your session.

## CLI

| Command | Does |
| --- | --- |
| `up [--ha] [--no-demo] [--smartds]` | Start the stack, migrate, load the IEEE feeders and 30 days of demo telemetry. Safe to rerun. |
| `down` / `reset` | Stop (keep data) / remove containers and data after confirmation |
| `status` | Containers, URL, feeders with root IDs, telemetry date range |
| `logs [service]` | Follow logs |
| `load ieee\|smartds\|hifld [...]` | Load a public dataset |
| `config [--write-desktop] [--claude-code]` | Connection settings |
| `connect` | stdio bridge for Claude Desktop |
| `inspect` | MCP Inspector against the local server |
| `doctor` | Check Node, Docker, Compose, ports, disk and registry access |

The IEEE root IDs are the same on every install (deterministic `uuid5` mRIDs):

| Feeder | Root ID |
| --- | --- |
| IEEE 13 | `c990acfa-4c84-5788-be92-53b20464fbd8` |
| IEEE 34 | `378451c2-afdb-524f-a587-9808c081df60` |
| IEEE 123 | `1dfeb41b-bf91-52f3-b6af-7b400b93c5b8` |

## Data

| Command | Loads |
| --- | --- |
| `load ieee` (run by `up`) | IEEE 13/34/123 test feeders (vendored under `tests/fixtures/feeders`) |
| `load smartds --region P1R --substation p1rhs0_1247 [--date YYYY-MM-DD]` | An NREL SMART-DS substation downloaded from OEDI at run time (~7 MB, 17k nodes); `--date` also ingests that day's load profiles |
| `load hifld --state CA` | HIFLD-schema substations and transmission lines (transmission tier, 500 m snapping) |
| `make load-pecan FILE=... REGION=IEEE123` (from source) | Your own Pecan Street Dataport 15-minute export, scaled onto a region's consumers. No Pecan Street data is distributed. |

Demo telemetry is simulated (deterministic, seed 42) for the 30 days before install.

## From source

Requires Docker, [uv](https://docs.astral.sh/uv/) and Python 3.12.

```bash
make up          # build images; postgres (127.0.0.1:5433), migrations, two MCP replicas behind nginx on :8000
make load-ieee   # IEEE 13/34/123 -> CIM, SCADA registry, latest values, network_edges
make demo        # 30 days of deterministic demo telemetry
make test        # unit, contract, golden (0.1%), loaders, writer, auth/RLS, tools, rehydration, spatial, SQL tools
make inspect     # MCP Inspector -> http://localhost:8000/mcp (Authorization: Bearer dev-token)
make ha-check    # stop the replica holding a session; the other must serve it
make lint        # ruff + import-linter (tools/ and compiler/ depend only on adapters/base.py)
```

`PG_PORT=` and `MCP_PORT=` move the host ports. The from-source stack uses the static token `dev-token`; the npm
path never does.

The npm CLI lives in [`npm/`](npm/) (`make npm-build`); its compose files are in `npm/compose/`.

### Adapters

The server reads a `DataSource`, writes a `StudySink` and trusts an `Auth` (protocols in
`feederline_mcp/adapters/base.py`). Implementations are discovered through the Python entry-point group
`feederline_mcp.adapters` and selected with `FEEDERLINE_MCP_ADAPTER` (default `local`: one Postgres, the `studies`
schema, a static bearer token). Another backend ships as a separate package that registers its own entry point:

```toml
[project.entry-points."feederline_mcp.adapters"]
mybackend = "my_package.adapter:build"   # build(settings) -> (DataSource, StudySink, Auth)
```

Any Postgres 16+ with PostGIS 3.4+ and pgRouting 3.4+ runs the schema. Sessions rehydrate on any replica from a
shared snapshot directory (`SNAPSHOT_DIR`).

## Docs

| Document | What it covers |
| --- | --- |
| [docs/playbook.md](docs/playbook.md) | Learning the grid with Claude Desktop: concepts, 13 lessons, cheat sheet, quiz |
| [tests/inspector.md](tests/inspector.md) | Exploring every tool with the MCP Inspector, field by field |

## Contributing, security, license

- [CONTRIBUTING.md](CONTRIBUTING.md): DCO sign-off (`git commit -s`), running the tests, code rules.
- [SECURITY.md](SECURITY.md): report vulnerabilities privately.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
- Licensed under the [Apache License 2.0](LICENSE). Third-party components and data: [NOTICE](NOTICE).

## Production and commercial support

For production deployments, other data platforms and support, see [kartha.energy](https://kartha.energy).
