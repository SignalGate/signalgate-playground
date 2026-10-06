"""Every dashboard word the console's copy quotes is the dashboard's own, as the labels contract pins it.

Chapters 4-6 tell you which buttons to press and read back what the
dashboard shows while it works. Each quoted label here must appear in its
copy exactly as ``contracts/playground-labels.v1.json`` stores it, so a
dashboard change that breaks the contract also shows up here.
"""

from __future__ import annotations

import json
import re

import pytest

from playground.copy.en import TEXT
from tests.contract.contracts import CONTRACTS_DIR

LABELS = json.loads((CONTRACTS_DIR / "playground-labels.v1.json").read_text(encoding="utf-8"))[
    "labels"
]

# copy key -> the labels it quotes.
QUOTES = {
    "ch4.click_path": (
        "nav_dashboard",
        "flows_heading",
        "new_flow",
        "form_name",
        "form_method",
        "form_target_method",
        "form_analysis_window",
        "form_conversion_threshold",
        "form_min_group_size",
        "form_action",
        "action_block",
        "create",
        "dry_run",
    ),
    "ch4.ask": ("dry_run",),
    "ch4.start": ("start",),
    "ch4.lesson": ("dry_run",),
    "ch4.warning": ("heavy_block_warning", "metric_blocked"),
    "ch4.where": ("nav_analytics", "nav_events"),
    "ch4.prediction": ("status_dry_run_complete", "metric_conv_now", "metric_conv_pred"),
    "ch2.intro": ("nav_api_access",),
    "wait.draft": ("status_awaiting_dry_run",),
    "wait.queued": ("status_dry_run_queued",),
    "wait.ready": ("status_dry_run_complete",),
    "wait.promoting": ("status_promoting",),
    "wait.active": ("status_active",),
    "wait.paused": ("status_paused",),
    "wf.paused": ("start",),
    "wf.draft": ("dry_run",),
    "wf.not_active": ("start",),
    "ch5.readiness.over_blocking": ("stop", "start"),
    "ch6.keep": ("stop",),
    "ch6.paused": ("start",),
}


@pytest.mark.parametrize(("key", "labels"), list(QUOTES.items()), ids=list(QUOTES))
def test_the_copy_quotes_the_dashboard_exactly(key, labels):
    copy = TEXT[key]
    for name in labels:
        label = LABELS[name]
        for field in ("text", "title"):
            if field in label:
                assert label[field] in copy, (key, name, label[field])


def test_the_new_flow_button_is_named_with_its_plus_icon():
    assert LABELS["new_flow"]["icon"] == "plus"
    assert '"New flow" (the button with the plus icon)' in TEXT["ch4.click_path"]


# The form's defaults, as the dashboard fills them in: the click path shows
# each value next to its label, to be left as it is.
FORM_DEFAULTS = {
    "form_analysis_window": "96",
    "form_conversion_threshold": "0.5",
    "form_min_group_size": "50",
    "form_action": LABELS["action_block"]["text"],
}


@pytest.mark.parametrize(("name", "value"), list(FORM_DEFAULTS.items()), ids=list(FORM_DEFAULTS))
def test_the_click_path_shows_each_default_the_way_the_form_does(name, value):
    line = rf"^ +{re.escape(LABELS[name]['text'])} +{re.escape(value)} \(leave\)$"
    assert re.search(line, TEXT["ch4.click_path"], re.MULTILINE), (name, value)


def test_the_heavy_block_warning_sits_next_to_the_blocked_number():
    glyph = LABELS["heavy_block_warning"]["text"]
    assert f"{glyph} next to {LABELS['metric_blocked']['text']} " in TEXT["ch4.warning"]
