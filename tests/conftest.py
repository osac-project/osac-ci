from __future__ import annotations

import pytest
from helpers import ROOT

from osac_ci.policy import Policy, load_policy


@pytest.fixture(scope="session")
def osac_policy() -> Policy:
    return load_policy(ROOT / "policy" / "osac.yml")


@pytest.fixture(scope="session")
def toy_policy() -> Policy:
    return load_policy(ROOT / "policy" / "toy.yml")
