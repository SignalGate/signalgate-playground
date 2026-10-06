"""``.playground/state.json``: atomic saves and defensive loads.

Covers the round trip, the set-aside path for anything ``load`` can't trust
(truncated JSON, a future version, a wrong shape), a version 1 file loading
as version 2, a version 2 file saved before the achievements and the time
were kept, sweeping stale temp files, a read-only save failing quietly,
the file's mode, the saved shape, and that repeated ``SIGKILL``s of a writer
loop never leave a half-written file.
"""

from __future__ import annotations

import json
import math
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from playground import statefile
from playground.machine.events import AppReady, Say
from playground.machine.machine import Machine
from tests.machine._scenarios import close_machine
from tests.machine.test_online_steps import at_code3

REPO_ROOT = Path(__file__).resolve().parent.parent

# A close-pending snapshot: exercises every non-null field of Machine.to_dict().
SNAPSHOT = close_machine().to_dict()


def test_round_trip_returns_the_same_snapshot(tmp_path):
    path = tmp_path / "state.json"

    assert statefile.save(path, SNAPSHOT) is True
    loaded, notice = statefile.load(path)

    assert notice is None
    assert dict(loaded) == SNAPSHOT


def test_missing_file_returns_none_and_no_notice(tmp_path):
    loaded, notice = statefile.load(tmp_path / "state.json")
    assert (loaded, notice) == (None, None)


def test_truncated_json_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"v": 1, "machine": {', encoding="utf-8")

    loaded, notice = statefile.load(path)

    assert (loaded, notice) == (None, "state.set_aside")
    assert not path.exists()
    assert (tmp_path / "state.json.bad").exists()


def test_future_version_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"v": 3, "machine": SNAPSHOT}), encoding="utf-8")

    loaded, notice = statefile.load(path)

    assert (loaded, notice) == (None, "state.set_aside")
    assert (tmp_path / "state.json.bad").exists()


def test_wrong_shape_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    broken = dict(SNAPSHOT)
    del broken["awaiting"]  # Machine.from_dict requires every key
    path.write_text(json.dumps({"v": 1, "machine": broken}), encoding="utf-8")

    loaded, notice = statefile.load(path)

    assert (loaded, notice) == (None, "state.set_aside")
    assert (tmp_path / "state.json.bad").exists()


def test_inconsistent_combination_is_set_aside(tmp_path):
    """A shape whose fields are each individually well-typed, but whose
    combination (a close question with no numbers) would crash the renderer
    the next time it's shown, is set aside just like any other bad shape."""
    path = tmp_path / "state.json"
    broken = dict(SNAPSHOT)
    assert broken["awaiting"] == "yes_no"
    broken["last_conversion"] = None
    path.write_text(json.dumps({"v": 1, "machine": broken}), encoding="utf-8")

    loaded, notice = statefile.load(path)

    assert (loaded, notice) == (None, "state.set_aside")
    assert (tmp_path / "state.json.bad").exists()


def _machine_with(**changes: object) -> dict:
    return {"v": 2, "machine": {**SNAPSHOT, **changes}}


# Each parses as JSON but is not a file save() could have written.
_NOT_WRITTEN_BY_SAVE = {
    "conversion_too_large_for_a_float": _machine_with(last_conversion=2**sys.float_info.max_exp),
    "negative_conversion": _machine_with(last_conversion=-SNAPSHOT["last_conversion"]),
    "conversion_above_one": _machine_with(last_conversion=1 + SNAPSHOT["last_conversion"]),
    "conversion_nan": _machine_with(last_conversion=math.nan),
    "conversion_infinite": _machine_with(last_conversion=math.inf),
    "negative_attack_in_ten": _machine_with(last_attack_in_ten=-SNAPSHOT["last_attack_in_ten"]),
    "unknown_machine_key": _machine_with(extra="x"),
    "unknown_top_level_key": {"v": 2, "machine": SNAPSHOT, "extra": 1},
    "version_true": {"v": True, "machine": SNAPSHOT},
    "version_float": {"v": 2.0, "machine": SNAPSHOT},
    "version_missing": {"machine": SNAPSHOT},
    "machine_missing": {"v": 2},
    "machine_not_an_object": {"v": 2, "machine": list(SNAPSHOT)},
    "v1_machine_not_an_object": {"v": 1, "machine": list(SNAPSHOT)},
    "v2_machine_missing_a_later_key": {
        "v": 2,
        "machine": {k: v for k, v in SNAPSHOT.items() if k != "rounds"},
    },
    "unknown_step": _machine_with(step="SOMEWHERE"),
    "later_chapter_without_a_session": _machine_with(step="CODE3", chapter1_done=True),
    "achievements_not_a_list": _machine_with(achievements="achievement.ch1"),
    "achievement_unknown": _machine_with(achievements=["achievement.elsewhere"]),
    "achievement_not_a_key": _machine_with(achievements=[1]),
    "achievements_unsorted": _machine_with(
        achievements=["achievement.connected", "achievement.ch1"]
    ),
    "achievement_twice": _machine_with(achievements=["achievement.ch1", "achievement.ch1"]),
    "elapsed_negative": _machine_with(elapsed_s=-1.0),
    "elapsed_infinite": _machine_with(elapsed_s=math.inf),
    "elapsed_not_a_number": _machine_with(elapsed_s="25 min"),
    "elapsed_true": _machine_with(elapsed_s=True),
    "only_one_of_the_later_keys": {
        "v": 2,
        "machine": {k: v for k, v in SNAPSHOT.items() if k != "elapsed_s"},
    },
    "top_level_not_an_object": [1, SNAPSHOT],
}


@pytest.mark.parametrize(
    "payload", list(_NOT_WRITTEN_BY_SAVE.values()), ids=list(_NOT_WRITTEN_BY_SAVE)
)
def test_a_file_save_could_not_have_written_is_set_aside(tmp_path, payload):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded, notice = statefile.load(path)

    assert (loaded, notice) == (None, "state.set_aside")
    assert not path.exists()
    assert (tmp_path / "state.json.bad").exists()


def test_a_loaded_snapshot_is_what_the_offered_machine_saves_back(tmp_path):
    """Whatever load() accepts, a machine offered it serialises back to the
    same snapshot until the user chooses."""
    path = tmp_path / "state.json"
    statefile.save(path, SNAPSHOT)

    loaded, notice = statefile.load(path)

    assert notice is None
    assert Machine(saved=loaded).to_dict() == dict(loaded)


def test_invalid_utf8_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"\xff\xfe\x00garbage")

    loaded, notice = statefile.load(path)

    assert (loaded, notice) == (None, "state.set_aside")
    assert (tmp_path / "state.json.bad").exists()


def test_truncated_mid_multibyte_character_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    # Truncated right after the first byte of a 2-byte UTF-8 sequence.
    path.write_bytes(b'{"v": 1, "machine": {\xc3')

    loaded, notice = statefile.load(path)

    assert (loaded, notice) == (None, "state.set_aside")
    assert (tmp_path / "state.json.bad").exists()


def test_deeply_nested_json_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    # Comfortably past whatever recursion limit this interpreter is running
    # with, so json.loads is guaranteed to raise RecursionError, not just a
    # JSONDecodeError.
    depth = sys.getrecursionlimit() * 2
    path.write_bytes(b"[" * depth)

    loaded, notice = statefile.load(path)

    assert (loaded, notice) == (None, "state.set_aside")
    assert (tmp_path / "state.json.bad").exists()


def test_set_aside_failure_reports_no_notice_and_leaves_the_file(tmp_path):
    """When the file can't even be moved aside (a read-only directory), load()
    must not claim it moved it: no notice, and the file is left in place."""
    path = tmp_path / "state.json"
    path.write_text("not json", encoding="utf-8")
    original_mode = tmp_path.stat().st_mode
    tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        loaded, notice = statefile.load(path)
    finally:
        tmp_path.chmod(original_mode)

    assert (loaded, notice) == (None, None)
    assert path.exists()


def test_stale_temp_files_are_swept_on_load(tmp_path):
    path = tmp_path / "state.json"
    stale = tmp_path / ".state.json.leftover123.tmp"
    stale.write_text("garbage", encoding="utf-8")

    loaded, notice = statefile.load(path)

    assert (loaded, notice) == (None, None)
    assert not stale.exists()


def test_stale_temp_files_are_swept_even_when_a_real_file_exists(tmp_path):
    path = tmp_path / "state.json"
    statefile.save(path, SNAPSHOT)
    stale = tmp_path / ".state.json.leftover456.tmp"
    stale.write_text("garbage", encoding="utf-8")

    loaded, notice = statefile.load(path)

    assert notice is None
    assert dict(loaded) == SNAPSHOT
    assert not stale.exists()


def test_save_into_a_read_only_directory_fails_quietly(tmp_path):
    path = tmp_path / "state.json"
    original_mode = tmp_path.stat().st_mode
    tmp_path.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = statefile.save(path, SNAPSHOT)
    finally:
        tmp_path.chmod(original_mode)

    assert result is False
    assert not path.exists()
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_saved_file_has_mode_0600(tmp_path):
    path = tmp_path / "state.json"
    statefile.save(path, SNAPSHOT)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_a_second_save_replaces_the_first_leaving_no_temp_file(tmp_path):
    path = tmp_path / "state.json"
    statefile.save(path, SNAPSHOT)
    other = dict(SNAPSHOT)
    other["running"] = True
    other["awaiting"] = None
    statefile.save(path, other)

    loaded, notice = statefile.load(path)
    assert notice is None
    assert dict(loaded) == other
    assert list(path.parent.glob(".state.json.*.tmp")) == []


# --- a version 1 file ------------------------------------------------------------

_V1_MACHINE = {k: SNAPSHOT[k] for k in ("offline_total", "running", "awaiting")} | {
    "chapter1_done": SNAPSHOT["chapter1_done"],
    "last_conversion": SNAPSHOT["last_conversion"],
    "last_attack_in_ten": SNAPSHOT["last_attack_in_ten"],
}


def test_a_version_1_file_loads_as_version_2(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"v": 1, "machine": _V1_MACHINE}), encoding="utf-8")

    loaded, notice = statefile.load(path)

    assert notice is None
    assert dict(loaded) == SNAPSHOT


def test_the_version_1_end_state_resumes_at_chapter_2(tmp_path):
    path = tmp_path / "state.json"
    ended = {**_V1_MACHINE, "awaiting": "any", "chapter1_done": True}
    path.write_text(json.dumps({"v": 1, "machine": ended}), encoding="utf-8")

    loaded, notice = statefile.load(path)

    assert notice is None
    assert (loaded["step"], loaded["awaiting"]) == ("KEY", "continue")
    assert loaded["achievements"] == ["achievement.ch1"]
    machine = Machine(saved=loaded)
    machine.on_event(AppReady())
    assert machine.handle("/continue")[-2] == Say("ch2.key.ask", {})


# --- a version 2 file from before achievements and time were kept ------------------


def test_an_older_version_2_file_loads_with_its_achievements_read_from_its_progress(tmp_path):
    current = at_code3().to_dict()
    assert current["achievements"] == ["achievement.ch1", "achievement.connected"]
    older = {k: v for k, v in current.items() if k not in ("achievements", "elapsed_s")}
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"v": 2, "machine": older}), encoding="utf-8")

    loaded, notice = statefile.load(path)

    assert notice is None
    assert dict(loaded) == {**older, "achievements": current["achievements"], "elapsed_s": None}
    # Offered, it saves back exactly what was loaded; picked up, it asks its question.
    machine = Machine(saved=loaded)
    assert machine.to_dict() == dict(loaded)
    machine.on_event(AppReady())
    assert machine.handle("/continue")[-2] == Say("ch3.ask", {})


def test_the_time_since_start_round_trips_as_a_duration(tmp_path):
    path = tmp_path / "state.json"
    snapshot = {**at_code3().to_dict(), "elapsed_s": 754.5}
    statefile.save(path, snapshot)

    loaded, notice = statefile.load(path)

    assert notice is None
    assert loaded["elapsed_s"] == 754.5
    assert Machine.from_dict(loaded).elapsed_s == 754.5


# --- schema: only known keys and types, nothing path-like ---------------------

_SCHEMA = {
    "offline_total": int,
    "running": bool,
    "awaiting": (str, type(None)),
    "chapter1_done": bool,
    "last_conversion": (int, float, type(None)),
    "last_attack_in_ten": (int, type(None)),
    "size": int,
    "step": (str, type(None)),
    "session": (dict, type(None)),
    "learn": (dict, type(None)),
    "prediction": (dict, type(None)),
    "rounds": list,
    "stage": (int, type(None)),
    "topup": int,
    "achievements": list,
    "elapsed_s": (int, float, type(None)),
}


def _assert_known_shape_and_no_path_like_strings(payload: dict) -> None:
    assert set(payload) == {"v", "machine"}
    assert payload["v"] == 2
    machine = payload["machine"]
    assert set(machine) == set(_SCHEMA)
    for key, allowed_types in _SCHEMA.items():
        value = machine[key]
        assert isinstance(value, allowed_types), (key, value)
        if isinstance(value, bool):
            continue
        if isinstance(value, str):
            assert "/" not in value and "\\" not in value, (key, value)


def test_saved_file_has_only_known_keys_and_types_with_no_path_like_strings(tmp_path):
    path = tmp_path / "state.json"
    statefile.save(path, SNAPSHOT)
    payload = json.loads(path.read_text(encoding="utf-8"))
    _assert_known_shape_and_no_path_like_strings(payload)


# --- repeated SIGKILL never corrupts the file ---------------------------------

_SIGKILL_ROUNDS = 20

_WRITER_SCRIPT = """
import sys
from pathlib import Path

from playground import statefile

path = Path(sys.argv[1])
from playground.machine.machine import Machine

snapshot = Machine().to_dict()
statefile.save(path, snapshot)
print("ready", flush=True)
while True:
    statefile.save(path, snapshot)
"""


def test_repeated_sigkill_never_leaves_an_unparseable_file(tmp_path):
    path = tmp_path / "state.json"

    for _ in range(_SIGKILL_ROUNDS):
        started_at = time.monotonic()
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", _WRITER_SCRIPT, str(path)],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            # The child prints only after its first save() returns, so this
            # never races an empty destination: by the time this line is
            # read, path.exists() is already true. The wait afterwards is
            # this round's own measured start-up time, not a fixed literal,
            # so slower runners still let the writer loop back into another
            # save() before the kill lands.
            ready_line = process.stdout.readline()
            assert ready_line.strip() == "ready"
            time_to_ready = time.monotonic() - started_at
            time.sleep(time_to_ready)
        finally:
            process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()

        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        _assert_known_shape_and_no_path_like_strings(payload)

        # A kill between mkstemp() and os.replace() can leave that attempt's
        # temp file behind (the destination itself is never half-written);
        # the next load() sweeps it, per "stale temp files are swept".
        statefile.load(path)
        assert list(path.parent.glob(".state.json.*.tmp")) == []
