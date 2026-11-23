# SPDX-License-Identifier: Apache-2.0
import pytest
from starlette.testclient import TestClient

from feederline_mcp import config
from feederline_mcp.adapters.base import Principal
from feederline_mcp.compiler import ScopeError, compile_feeder
from loaders.common import mrid
from tests.conftest import MCP_URL

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def client():
    import os

    os.environ["FEEDERLINE_MCP_NO_APP"] = "1"
    from feederline_mcp.app import create_app

    app = create_app(config.Settings(database_url=MCP_URL, bearer_token="t0k", auth_mode="on"))
    return TestClient(app)


def test_missing_token_is_401(client):
    r = client.post("/mcp", json={})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_403(client):
    assert client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"}).status_code == 403


def test_healthz_is_open(client):
    assert client.get("/healthz").json()["db"] == "ok"


def test_region_a_principal_cannot_scope_region_b_root(loaded, ds):
    a = Principal("a", ["IEEE13"])
    with pytest.raises(ScopeError):
        compile_feeder(ds, a, mrid("ieee123", "breaker", "head"), "operational", "IEEE13")
    with pytest.raises(ScopeError):
        compile_feeder(ds, a, mrid("ieee123", "breaker", "head"), "operational", "IEEE123")
    assert compile_feeder(ds, a, mrid("ieee13", "breaker", "head"), "operational", "IEEE13").model.counts


def test_rls_filters_rows(loaded, ds):
    rows = ds.query("SELECT DISTINCT region_code FROM conducting_equipment", {}, ["IEEE13"])
    assert [r["region_code"] for r in rows] == ["IEEE13"]
    assert ds.query("SELECT count(*) AS n FROM measurement", {}, []) == [{"n": 0}]
