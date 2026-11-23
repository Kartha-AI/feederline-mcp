# SPDX-License-Identifier: Apache-2.0
"""FastMCP app over Streamable HTTP. uvicorn feederline_mcp.app:app --host 0.0.0.0 --port 8000 --workers 1"""

from __future__ import annotations

import json
import logging
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from . import __version__, config, engine
from .adapters.base import Auth, AuthError
from .sessions import SessionStore
from .tools import register_all
from .tools._runner import Services
from .transport import RehydratingSessionManager

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
log = logging.getLogger("feederline_mcp")

OPEN_PATHS = ("/healthz", "/metrics")
MIGRATION = "SELECT change FROM sqitch.changes ORDER BY committed_at DESC LIMIT 1"
FEEDER_COUNT = """
SELECT count(*) AS n FROM conducting_equipment ce JOIN voltage_level vl ON vl.mrid = ce.equipment_container_mrid
WHERE ce.cim_class IN ('Breaker', 'Recloser') AND ce.valid_to IS NULL
"""
SERVED_BY = os.environ.get("HOSTNAME", "local").encode()


class AuthMiddleware:
    """Resolve the Principal for every request; 401/403 before the MCP layer sees it."""

    def __init__(self, app, auth: Auth):
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS:
            return await self.app(scope, receive, send)
        try:
            principal = self.auth.principal(Request(scope))
        except AuthError as e:
            body = json.dumps({"error": "unauthorized" if e.status == 401 else "forbidden", "message": str(e)}).encode()
            headers = [(b"content-type", b"application/json")]
            if e.status == 401:
                headers.append((b"www-authenticate", b"Bearer"))
            await send({"type": "http.response.start", "status": e.status, "headers": headers})
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["principal"] = principal

        async def tagged_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-served-by", SERVED_BY)]
            await send(message)

        return await self.app(scope, receive, tagged_send)


def create_app(settings: config.Settings | None = None):
    settings = settings or config.Settings()
    ds, sink, auth = config.build(settings)
    store = SessionStore(settings.max_contexts, settings.idle_ttl_s, settings.max_concurrent_solves, settings.snapshot_dir,
                         settings.snapshot_ttl_s)
    services = Services(ds=ds, sink=sink, store=store)
    mcp = FastMCP(
        "feederline-mcp", stateless_http=False, host="0.0.0.0",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    register_all(mcp, services)
    # Sessions survive a replica change or restart: see transport.RehydratingSessionManager.
    mcp._session_manager = RehydratingSessionManager(
        app=mcp._mcp_server, json_response=mcp.settings.json_response, stateless=False,
        security_settings=mcp.settings.transport_security, session_idle_timeout=mcp.settings.session_idle_timeout,
        max_request_body_size=mcp.settings.max_request_body_size, max_sessions=mcp.settings.max_sessions, store=store,
    )

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        body = {"status": "ok", "version": __version__, "engine": engine.version(), "adapter": settings.adapter,
                "sessions": len(store), "db": "ok", "migration": None, "feeders": None}
        try:
            ds.query("SELECT 1 AS ok", {}, ["*"])
        except Exception as e:  # report, don't crash the probe
            body.update(status="degraded", db=f"error: {type(e).__name__}")
            return JSONResponse(body, status_code=503)
        try:
            row = ds.query(MIGRATION, {}, ["*"])
            body["migration"] = row[0]["change"] if row else None
        except Exception:  # registry not readable (e.g. migrations not run yet)
            body["migration"] = None
        try:
            body["feeders"] = ds.query(FEEDER_COUNT, {}, ["*"])[0]["n"]
        except Exception:
            body["feeders"] = None
        return JSONResponse(body)

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics(request: Request) -> PlainTextResponse:
        store.evict_idle()
        lines = [f"feederline_mcp_sessions {len(store)}"]
        lines += [f"feederline_mcp_{k} {v}" for k, v in sorted(store.metrics.items())]
        return PlainTextResponse("\n".join(lines) + "\n")

    app = mcp.streamable_http_app()
    app.add_middleware(AuthMiddleware, auth=auth)
    app.state.services = services
    return app


app = create_app() if os.environ.get("FEEDERLINE_MCP_NO_APP") != "1" else None
