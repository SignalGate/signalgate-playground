"""The test side of the end-to-end start-up hook (see ``_sitecustomize.py``).

``Seam`` puts the hook on ``PYTHONPATH`` for a console subprocess and reads
back what the sample app's process wrote: its boot files, the log
deliveries it answered, and the SDK counters it dumped when a client
closed. It can also read ``/__playground/health`` for a boot, using the
token from that boot's file.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from playground.traffic.local_constants import (
    APP_HEALTH_POLL_S,
    APP_START_TIMEOUT_S,
    LOOPBACK_REQUEST_TIMEOUT_S,
)

# The variable naming the hook's config file. Only the hook itself and these
# tests read it; nothing in the product does.
SEAM_ENV = "PLAYGROUND_E2E_SEAM"

HEALTH_PATH = "/__playground/health"

_SOURCE = Path(__file__).with_name("_sitecustomize.py")


def install_hook(directory: Path) -> Path:
    """Write the hook into ``directory`` as ``sitecustomize.py``; return ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_SOURCE, directory / "sitecustomize.py")
    return directory


def python_path(*entries: Path) -> str:
    """``entries`` first, then whatever ``PYTHONPATH`` already held."""
    existing = os.environ.get("PYTHONPATH")
    parts = [str(entry) for entry in entries] + ([existing] if existing else [])
    return os.pathsep.join(parts)


@dataclass
class Seam:
    """One test's hook: where it lives, where the app writes, and its gate."""

    hook_dir: Path
    dump_dir: Path
    config_path: Path
    release_file: Path | None

    @classmethod
    def create(
        cls, base: Path, *, stub: bool = True, gated: bool = False, fake_url: str | None = None
    ) -> Seam:
        hook_dir = install_hook(base / "hook")
        dump_dir = base / "dump"
        dump_dir.mkdir(parents=True, exist_ok=True)
        release_file = dump_dir / "release" if gated else None
        config_path = base / "seam.json"
        config = {
            "dump_dir": str(dump_dir),
            "stub": stub,
            "release_file": str(release_file) if release_file is not None else None,
            "poll_s": APP_HEALTH_POLL_S,
            "fake_url": fake_url,
        }
        config_path.write_text(json.dumps(config))
        return cls(hook_dir, dump_dir, config_path, release_file)

    def env(self, tree: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment for a console run from ``tree`` with this hook on."""
        env = dict(os.environ)
        env["PYTHONPATH"] = python_path(self.hook_dir, tree)
        env[SEAM_ENV] = str(self.config_path)
        env.update(extra or {})
        return env

    def release(self) -> None:
        """Let held log deliveries through."""
        assert self.release_file is not None, "this seam has no gate"
        self.release_file.touch()

    # -- what the app wrote ---------------------------------------------------

    def boots(self) -> list[dict[str, Any]]:
        """Every boot file so far, oldest first."""
        boots = []
        for path in self.dump_dir.glob("boot-*.json"):
            try:
                boots.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue  # being replaced right now; the next read sees it
        return sorted(boots, key=lambda boot: boot["started_ns"])

    def pids(self) -> list[int]:
        return [boot["pid"] for boot in self.boots()]

    def wait_for_boots(self, count: int, within: float) -> list[dict[str, Any]]:
        """Wait until ``count`` boots have written their boot id; return all boots."""
        deadline = time.monotonic() + within
        while True:
            boots = self.boots()
            if len(boots) >= count and all("boot_id" in boot for boot in boots[:count]):
                return boots
            if time.monotonic() > deadline:
                raise AssertionError(f"expected {count} boot(s), saw {boots}")
            time.sleep(APP_HEALTH_POLL_S)

    def deliveries(self) -> list[dict[str, Any]]:
        """Every log delivery the app's processes answered, in file order per process."""
        lines: list[dict[str, Any]] = []
        for path in sorted(self.dump_dir.glob("deliveries-*.jsonl")):
            lines.extend(json.loads(line) for line in path.read_text().splitlines() if line)
        return lines

    def closed(self, pid: int) -> list[dict[str, Any]]:
        """The counters dumped each time an SDK client in ``pid`` closed, in order."""
        paths = sorted(
            self.dump_dir.glob(f"closed-{pid}-*.json"),
            key=lambda path: int(path.stem.rsplit("-", 1)[1]),
        )
        return [json.loads(path.read_text()) for path in paths]

    def exit_record(self, pid: int) -> dict[str, Any] | None:
        path = self.dump_dir / f"exit-{pid}.json"
        return json.loads(path.read_text()) if path.exists() else None


def read_health(boot: Mapping[str, Any]) -> dict[str, Any]:
    """``GET /__playground/health`` for ``boot``, with the token from its boot file."""
    response = httpx.get(
        f"http://127.0.0.1:{boot['port']}{HEALTH_PATH}",
        headers={"Authorization": f"Bearer {boot['token']}"},
        timeout=LOOPBACK_REQUEST_TIMEOUT_S,
        trust_env=False,
    )
    response.raise_for_status()
    return response.json()


def wait_for_health(boot: Mapping[str, Any]) -> dict[str, Any]:
    """``read_health``, retried until the app answers or its start-up time is up."""
    deadline = time.monotonic() + APP_START_TIMEOUT_S
    while True:
        try:
            return read_health(boot)
        except httpx.HTTPError:
            if time.monotonic() > deadline:
                raise
            time.sleep(APP_HEALTH_POLL_S)


__all__ = ["SEAM_ENV", "Seam", "install_hook", "python_path", "read_health", "wait_for_health"]
