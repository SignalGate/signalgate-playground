"""Deeper tests for GET /__playground/ledger: kinds, run tagging, delivery,
paging and the run filter (auth is covered in test_control.py)."""

from __future__ import annotations

import itertools
import json
import sys
import threading
import uuid

import pytest

from app.kit import actions, context, control, tokens
from app.kit.sequence import Ledger
from tests.contract.samples import INBOX, LEDGER, VALIDATION_ERROR, assert_same_shape

# Enough writers, each doing enough visits, that their appends interleave
# many times over; the interpreter is also asked to switch threads far more
# often than usual while they run.
_WRITER_THREADS = 6
_VISITS_PER_WRITER = 150
_SWITCH_INTERVAL_DIVISOR = 5000


def _auth(control_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {control_token}"}


def _act(client, ref: str, user: str, run: str | None = None) -> None:
    headers = {"X-Playground-Ref": ref}
    if run is not None:
        headers["X-Playground-Run"] = run
    response = client.post("/api/action", json={"user": user}, headers=headers)
    assert response.status_code == 200


def _deliver(client, control_token: str, ref: str) -> str:
    response = client.get(
        "/__playground/inbox", params=[("ref", ref)], headers=_auth(control_token)
    )
    assert response.status_code == 200
    assert_same_shape(response.json(), INBOX)
    return response.json()["tokens"][ref]


def _confirm(client, user: str, token: str, ref: str, run: str | None = None) -> None:
    headers = {"X-Playground-Ref": ref}
    if run is not None:
        headers["X-Playground-Run"] = run
    response = client.post("/api/confirm", json={"user": user, "token": token}, headers=headers)
    assert response.status_code == 200


def _read_all(client, control_token: str, run: str | None = None) -> list[dict]:
    entries: list[dict] = []
    cursor = 0
    params: dict = {"cursor": cursor, "limit": 7}
    if run is not None:
        params["run"] = run
    while True:
        params["cursor"] = cursor
        response = client.get("/__playground/ledger", params=params, headers=_auth(control_token))
        assert response.status_code == 200
        body = response.json()
        assert_same_shape(body, LEDGER)
        entries.extend(body["entries"])
        cursor = body["cursor"]
        if not body["more"]:
            break
    return entries


def test_ledger_is_empty_at_first(client, control_token):
    response = client.get("/__playground/ledger", headers=_auth(control_token))
    assert response.status_code == 200
    assert_same_shape(response.json(), LEDGER)
    assert response.json() == {"entries": [], "cursor": 0, "more": False}


def test_actions_and_confirms_are_tagged_with_their_kind_and_run(client, control_token):
    ref, user, run = f"ref-{uuid.uuid4().hex}", f"user-{uuid.uuid4().hex[:10]}", "run-one"
    _act(client, ref, user, run=run)
    token = _deliver(client, control_token, ref)
    _confirm(client, user, token, ref, run=run)

    entries = _read_all(client, control_token)
    kinds = {entry["kind"] for entry in entries if entry["ref"] == ref}
    assert kinds == {"action", "inbox", "confirm"}
    for entry in entries:
        if entry["ref"] == ref:
            assert entry["run"] == run


def test_inbox_entry_never_holds_the_token(client, control_token):
    ref, user = f"ref-{uuid.uuid4().hex}", f"user-{uuid.uuid4().hex[:10]}"
    _act(client, ref, user)
    _deliver(client, control_token, ref)

    entries = _read_all(client, control_token)
    inbox_entries = [e for e in entries if e["kind"] == "inbox" and e["ref"] == ref]
    assert len(inbox_entries) == 1
    assert set(inbox_entries[0]) == {"kind", "seq", "ref", "run"}


def test_no_inbox_entry_for_a_ref_with_nothing_pending(client, control_token):
    ref = f"ref-{uuid.uuid4().hex}"
    response = client.get(
        "/__playground/inbox", params=[("ref", ref)], headers=_auth(control_token)
    )
    assert response.status_code == 200
    assert_same_shape(response.json(), INBOX)
    assert response.json() == {"tokens": {}}

    entries = _read_all(client, control_token)
    assert not any(e["kind"] == "inbox" and e["ref"] == ref for e in entries)


def test_inbox_run_is_the_issuing_actions_run_even_without_a_run_header_on_inbox(
    client, control_token
):
    ref, user, run = f"ref-{uuid.uuid4().hex}", f"user-{uuid.uuid4().hex[:10]}", "run-issued"
    _act(client, ref, user, run=run)

    # The /inbox request itself carries no run header.
    response = client.get(
        "/__playground/inbox", params=[("ref", ref)], headers=_auth(control_token)
    )
    assert response.status_code == 200
    assert_same_shape(response.json(), INBOX)

    entries = _read_all(client, control_token)
    inbox_entry = next(e for e in entries if e["kind"] == "inbox" and e["ref"] == ref)
    assert inbox_entry["run"] == run


def test_confirm_ref_and_run_come_from_the_confirm_requests_own_headers(client, control_token):
    ref, user = f"ref-{uuid.uuid4().hex}", f"user-{uuid.uuid4().hex[:10]}"
    _act(client, ref, user, run="run-action")
    token = _deliver(client, control_token, ref)
    confirm_ref = f"ref-{uuid.uuid4().hex}"
    _confirm(client, user, token, confirm_ref, run="run-confirm")

    entries = _read_all(client, control_token)
    confirm_entry = next(e for e in entries if e["kind"] == "confirm" and e["ref"] == confirm_ref)
    assert confirm_entry["run"] == "run-confirm"
    assert confirm_entry["ok"] is True


def test_paging_yields_every_entry_exactly_once_in_seq_order_across_two_runs(client, control_token):
    run_a, run_b = "run-a", "run-b"
    for index in range(4):
        ref = f"ref-{uuid.uuid4().hex}"
        user = f"user-{uuid.uuid4().hex[:10]}"
        _act(client, ref, user, run=run_a if index % 2 == 0 else run_b)
        token = _deliver(client, control_token, ref)
        _confirm(client, user, token, ref, run=run_a if index % 2 == 0 else run_b)

    entries = _read_all(client, control_token)
    seqs = [entry["seq"] for entry in entries]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))
    # 4 actions + 4 deliveries + 4 confirms.
    assert len(entries) == 12


def test_run_filter_only_returns_matching_entries(client, control_token):
    run_a, run_b = "run-alpha", "run-beta"
    ref_a, ref_b = f"ref-{uuid.uuid4().hex}", f"ref-{uuid.uuid4().hex}"
    user_a, user_b = f"user-{uuid.uuid4().hex[:10]}", f"user-{uuid.uuid4().hex[:10]}"
    _act(client, ref_a, user_a, run=run_a)
    _act(client, ref_b, user_b, run=run_b)

    only_a = _read_all(client, control_token, run=run_a)
    assert all(entry["run"] == run_a for entry in only_a)
    assert any(entry["ref"] == ref_a for entry in only_a)
    assert not any(entry["ref"] == ref_b for entry in only_a)


def test_run_pattern_violations_are_stamped_as_the_empty_run(client, control_token):
    too_long = "x" * 65
    for bad_run in ("", too_long, "has space", "has/slash"):
        ref = f"ref-{uuid.uuid4().hex}"
        user = f"user-{uuid.uuid4().hex[:10]}"
        _act(client, ref, user, run=bad_run)
        entries = _read_all(client, control_token)
        entry = next(e for e in entries if e["ref"] == ref)
        assert entry["run"] == ""


def test_no_run_header_at_all_defaults_to_the_empty_run(client, control_token):
    ref = f"ref-{uuid.uuid4().hex}"
    user = f"user-{uuid.uuid4().hex[:10]}"
    _act(client, ref, user)  # no X-Playground-Run header sent
    entries = _read_all(client, control_token)
    entry = next(e for e in entries if e["ref"] == ref)
    assert entry["run"] == ""


def test_bad_cursor_is_400(client, control_token):
    response = client.get(
        "/__playground/ledger", params={"cursor": -1}, headers=_auth(control_token)
    )
    assert response.status_code == 400
    assert_same_shape(response.json(), VALIDATION_ERROR)
    assert response.json() == {"ok": False, "error": "validation_error"}


def test_bad_limit_is_400(client, control_token):
    response = client.get("/__playground/ledger", params={"limit": 0}, headers=_auth(control_token))
    assert response.status_code == 400
    assert_same_shape(response.json(), VALIDATION_ERROR)


def test_cursor_past_the_end_is_400(client, control_token):
    response = client.get(
        "/__playground/ledger", params={"cursor": 1_000_000}, headers=_auth(control_token)
    )
    assert response.status_code == 400
    assert_same_shape(response.json(), VALIDATION_ERROR)


def _one_visit(run: str) -> None:
    """One action, its token's delivery and its confirm, as three requests on
    this thread would make them: each appends one ledger entry."""
    ref, user = f"ref-{uuid.uuid4().hex}", f"user-{uuid.uuid4().hex[:10]}"
    context._ref.set(ref)
    context._run.set(run)
    actions.perform_action(user)
    token = control.read_inbox(ref=[ref])["tokens"][ref]
    assert tokens.verify(user, token) is True


def _visit_repeatedly(run: str, start: threading.Barrier) -> None:
    start.wait()
    for _ in range(_VISITS_PER_WRITER):
        _one_visit(run)


def test_concurrent_writers_page_back_every_entry_once_in_strict_seq_order(
    client, control_token, capsys
):
    start = threading.Barrier(_WRITER_THREADS)
    writers = [
        threading.Thread(target=_visit_repeatedly, args=(f"run-{index}", start))
        for index in range(_WRITER_THREADS)
    ]
    usual_switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(usual_switch_interval / _SWITCH_INTERVAL_DIVISOR)
    try:
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join()
    finally:
        sys.setswitchinterval(usual_switch_interval)

    entries = _read_all(client, control_token)
    seqs = [entry["seq"] for entry in entries]
    assert len(entries) == _WRITER_THREADS * _VISITS_PER_WRITER * 3
    assert all(earlier < later for earlier, later in itertools.pairwise(seqs))
    for kind in ("action", "inbox", "confirm"):
        assert sum(entry["kind"] == kind for entry in entries) == (
            _WRITER_THREADS * _VISITS_PER_WRITER
        )

    # The stdout echo comes out in that same order.
    echoed = [
        json.loads(line)["ledger"]["seq"]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith('{"ledger"')
    ]
    assert echoed == seqs


def test_the_ledger_assigns_seq_itself():
    ledger = Ledger()
    first = ledger.append({"kind": "action", "ref": "r", "run": ""})
    second = ledger.append({"kind": "confirm", "ref": "r", "run": ""})
    assert second["seq"] > first["seq"]
    with pytest.raises(ValueError):
        ledger.append({"kind": "action", "seq": second["seq"] + 1, "ref": "r", "run": ""})
    assert len(ledger) == 2
