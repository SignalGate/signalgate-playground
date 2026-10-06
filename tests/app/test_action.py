"""Tests for POST /api/action."""

from __future__ import annotations

from app.kit.sequence import ledger


def test_valid_user_succeeds(client, unique_ref, unique_user):
    response = client.post(
        "/api/action",
        json={"user": unique_user},
        headers={"X-Playground-Ref": unique_ref},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_reserved_prefix_is_always_rejected(client, unique_ref):
    before = len(ledger)
    response = client.post(
        "/api/action",
        json={"user": "pg-bad-anything"},
        headers={"X-Playground-Ref": unique_ref},
    )
    assert response.status_code == 400
    assert len(ledger) == before


def test_malformed_user_is_rejected(client, unique_ref):
    response = client.post(
        "/api/action",
        json={"user": "!not-allowed"},
        headers={"X-Playground-Ref": unique_ref},
    )
    assert response.status_code == 400


def test_missing_user_field_is_400_not_422(client):
    response = client.post("/api/action", json={})
    assert response.status_code == 400


def test_action_fail_header_gives_502(client, unique_ref, unique_user):
    before = len(ledger)
    response = client.post(
        "/api/action",
        json={"user": unique_user},
        headers={"X-Playground-Ref": unique_ref, "X-Playground-Action-Fail": "1"},
    )
    assert response.status_code == 502
    assert len(ledger) == before


def test_envelope_fields_are_accepted_and_optional(client, unique_ref, unique_user):
    response = client.post(
        "/api/action",
        json={
            "user": unique_user,
            "signalgate": {"anything": True},
            "signalgate_log": [1, 2, 3],
        },
        headers={"X-Playground-Ref": unique_ref},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
