# SPDX-License-Identifier: Apache-2.0
"""Kafka path against Redpanda (`docker compose --profile kafka up -d redpanda`); skipped when it is not running."""

import json
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from feederline_mcp.adapters.local import LocalDataSource
from ingest.kafka import TelemetryConsumer, build
from ingest.writer import TelemetryWriter
from tests.conftest import WRITER_URL

pytestmark = pytest.mark.db
BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:19092")


def redpanda_up() -> bool:
    try:
        host, port = BOOTSTRAP.rsplit(":", 1)
        socket.create_connection((host, int(port)), timeout=1).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not redpanda_up(), reason="Redpanda not running (docker compose --profile kafka up -d redpanda)")
def test_consumer_writes_commits_and_dead_letters(loaded, ds):
    pytest.importorskip("confluent_kafka")
    from confluent_kafka import Consumer, Producer

    run = uuid.uuid4().hex[:8]
    topic, dlq = f"scada.test.{run}", f"scada.test.{run}.dlq"
    t0 = datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)
    good = [{"tag": "IEEE13_671_P", "ts": (t0 + timedelta(minutes=i)).isoformat(), "value": 1000 + i} for i in range(5)]
    p = Producer({"bootstrap.servers": BOOTSTRAP})
    for m in good + [{"tag": "NOPE", "ts": t0.isoformat(), "value": 1}]:
        p.produce(topic, json.dumps(m).encode())
    p.produce(topic, b"{not json")
    p.flush(10)

    w = LocalDataSource(WRITER_URL)
    consumer, producer = build(BOOTSTRAP, topic, f"g-{run}", dlq)
    try:
        stats = TelemetryConsumer(TelemetryWriter(w), consumer, producer, dlq, poll_s=0.5).run(max_messages=7,
                                                                                              idle_exit_s=15)
    finally:
        consumer.close()
        w.close()
    assert stats["messages"] == 7 and stats["written"] == 5 and stats["unknown"] == 1 and stats["bad"] == 1
    n = ds.query("""SELECT count(*) AS n FROM measurement_value v JOIN measurement m ON m.mrid = v.measurement_mrid
                    WHERE m.name = 'IEEE13_671_P' AND v.ts = ANY(%(ts)s) AND v.value = ANY(%(vals)s)""",
                 {"ts": [datetime.fromisoformat(m["ts"]) for m in good], "vals": [float(m["value"]) for m in good]},
                 ["*"])[0]["n"]
    assert n == 5

    # committed: the same group sees nothing new
    again, _ = build(BOOTSTRAP, topic, f"g-{run}", None)
    try:
        assert not [m for m in again.consume(num_messages=10, timeout=3) if not m.error()]
    finally:
        again.close()

    dl = Consumer({"bootstrap.servers": BOOTSTRAP, "group.id": f"d-{run}", "auto.offset.reset": "earliest"})
    dl.subscribe([dlq])
    try:
        got = [m for m in dl.consume(num_messages=10, timeout=10) if not m.error()]
    finally:
        dl.close()
    reasons = sorted(dict(m.headers())["reason"].decode().split(":")[0] for m in got)
    assert reasons == ["unknown tag NOPE", "unparseable"]
