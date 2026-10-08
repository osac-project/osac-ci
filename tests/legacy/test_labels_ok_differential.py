"""Exhaustive check of queue_labels_ok against the REAL ``labels_ok`` bash function in auto-queue.sh.

7 relevant labels give 128 subsets; each is also run with an unrelated label present. If this fails, the port is
wrong, not the test (the legacy script is the spec until the parity gate passes).
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
from pathlib import Path

import pytest
from legacy_sources import AUTO_QUEUE, require

from osac_ci.rules.labels import QUEUE_BLOCKING_LABELS, REQUIRED_LABELS, queue_labels_ok

pytestmark = pytest.mark.legacy


def _extract_function(script: Path, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", script.read_text(), re.S | re.M)
    assert match, f"{name}() not found in {script}"
    return match.group(0)


def test_labels_ok_matches_the_real_bash_function(tmp_path: Path) -> None:
    script = require(AUTO_QUEUE)
    fn = tmp_path / "labels_ok.sh"
    fn.write_text(_extract_function(script, "labels_ok"))

    relevant = [*REQUIRED_LABELS, *QUEUE_BLOCKING_LABELS]
    cases: list[frozenset[str]] = []
    for r in range(len(relevant) + 1):
        for combo in itertools.combinations(relevant, r):
            cases.append(frozenset(combo))
            cases.append(frozenset({*combo, "bug"}))
    lines = "\n".join(json.dumps(sorted(c)) for c in cases) + "\n"
    inputs = tmp_path / "inputs.txt"
    inputs.write_text(lines)

    out = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; while IFS= read -r l; do labels_ok "$l"; done < "$2"',
            "bash",
            str(fn),
            str(inputs),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    assert len(out) == len(cases) == 2**7 * 2
    mismatches = [(sorted(c), got) for c, got in zip(cases, out, strict=True) if (got == "true") != queue_labels_ok(c)]
    assert not mismatches, f"python and bash disagree on {len(mismatches)} label sets, e.g. {mismatches[:3]}"
