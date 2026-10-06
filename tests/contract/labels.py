"""The dashboard's own words that the playground's copy quotes, exactly as the dashboard shows them.

Each entry is the text a person sees (``text``), a hover title
(``title``), or a line with values filled in (``template``, with
``{name}`` placeholders), plus the kind of element it is and a short hint
of where it sits on the page. ``icon`` names an icon drawn next to the text
that isn't part of its accessible name.

These are the dashboard's strings, not the console's: the console's own
copy may word things its own way, and quotes these when it points at the
page.
"""

from __future__ import annotations

from tests.contract.samples import STR, Enum, MapOf, Obj, Opt

ELEMENTS = (
    "button",
    "heading",
    "nav_link",
    "form_label",
    "select_option",
    "status",
    "metric_label",
    "warning_glyph",
)

LABEL = Obj(
    {
        "text": Opt(STR),
        "title": Opt(STR),
        "template": Opt(STR),
        "icon": Opt(Enum("plus")),
        "element": Enum(*ELEMENTS),
        "source_hint": STR,
    }
)
LABELS_SPEC = MapOf(LABEL)

# The keys an entry quotes the dashboard with; every entry has at least one.
QUOTED_KEYS = ("text", "title", "template")

LABELS: dict[str, dict[str, str]] = {
    # Navigation.
    "nav_dashboard": {
        "text": "Dashboard",
        "element": "nav_link",
        "source_hint": "sidebar navigation",
    },
    "nav_analytics": {
        "text": "Analytics",
        "element": "nav_link",
        "source_hint": "sidebar navigation",
    },
    "nav_events": {"text": "Events", "element": "nav_link", "source_hint": "sidebar navigation"},
    "nav_api_access": {
        "text": "API access",
        "element": "nav_link",
        "source_hint": "sidebar navigation",
    },
    # The Flows card and its buttons.
    "flows_heading": {"text": "Flows", "element": "heading", "source_hint": "Flows card title"},
    "new_flow": {
        "text": "New flow",
        "icon": "plus",
        "element": "button",
        "source_hint": "Flows card header; the text is the button's accessible name",
    },
    "create": {
        "text": "Create",
        "element": "button",
        "source_hint": "Flows card editor, while creating a flow",
    },
    "edit": {"text": "Edit", "element": "button", "source_hint": "Flows card editor"},
    "dry_run": {
        "text": "▶ Dry Run",
        "element": "button",
        "source_hint": "Flows card editor, on a draft",
    },
    "start": {
        "text": "▶ Start",
        "element": "button",
        "source_hint": "Flows card editor, on a draft or a paused flow",
    },
    "stop": {
        "text": "■ Stop",
        "element": "button",
        "source_hint": "Flows card editor, on an active flow",
    },
    "rerun_dry_run": {
        "text": "↻ Rerun Dry Run",
        "element": "button",
        "source_hint": "Flows card editor, on a live flow",
    },
    # The flow form.
    "form_name": {"text": "name", "element": "form_label", "source_hint": "flow form"},
    "form_method": {"text": "method", "element": "form_label", "source_hint": "flow form"},
    "form_target_method": {
        "text": "target_method",
        "element": "form_label",
        "source_hint": "flow form",
    },
    "form_analysis_window": {
        "text": "analysis_window (h)",
        "element": "form_label",
        "source_hint": "flow form",
    },
    "form_conversion_threshold": {
        "text": "conversion threshold (%)",
        "element": "form_label",
        "source_hint": "flow form",
    },
    "form_min_group_size": {
        "text": "min group size",
        "element": "form_label",
        "source_hint": "flow form",
    },
    "form_action": {"text": "action", "element": "form_label", "source_hint": "flow form"},
    "action_block": {
        "text": "block",
        "element": "select_option",
        "source_hint": "flow form, action picker",
    },
    "action_dry_run_block": {
        "text": "dry_run_block",
        "element": "select_option",
        "source_hint": "flow form, action picker",
    },
    # Run results: the panel, its status line and its numbers.
    "results_heading": {
        "text": "Dry Run Results",
        "element": "heading",
        "source_hint": "results strip under the selected flow",
    },
    "progress_stage": {
        "template": "stage {stage} of {stages_total} · {percent}%",
        "element": "status",
        "source_hint": "results strip status, while a dry run is running",
    },
    "status_dry_run_queued": {
        "text": "dry run queued",
        "element": "status",
        "source_hint": "results strip status, before a dry run starts",
    },
    "status_dry_run_stalled": {
        "text": "dry run — no response",
        "element": "status",
        "source_hint": "results strip status, when a dry run stops reporting",
    },
    "status_awaiting_dry_run": {
        "text": "awaiting dry run",
        "element": "status",
        "source_hint": "results strip status, on a draft never run",
    },
    "status_dry_run_in_progress": {
        "text": "dry run in progress",
        "element": "status",
        "source_hint": "results strip status, and the disabled buttons' text",
    },
    "status_dry_run_complete": {
        "text": "dry run complete",
        "element": "status",
        "source_hint": "results strip status, on a draft ready to start",
    },
    "status_promoting": {
        "text": "promoting…",
        "element": "status",
        "source_hint": "results strip status, after Start on a draft",
    },
    "status_active": {
        "text": "active",
        "element": "status",
        "source_hint": "results strip status, on a live flow",
    },
    "status_paused": {
        "text": "paused",
        "element": "status",
        "source_hint": "results strip status, on a stopped flow",
    },
    "metric_conv_now": {
        "text": "conv now",
        "element": "metric_label",
        "source_hint": "results strip numbers",
    },
    "metric_conv_pred": {
        "text": "conv pred",
        "element": "metric_label",
        "source_hint": "results strip numbers",
    },
    "metric_blocked": {
        "text": "blocked",
        "element": "metric_label",
        "source_hint": "results strip numbers",
    },
    "heavy_block_warning": {
        "text": "⚠",
        "title": "Blocks >25% of actions",
        "element": "warning_glyph",
        "source_hint": "results strip, next to blocked; the title shows on hover",
    },
}

TITLE = "Dashboard labels the playground quotes"

__all__ = ["ELEMENTS", "LABEL", "LABELS", "LABELS_SPEC", "QUOTED_KEYS", "TITLE"]
