"""The verifier's words: every finding renders in the message format, within 90 columns.

Also: the summary line reads as the console shows it, anything shaped like
an API key is masked, and every reason a check can be NOT RUN has words.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from playground.copy.en import TEXT
from playground.verify import Check, Finding, GateResult, Site, Verdict, finding_lines, gate_lines
from playground.verify import findings as F
from playground.verify.messages import MAX_COLUMNS, summary_line
from playground.verify.model import not_run

VERIFY_DIR = Path(__file__).resolve().parents[2] / "playground" / "verify"
PARTS = ("title", "sent", "why", "fix")

# One sample value for every parameter any finding's words use.
PARAMS = {
    "user": "pg-u-0123456789ab",
    "status": 500,
    "handler": "confirm",
    "method": "playground.success",
    "expected": "playground.success",
    "count": 1234,
    "field": "signalgate_log",
    "expected_field": "signalgate",
    "got": "2026-01-02T03:04:05.678901",
    "ip": "203.0.113.254",
    "code": 422,
    "verdict": "dry_run_block",
    "function": "action_handler_with_a_long_name",
    "accepted": 1960,
    "posted": 2000,
    "stored": 1900,
    "error": "AttributeError: 'ConfirmBody' object has no attribute 'signalgate_log'",
    "location": "app/routes.py:41",
}

# Reasons returned by a helper rather than written at the call that skips.
INDIRECT_REASONS = {"self_test", "key", "no_proof"}


def _variants(finding_id: str) -> set[str | None]:
    prefix = f"finding.{finding_id}."
    variants: set[str | None] = {None}
    for key in TEXT:
        if key.startswith(prefix):
            rest = key[len(prefix) :].split(".")
            if len(rest) == 2:
                variants.add(rest[1])
    return variants


def test_every_finding_has_all_four_parts():
    for finding_id in F.ALL:
        for part in PARTS:
            assert f"finding.{finding_id}.{part}" in TEXT, (finding_id, part)


def test_every_finding_key_belongs_to_a_known_finding_and_part():
    for key in TEXT:
        if key.startswith("finding."):
            _, finding_id, part, *_ = key.split(".")
            assert finding_id in F.ALL, key
            assert part in PARTS, key


@pytest.mark.parametrize("finding_id", F.ALL)
def test_every_finding_renders_within_the_width_with_and_without_a_site(finding_id):
    long_code = "if ev: sg().log(ev)  # " + "x" * 120
    for variant in _variants(finding_id):
        without_site: list[str] = []
        for site in (None, Site("app/routes.py", 41, long_code)):
            found = Finding(finding_id, finding_id not in F.NOTES, site, variant, PARAMS)
            lines = finding_lines(found)
            assert all(len(line) <= MAX_COLUMNS for line in lines), lines
            mark = "✗" if found.blocks else "!"
            assert lines[0].startswith(f"{mark} ")
            assert any(line.startswith("  Why: ") for line in lines)
            assert any(line.startswith("  Fix: ") for line in lines)
            if site is None:
                without_site = lines
                continue
            # The location line comes right after the title, however many
            # lines the title wraps to.
            at = next(i for i, (a, b) in enumerate(zip(lines, [*without_site, ""])) if a != b)
            assert at >= 1, lines
            assert lines[at].startswith("  app/routes.py:41   if ev: sg().log(ev)"), lines
            assert lines[:at] + lines[at + 1 :] == without_site


def test_the_message_format_matches_the_console_example():
    found = F.finding(
        F.SUCCESS_LOG_ON_FAILURE,
        Site("app/routes.py", 41, "if ev: sg().log(ev)"),
        user="pg-u-7f3a",
        status=400,
    )
    assert finding_lines(found) == [
        "✗ playground.success is logged even when the token is wrong",
        "  app/routes.py:41   if ev: sg().log(ev)",
        "  I sent a wrong token for user pg-u-7f3a. Your app answered 400 but still logged it.",
        "  Why: SignalGate would count failed attempts as real successes.",
        "  Fix: move this log() inside the success branch, after the token check passes.",
    ]


def test_the_summary_line_reads_as_the_console_shows_it():
    checks = [Check.judged(f"probe_{n}", []) for n in range(1, 7)]
    note = F.finding(F.NO_403_ON_BLOCK, None, user="u", status=200)
    checks.append(Check.judged("probe_7", [note]))
    result = GateResult("code", tuple(checks), accepted=2, needs_proof=True)
    assert summary_line(result) == [
        "✓ 7 passed · ! 1 note · ◌ 0 not run — proven by 2 real events SignalGate accepted."
    ]


def test_a_failed_or_unproven_gate_claims_no_proof():
    failed = GateResult(
        "code",
        (Check.judged("probe_1", [F.finding(F.LOG_MISSING, None, user="u", status=200)]),),
        accepted=1,
        needs_proof=True,
    )
    assert summary_line(failed) == ["✗ 1 failed · ✓ 0 passed · ! 0 notes · ◌ 0 not run"]
    unproven = GateResult("code", (Check.judged("probe_1", []),), accepted=0, needs_proof=True)
    assert unproven.verdict is Verdict.NOT_RUN
    lines = gate_lines(unproven)
    assert lines[0] == "◌ " + TEXT["verify.not_run.no_proof"]
    assert "proven" not in lines[-1]


def test_one_finding_per_line_of_code_and_each_reason_once():
    site = Site("app/routes.py", 12, "sg().log(ev)")
    first = F.finding(F.LOG_BEFORE_ACTION, site, user="u", status=200)
    second = F.finding(F.LOG_WITHOUT_ACTION, site, "invalid_user", user="u", status=400)
    result = GateResult(
        "code",
        (
            Check.judged("probe_1", [first]),
            Check.judged("probe_2", [second]),
            Check.skipped("probe_5", "depends"),
            Check.skipped("probe_6", "depends"),
        ),
    )
    lines = gate_lines(result)
    titles = [line for line in lines if line.startswith("✗ playground")]
    assert titles == ["✗ playground.action is logged before the action happens"]
    assert lines.count("◌ " + TEXT["verify.not_run.depends"]) == 1


def test_anything_shaped_like_a_key_is_masked():
    key = "pk_live_" + "Zz9_" * 8
    found = F.finding(
        F.USER_ID_MISMATCH, Site("app/routes.py", 3, f'k = "{key}"'), user="u", got=key
    )
    rendered = "\n".join(finding_lines(found))
    assert key not in rendered
    assert rendered.count("pk_live_…") == 2


def _literal_reasons() -> set[str]:
    """Every reason written as a literal where a check or a gate is skipped."""
    reasons: set[str] = set()
    names = {"skipped": 1, "not_run": 1, "EvidenceUnavailable": 0}
    for path in VERIFY_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in names or len(node.args) <= names[name]:
                continue
            for sub in ast.walk(node.args[names[name]]):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    reasons.add(sub.value)
    return reasons


def test_every_not_run_reason_has_words():
    reasons = _literal_reasons() | INDIRECT_REASONS
    assert {"app", "signalgate", "depends", "lease", "restarted"} <= reasons
    for reason in reasons:
        assert f"verify.not_run.{reason}" in TEXT, reason
    assert gate_lines(not_run("code", "routes"))[0] == "◌ " + TEXT["verify.not_run.routes"]
