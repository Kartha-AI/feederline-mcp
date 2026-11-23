# SPDX-License-Identifier: Apache-2.0
"""TelemetryWriter: tag -> mrid, COPY into measurement_value, upsert latest and switch.open in one transaction."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from feederline_mcp.adapters.base import DataSource, Sample

log = logging.getLogger("ingest.writer")


@dataclass
class BatchReport:
    written: int
    unknown: list[Sample] = field(default_factory=list)


class TelemetryWriter:
    def __init__(self, ds: DataSource):
        self.ds = ds
        self.tags = ds.load_tag_map()  # remote_source.name -> (measurement_mrid, measurement_type, switch_mrid|None)

    def refresh_tags(self) -> None:
        self.tags = self.ds.load_tag_map()

    def write_batch(self, samples: list[Sample]) -> BatchReport:
        t0 = time.perf_counter()
        rows, unknown = [], []
        latest: dict[UUID, tuple[datetime, float, int, int]] = {}
        switches: dict[UUID, tuple[datetime, bool]] = {}
        for s in samples:
            hit = self.tags.get(s.tag)
            if not hit:
                unknown.append(s)
                continue
            mrid, mtype, switch_mrid = hit
            rows.append((s.ts, mrid, s.value, s.quality, s.source))
            if mrid not in latest or s.ts > latest[mrid][0]:
                latest[mrid] = (s.ts, s.value, s.quality, s.source)
            if mtype == "SwitchPosition" and switch_mrid and (switch_mrid not in switches or s.ts > switches[switch_mrid][0]):
                switches[switch_mrid] = (s.ts, s.value == 0)  # 0 = open, 1 = closed
        with self.ds.tx() as cur:
            written = cur.copy_rows("measurement_value", rows, on_conflict="DO NOTHING")
            cur.upsert_latest(latest)
            cur.upsert_switch_open(switches)
        log.info(json.dumps({"batch": len(samples), "written": written, "unknown": len(unknown),
                             "switches": len(switches), "duration_ms": int((time.perf_counter() - t0) * 1000)}))
        return BatchReport(written=written, unknown=unknown)
