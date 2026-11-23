# SPDX-License-Identifier: Apache-2.0
"""Feeder root IDs are part of the public contract: the playbook and user prompts quote them."""

from uuid import UUID

import pytest

from loaders.common import NAMESPACE, mrid

ROOTS = {
    13: UUID("c990acfa-4c84-5788-be92-53b20464fbd8"),
    34: UUID("378451c2-afdb-524f-a587-9808c081df60"),
    123: UUID("1dfeb41b-bf91-52f3-b6af-7b400b93c5b8"),
}


def test_namespace_is_frozen():
    # Changing the namespace changes every mRID in every install; pin it by value.
    assert NAMESPACE == UUID("7d14ca31-0d37-54cf-a256-592e70e35bd2")


@pytest.mark.parametrize("n", sorted(ROOTS))
def test_ieee_root_mrid_is_deterministic(n):
    assert mrid(f"ieee{n}", "breaker", "head") == ROOTS[n]
