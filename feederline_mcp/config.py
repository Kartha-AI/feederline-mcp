# SPDX-License-Identifier: Apache-2.0
"""Settings and the adapter registry: FEEDERLINE_MCP_ADAPTER names an entry point -> (DataSource, StudySink, Auth).

Adapters are discovered through the Python entry-point group `feederline_mcp.adapters`. Each entry point is a
factory `build(settings) -> (DataSource, StudySink, Auth)`. The package registers `local`; other adapters ship as
separate packages that register their own entry point, so the core never imports them.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path

from .adapters.base import Auth, DataSource, StudySink

ADAPTER_GROUP = "feederline_mcp.adapters"
DEFAULT_ADAPTER = "local"


@dataclass(frozen=True)
class Settings:
    adapter: str = field(default_factory=lambda: os.environ.get("FEEDERLINE_MCP_ADAPTER", DEFAULT_ADAPTER))
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", "postgresql://mcp:mcp@localhost:5433/grid"))
    bearer_token: str = field(default_factory=lambda: os.environ.get("MCP_BEARER_TOKEN", ""))
    auth_mode: str = field(default_factory=lambda: os.environ.get("MCP_AUTH", "on"))
    regions: list[str] = field(default_factory=lambda: [r for r in os.environ.get("MCP_REGIONS", "*").split(",") if r])
    max_contexts: int = field(default_factory=lambda: int(os.environ.get("MAX_CONTEXTS", "32")))
    idle_ttl_s: int = field(default_factory=lambda: int(os.environ.get("IDLE_TTL_S", "1800")))
    max_concurrent_solves: int = field(default_factory=lambda: int(os.environ.get("MAX_CONCURRENT_SOLVES", str(os.cpu_count() or 2))))
    snapshot_dir: Path = field(default_factory=lambda: Path(os.environ.get("SNAPSHOT_DIR", "data/snapshots")))
    snapshot_ttl_s: int = field(default_factory=lambda: int(os.environ.get("SNAPSHOT_TTL_S", "86400")))
    history_dir: Path = field(default_factory=lambda: Path(os.environ.get("HISTORY_DIR", "data/parquet")))


AdapterFactory = Callable[[Settings], tuple[DataSource, StudySink, Auth]]


def installed_adapters() -> dict[str, object]:
    return {ep.name: ep for ep in entry_points(group=ADAPTER_GROUP)}


def load_adapter(name: str) -> AdapterFactory:
    eps = installed_adapters()
    if name not in eps:
        listed = ", ".join(sorted(eps)) or "none"
        raise RuntimeError(f"unknown adapter {name!r} (FEEDERLINE_MCP_ADAPTER); installed adapters: {listed}")
    return eps[name].load()  # type: ignore[attr-defined]


def build(settings: Settings | None = None) -> tuple[DataSource, StudySink, Auth]:
    s = settings or Settings()
    return load_adapter(s.adapter)(s)
