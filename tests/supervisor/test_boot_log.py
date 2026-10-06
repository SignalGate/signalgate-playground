"""Tests for the pure text-parsing helpers over one boot's log slice."""

from __future__ import annotations

import sysconfig
from pathlib import Path

from playground.copy.en import TEXT, text
from playground.supervisor.boot_log import (
    SOURCE_MAX_CHARS,
    bind_failed,
    boot_failure,
    failure_location,
)


def _site_packages_sample() -> str:
    """An absolute path plausibly under some interpreter's site-packages,
    built from ``sysconfig`` rather than typed, per the local-path guard."""
    purelib = sysconfig.get_path("purelib")
    return str(Path(purelib) / "somepkg" / "mod.py")


def test_failure_location_finds_a_syntax_error_frame(tmp_path):
    routes = tmp_path / "app" / "routes.py"
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "app" / "main.py"}", line 5, in <module>\n'
        "    from app.routes import router\n"
        f'  File "{routes}", line 12\n'
        "    def broken(:\n"
        "SyntaxError: invalid syntax\n"
    )
    assert failure_location(text, tmp_path) == "app/routes.py:12"


def test_failure_location_finds_the_last_matching_frame(tmp_path):
    text = (
        f'  File "{tmp_path / "app" / "routes.py"}", line 3, in <module>\n'
        f'  File "{tmp_path / "app" / "kit" / "facade.py"}", line 40, in build\n'
        "IndentationError: unexpected indent\n"
    )
    assert failure_location(text, tmp_path) == "app/kit/facade.py:40"


def test_failure_location_ignores_frames_outside_root_app(tmp_path):
    site_packages_frame = _site_packages_sample()
    text = (
        f'  File "{site_packages_frame}", line 9, in <module>\n'
        "ImportError: cannot import name 'thing'\n"
    )
    assert failure_location(text, tmp_path) is None


def test_failure_location_finds_an_indentation_error_frame_in_routes(tmp_path):
    routes = tmp_path / "app" / "routes.py"
    text = (
        f'  File "{tmp_path / "app" / "main.py"}", line 3, in <module>\n'
        "    from app.routes import router\n"
        f'  File "{routes}", line 9\n'
        "    def broken():\n"
        "IndentationError: unexpected indent\n"
    )
    assert failure_location(text, tmp_path) == "app/routes.py:9"


def test_failure_location_finds_an_import_error_frame_in_routes(tmp_path):
    routes = tmp_path / "app" / "routes.py"
    text = (
        f'  File "{tmp_path / "app" / "main.py"}", line 1, in <module>\n'
        "    from app.routes import router\n"
        f'  File "{routes}", line 1, in <module>\n'
        "    from app.nothere import x\n"
        "ImportError: cannot import name 'x' from 'app.nothere'\n"
    )
    assert failure_location(text, tmp_path) == "app/routes.py:1"


def test_failure_location_handles_name_error_at_import(tmp_path):
    text = (
        f'  File "{tmp_path / "app" / "routes.py"}", line 7, in <module>\n'
        "    thing_that_does_not_exist()\n"
        "NameError: name 'thing_that_does_not_exist' is not defined\n"
    )
    assert failure_location(text, tmp_path) == "app/routes.py:7"


def test_failure_location_is_never_absolute(tmp_path):
    text = f'  File "{tmp_path / "app" / "routes.py"}", line 1\nSyntaxError: bad\n'
    location = failure_location(text, tmp_path)
    assert location is not None
    assert not location.startswith("/")


def test_failure_location_handles_windows_shaped_drive_letter_paths():
    # Built from fragments rather than typed, like every absolute sample here.
    root = "".join(["C:", "\\", "Users", "\\", "ci", "\\", "project"])  # noqa: FLY002
    text = f'  File "{root}\\app\\routes.py", line 21\nSyntaxError: bad\n'
    assert failure_location(text, root) == "app/routes.py:21"


def test_failure_location_windows_paths_compare_case_insensitively():
    root = "".join(["C:", "\\", "Users", "\\", "ci", "\\", "Project"])  # noqa: FLY002
    frame_root = "".join(["c:", "\\", "users", "\\", "ci", "\\", "project"])  # noqa: FLY002
    text = f'  File "{frame_root}\\app\\routes.py", line 8\nSyntaxError: bad\n'
    assert failure_location(text, root) == "app/routes.py:8"


def test_failure_location_resolves_an_unresolved_symlinked_root(tmp_path):
    # A real traceback always names the resolved path; `root` itself may be
    # an unresolved symlink (a developer's temp dir on macOS, for example),
    # so the comparison must still succeed.
    real_root = tmp_path / "real"
    (real_root / "app").mkdir(parents=True)
    link_root = tmp_path / "link"
    link_root.symlink_to(real_root, target_is_directory=True)

    text = f'  File "{real_root / "app" / "routes.py"}", line 4\nSyntaxError: bad\n'
    assert failure_location(text, link_root) == "app/routes.py:4"


def test_bind_failed_true_for_macos_and_linux_wording():
    assert bind_failed("OSError: [Errno 48] Address already in use") is True
    assert bind_failed("OSError: [Errno 98] Address already in use") is True


def test_bind_failed_true_for_windows_wording():
    assert bind_failed("OSError: [WinError 10048] Only one usage of each socket address") is True


def test_bind_failed_false_for_other_bind_errors():
    assert bind_failed("OSError: [Errno 13] Permission denied") is False
    assert bind_failed("") is False


# -- boot_failure: why a boot failed, in words the console can show -----------


def _boot_log(root: Path, frames: str, last: str) -> str:
    """A boot's log slice as the server prints it: its own frames, then the app's."""
    site_frame = _site_packages_sample()
    return (
        "Traceback (most recent call last):\n"
        f'  File "{site_frame}", line 19, in import_from_string\n'
        "    module = importlib.import_module(module_str)\n"
        "             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\n"
        f'  File "{root / "app" / "main.py"}", line 31, in <module>\n'
        "    from app.routes import router as routes_router\n"
        f"{frames}"
        f"{last}\n"
    )


def test_an_indentation_error_names_the_line_keeps_the_source_and_hints(tmp_path):
    frames = f'  File "{tmp_path / "app" / "routes.py"}", line 57\n        ev = sg_event(request)\n'
    failure = boot_failure(
        _boot_log(tmp_path, frames, "IndentationError: unexpected indent"), tmp_path
    )
    assert failure is not None
    assert failure.error == "IndentationError"
    assert failure.message == "unexpected indent"
    assert failure.location == "app/routes.py:57"
    assert failure.source == "        ev = sg_event(request)"
    assert failure.caret is None
    assert failure.hint == "app.down.hint.indent"
    assert failure.hint in TEXT


def test_a_tab_error_gets_the_same_hint(tmp_path):
    frames = f'  File "{tmp_path / "app" / "routes.py"}", line 44\n    ev = 1\n'
    last = "TabError: inconsistent use of tabs and spaces in indentation"
    failure = boot_failure(_boot_log(tmp_path, frames, last), tmp_path)
    assert failure is not None
    assert (failure.error, failure.location) == ("TabError", "app/routes.py:44")
    assert failure.hint == "app.down.hint.indent"


def test_a_syntax_error_keeps_its_caret_line(tmp_path):
    frames = (
        f'  File "{tmp_path / "app" / "routes.py"}", line 44\n'
        "    return {'ok': True\n"
        "    ^^^^^^\n"
    )
    failure = boot_failure(_boot_log(tmp_path, frames, "SyntaxError: invalid syntax"), tmp_path)
    assert failure is not None
    assert failure.error == "SyntaxError"
    assert failure.source == "    return {'ok': True"
    assert failure.caret == "    ^^^^^^"
    assert failure.hint is None


def test_an_import_error_from_the_client_module_hints_and_shortens_the_path(tmp_path):
    frames = (
        f'  File "{tmp_path / "app" / "routes.py"}", line 24, in <module>\n'
        "    from app.signalgate_client import sg, sg_evnt\n"
    )
    client = tmp_path / "app" / "signalgate_client.py"
    last = (
        "ImportError: cannot import name 'sg_evnt' from 'app.signalgate_client' "
        f"({client}). Did you mean: 'sg_event'?"
    )
    failure = boot_failure(_boot_log(tmp_path, frames, last), tmp_path)
    assert failure is not None
    assert failure.error == "ImportError"
    assert failure.location == "app/routes.py:24"
    assert failure.source == "    from app.signalgate_client import sg, sg_evnt"
    assert failure.hint == "app.down.hint.import"
    assert failure.hint in TEXT
    assert "(app/signalgate_client.py)" in failure.message
    assert str(tmp_path) not in failure.message


def test_a_path_under_the_resolved_root_is_shortened_too(tmp_path):
    real_root = tmp_path / "real"
    (real_root / "app").mkdir(parents=True)
    link_root = tmp_path / "link"
    link_root.symlink_to(real_root, target_is_directory=True)
    frames = f'  File "{real_root / "app" / "routes.py"}", line 2, in <module>\n    import x\n'
    last = f"ModuleNotFoundError: No module named 'x' ({real_root / 'app' / 'routes.py'})"
    failure = boot_failure(_boot_log(real_root, frames, last), link_root)
    assert failure is not None
    assert failure.location == "app/routes.py:2"
    assert failure.message == "No module named 'x' (app/routes.py)"


def test_a_mistyped_module_name_gets_no_client_hint(tmp_path):
    frames = (
        f'  File "{tmp_path / "app" / "routes.py"}", line 24, in <module>\n'
        "    from app.signalgate_clients import sg\n"
    )
    last = "ModuleNotFoundError: No module named 'app.signalgate_clients'"
    failure = boot_failure(_boot_log(tmp_path, frames, last), tmp_path)
    assert failure is not None
    assert failure.error == "ModuleNotFoundError"
    assert failure.hint is None


def test_a_name_error_at_import_has_no_hint(tmp_path):
    frames = (
        f'  File "{tmp_path / "app" / "routes.py"}", line 7, in <module>\n'
        "    thing_that_does_not_exist()\n"
    )
    last = "NameError: name 'thing_that_does_not_exist' is not defined"
    failure = boot_failure(_boot_log(tmp_path, frames, last), tmp_path)
    assert failure is not None
    assert failure.error == "NameError"
    assert failure.message == "name 'thing_that_does_not_exist' is not defined"
    assert failure.location == "app/routes.py:7"
    assert failure.hint is None


def test_no_frame_under_the_app_means_no_failure(tmp_path):
    text_without_app = (
        "Traceback (most recent call last):\n"
        f'  File "{_site_packages_sample()}", line 9, in <module>\n'
        "    import thing\n"
        "ImportError: cannot import name 'thing'\n"
    )
    assert boot_failure(text_without_app, tmp_path) is None
    assert boot_failure("", tmp_path) is None


def test_an_app_frame_without_an_exception_line_means_no_failure(tmp_path):
    cut_short = f'  File "{tmp_path / "app" / "routes.py"}", line 3, in <module>\n    x = 1\n'
    assert boot_failure(cut_short, tmp_path) is None


def test_a_key_shaped_value_is_masked_in_the_source_and_the_message(tmp_path):
    key = "pk_live_" + "Zz9_" * 8
    frames = (
        f'  File "{tmp_path / "app" / "routes.py"}", line 5, in <module>\n    KEY = "{key}" +\n'
    )
    failure = boot_failure(_boot_log(tmp_path, frames, f"SyntaxError: bad {key}"), tmp_path)
    assert failure is not None
    masked = text("verify.key_masked")
    assert key not in (failure.source or "")
    assert key not in failure.message
    assert failure.source == f'    KEY = "{masked}" +'
    assert failure.message == f"bad {masked}"


def test_the_servers_own_log_lines_are_never_read_as_the_exception(tmp_path):
    frames = f'  File "{tmp_path / "app" / "routes.py"}", line 9, in <module>\n    boom()\n'
    log = (
        _boot_log(tmp_path, frames, "RuntimeError: boom")
        + "ERROR:    Application startup failed.\n"
    )
    failure = boot_failure(log, tmp_path)
    assert failure is not None
    assert (failure.error, failure.message) == ("RuntimeError", "boom")


def test_an_exception_with_no_message_has_an_empty_message(tmp_path):
    frames = (
        f'  File "{tmp_path / "app" / "routes.py"}", line 9, in <module>\n    raise ValueError\n'
    )
    failure = boot_failure(_boot_log(tmp_path, frames, "ValueError"), tmp_path)
    assert failure is not None
    assert (failure.error, failure.message) == ("ValueError", "")


def test_the_last_app_frame_is_the_location(tmp_path):
    frames = (
        f'  File "{tmp_path / "app" / "routes.py"}", line 3, in <module>\n'
        "    from app.kit import facade\n"
        f'  File "{tmp_path / "app" / "kit" / "facade.py"}", line 40, in build\n'
        "    raise LookupError('no')\n"
    )
    failure = boot_failure(_boot_log(tmp_path, frames, "LookupError: no"), tmp_path)
    assert failure is not None
    assert failure.location == "app/kit/facade.py:40"
    assert failure.location == failure_location(
        _boot_log(tmp_path, frames, "LookupError: no"), tmp_path
    )
    assert failure.source == "    raise LookupError('no')"


def test_a_windows_shaped_log_is_read_the_same_way():
    root = "".join(["C:", "\\", "Users", "\\", "ci", "\\", "project"])  # noqa: FLY002
    log = (
        f'  File "{root}\\app\\routes.py", line 21\n'
        "    ev = 1\n"
        "IndentationError: unexpected indent\n"
    )
    failure = boot_failure(log, root)
    assert failure is not None
    assert failure.location == "app/routes.py:21"
    assert failure.hint == "app.down.hint.indent"


def test_the_exception_is_read_forward_from_the_frame_not_from_the_end(tmp_path):
    frames = f'  File "{tmp_path / "app" / "routes.py"}", line 12, in <module>\n    load()\n'
    # A message that runs over two lines, then a line printed after the
    # traceback: both look like "name: text" and neither is the exception.
    last = "ValueError: bad settings\nhint: set the value in app/.env\nreloader: stopped"
    failure = boot_failure(_boot_log(tmp_path, frames, last), tmp_path)
    assert failure is not None
    assert (failure.error, failure.message) == ("ValueError", "bad settings")
    assert failure.location == "app/routes.py:12"


def test_deeper_frames_and_their_lines_are_passed_over(tmp_path):
    frames = (
        f'  File "{tmp_path / "app" / "routes.py"}", line 4, in <module>\n'
        "    build()\n"
        f'  File "{_site_packages_sample()}", line 88, in build\n'
        "    raise KeyError(name)\n"
        "    ^^^^^^^^^^^^^^^^^^^^\n"
    )
    failure = boot_failure(_boot_log(tmp_path, frames, "KeyError: 'name'"), tmp_path)
    assert failure is not None
    assert (failure.error, failure.message) == ("KeyError", "'name'")
    assert failure.location == "app/routes.py:4"


def test_of_a_chain_the_exception_the_app_frame_ends_with_is_read(tmp_path):
    frames = f'  File "{tmp_path / "app" / "routes.py"}", line 9, in <module>\n    boom()\n'
    chained = (
        _boot_log(tmp_path, frames, "RuntimeError: boom")
        + "\nDuring handling of the above exception, another exception occurred:\n\n"
        + "Traceback (most recent call last):\n"
        + f'  File "{_site_packages_sample()}", line 70, in report\n'
        + "    log(error)\n"
        + "OSError: log closed\n"
    )
    failure = boot_failure(chained, tmp_path)
    assert failure is not None
    assert (failure.error, failure.message) == ("RuntimeError", "boom")


def test_a_very_long_source_line_is_cut_short_with_its_caret_line(tmp_path):
    long_line = "    x = (" + "1 + " * 120 + ")"
    caret = " " * 4 + "^" * (len(long_line) - 4)
    frames = f'  File "{tmp_path / "app" / "routes.py"}", line 5\n{long_line}\n{caret}\n'
    failure = boot_failure(_boot_log(tmp_path, frames, "SyntaxError: invalid syntax"), tmp_path)
    assert failure is not None
    assert failure.source is not None and failure.caret is not None
    assert len(failure.source) == SOURCE_MAX_CHARS
    assert failure.source == long_line[: SOURCE_MAX_CHARS - 1] + text("verify.ellipsis")
    assert len(failure.caret) <= SOURCE_MAX_CHARS - 1
    assert failure.caret == caret[: SOURCE_MAX_CHARS - 1]


def test_a_caret_past_the_cut_is_dropped(tmp_path):
    long_line = "    x = 1" + " " * 300 + "+"
    caret = " " * (len(long_line) - 1) + "^"
    frames = f'  File "{tmp_path / "app" / "routes.py"}", line 5\n{long_line}\n{caret}\n'
    failure = boot_failure(_boot_log(tmp_path, frames, "SyntaxError: invalid syntax"), tmp_path)
    assert failure is not None and failure.source is not None
    assert len(failure.source) == SOURCE_MAX_CHARS
    assert failure.caret is None


def test_a_short_source_line_is_kept_whole(tmp_path):
    line = "    x = " + "y" * (SOURCE_MAX_CHARS - 8)
    assert len(line) == SOURCE_MAX_CHARS
    frames = f'  File "{tmp_path / "app" / "routes.py"}", line 5, in <module>\n{line}\n'
    failure = boot_failure(_boot_log(tmp_path, frames, "NameError: y"), tmp_path)
    assert failure is not None
    assert failure.source == line
