# SPDX-License-Identifier: Apache-2.0
"""Streamable HTTP session manager whose sessions survive a replica change or restart.

The SDK answers 404 for a Mcp-Session-Id it did not create. Behind a proxy that hashes on that header the
initialize request (no id yet) and the session's later requests can land on different replicas, and a restart
forgets every transport. This manager records each new session in the shared SessionStore and, for an unknown id
the store knows and that belongs to the same principal, opens a transport with that id in the initialized state;
the tools then rehydrate the engine context from the store's snapshot.
"""

from __future__ import annotations

import logging

import anyio
from anyio.abc import TaskStatus
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER, StreamableHTTPServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

from .sessions import SessionStore

log = logging.getLogger(__name__)


class RehydratingSessionManager(StreamableHTTPSessionManager):
    def __init__(self, *args, store: SessionStore, **kwargs):
        super().__init__(*args, **kwargs)
        self.store = store

    async def _handle_stateful_request(self, scope: Scope, receive: Receive, send: Send) -> None:
        sid = Request(scope).headers.get(MCP_SESSION_ID_HEADER)
        principal = scope.get("state", {}).get("principal")
        if sid is not None and sid not in self._server_instances:
            if await anyio.to_thread.run_sync(self.store.resumable, sid, principal):
                await self._resume(sid, scope, receive, send)
                return
        if sid is not None or principal is None:
            await super()._handle_stateful_request(scope, receive, send)
            return

        async def recording_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers") or [])
                new_id = headers.get(MCP_SESSION_ID_HEADER.encode())
                if new_id and message.get("status", 500) < 400:
                    await anyio.to_thread.run_sync(self.store.mark, new_id.decode(), principal)
            await send(message)

        await super()._handle_stateful_request(scope, receive, recording_send)

    async def _resume(self, sid: str, scope: Scope, receive: Receive, send: Send) -> None:
        async with self._session_creation_lock:
            transport = self._server_instances.get(sid)
            if transport is None:
                transport = StreamableHTTPServerTransport(
                    mcp_session_id=sid,
                    is_json_response_enabled=self.json_response,
                    event_store=self.event_store,
                    security_settings=self.security_settings,
                    retry_interval=self.retry_interval,
                    idle_timeout=self.session_idle_timeout,
                )
                self._server_instances[sid] = transport

                async def run_server(*, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
                    async with transport.connect() as (read_stream, write_stream):
                        task_status.started()
                        try:
                            async with anyio.create_task_group() as tg:
                                if transport.idle_scope is not None:

                                    async def end_when_idle(idle_scope: anyio.CancelScope) -> None:
                                        with idle_scope:
                                            await anyio.sleep_forever()
                                        await self._discard_session(sid, transport)

                                    tg.start_soon(end_when_idle, transport.idle_scope)
                                # stateless=True starts the server session initialized: the client already did that.
                                await self.app.run(read_stream, write_stream, self.app.create_initialization_options(),
                                                   stateless=True)
                                tg.cancel_scope.cancel()
                        except Exception:
                            log.exception("resumed session %s crashed", sid)
                        finally:
                            await self._discard_session(sid, transport)

                assert self._task_group is not None
                await self._task_group.start(run_server)
                log.info("resumed MCP session %s from the snapshot store", sid)
        await transport.handle_request(scope, receive, send)
        if transport.is_terminated:
            await self._discard_session(sid, transport)
