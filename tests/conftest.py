# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os

import psycopg
import pytest

MCP_URL = os.environ.get("DATABASE_URL", "postgresql://mcp:mcp@localhost:5433/grid")
LOADER_URL = os.environ.get("LOADER_URL", "postgresql://grid_load:grid_load@localhost:5433/grid")
WRITER_URL = os.environ.get("WRITER_URL", "postgresql://grid_write:grid_write@localhost:5433/grid")


def _db_up() -> bool:
    try:
        with psycopg.connect(MCP_URL, connect_timeout=2):
            return True
    except psycopg.Error:
        return False


DB_UP = _db_up()


def pytest_collection_modifyitems(config, items):
    if DB_UP:
        return
    skip = pytest.mark.skip(reason="Postgres not reachable (make up && make load-ieee)")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def loaded():
    """Reload the IEEE feeders (idempotent) so seeded latest values are fresh for operational compiles."""
    from loaders import ieee

    return {n: ieee.load(n, LOADER_URL) for n in (13, 34, 123)}


@pytest.fixture(scope="session")
def ds():
    from feederline_mcp.adapters.local import LocalDataSource

    d = LocalDataSource(MCP_URL)
    yield d
    d.close()


@pytest.fixture(scope="session")
def services(ds, tmp_path_factory):
    from feederline_mcp.adapters.local import PostgresStudySink
    from feederline_mcp.sessions import SessionStore
    from feederline_mcp.tools._runner import Services

    return Services(ds, PostgresStudySink(ds), SessionStore(snapshot_dir=tmp_path_factory.mktemp("snapshots")))


@pytest.fixture(scope="session")
def sql_tool(services):
    """Call a SQL-family tool body through run_tool (no session): sql_tool(fn, InputCls, raw, principal=...)."""
    from feederline_mcp.adapters.base import Principal
    from feederline_mcp.tools._runner import run_tool

    def call(fn, cls, raw, principal=None, sid="sql-tests"):
        return run_tool(services, sid, principal or Principal("tester", ["*"]), fn.__name__, cls, raw, fn,
                        needs_model=False, needs_session=False)

    return call
