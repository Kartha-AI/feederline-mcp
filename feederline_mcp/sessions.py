# SPDX-License-Identifier: Apache-2.0
"""One dss_python context per MCP session; locks, limits, snapshot/restore, idle eviction, rehydration.

Snapshot store layout (SNAPSHOT_DIR, shared by every replica):
    <session>/session.json   principal, written when the MCP session opens; plus model, names and compile
                             context once a feeder is loaded
    <session>/model/         Save Circuit output of the loaded feeder (+ Open.dss)
    <session>/sweep/         scratch snapshot taken around a sweep
A replica that receives a session it does not hold rebuilds it from <session>/ in about compile time.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from . import engine
from .adapters.base import ModelVersion, Principal, SnapshotMirror
from .compiler import CompiledFeeder
from .compiler.emit import LoadSeed
from .compiler.report import Head
from .studies import ToolError

log = logging.getLogger(__name__)

QUEUE_TIMEOUT_S = 20.0
REQUEST_CACHE = 64
META = "session.json"


@dataclass
class Session:
    id: str
    principal: Principal
    dss: Any = field(default_factory=engine.new_context)
    model: ModelVersion | None = None
    names: dict[str, tuple[UUID, str]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.monotonic)
    history: list[ModelVersion] = field(default_factory=list)
    compiled: Any = None  # compiler.CompiledFeeder of the current model
    solved: bool = False
    results: dict[str, Any] = field(default_factory=dict)  # last data per tool, for visualisation
    requests: dict[str, Any] = field(default_factory=dict)  # request_id -> envelope
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    rehydrated: bool = False

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def set_model(self, compiled: Any, dss: Any) -> None:
        if self.model is not None:
            self.history.append(self.model)
        self.dss = dss
        self.compiled = compiled
        self.model = compiled.model
        self.names = dict(compiled.names)
        self.solved = False
        self.results.clear()

    def require_model(self) -> None:
        if self.model is None:
            raise ToolError("no_model", "no feeder loaded in this session; call load_feeder first")

    def remember(self, request_id: str | None, env: Any) -> None:
        if request_id:
            self.requests[request_id] = env
            while len(self.requests) > REQUEST_CACHE:
                self.requests.pop(next(iter(self.requests)))


class SessionStore:
    def __init__(self, max_contexts: int = 32, idle_ttl_s: float = 1800, max_concurrent_solves: int = 4,
                 snapshot_dir: Path | str = "data/snapshots", snapshot_ttl_s: float = 86_400,
                 mirror: SnapshotMirror | None = None):
        self.max_contexts = max_contexts
        self.idle_ttl_s = idle_ttl_s
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_ttl_s = snapshot_ttl_s
        self.mirror = mirror
        self._sessions: dict[str, Session] = {}
        self._guard = threading.Lock()
        self._solves = threading.BoundedSemaphore(max_concurrent_solves)
        self.metrics: Counter = Counter()

    def __len__(self) -> int:
        return len(self._sessions)

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def dir(self, session_id: str) -> Path:
        return self.snapshot_dir / _safe(session_id)

    def for_ctx(self, session_id: str, principal: Principal) -> Session:
        with self._guard:
            s = self._sessions.get(session_id)
            if s is not None:
                if s.principal.name != principal.name:
                    raise ToolError("forbidden", "session belongs to another principal")
                s.touch()
                return s
            self._evict_idle_locked()
            if len(self._sessions) >= self.max_contexts:
                self._evict_lru_locked()
            if len(self._sessions) >= self.max_contexts:
                raise ToolError("capacity", f"all {self.max_contexts} engine contexts are busy; retry shortly")
            s = self._rehydrate(session_id, principal) or Session(id=session_id, principal=principal)
            self._sessions[session_id] = s
            self.metrics["sessions_rehydrated" if s.rehydrated else "sessions_created"] += 1
            return s

    def evict_idle(self) -> int:
        with self._guard:
            n = self._evict_idle_locked()
        self._expire_snapshots()
        return n

    def _evict_idle_locked(self) -> int:
        now = time.monotonic()
        stale = [k for k, s in self._sessions.items() if now - s.last_used > self.idle_ttl_s and not s.lock.locked()]
        for k in stale:
            self._drop(k)
        return len(stale)

    def _evict_lru_locked(self) -> None:
        idle = sorted((s for s in self._sessions.values() if not s.lock.locked()), key=lambda s: s.last_used)
        if idle:
            self._drop(idle[0].id)

    def _drop(self, session_id: str) -> None:
        """Free the engine context; the persisted snapshot stays so the session can come back."""
        self._sessions.pop(session_id, None)
        self.metrics["sessions_evicted"] += 1

    def close(self, session_id: str) -> None:
        with self._guard:
            self._drop(session_id)
        shutil.rmtree(self.dir(session_id), ignore_errors=True)

    def _expire_snapshots(self) -> None:
        if not self.snapshot_dir.exists():
            return
        cutoff = time.time() - self.snapshot_ttl_s
        for d in self.snapshot_dir.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff and _safe(d.name) not in {_safe(k) for k in self._sessions}:
                shutil.rmtree(d, ignore_errors=True)

    @contextmanager
    def acquire(self, s: Session) -> Iterator[Session]:
        """One tool at a time per session (busy otherwise); bounded concurrent solves across sessions."""
        if not s.lock.acquire(blocking=False):
            raise ToolError("busy", "another tool is running on this session")
        try:
            if not self._solves.acquire(timeout=QUEUE_TIMEOUT_S):
                raise ToolError("capacity", "solver capacity exhausted; retry shortly")
            try:
                s.touch()
                yield s
            finally:
                self._solves.release()
        finally:
            s.touch()
            s.lock.release()

    # --- snapshot / restore around sweeps -----------------------------------------------------------------------
    def snapshot(self, s: Session, kind: str = "sweep") -> Path:
        d = self.dir(s.id) / kind
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        engine.save_circuit(s.dss, str(d))
        return d

    def restore(self, s: Session, d: Path) -> None:
        engine.redirect(s.dss, str(d / "Master.dss"))
        s.solved = False

    @contextmanager
    def sweep(self, s: Session) -> Iterator[None]:
        """Snapshot before a sweep, restore after it, always."""
        was_solved = s.solved
        d = self.snapshot(s)
        try:
            yield
        finally:
            self.restore(s, d)
            if was_solved:
                s.solved = engine.solve(s.dss).converged

    # --- persistence for rehydration ---------------------------------------------------------------------------
    def mark(self, session_id: str, principal: Principal) -> None:
        """Record that an MCP session exists (written when it opens, before any feeder is loaded)."""
        path = self.dir(session_id) / META
        if not path.exists():
            _write_json(path, {"session_id": session_id, "principal": asdict(principal), "created": time.time()})
            self._push(session_id)

    def resumable(self, session_id: str, principal: Principal | None) -> bool:
        """True when the store knows this session and it belongs to the caller."""
        meta = _read_json(self.dir(session_id) / META) or self._pull(session_id)
        return bool(meta) and principal is not None and meta["principal"]["name"] == principal.name

    def persist(self, s: Session) -> None:
        """After load_feeder: the circuit plus everything the tools need to run on it elsewhere."""
        c = s.compiled
        self.snapshot(s, "model")
        _write_json(self.dir(s.id) / META, {
            "session_id": s.id, "principal": asdict(s.principal), "created": time.time(),
            "model": s.model.report(),
            "names": {k: [str(m), h] for k, (m, h) in s.names.items()},
            "compiled": {
                "root": c.root, "head": asdict(c.head), "voltage_bases": c.voltage_bases,
                "bus_nominal": c.bus_nominal,
                "load_seeds": [[str(ls.row["mrid"]), ls.row["name"], ls.kw, ls.kvar, ls.model, ls.how]
                               for ls in c.load_seeds],
            },
        })
        self._push(s.id)

    def _push(self, session_id: str) -> None:
        if self.mirror is not None:
            self.mirror.push(session_id, self.dir(session_id))

    def _pull(self, session_id: str) -> dict[str, Any] | None:
        if self.mirror is not None and self.mirror.pull(session_id, self.dir(session_id)):
            return _read_json(self.dir(session_id) / META)
        return None

    def _rehydrate(self, session_id: str, principal: Principal) -> Session | None:
        meta = _read_json(self.dir(session_id) / META)
        if meta is None or "model" not in meta:
            meta = self._pull(session_id) or meta
        if not meta or "model" not in meta:
            return None
        if meta["principal"]["name"] != principal.name:
            raise ToolError("forbidden", "session belongs to another principal")
        t0 = time.perf_counter()
        s = Session(id=session_id, principal=principal, rehydrated=True)
        engine.redirect(s.dss, str(self.dir(session_id) / "model" / "Master.dss"))
        model = _model_from_report(meta["model"])
        c = meta["compiled"]
        root = dict(c["root"])
        for k in ("mrid", "source_cn", "load_cn", "load_terminal"):
            if root.get(k):
                root[k] = UUID(root[k])
        head = Head(**{k: (datetime.fromisoformat(v) if k == "ts" and v else v) for k, v in c["head"].items()})
        seeds = [LoadSeed({"mrid": UUID(m), "name": n}, kw, kvar, mdl, how) for m, n, kw, kvar, mdl, how in c["load_seeds"]]
        names = {k: (UUID(m), h) for k, (m, h) in meta["names"].items()}
        s.compiled = CompiledFeeder([], names, model, c["voltage_bases"], head, root, seeds, c["bus_nominal"])
        s.model, s.names = model, names
        log.info(json.dumps({"rehydrated": session_id, "root": model.root_name,
                             "duration_ms": int((time.perf_counter() - t0) * 1000)}))
        return s


def _model_from_report(r: dict[str, Any]) -> ModelVersion:
    r = dict(r)
    r["model_id"], r["root_mrid"] = UUID(r["model_id"]), UUID(r["root_mrid"])
    r["cim_updated_at"] = datetime.fromisoformat(r["cim_updated_at"])
    r["telemetry_ts"] = datetime.fromisoformat(r["telemetry_ts"]) if r.get("telemetry_ts") else None
    return ModelVersion(**r)


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, default=str))
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _safe(session_id: str) -> str:
    return "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "anon"
