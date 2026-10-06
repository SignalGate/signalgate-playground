"""What a verifier check ends as, and what a finding carries.

Every check ends as PASS, FAIL or NOT RUN. A check that couldn't run is
NOT RUN, never PASS. A gate is the list of its checks: it passes only when
every check passes (notes don't block), and a gate that needs proof also
needs at least one real event SignalGate accepted.

A ``Finding`` names one public finding ID (``playground.verify.findings``)
and, where it can, the file and line it points at. Its words live in the
copy module under ``finding.<ID>.*``; ``params`` fills them in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"


@dataclass(frozen=True)
class Site:
    """Where a finding points: a repo-relative file, a line and that line's code."""

    file: str
    line: int
    code: str = ""


@dataclass(frozen=True)
class Finding:
    """One problem (or, when ``blocks`` is False, a note) the verifier found."""

    id: str
    blocks: bool = True
    site: Site | None = None
    variant: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Check:
    """One check of a gate. ``not_run`` names the reason when it couldn't run."""

    name: str
    verdict: Verdict
    findings: tuple[Finding, ...] = ()
    not_run: str | None = None

    @classmethod
    def judged(cls, name: str, findings: list[Finding] | tuple[Finding, ...]) -> Check:
        """FAIL when any finding blocks, else PASS (with any notes)."""
        verdict = Verdict.FAIL if any(f.blocks for f in findings) else Verdict.PASS
        return cls(name, verdict, tuple(findings))

    @classmethod
    def skipped(
        cls, name: str, reason: str, findings: list[Finding] | tuple[Finding, ...] = ()
    ) -> Check:
        """NOT RUN for ``reason``. ``findings`` keep the IDs of what was seen
        (for telemetry and /report); the console shows only the reason."""
        return cls(name, Verdict.NOT_RUN, tuple(findings), reason)


@dataclass(frozen=True)
class GateResult:
    """Every check of one gate run, and how many real events SignalGate accepted.

    ``routes_sha256`` is the hash of the file the gate read, so a caller can
    tell whether a PASS still describes the file on disk.
    """

    gate: str
    checks: tuple[Check, ...]
    accepted: int = 0
    needs_proof: bool = False
    routes_sha256: str | None = None

    @property
    def verdict(self) -> Verdict:
        verdicts = {check.verdict for check in self.checks}
        if Verdict.FAIL in verdicts:
            return Verdict.FAIL
        if Verdict.NOT_RUN in verdicts or not self.checks:
            return Verdict.NOT_RUN
        if self.needs_proof and self.accepted < 1:
            return Verdict.NOT_RUN
        return Verdict.PASS

    @property
    def findings(self) -> tuple[Finding, ...]:
        return tuple(f for check in self.checks for f in check.findings)

    @property
    def finding_ids(self) -> tuple[str, ...]:
        """Every finding ID, once each, in the order found (for telemetry)."""
        return tuple(dict.fromkeys(f.id for f in self.findings))

    def counts(self) -> dict[str, int]:
        """``{passed, failed, notes, not_run}`` over the checks."""
        return {
            "passed": sum(c.verdict is Verdict.PASS for c in self.checks),
            "failed": sum(c.verdict is Verdict.FAIL for c in self.checks),
            "notes": sum(not f.blocks for f in self.findings),
            "not_run": sum(c.verdict is Verdict.NOT_RUN for c in self.checks),
        }


def not_run(gate: str, reason: str, *, needs_proof: bool = False) -> GateResult:
    """A gate that couldn't run at all, for ``reason``."""
    return GateResult(gate, (Check.skipped(gate, reason),), needs_proof=needs_proof)


__all__ = ["Check", "Finding", "GateResult", "Site", "Verdict", "not_run"]
