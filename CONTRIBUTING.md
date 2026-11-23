# Contributing to feederline-mcp

Thanks for helping. Issues and pull requests are welcome.

## Developer Certificate of Origin

Every commit must be signed off, certifying the [Developer Certificate of Origin](https://developercertificate.org/):

```bash
git commit -s -m "compiler: emit split-phase loads"
```

This adds a `Signed-off-by: Your Name <you@example.com>` line. Pull requests with unsigned commits can't be merged.

## Development setup

Requires Docker, [uv](https://docs.astral.sh/uv/), Python 3.12 and Node.js 18+.

```bash
uv sync                  # Python env (add --extra kafka for the Kafka consumer tests)
make up && make load-ieee
make test                # full suite; the golden tests must stay within 0.1% of the IEEE published solutions
make lint                # ruff + import-linter
make npm-build           # build and test the npm CLI
```

`make test-unit` runs only the tests that need no database. Never weaken a test to make it pass.

## Code rules

- **CIM names, snake_case, `mrid` UUID keys.** One table per concrete CIM class; connectivity is `terminal` →
  `connectivity_node`; switch state is `switch.open`.
- **Adapter seam.** `feederline_mcp/tools/` and `feederline_mcp/compiler/` import only `feederline_mcp/adapters/base.py`.
  The import-linter contracts in `pyproject.toml` enforce this, and SQL tools never touch sessions or the engine.
- **Adapters plug in through entry points** (`feederline_mcp.adapters`), never through imports in the core.
- **Read-only against the model.** No tool writes to CIM tables, `switch.open` or telemetry; studies write only to `studies.*`.
- **One engine context per MCP session**; no module-level DSS globals.
- **Every tool returns the envelope.** Non-converged solves return `success=false`. Data-quality findings go in
  `warnings`, never raise.
- **No DSS text from clients.** Tool inputs are typed (Pydantic); DSS command strings are built only in `compiler/emit.py`.
- **Deterministic mRIDs in loaders**: `uuid5(NAMESPACE, f"{source}:{kind}:{id}")`. Never change `NAMESPACE`; the feeder
  IDs users quote depend on it (`tests/unit/test_stable_ids.py`).
- SQL lives in named constants with `%(name)s` parameters, never f-strings with values. Use `logging`, not `print`.
- Add `SPDX-License-Identifier: Apache-2.0` to new source files.

## Data

Use only public datasets. Never add real utility network data (CEII), Pecan Street data, or credentials to the
repository, images or test fixtures.

## Trademarks

"Kartha" and "Kartha Energy" are names controlled by Kartha Energy. The Apache-2.0 license does not grant rights to
them; don't use them to name or promote forks.
