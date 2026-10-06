"""Command-line entry point for the playground console."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path

import filelock
from rich.console import Console

from playground import runner, statefile
from playground.copy.en import text
from playground.machine.events import Expect, Say
from playground.machine.machine import Machine
from playground.tui.colour import detect_ascii, detect_level
from playground.tui.console import InteractiveRenderer
from playground.tui.jsonl import JsonLinesRenderer
from playground.tui.prompt import Prompt

SIZE_MIN = 1100
SIZE_MAX = 5000
# Visitors per run through SignalGate when --size isn't given.
SIZE_DEFAULT = 2000

# argparse's default parse-error exit status (2) is reserved elsewhere in
# this project, so parse/validation errors here exit with this status
# instead.
PARSE_ERROR_EXIT_CODE = 3

_LOCK_RELATIVE = Path(".playground") / "console.lock"
_ENV_RELATIVE = Path("app") / ".env"
_ENV_EXAMPLE_RELATIVE = Path("app") / ".env.example"


class PlaygroundArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser whose parse errors exit with a non-default status."""

    def error(self, message: str) -> None:
        self.exit(PARSE_ERROR_EXIT_CODE, f"{self.prog}: error: {message}\n")


def _size(value: str) -> int:
    try:
        size = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(text("cli.error.size_not_int", value=value)) from exc
    if not SIZE_MIN <= size <= SIZE_MAX:
        raise argparse.ArgumentTypeError(
            text("cli.error.size_range", min=SIZE_MIN, max=SIZE_MAX, value=size)
        )
    return size


def build_parser() -> PlaygroundArgumentParser:
    parser = PlaygroundArgumentParser(
        prog="playground",
        description=text("cli.description"),
    )
    parser.add_argument(
        "--size", type=_size, help=text("cli.help.size", min=SIZE_MIN, max=SIZE_MAX)
    )
    parser.add_argument("--plain", action="store_true", help=text("cli.help.plain"))
    parser.add_argument("--no-telemetry", action="store_true", help=text("cli.help.no_telemetry"))
    parser.add_argument("--ci", action="store_true", help=text("cli.help.ci"))
    parser.add_argument("--yes", action="store_true", help=text("cli.help.yes"))
    parser.add_argument("--until", type=int, help=text("cli.help.until"))
    return parser


def _ci_read_line(renderer: JsonLinesRenderer) -> runner.ReadLine:
    async def read_line(_expect: Expect) -> str:
        return await renderer.read_line()

    return read_line


def _ensure_env_file(root: Path) -> bool:
    """Copy ``app/.env.example`` to ``app/.env`` if it's missing.

    Returns whether it was created just now.
    """
    env_path = root / _ENV_RELATIVE
    example_path = root / _ENV_EXAMPLE_RELATIVE
    if env_path.exists() or not example_path.is_file():
        return False
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(example_path.read_text())
    return True


def _build_renderer(
    *, ci: bool, plain: bool
) -> tuple[object, runner.ReadLine, Callable[[], None] | None]:
    """The renderer, how to read a line, and (on a terminal) how to drop the
    keys typed while the console was busy."""
    if ci:
        json_renderer = JsonLinesRenderer(sys.stdout, sys.stdin)
        return json_renderer, _ci_read_line(json_renderer), None

    ascii_mode = detect_ascii()
    level = detect_level(plain=plain)
    console = Console()
    interactive_renderer = InteractiveRenderer(console, level, ascii=ascii_mode)
    prompt = Prompt(ascii=ascii_mode)
    return interactive_renderer, prompt.read_line, prompt.discard_typeahead


def _run(args: argparse.Namespace) -> int:
    root = runner.find_repo_root()
    lock_path = root / _LOCK_RELATIVE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = filelock.FileLock(str(lock_path), timeout=0)
    try:
        lock.acquire()
    except filelock.Timeout:
        print(text("console.lock_held"), file=sys.stderr)
        return PARSE_ERROR_EXIT_CODE

    try:
        ci = args.ci or not sys.stdout.isatty()
        renderer_obj, read_line, discard_typeahead = _build_renderer(ci=ci, plain=args.plain)

        if _ensure_env_file(root):
            renderer_obj.show(Say("console.env_created"))

        # An unattended (--yes) session never reads or writes saved progress,
        # so a scripted run in the same checkout can't overwrite it.
        state_path: Path | None = None
        saved = None
        if not args.yes:
            state_path = root / runner.STATE_RELATIVE
            saved, notice = statefile.load(state_path)
            if notice is not None:
                renderer_obj.show(Say(notice))

        machine = Machine(
            offline_total=runner.CHAPTER1_VISITORS,
            saved=saved,
            unattended=args.yes,
            size=args.size or SIZE_DEFAULT,
        )
        return asyncio.run(
            runner.run(
                machine,
                renderer_obj,
                read_line,
                root=root,
                yes=args.yes,
                until=args.until,
                state_path=state_path,
                interactive=not ci,
                telemetry=not args.no_telemetry,
                discard_typeahead=discard_typeahead,
            )
        )
    finally:
        lock.release()


def _settle_output() -> None:
    """Flush stdout and stderr, pointing a stream whose terminal hung up at
    ``os.devnull``, so the interpreter's own final flush can't fail and
    turn a clean exit into an error."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except OSError:
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, stream.fileno())
                os.close(devnull)
            except (OSError, ValueError):
                pass
        except ValueError:
            pass  # already closed


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    finally:
        _settle_output()
