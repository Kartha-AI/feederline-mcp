# SPDX-License-Identifier: Apache-2.0
"""Kafka consumer: gateway messages -> TelemetryWriter; offsets committed after the batch is written.

    python -m ingest.kafka --bootstrap localhost:19092 --topic scada.telemetry --dlq scada.telemetry.dlq

Messages are JSON {"tag": str, "ts": ISO-8601 with offset, "value": number, "quality"?: int, "source"?: int}
(the protocol gateway's shape; source defaults to 1, SCADA). Unknown tags and unparseable messages go to the
dead-letter topic with a reason header. A batch is written in one transaction, then its offsets are committed, so a
crash replays the batch and the writer's ON CONFLICT DO NOTHING makes the replay harmless. Locally this runs
against Redpanda (compose profile `kafka`).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from feederline_mcp.adapters.base import Sample
from feederline_mcp.adapters.local import LocalDataSource

from .writer import TelemetryWriter

log = logging.getLogger("ingest.kafka")

SOURCE_SCADA = 1


def parse(raw: bytes) -> Sample:
    d = json.loads(raw)
    ts = datetime.fromisoformat(str(d["ts"]).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError("ts must carry a UTC offset")
    return Sample(tag=str(d["tag"]), ts=ts, value=float(d["value"]), quality=int(d.get("quality", 0)),
                  source=int(d.get("source", SOURCE_SCADA)))


class TelemetryConsumer:
    def __init__(self, writer: TelemetryWriter, consumer: Any, producer: Any | None, dlq_topic: str | None,
                 batch_size: int = 500, poll_s: float = 1.0):
        self.writer = writer
        self.consumer = consumer
        self.producer = producer
        self.dlq_topic = dlq_topic
        self.batch_size = batch_size
        self.poll_s = poll_s
        self.running = True
        self.stats = {"messages": 0, "written": 0, "unknown": 0, "bad": 0, "batches": 0}

    def _dead_letter(self, raw: bytes | None, reason: str) -> None:
        if self.producer is not None and self.dlq_topic:
            self.producer.produce(self.dlq_topic, value=raw or b"", headers=[("reason", reason.encode())])

    def process(self, messages: Iterable[Any]) -> None:
        samples: list[Sample] = []
        raws: dict[int, bytes] = {}
        for m in messages:
            if m.error():
                log.warning("kafka error: %s", m.error())
                continue
            self.stats["messages"] += 1
            try:
                s = parse(m.value())
            except (ValueError, KeyError, TypeError) as e:
                self.stats["bad"] += 1
                self._dead_letter(m.value(), f"unparseable: {type(e).__name__}")
                continue
            raws[id(s)] = m.value()
            samples.append(s)
        if samples:
            report = self.writer.write_batch(samples)
            self.stats["written"] += report.written
            self.stats["unknown"] += len(report.unknown)
            for s in report.unknown:
                self._dead_letter(raws.get(id(s)), f"unknown tag {s.tag}")
        if self.producer is not None:
            self.producer.flush(10)
        self.consumer.commit(asynchronous=False)  # only after the batch is durable
        self.stats["batches"] += 1

    def run(self, max_messages: int | None = None, idle_exit_s: float | None = None) -> dict[str, int]:
        idle = 0.0
        while self.running:
            msgs = self.consumer.consume(num_messages=self.batch_size, timeout=self.poll_s)
            if not msgs:
                idle += self.poll_s
                if idle_exit_s is not None and idle >= idle_exit_s:
                    break
                continue
            idle = 0.0
            self.process(msgs)
            log.info(json.dumps(self.stats))
            if max_messages is not None and self.stats["messages"] >= max_messages:
                break
        return self.stats


def build(bootstrap: str, topic: str, group: str, dlq: str | None):
    from confluent_kafka import Consumer, Producer

    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": group, "enable.auto.commit": False,
                         "auto.offset.reset": "earliest"})
    consumer.subscribe([topic])
    producer = Producer({"bootstrap.servers": bootstrap}) if dlq else None
    return consumer, producer


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bootstrap", default=os.environ.get("KAFKA_BOOTSTRAP", "localhost:19092"))
    ap.add_argument("--topic", default=os.environ.get("KAFKA_TOPIC", "scada.telemetry"))
    ap.add_argument("--dlq", default=os.environ.get("KAFKA_DLQ", "scada.telemetry.dlq"))
    ap.add_argument("--group", default=os.environ.get("KAFKA_GROUP", "feederline-telemetry-writer"))
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--max-messages", type=int)
    ap.add_argument("--idle-exit", type=float, help="stop after this many seconds without messages")
    args = ap.parse_args()
    ds = LocalDataSource(os.environ.get("DATABASE_URL", "postgresql://grid_write:grid_write@localhost:5433/grid"))
    consumer, producer = build(args.bootstrap, args.topic, args.group, args.dlq)
    loop = TelemetryConsumer(TelemetryWriter(ds), consumer, producer, args.dlq, args.batch_size)
    signal.signal(signal.SIGTERM, lambda *_: setattr(loop, "running", False))
    try:
        log.info(json.dumps(loop.run(args.max_messages, args.idle_exit)))
    finally:
        consumer.close()
        ds.close()


if __name__ == "__main__":
    main()
