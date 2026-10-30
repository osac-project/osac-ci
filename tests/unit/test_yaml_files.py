"""Every action and workflow of the repository is valid YAML. GitHub refuses a broken one only when it is used, which
for a step of the merge queue means a queue entry that fails for nothing."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
FILES = sorted([*(ROOT / ".github/actions").glob("*/action.yml"), *(ROOT / ".github/workflows").glob("*.yml")])


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_the_file_is_yaml(path: Path) -> None:
    assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)


def test_the_files_were_found() -> None:
    assert len(FILES) > 20
