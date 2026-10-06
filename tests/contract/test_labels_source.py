"""Every entry in the labels file is still written, verbatim, in the dashboard's own source.

The dashboard's source is a separate checkout. The test finds it through
``PLAYGROUND_DASHBOARD_SRC``, or as a directory next to this checkout (or
next to the main checkout, from a git worktree) that holds the three
dashboard components the labels come from. With no such checkout it skips.

A ``text`` or ``title`` must appear as a whole string: a form label as a
``<label>``'s whole content, a title as a ``title="..."`` attribute, and
anything else bounded by a quote, a tag bracket or white space. A
``template``'s literal parts must appear in order inside one template
string, with an interpolation where each placeholder is.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.contract.labels import LABELS

DASHBOARD_ENV = "PLAYGROUND_DASHBOARD_SRC"
COMPONENTS = ("admin/FlowsCard.tsx", "admin/RunResults.tsx", "app/AppShell.tsx")

_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


def _is_dashboard(directory: Path) -> bool:
    return all((directory / "src" / "components" / name).is_file() for name in COMPONENTS)


def _checkout_roots() -> list[Path]:
    here = Path(__file__).resolve().parents[2]
    roots = [here]
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=here,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return roots
    main = Path(common).parent
    if main != here:
        roots.append(main)
    return roots


def _dashboard_checkout() -> Path | None:
    candidates: list[Path] = []
    configured = os.environ.get(DASHBOARD_ENV)
    if configured:
        candidates.append(Path(configured))
    for root in _checkout_roots():
        try:
            candidates.extend(sorted(p for p in root.parent.iterdir() if p.is_dir()))
        except OSError:
            continue
    return next((c for c in candidates if _is_dashboard(c)), None)


def _whole(text: str) -> re.Pattern[str]:
    return re.compile(r"(?<=[>\"'`\s])" + re.escape(text) + r"(?=[<\"'`\s])")


def _pattern(key: str, value: str, element: str) -> re.Pattern[str]:
    if key == "template":
        literals = [re.escape(part) for part in _PLACEHOLDER.split(value)]
        return re.compile("`" + r"\$\{[^}`]+\}".join(literals) + "`")
    if key == "title":
        return re.compile(r'title="' + re.escape(value) + '"')
    if element == "form_label":
        return re.compile(r"<label\b[^>]*>" + re.escape(value) + r"</label>")
    return _whole(value)


def test_the_template_pattern_matches_an_interpolated_string():
    pattern = _pattern("template", "stage {n} of {total} · {p}%", "status")
    assert pattern.search("x = `stage ${p.stage} of ${p.total} · ${Math.round(f * 100)}%`;")
    assert not pattern.search("x = `stage ${p.stage} of ${p.total}%`;")


def test_a_whole_string_is_not_found_inside_a_longer_one():
    assert _whole("Create").search('{busy ? "Creating…" : "Create"}')
    assert not _whole("Create").search('{busy ? "Creating…" : "Created"}')
    assert _pattern("text", "name", "form_label").search('<label htmlFor="x">name</label>')
    assert not _pattern("text", "name", "form_label").search("<span>name</span>")


def test_every_label_is_still_in_the_dashboard_source():
    checkout = _dashboard_checkout()
    if checkout is None:
        pytest.skip(f"no dashboard checkout found (set {DASHBOARD_ENV} to point at one)")
    source = "\n".join(
        (checkout / "src" / "components" / name).read_text(encoding="utf-8") for name in COMPONENTS
    )
    missing = []
    for label_key, entry in LABELS.items():
        for key in ("text", "title", "template"):
            if key in entry and not _pattern(key, entry[key], entry["element"]).search(source):
                missing.append(f"{label_key}.{key}: {entry[key]!r}")
    assert missing == [], "labels no longer in the dashboard source:\n" + "\n".join(missing)
