"""A gate's result in words: one block per finding, then the summary line.

Every word comes from the copy module. A finding reads::

    ✗ <what is wrong>
      <file>:<line>   <that line of code>
      <what the console sent, and how the app answered>
      Why: <why it matters>
      Fix: <what to change>

Lines are wrapped to ``MAX_COLUMNS``. Anything shaped like an API key is
masked before a line is returned, whatever it came from.
"""

from __future__ import annotations

import textwrap
from typing import Any

from playground.copy.en import TEXT, text
from playground.verify.model import Finding, GateResult, Verdict
from playground.verify.static import KEY_SHAPE

MAX_COLUMNS = 90
_INDENT = "  "
# Lines after the first of a why or fix text line up under its words.
_HANG = " " * len(text("verify.why", why=""))


def _mask(line: str) -> str:
    return KEY_SHAPE.sub(text("verify.key_masked"), line)


def _wrap(line: str, indent: str = _INDENT) -> list[str]:
    return textwrap.wrap(
        line,
        width=MAX_COLUMNS,
        subsequent_indent=indent,
        break_long_words=True,
        break_on_hyphens=False,
    ) or [line]


def _part(found: Finding, part: str) -> str:
    keys = [f"finding.{found.id}.{part}"]
    if found.variant:
        keys.insert(0, f"finding.{found.id}.{part}.{found.variant}")
    for key in keys:
        if key in TEXT:
            return text(key, **found.params)
    raise KeyError(keys[-1])


def _location(found: Finding) -> list[str]:
    site = found.site
    if site is None:
        return []
    head = text("verify.location", file=site.file, line=site.line, code="")
    room = MAX_COLUMNS - len(head)
    code = _mask(site.code)
    if len(code) > room:
        code = code[: max(0, room - 1)] + text("verify.ellipsis")
    return [text("verify.location", file=site.file, line=site.line, code=code).rstrip()]


def finding_lines(found: Finding) -> list[str]:
    """One finding in the message format, wrapped and masked."""
    mark = text("mark.fail") if found.blocks else text("mark.note")
    lines = _wrap(text("verify.title", mark=mark, title=_part(found, "title")))
    lines += _location(found)
    lines += _wrap(_INDENT + _part(found, "sent"), _INDENT)
    lines += _wrap(text("verify.why", why=_part(found, "why")), _HANG)
    lines += _wrap(text("verify.fix", fix=_part(found, "fix")), _HANG)
    return [_mask(line) for line in lines]


def summary_line(result: GateResult) -> list[str]:
    """The gate's summary: counts per verdict, then what proved it."""
    counts = result.counts()
    params: dict[str, Any] = {
        "passed": counts["passed"],
        "notes": counts["notes"],
        "not_run": counts["not_run"],
        "notes_word": text("verify.word.note" if counts["notes"] == 1 else "verify.word.notes"),
    }
    line = text("verify.summary", **params)
    if counts["failed"]:
        line = text("verify.summary.failed", failed=counts["failed"]) + line
    if result.needs_proof and result.verdict is Verdict.PASS:
        word = "verify.word.event" if result.accepted == 1 else "verify.word.events"
        line += text("verify.summary.proof", accepted=result.accepted, events_word=text(word))
    elif result.gate == "rehearsal":
        line += text("verify.summary.rehearsal")
    return _wrap(line)


def gate_lines(result: GateResult) -> list[str]:
    """Every finding of a gate (one per line of code), each NOT RUN reason once, the summary."""
    lines: list[str] = []
    seen_sites: set[tuple[str, int]] = set()
    reasons: list[str] = []
    for check in result.checks:
        if check.verdict is Verdict.NOT_RUN:
            # A check that couldn't finish shows why, never a failure.
            if check.not_run and check.not_run not in reasons:
                reasons.append(check.not_run)
            continue
        for found in check.findings:
            if found.site is not None:
                where = (found.site.file, found.site.line)
                if where in seen_sites:
                    continue
                seen_sites.add(where)
            lines += finding_lines(found)
    if result.needs_proof and result.verdict is Verdict.NOT_RUN and not reasons:
        reasons.append("no_proof")
    for reason in reasons:
        lines += _wrap(
            text(
                "verify.not_run", mark=text("mark.not_run"), reason=text(f"verify.not_run.{reason}")
            )
        )
    lines += summary_line(result)
    return lines


__all__ = ["MAX_COLUMNS", "finding_lines", "gate_lines", "summary_line"]
