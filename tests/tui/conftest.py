"""Snapshot support for the TUI tests.

Snapshots live as plain text files under ``tests/tui/snapshots/``. They are
regenerated only when ``PLAYGROUND_UPDATE_SNAPSHOTS=1`` is set; otherwise a
mismatch (or a missing file) fails the test. ``PLAYGROUND_SKIP_SNAPSHOTS=1``
skips the comparison instead, for work in progress whose output is still
changing; the snapshots are then brought up to date in one go.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


@pytest.fixture
def snapshot():
    def check(name: str, actual: str) -> None:
        if os.environ.get("PLAYGROUND_SKIP_SNAPSHOTS") == "1":
            pytest.skip("snapshot comparison skipped (PLAYGROUND_SKIP_SNAPSHOTS=1)")
        path = SNAPSHOT_DIR / f"{name}.txt"
        if os.environ.get("PLAYGROUND_UPDATE_SNAPSHOTS") == "1":
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(actual)
            return
        assert path.exists(), f"missing snapshot {path}; rerun with PLAYGROUND_UPDATE_SNAPSHOTS=1"
        expected = path.read_text()
        assert actual == expected, f"{name} snapshot mismatch"

    return check
