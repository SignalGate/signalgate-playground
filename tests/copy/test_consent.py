"""The consent the console asks for before it opens a playground session.

The console sends ``CONSENT_VERSION`` with every new session, so the version
has to change whenever the consent text does.
"""

from __future__ import annotations

import hashlib

from playground.api.client import CONSENT_VERSION
from playground.copy.en import text

AUTHORITY = (
    "By continuing you confirm you're allowed to use this workspace's API key and to add "
    "tutorial traffic to it."
)
TELEMETRY = (
    "I'll also tell SignalGate which tutorial checks failed and your run's conversion "
    "before and after, never your code or keys."
)
OPT_OUT = "Run with --no-telemetry to turn that off."

# The consent text each version stands for, as a digest. A change to the
# text fails here until CONSENT_VERSION moves on and gets its own entry.
CONSENT_DIGESTS = {
    "2": "2953436ee4c2da2583ae664afb38b8bbdbc6c5f77957ad50a7e61e6cfc8a0178",
}


def test_the_consent_asks_about_the_key_right_before_the_question():
    paragraphs = text("ch2.consent").split("\n\n")
    assert paragraphs[-1] == "Do you agree? (yes/no)"
    assert paragraphs[-2] == AUTHORITY


def test_the_consent_says_what_telemetry_reports_and_how_to_turn_it_off():
    assert f"{TELEMETRY} {OPT_OUT}" in text("ch2.consent")
    help_line = text("cli.help.no_telemetry")
    assert "tutorial checks failed" in help_line
    assert "conversion before and after" in help_line


def test_the_consent_version_names_this_text():
    digest = hashlib.sha256(text("ch2.consent").encode("utf-8")).hexdigest()
    assert CONSENT_DIGESTS[CONSENT_VERSION] == digest
