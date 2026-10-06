"""Tests for the sample-app file watcher."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import time
from pathlib import Path

import pytest

import playground.supervisor.watcher as watcher_module
from playground.supervisor.watcher import AppWatcher, code_hash, is_watched
from playground.traffic.local_constants import RELOAD_DEBOUNCE_S

pytestmark = pytest.mark.usefixtures("_force_polling")

# Generous bounds derived from the real debounce, so a genuine regression
# fails promptly instead of hanging the suite; never a bare literal.
_SETTLE_TIMEOUT_S = RELOAD_DEBOUNCE_S * 97
_TOUCH_SPACING_S = RELOAD_DEBOUNCE_S / 3
_TOUCH_SETTLE_S = RELOAD_DEBOUNCE_S * 4
# How often the polling backend looks, and the gap between the burst
# test's writes: small enough that all 5 writes land well inside 100 ms.
_POLL_S = RELOAD_DEBOUNCE_S / 50
_BURST_WINDOW_S = RELOAD_DEBOUNCE_S / 3
# Every save moves its file's mtime this far past the previous save's.
_MTIME_STEP_S = RELOAD_DEBOUNCE_S * 7


async def _wait_until(predicate, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(_POLL_S)


@pytest.fixture
def _force_polling(monkeypatch):
    """Watchfiles' native backend can miss changes in a short-lived test
    process; polling is slower but deterministic, and the docs recommend it
    for exactly this situation. A short poll delay keeps these tests quick."""
    monkeypatch.setenv("WATCHFILES_FORCE_POLLING", "true")
    monkeypatch.setenv("WATCHFILES_POLL_DELAY_MS", str(round(_POLL_S * 1000)))


class _Saver:
    """Saves files the way an editor would, one distinct mtime per save.

    The polling backend can miss a save that lands within the same second
    as the one before it, even when the contents differ. Moving each save's
    mtime to its own second makes every save visible to it, including a
    touch that rewrites the same bytes.
    """

    def __init__(self) -> None:
        self._mtime = time.time()

    def save(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self._mtime += _MTIME_STEP_S
        os.utime(path, (self._mtime, self._mtime))


def _record_processed_batches(monkeypatch) -> list:
    """Every batch the watcher has finished handling, in order.

    A batch is recorded only once the watcher asks for the next one, so by
    then it has hashed the files and decided about ``dirty`` for it.
    """
    processed: list = []
    real_awatch = watcher_module.awatch

    async def recording_awatch(*args, **kwargs):
        async for batch in real_awatch(*args, **kwargs):
            yield batch
            processed.append(batch)

    monkeypatch.setattr(watcher_module, "awatch", recording_awatch)
    return processed


def _handled(processed: list, path: Path) -> bool:
    return any(Path(seen).name == path.name for batch in processed for _change, seen in batch)


@pytest.mark.parametrize(
    "relative",
    ["routes.py", os.path.join("kit", "x.py"), ".env"],
)
def test_is_watched_true_cases(tmp_path, relative):
    app_dir = tmp_path / "app"
    (app_dir / os.path.dirname(relative)).mkdir(parents=True, exist_ok=True) if os.path.dirname(
        relative
    ) else app_dir.mkdir(parents=True, exist_ok=True)
    assert is_watched(str(app_dir / relative), str(app_dir)) is True


@pytest.mark.parametrize(
    "relative",
    [
        os.path.join("__pycache__", "r.cpython-312.pyc"),
        ".routes.py.swp",
        "routes.py~",
        "4913",
        "routes.py.tmp",
        ".#routes.py",
        ".env.example",
        ".env.tmp",
        "notes.txt",
    ],
)
def test_is_watched_false_cases_under_app(tmp_path, relative):
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    assert is_watched(str(app_dir / relative), str(app_dir)) is False


def test_is_watched_false_for_paths_outside_app_dir(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "playground").mkdir()
    (tmp_path / ".playground").mkdir()
    assert is_watched(str(tmp_path / "tests" / "x.py"), str(app_dir)) is False
    assert is_watched(str(tmp_path / "playground" / "x.py"), str(app_dir)) is False
    assert is_watched(str(tmp_path / ".playground" / "state.json"), str(app_dir)) is False


def test_is_watched_answers_for_a_symlink_that_points_at_itself(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    loop = app_dir / "loop.py"
    os.symlink(loop, loop)
    assert is_watched(str(loop), str(app_dir)) is True
    assert isinstance(asyncio.run(code_hash(str(app_dir))), str)


# -- code_hash while the tree changes under it ----------------------------------


def _two_packages(tmp_path: Path) -> tuple[Path, list[Path]]:
    app_dir = tmp_path / "app"
    packages = [app_dir / "first_pkg", app_dir / "second_pkg"]
    for package in packages:
        package.mkdir(parents=True)
        (package / "mod.py").write_text("value = 1\n", encoding="utf-8")
    (app_dir / "routes.py").write_text("value = 2\n", encoding="utf-8")
    return app_dir, packages


class _ListingThatVanishes:
    """A directory listing that fails part-way, as it does when the directory
    is deleted while it is being read."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def __iter__(self):
        return self

    def __next__(self):
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))

    def close(self) -> None:
        return None


def test_code_hash_survives_a_directory_removed_after_it_was_listed(tmp_path, monkeypatch):
    app_dir, packages = _two_packages(tmp_path)
    removed: list[Path] = []
    real_is_watched = watcher_module.is_watched

    def removing_the_other_package(path: str, app: str) -> bool:
        # Checking a file in one package deletes every other package, after
        # the walk has listed them but before it opens them.
        parent = Path(path).parent
        if not removed and parent in packages:
            for package in packages:
                if package != parent:
                    shutil.rmtree(package)
                    removed.append(package)
        return real_is_watched(path, app)

    with monkeypatch.context() as patch:
        patch.setattr(watcher_module, "is_watched", removing_the_other_package)
        during = asyncio.run(code_hash(str(app_dir)))

    assert removed
    assert during == asyncio.run(code_hash(str(app_dir)))


def test_code_hash_survives_a_directory_removed_while_it_is_listed(tmp_path, monkeypatch):
    app_dir, packages = _two_packages(tmp_path)
    target = packages[-1]
    real_scandir = os.scandir
    vanished: list[Path] = []

    def scandir(path="."):
        # Every listing of the target fails part-way; the first one also
        # deletes it, so the tree afterwards really is without it.
        if not isinstance(path, int) and Path(path) == target:
            if not vanished:
                shutil.rmtree(target)
                vanished.append(target)
            return _ListingThatVanishes()
        return real_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", scandir)
        during = asyncio.run(code_hash(str(app_dir)))

    assert vanished
    assert during == asyncio.run(code_hash(str(app_dir)))


def test_code_hash_survives_a_file_removed_before_it_is_read(tmp_path, monkeypatch):
    app_dir, _packages = _two_packages(tmp_path)
    target = app_dir / "routes.py"
    real_is_watched = watcher_module.is_watched

    def removing_after_the_check(path: str, app: str) -> bool:
        answer = real_is_watched(path, app)
        if Path(path) == target:
            target.unlink()
        return answer

    with monkeypatch.context() as patch:
        patch.setattr(watcher_module, "is_watched", removing_after_the_check)
        during = asyncio.run(code_hash(str(app_dir)))

    assert not target.exists()
    assert isinstance(during, str)
    # The vanished file shows up as a change, never as the tree it used to be.
    target.write_text("value = 2\n", encoding="utf-8")
    assert during != asyncio.run(code_hash(str(app_dir)))


async def _rewrite_until_dirty(
    watcher: AppWatcher, saver: _Saver, path: Path, base_content: str
) -> None:
    """Keep changing ``path`` until the watcher notices.

    One save is normally enough; repeating it only guards against a save
    that lands before the watcher's first look, without changing what's
    being proven -- that a real content change eventually flips ``dirty``.
    """
    attempt = 0
    while not watcher.dirty:
        attempt += 1
        saver.save(path, f"{base_content}\n# {attempt}\n")
        await asyncio.sleep(_TOUCH_SPACING_S)


def test_five_quick_writes_give_dirty_once_and_one_settle(tmp_path):
    asyncio.run(_five_quick_writes_give_dirty_once_and_one_settle(tmp_path))


async def _five_quick_writes_give_dirty_once_and_one_settle(tmp_path) -> None:
    app_dir = tmp_path / "app"
    target = app_dir / "routes.py"
    saver = _Saver()
    saver.save(target, "value = 0\n")

    watcher = AppWatcher(str(app_dir))
    stop = asyncio.Event()
    run_task = asyncio.ensure_future(watcher.run(stop))

    # A real settle *count*, not an inference from a second no-op call: this
    # task loops `wait_settled()` -> `baseline()`, the same pairing
    # `restart(before_spawn=watcher.baseline)` uses in production, so each
    # loop iteration proves one genuine settle rather than the same one seen
    # twice.
    settles = 0
    settle_task_stop = asyncio.Event()

    async def _count_settles() -> None:
        nonlocal settles
        while not settle_task_stop.is_set():
            await watcher.wait_settled()
            settles += 1
            await watcher.baseline()

    settle_task = asyncio.ensure_future(_count_settles())
    try:
        # Let the watcher actually attach before the burst, so none of the
        # five writes below land before it's listening.
        await asyncio.sleep(RELOAD_DEBOUNCE_S)
        first_write_at = time.monotonic()
        for count in range(5):
            if count:
                await asyncio.sleep(_POLL_S)
            saver.save(target, f"value = {count + 1}\n")
        assert time.monotonic() - first_write_at < _BURST_WINDOW_S

        await _wait_until(lambda: settles >= 1, timeout_s=_SETTLE_TIMEOUT_S)
        assert watcher.generation == 1
        assert settles == 1

        # A further quiet window with nothing written: still exactly the one
        # settle above, never a second, spurious one.
        await asyncio.sleep(_TOUCH_SETTLE_S)
        assert settles == 1
        assert watcher.generation == 1
    finally:
        settle_task_stop.set()
        settle_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await settle_task
        stop.set()
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task


def test_a_touch_with_the_same_content_is_not_dirty(tmp_path, monkeypatch):
    asyncio.run(_a_touch_with_the_same_content_is_not_dirty(tmp_path, monkeypatch))


async def _a_touch_with_the_same_content_is_not_dirty(tmp_path, monkeypatch) -> None:
    app_dir = tmp_path / "app"
    target = app_dir / "routes.py"
    saver = _Saver()
    saver.save(target, "value = 0\n")
    processed = _record_processed_batches(monkeypatch)

    watcher = AppWatcher(str(app_dir))
    stop = asyncio.Event()
    run_task = asyncio.ensure_future(watcher.run(stop))
    try:
        await asyncio.sleep(RELOAD_DEBOUNCE_S)

        # Touch: rewrite the exact same bytes, only the mtime moves. Keep
        # touching until the watcher has actually handled a batch for this
        # file, so "not dirty" below is a decision it made, not a batch
        # that never arrived.
        async def touch_until_handled() -> None:
            while not _handled(processed, target):
                saver.save(target, "value = 0\n")
                await asyncio.sleep(_TOUCH_SPACING_S)

        await asyncio.wait_for(touch_until_handled(), timeout=_SETTLE_TIMEOUT_S)
        assert watcher.dirty is False
        assert watcher.generation == 0

        # The same watcher does flip on a real change, so the touch above
        # really went through the path that decides.
        saver.save(target, "value = 1\n")
        await _wait_until(lambda: watcher.dirty, timeout_s=_SETTLE_TIMEOUT_S)
        assert watcher.generation == 1
    finally:
        stop.set()
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task


def test_change_after_baseline_during_a_simulated_restart_keeps_dirty_set(tmp_path):
    asyncio.run(_change_after_baseline_during_a_simulated_restart_keeps_dirty_set(tmp_path))


async def _change_after_baseline_during_a_simulated_restart_keeps_dirty_set(tmp_path) -> None:
    app_dir = tmp_path / "app"
    target = app_dir / "routes.py"
    saver = _Saver()
    saver.save(target, "value = 0\n")

    watcher = AppWatcher(str(app_dir))
    stop = asyncio.Event()
    run_task = asyncio.ensure_future(watcher.run(stop))
    try:
        await asyncio.sleep(RELOAD_DEBOUNCE_S)
        await asyncio.wait_for(
            _rewrite_until_dirty(watcher, saver, target, "value = 1"), timeout=_SETTLE_TIMEOUT_S
        )

        # Simulate a restart's before_spawn call clearing the flag ...
        await watcher.baseline()
        assert watcher.dirty is False

        # ... then a further change arrives while that restart is underway.
        await asyncio.wait_for(
            _rewrite_until_dirty(watcher, saver, target, "value = 2"), timeout=_SETTLE_TIMEOUT_S
        )
        assert watcher.dirty is True
    finally:
        stop.set()
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task


def test_a_file_caught_half_way_through_a_save_is_not_a_change(tmp_path, monkeypatch):
    """A save that empties the file and then rewrites the same bytes can be
    seen in between. The watcher must look again before calling it a change."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "routes.py").write_text("value = 0\n", encoding="utf-8")
    watcher = AppWatcher(str(app_dir))
    baseline = watcher._baseline_hash
    hashes = iter(["half-written", baseline])

    async def fake_code_hash(_app_dir: str) -> str:
        return next(hashes)

    async def one_batch(*_args, **_kwargs):
        yield {("modified", str(app_dir / "routes.py"))}

    monkeypatch.setattr(watcher_module, "code_hash", fake_code_hash)
    monkeypatch.setattr(watcher_module, "awatch", one_batch)
    asyncio.run(watcher.run(asyncio.Event()))
    assert watcher.dirty is False


def test_a_change_that_is_still_there_after_the_settle_is_dirty(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "routes.py").write_text("value = 0\n", encoding="utf-8")
    watcher = AppWatcher(str(app_dir))

    async def fake_code_hash(_app_dir: str) -> str:
        return "really-changed"

    async def one_batch(*_args, **_kwargs):
        yield {("modified", str(app_dir / "routes.py"))}

    monkeypatch.setattr(watcher_module, "code_hash", fake_code_hash)
    monkeypatch.setattr(watcher_module, "awatch", one_batch)
    asyncio.run(watcher.run(asyncio.Event()))
    assert watcher.dirty is True
