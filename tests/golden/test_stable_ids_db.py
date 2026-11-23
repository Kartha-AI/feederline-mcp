# SPDX-License-Identifier: Apache-2.0
"""Loading the IEEE feeders twice yields the playbook's root IDs in the database."""

import psycopg
import pytest

from loaders import ieee
from tests.conftest import LOADER_URL
from tests.unit.test_stable_ids import ROOTS

pytestmark = pytest.mark.db

ROOT = """
SELECT ce.mrid FROM conducting_equipment ce
JOIN voltage_level vl ON vl.mrid = ce.equipment_container_mrid
WHERE ce.cim_class = 'Breaker' AND ce.region_code = %s
"""


def test_root_ids_survive_reload():
    for _ in range(2):
        for n in ROOTS:
            ieee.load(n, LOADER_URL)
    with psycopg.connect(LOADER_URL) as conn:
        for n, expected in ROOTS.items():
            rows = [r[0] for r in conn.execute(ROOT, (f"IEEE{n}",)).fetchall()]
            assert rows == [expected]
