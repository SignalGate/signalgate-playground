"""The files under ``contracts/``, each built from the specs in this package.

- ``playground-api.v1.json``: the playground API (``playground_api.py``);
- ``app-control.v1.json``: the sample app's control routes, and the headers
  the console sets on its requests (``samples.py``);
- ``playground-labels.v1.json``: the dashboard labels the copy quotes
  (``labels.py``).

``scripts/export_contracts.py`` writes them; a test fails when a committed
file differs from what ``render`` gives for it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.contract import labels, playground_api
from tests.contract.json_schema import DIALECT, HeaderSet, Route, contract_document, to_json_schema
from tests.contract.samples import (
    CALLS,
    CANARY,
    CANARY_REQUEST,
    CONTROL_HEADERS,
    FORBIDDEN,
    HEALTH,
    INBOX,
    LEDGER,
    METRICS,
    SHUTDOWN,
    UNAUTHORIZED,
    VALIDATION_ERROR,
    VISIT_HEADERS,
)

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"

PLAYGROUND_API_FILE = "playground-api.v1.json"
APP_CONTROL_FILE = "app-control.v1.json"
LABELS_FILE = "playground-labels.v1.json"

CONTROL_PREFIX = "/__playground"

# Every control route refuses a request that carries X-Forwarded-For (403)
# or lacks the per-boot token (401).
_REFUSALS = {401: UNAUTHORIZED, 403: FORBIDDEN}

APP_CONTROL_ROUTES: dict[str, Route] = {
    "health": Route("GET", f"{CONTROL_PREFIX}/health", responses={200: HEALTH, **_REFUSALS}),
    "metrics": Route("GET", f"{CONTROL_PREFIX}/metrics", responses={200: METRICS, **_REFUSALS}),
    "ledger": Route(
        "GET",
        f"{CONTROL_PREFIX}/ledger",
        query=("run", "cursor", "limit"),
        responses={200: LEDGER, 400: VALIDATION_ERROR, **_REFUSALS},
    ),
    "calls": Route(
        "GET",
        f"{CONTROL_PREFIX}/calls",
        query=("cursor", "limit"),
        responses={200: CALLS, 400: VALIDATION_ERROR, **_REFUSALS},
    ),
    "canary": Route(
        "POST",
        f"{CONTROL_PREFIX}/canary",
        request=CANARY_REQUEST,
        responses={200: CANARY, 400: VALIDATION_ERROR, **_REFUSALS},
    ),
    "shutdown": Route("POST", f"{CONTROL_PREFIX}/shutdown", responses={200: SHUTDOWN, **_REFUSALS}),
    "inbox": Route(
        "GET", f"{CONTROL_PREFIX}/inbox", query=("ref",), responses={200: INBOX, **_REFUSALS}
    ),
}

APP_CONTROL_HEADERS = {
    "control": HeaderSet(f"every {CONTROL_PREFIX} request", CONTROL_HEADERS),
    "visitor": HeaderSet("a visitor's POST /api/action and POST /api/confirm", VISIT_HEADERS),
}


def labels_document() -> dict[str, Any]:
    """The labels file: the schema every entry follows, then the entries."""
    return {
        "title": labels.TITLE,
        "version": 1,
        "dialect": DIALECT,
        "schema": to_json_schema(labels.LABELS_SPEC),
        "labels": labels.LABELS,
    }


def documents() -> dict[str, dict[str, Any]]:
    """Every contract file's content, by file name."""
    return {
        PLAYGROUND_API_FILE: contract_document(
            playground_api.TITLE, playground_api.HEADER_SETS, playground_api.ROUTES
        ),
        APP_CONTROL_FILE: contract_document(
            "Sample app control plane", APP_CONTROL_HEADERS, APP_CONTROL_ROUTES
        ),
        LABELS_FILE: labels_document(),
    }


def render(document: dict[str, Any]) -> str:
    """A document as the committed file's text."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


__all__ = [
    "APP_CONTROL_FILE",
    "APP_CONTROL_HEADERS",
    "APP_CONTROL_ROUTES",
    "CONTRACTS_DIR",
    "LABELS_FILE",
    "PLAYGROUND_API_FILE",
    "documents",
    "labels_document",
    "render",
]
