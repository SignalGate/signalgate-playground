"""What happens when `app.routes` calls `perform_action(user)`.

This validates the user, records the attempt in the ledger, and queues a
single-use confirmation token in the inbox for whoever picks it up next.
It can also be told to fail on purpose, for exercising the failure path.
No network code.
"""

from __future__ import annotations

from app.kit import context, inbox, tokens
from app.kit.errors import ActionFailed, InvalidUser
from app.kit.sequence import ledger
from app.kit.users import is_valid_user

__all__ = ["perform_action"]


def perform_action(user: str) -> None:
    """Run the action for `user`.

    Raises `InvalidUser` if `user` doesn't pass validation, or `ActionFailed`
    if the caller asked this request to fail
    (`X-Playground-Action-Fail: 1`). Otherwise records the attempt and
    issues a confirmation token.
    """
    if not is_valid_user(user):
        raise InvalidUser(user)
    if context.action_should_fail():
        raise ActionFailed(user)

    ref = context.get_ref()
    run = context.get_run()
    ledger.append(
        {
            "kind": "action",
            "ref": ref,
            "user": user,
            "client_ip": context.get_client_ip(),
            "run": run,
        }
    )
    token = tokens.issue(user)
    inbox.put(ref, token, run)
