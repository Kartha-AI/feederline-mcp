# SPDX-License-Identifier: Apache-2.0
"""Shared tool plumbing: session lookup, lock, timing, envelope, study record, one log line per call.

Engine tools (needs_session=True) run under the session lock on the session's engine context. SQL tools
(needs_session=False) take no lock, create no context and keep no request_id cache: their body receives a Call
(data source, sink, principal) and the envelope carries model=None and meta.engine=None.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import anyio
from pydantic import BaseModel, ValidationError

from .. import engine
from ..adapters.base import ALL_REGIONS, DataSource, Principal, StudyResult, StudySink
from ..sessions import Session, SessionStore
from ..studies import ErrorInfo, Meta, ToolError, envelope, model_info

log = logging.getLogger("feederline_mcp.tools")


class AreaCache:
    """Last affected_by_area result per (MCP session, polygon hash); in memory, bounded, idle entries expire."""

    def __init__(self, max_entries: int = 256, ttl_s: float = 1800):
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._items: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def put(self, session_id: str, key: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._items.pop((session_id, key), None)
            self._items[(session_id, key)] = (time.monotonic(), data)
            while len(self._items) > self.max_entries:
                self._items.pop(next(iter(self._items)))

    def get(self, session_id: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            hit = self._items.get((session_id, key))
            if hit is None or time.monotonic() - hit[0] > self.ttl_s:
                self._items.pop((session_id, key), None)
                return None
            return hit[1]


@dataclass
class Services:
    ds: DataSource
    sink: StudySink
    store: SessionStore
    sink_pool: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(1, thread_name_prefix="sink"))
    areas: AreaCache = field(default_factory=AreaCache)
    cache: dict[Any, Any] = field(default_factory=dict)  # resource caches keyed by content version

    def submit(self, fn: Callable[..., None], *args: Any) -> None:
        def run() -> None:
            try:
                fn(*args)
            except Exception:
                log.exception("study sink write failed")

        self.sink_pool.submit(run)


@dataclass
class Call:
    """What a SQL tool body gets instead of a session."""

    ds: DataSource
    sink: StudySink
    principal: Principal
    session_id: str
    areas: AreaCache
    cache: dict[Any, Any]


@dataclass
class Output:
    data: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    record: bool = False  # write a studies.results row


def session_id(ctx: Any) -> str:
    req = getattr(ctx.request_context, "request", None)
    sid = req.headers.get("mcp-session-id") if req is not None else None
    return sid or f"local-{id(ctx.request_context.session):x}"


def principal_of(ctx: Any) -> Principal:
    req = getattr(ctx.request_context, "request", None)
    p = getattr(getattr(req, "state", None), "principal", None) if req is not None else None
    return p or Principal("local", [ALL_REGIONS])


def _buses(s: Session) -> int:
    return sum(1 for k in s.names if k.startswith("cn_"))


def _rows(data: dict[str, Any] | None) -> int:
    if not data:
        return 0
    for k in ("rows", "features", "nodes", "hops", "points", "series"):
        if isinstance(data.get(k), list):
            return len(data[k])
    return 0


async def invoke(services: Services, ctx: Any, tool: str, input_cls: type[BaseModel], raw: dict[str, Any],
                 body: Callable[[Any, Any], Output], *, needs_model: bool = True,
                 needs_session: bool = True) -> dict[str, Any]:
    sid, principal = session_id(ctx), principal_of(ctx)
    return await anyio.to_thread.run_sync(
        lambda: run_tool(services, sid, principal, tool, input_cls, raw, body, needs_model=needs_model,
                         needs_session=needs_session)
    )


def run_tool(services: Services, sid: str, principal: Principal, tool: str, input_cls: type[BaseModel],
             raw: dict[str, Any], body: Callable[[Any, Any], Output], *, needs_model: bool = True,
             needs_session: bool = True) -> dict[str, Any]:
    t0 = time.perf_counter()
    if needs_session:
        engine.prepare_thread()
    s: Session | None = None
    study_id = None
    out: Output | None = None
    errors: list[ErrorInfo] | None = None
    inputs: BaseModel | None = None
    request_id = raw.get("request_id")
    try:
        try:
            inputs = input_cls.model_validate(raw)
        except ValidationError as e:
            raise ToolError("invalid_input", "; ".join(
                f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())) from None
        if needs_session:
            s = services.store.for_ctx(sid, principal)
            if request_id and request_id in s.requests:
                return s.requests[request_id]
            with services.store.acquire(s):
                if needs_model:
                    s.require_model()
                out = body(s, inputs)
        else:
            out = body(Call(services.ds, services.sink, principal, sid, services.areas, services.cache), inputs)
    except ToolError as e:
        errors = [ErrorInfo(code=e.code, message=str(e), **e.extra)]
    except Exception as e:  # never leak a stack trace to the client
        log.exception("tool %s failed", tool)
        errors = [ErrorInfo(code="internal", message=f"{type(e).__name__}: {e}")]
    duration_ms = int((time.perf_counter() - t0) * 1000)
    success = errors is None and out is not None
    if success and out.record and s is not None and s.model is not None:
        study_id = uuid.uuid4()
        services.submit(services.sink.write_result, StudyResult(
            study_id=study_id, model_id=s.model.model_id, tool=tool, inputs=inputs.model_dump(mode="json") if inputs else raw,
            data=out.data, warnings=out.warnings, duration_ms=duration_ms,
        ))
    env = envelope(
        success=success,
        data=out.data if out else None,
        model=model_info(s.model, _buses(s)) if s is not None else None,
        warnings=out.warnings if out else [],
        errors=errors,
        meta=Meta(session_id=sid, study_id=str(study_id) if study_id else None,
                  engine=engine.version() if needs_session else None,
                  duration_ms=duration_ms, source=services.ds.source_name),
    ).model_dump(mode="json")
    if s is not None:
        s.remember(request_id, env)
        s.tool_log.append({"tool": tool, "ts": time.time(), "success": success, "duration_ms": duration_ms})
    services.store.metrics[f"tool_calls{{tool=\"{tool}\",success=\"{str(success).lower()}\"}}"] += 1
    services.store.metrics[f"tool_duration_ms_sum{{tool=\"{tool}\"}}"] += duration_ms
    log.info(json.dumps({
        "tool": tool, "session": sid, "principal": principal.name, "duration_ms": duration_ms, "success": success,
        "counts": {**({"buses": _buses(s)} if s else {}), "rows": _rows(env["data"]),
                   "truncated": bool((env["data"] or {}).get("truncated"))},
        "warnings": len(env["warnings"]),
        "error": errors[0].code if errors else None,
    }))
    return env
