# SPDX-License-Identifier: Apache-2.0
"""MCP tools. Each module exposes register(mcp, services).

tools/engine: compile a feeder into the session's OpenDSS context and study it (session lock, solves).
tools/sql: answer topology, spatial, lookup and telemetry questions from Postgres alone (no session, no engine).
"""

from . import engine, resources, sql

MODULES = (*engine.MODULES, *sql.MODULES, resources)


def register_all(mcp, services) -> None:
    for m in MODULES:
        m.register(mcp, services)
