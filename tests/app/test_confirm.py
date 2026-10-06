"""Tests for POST /api/confirm, reached through /api/action and the inbox."""

from __future__ import annotations


def _issue_token(client, control_token: str, ref: str, user: str) -> str:
    action = client.post("/api/action", json={"user": user}, headers={"X-Playground-Ref": ref})
    assert action.status_code == 200
    inbox = client.get(
        "/__playground/inbox",
        params=[("ref", ref)],
        headers={"Authorization": f"Bearer {control_token}"},
    )
    assert inbox.status_code == 200
    return inbox.json()["tokens"][ref]


def test_confirm_with_right_token_succeeds(client, control_token, unique_ref, unique_user):
    token = _issue_token(client, control_token, unique_ref, unique_user)
    response = client.post("/api/confirm", json={"user": unique_user, "token": token})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_confirm_is_single_use(client, control_token, unique_ref, unique_user):
    token = _issue_token(client, control_token, unique_ref, unique_user)
    first = client.post("/api/confirm", json={"user": unique_user, "token": token})
    assert first.status_code == 200

    second = client.post("/api/confirm", json={"user": unique_user, "token": token})
    assert second.status_code == 400


def test_confirm_rejects_wrong_user_binding(client, control_token, unique_ref, unique_user):
    token = _issue_token(client, control_token, unique_ref, unique_user)
    response = client.post("/api/confirm", json={"user": "someone-else", "token": token})
    assert response.status_code == 400


def test_confirm_rejects_unknown_token(client, unique_user):
    response = client.post("/api/confirm", json={"user": unique_user, "token": "not-a-real-token"})
    assert response.status_code == 400


def test_action_response_never_carries_the_token(client, unique_ref, unique_user):
    response = client.post(
        "/api/action",
        json={"user": unique_user},
        headers={"X-Playground-Ref": unique_ref},
    )
    assert response.json() == {"ok": True}
