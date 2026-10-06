"""The static checks on app/routes.py: the four facts they may fail a gate on, and nothing else.

A file with an API key in it is written here at test time, from pieces,
so no key-shaped string is ever committed.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from playground.verify import Verdict, gate_lines
from playground.verify import findings as F
from playground.verify.evidence import Sources
from playground.verify.static import SourceUnreadable, call_line, check_source
from tests.verify.harness import FIXTURES, ROOT, code_gate

# 32 characters after the prefix, built so no key shape sits in this file.
KEY = "pk_live_" + "Ab3-" * 8


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "routes.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return path


def _ids(tmp_path: Path, source: str, chapter: int) -> list[tuple[str, int]]:
    path = _write(tmp_path, source)
    found = check_source(path, chapter=chapter, sources=Sources(tmp_path))
    return [(f.id, f.site.line) for f in found]


def test_the_pristine_routes_file_has_no_static_finding():
    routes = ROOT / "app" / "routes.py"
    for chapter in (3, 5):
        assert check_source(routes, chapter=chapter, sources=Sources(ROOT)) == []


@pytest.mark.parametrize(
    "source",
    [
        f'API_KEY = "{KEY}"\n',
        f'client_key = f"{KEY}"\n',
        f'def key():\n    """Uses {KEY}."""\n',
    ],
)
def test_a_key_in_the_code_is_found(tmp_path, source):
    assert [finding_id for finding_id, _ in _ids(tmp_path, source, 3)] == [F.KEY_IN_CODE]


def test_a_key_in_a_comment_or_a_longer_token_is_not_a_finding(tmp_path):
    source = f'# {KEY}\nTOKEN = "{KEY}x"\nOTHER = "x{KEY}"\n'
    assert _ids(tmp_path, source, 3) == []


def test_a_key_in_the_code_fails_the_code_gate_and_is_never_shown(tmp_path, fake):
    base = (FIXTURES / "right" / "ch3_plain.py").read_text(encoding="utf-8")
    path = tmp_path / "routes.py"
    path.write_text(base + f'\nSIGNALGATE_KEY = "{KEY}"\n', encoding="utf-8")
    result = code_gate(path, fake, 3)
    assert result.verdict is Verdict.FAIL
    assert F.KEY_IN_CODE in result.finding_ids
    rendered = "\n".join(gate_lines(result))
    assert KEY not in rendered
    assert "pk_live_…" in rendered


@pytest.mark.parametrize(
    "source",
    [
        "def action():\n    sg().check(ev)\n",
        "def action():\n    get_client().check(ev)\n",
        "def action():\n    client = sg()\n    client.check(ev)\n",
        "def action():\n    if sg().check(ev).action == 'block':\n        pass\n",
    ],
)
def test_a_live_check_in_chapter_3_is_found_and_allowed_in_chapter_5(tmp_path, source):
    ids = [finding_id for finding_id, _ in _ids(tmp_path, source, 3)]
    assert ids == [F.CHECK_TOO_EARLY]
    assert _ids(tmp_path, source, 5) == []


def test_other_check_calls_and_commented_ones_are_not_findings(tmp_path):
    source = "# sg().check(ev)\ndef action():\n    tokens.check(x)\n    thing().check(ev)\n"
    assert _ids(tmp_path, source, 3) == []


@pytest.mark.parametrize(
    "source",
    [
        "from signalgate import Client\ndef action():\n    Client(key)\n",
        "import signalgate\ndef action():\n    signalgate.Client(key)\n",
        "import signalgate as sgk\ndef action():\n    sgk.Client(key)\n",
        "import signalgate.client\ndef action():\n    signalgate.client.Client(key)\n",
        "from signalgate import Client as SignalGate\ndef action():\n    SignalGate(key)\n",
        "from signalgate import *\ndef action():\n    Client(key)\n",
        (
            "from signalgate import Client\ndef action():\n"
            "    with Client(key) as client:\n        client.log(ev)\n"
        ),
        "def action():\n    from signalgate import Client\n    Client(key)\n",
    ],
)
def test_the_sdk_client_built_in_the_routes_file_is_found(tmp_path, source):
    ids = [finding_id for finding_id, _ in _ids(tmp_path, source, 3)]
    assert ids == [F.CLIENT_PER_REQUEST]


@pytest.mark.parametrize(
    "source",
    [
        "import httpx\ndef action():\n    with httpx.Client() as http:\n        http.get(url)\n",
        "import httpx\nHTTP = httpx.Client(timeout=2.0)\n",
        "from httpx import Client\ndef action():\n    Client()\n",
        "import signalgate\nfrom httpx import Client\ndef action():\n    Client()\n",
        "import signalgate\ndef action():\n    thing.Client(key)\n",
        "def action():\n    Client(key)\n",
    ],
)
def test_a_client_that_is_not_the_sdks_is_not_a_finding(tmp_path, source):
    assert _ids(tmp_path, source, 3) == []
    assert _ids(tmp_path, source, 5) == []


def test_an_http_client_in_a_handler_passes_the_code_gate(tmp_path, fake):
    base = (FIXTURES / "right" / "ch3_plain.py").read_text(encoding="utf-8")
    source = base.replace("from typing import Any\n", "from typing import Any\n\nimport httpx\n")
    taught = "    # the action happens here\n    perform_action(body.user)\n"
    assert taught in source
    source = source.replace(
        taught,
        "    # the action happens here\n    with httpx.Client(timeout=1.0):\n"
        "        perform_action(body.user)\n",
    )
    path = tmp_path / "routes.py"
    path.write_text(source, encoding="utf-8")
    assert check_source(path, chapter=3, sources=Sources(tmp_path)) == []
    result = code_gate(path, fake, 3)
    assert result.verdict is Verdict.PASS, gate_lines(result)


def test_the_sdk_client_built_through_its_package_fails_the_code_gate(tmp_path, fake):
    base = (FIXTURES / "wrong" / "client_per_request.py").read_text(encoding="utf-8")
    source = base.replace("from signalgate import Client\n", "import signalgate\n")
    source = source.replace("with Client(", "with signalgate.Client(")
    assert "signalgate.Client(" in source and "import Client" not in source
    path = tmp_path / "routes.py"
    path.write_text(source, encoding="utf-8")
    line = next(n for n, text in enumerate(source.splitlines(), 1) if "signalgate.Client(" in text)
    found = check_source(path, chapter=3, sources=Sources(tmp_path))
    assert [(f.id, f.site.line) for f in found] == [(F.CLIENT_PER_REQUEST, line)]
    result = code_gate(path, fake, 3)
    assert result.verdict is Verdict.FAIL
    assert F.CLIENT_PER_REQUEST in result.finding_ids


def test_a_check_called_straight_from_async_def_is_found(tmp_path):
    source = "async def action():\n    result = sg().check(ev)\n"
    path = _write(tmp_path, source)
    found = check_source(path, chapter=5, sources=Sources(tmp_path))
    assert [(f.id, f.site.line, f.params["function"]) for f in found] == [
        (F.CHECK_BLOCKS_EVENT_LOOP, 2, "action")
    ]


@pytest.mark.parametrize(
    "source",
    [
        "async def action():\n    await asyncio.to_thread(sg().check, ev)\n",
        "async def action():\n    await run_in_threadpool(lambda: sg().check(ev))\n",
        (
            "async def action():\n    def gate():\n        return sg().check(ev)\n"
            "    await asyncio.to_thread(gate)\n"
        ),
        "def action():\n    sg().check(ev)\n",
    ],
)
def test_a_check_off_the_event_loop_is_not_a_finding(tmp_path, source):
    assert _ids(tmp_path, source, 5) == []


def test_every_right_fixture_is_clean_and_every_static_fixture_is_found():
    sources = Sources(ROOT)
    for path in (FIXTURES / "right").glob("*.py"):
        chapter = 3 if path.stem.startswith("ch3") else 5
        assert check_source(path, chapter=chapter, sources=sources) == [], path.name
    async_def = check_source(
        FIXTURES / "wrong" / "check_in_async_def.py", chapter=5, sources=sources
    )
    assert [f.id for f in async_def] == [F.CHECK_BLOCKS_EVENT_LOOP]


def test_an_unreadable_file_is_reported_as_such(tmp_path):
    with pytest.raises(SourceUnreadable):
        check_source(_write(tmp_path, "def broken(:\n"), chapter=3, sources=Sources(tmp_path))
    with pytest.raises(SourceUnreadable):
        check_source(tmp_path / "missing.py", chapter=3, sources=Sources(tmp_path))


def test_call_line_points_at_the_action():
    routes = ROOT / "app" / "routes.py"
    line = call_line(routes, "perform_action")
    assert line is not None
    assert "perform_action(" in routes.read_text().splitlines()[line - 1]
    assert call_line(routes, "no_such_call") is None
