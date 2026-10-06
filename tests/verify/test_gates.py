"""The gates never pass on what they couldn't check, and stay in step with the kit.

A check that couldn't run is NOT RUN, never PASS: an app that doesn't
answer, a missing key, a lease without the right envelopes, an unreadable
routes file, or a SignalGate the app can't reach.
"""

from __future__ import annotations

import asyncio
import socket

from app.kit import context, users
from app.kit.kit_transport import METHODS
from playground.verify import AppLink, Verdict, gate_lines, probes, run_code_gate, run_rehearsal
from tests.support.sdk_stub import Reply, install_sdk_stub
from tests.verify.harness import (
    CONTROL_TOKEN,
    FIXTURES,
    ROOT,
    code_gate,
    lease,
    load_router,
    probe_lease,
    serve,
)

PLAIN_3 = FIXTURES / "right" / "ch3_plain.py"
PLAIN_5 = FIXTURES / "right" / "ch5_plain.py"


def _reasons(result) -> set[str]:
    return {check.not_run for check in result.checks if check.not_run}


def test_the_probes_use_the_kits_own_hooks_and_names():
    assert probes.BAD_USER_PREFIX == users.RESERVED_BAD_PREFIX
    assert probes.ACTION_FAIL_HEADER.lower() == context.ACTION_FAIL_HEADER
    assert probes.REF_HEADER.lower() == context.REF_HEADER
    assert (probes.ACTION_METHOD, probes.SUCCESS_METHOD) == METHODS


def test_a_probe_lease_carries_what_each_chapter_needs(fake):
    for chapter in (3, 5):
        visits = probe_lease(fake, chapter)
        assert probes.probe_visits(visits, chapter) is not None
        assert {visit["class"] for visit in visits} == {"real"}
    chapter_3 = probe_lease(fake, 3)
    assert all("action_check" not in visit["envelopes"] for visit in chapter_3)
    assert probes.probe_visits(chapter_3, 5) is None


def test_a_lease_without_the_needed_envelopes_is_not_run(fake):
    visits = lease(fake, "learn", probes.PROBE_VISITS)
    with serve(load_router(PLAIN_5)) as link:
        result = asyncio.run(run_code_gate(link, visits, chapter=5, routes=PLAIN_5, root=ROOT))
    assert result.verdict is Verdict.NOT_RUN
    assert _reasons(result) == {"lease"}
    assert gate_lines(result)[-1].startswith("✓ 0 passed")


def test_an_app_that_doesnt_answer_is_not_run(fake):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    link = AppLink(f"http://127.0.0.1:{port}", CONTROL_TOKEN)
    visits = probe_lease(fake, 3)
    code = asyncio.run(run_code_gate(link, visits, chapter=3, routes=PLAIN_3, root=ROOT))
    rehearsal = asyncio.run(run_rehearsal(link, routes=PLAIN_5, root=ROOT))
    assert (code.verdict, _reasons(code)) == (Verdict.NOT_RUN, {"app"})
    assert (rehearsal.verdict, _reasons(rehearsal)) == (Verdict.NOT_RUN, {"app"})


def test_an_app_without_a_key_is_not_run(fake, monkeypatch, tmp_path):
    from app import signalgate_client
    from app.kit import settings

    empty = tmp_path / ".env"
    empty.write_text("SIGNALGATE_API_KEY=PASTE_YOUR_API_KEY_HERE\n")
    monkeypatch.setattr(settings, "ENV_PATH", empty)
    signalgate_client._reset()
    result = code_gate(PLAIN_3, fake, 3)
    assert (result.verdict, _reasons(result)) == (Verdict.NOT_RUN, {"key"})


def test_an_unreadable_routes_file_is_not_run(fake, tmp_path):
    broken = tmp_path / "routes.py"
    broken.write_text("def broken(:\n")
    visits = probe_lease(fake, 3)
    link = AppLink("http://127.0.0.1:9", CONTROL_TOKEN)
    result = asyncio.run(run_code_gate(link, visits, chapter=3, routes=broken, root=ROOT))
    assert (result.verdict, _reasons(result)) == (Verdict.NOT_RUN, {"routes"})
    rehearsal = asyncio.run(run_rehearsal(link, routes=broken, root=ROOT))
    assert (rehearsal.verdict, _reasons(rehearsal)) == (Verdict.NOT_RUN, {"routes"})


def test_a_signalgate_the_app_cant_reach_is_not_run_never_pass(fake, monkeypatch):
    # Every SDK request fails like a refused connection: the logs are retried,
    # then dropped, and no real event is ever accepted.
    visits = probe_lease(fake, 3)
    install_sdk_stub(monkeypatch, Reply(raise_="connect"))
    with serve(load_router(PLAIN_3)) as link:
        result = asyncio.run(run_code_gate(link, visits, chapter=3, routes=PLAIN_3, root=ROOT))
    assert result.verdict is Verdict.NOT_RUN
    assert result.accepted == 0
    assert _reasons(result) == {"signalgate"}
    assert result.findings == ()


def test_a_pass_is_bound_to_the_file_it_read(fake, tmp_path):
    routes = tmp_path / "routes.py"
    routes.write_bytes(PLAIN_3.read_bytes())
    result = code_gate(routes, fake, 3)
    assert result.verdict is Verdict.PASS
    assert result.routes_sha256 == probes.routes_digest(routes)
    routes.write_text(routes.read_text() + "\n# changed\n")
    assert result.routes_sha256 != probes.routes_digest(routes)
