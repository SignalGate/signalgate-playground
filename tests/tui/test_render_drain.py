"""The wait for the sample app's queued log events: one line that updates in
place, a bar in the run panel, and the plain fallbacks.

A small screen model works out what a terminal shows from what the console
wrote (line breaks, carriage returns, cursor-up and erase-line codes), so a
test can tell what is on screen at the end, not just what was written on the
way.
"""

from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

from playground.copy.en import text
from playground.machine.events import DeliverProgress, RunProgress, Say
from playground.progress import eta_words
from playground.tui.console import InteractiveRenderer
from playground.tui.render import BAR_CELLS, BAR_CELLS_MIN, render_drain_line, render_panel

_CONTROL = re.compile(r"\x1b\[(\??)([0-9;]*)([A-Za-z])")
SENDING = text("drain.sending")


class _Screen:
    """What a terminal shows after the given output (single-width text only)."""

    def __init__(self, output: str) -> None:
        self.rows: list[list[str]] = [[]]
        self.row = self.col = 0
        at = 0
        while at < len(output):
            match = _CONTROL.match(output, at)
            if match is not None:
                self._control(match.group(1), match.group(2), match.group(3))
                at = match.end()
                continue
            self._put(output[at])
            at += 1

    def _control(self, private: str, params: str, final: str) -> None:
        if private:
            return  # cursor shown / hidden
        count = int(params or "1")
        if final == "A":
            self.row = max(0, self.row - count)
        elif final == "K" and params == "2":
            self.rows[self.row] = []  # erase the whole line
        elif final == "K" and params in ("", "0"):
            del self.rows[self.row][self.col :]  # erase to the end of the line

    def _put(self, char: str) -> None:
        if char == "\r":
            self.col = 0
        elif char == "\n":
            self.row += 1
            self.col = 0
            while len(self.rows) <= self.row:
                self.rows.append([])
        else:
            line = self.rows[self.row]
            line.extend(" " * (self.col - len(line)))
            if self.col < len(line):
                line[self.col] = char
            else:
                line.append(char)
            self.col += 1

    @property
    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.rows]


def _terminal(width: int = 120, level: str = "none", *, ascii: bool = False):
    buffer = io.StringIO()
    colour_system = None if level == "none" else "truecolor"
    console = Console(file=buffer, width=width, color_system=colour_system, force_terminal=True)
    return buffer, InteractiveRenderer(console, level, ascii=ascii)


# -- the standalone line ------------------------------------------------------------------


def test_a_standalone_wait_is_one_line_that_updates_in_place_and_goes_away():
    buffer, renderer = _terminal()
    renderer.show(Say("ch1.intro", {"total": 10_000}))
    renderer.drain_begin(Say("drain.waiting", {"remaining": 922}))
    renderer.drain_progress(DeliverProgress(120, 922, None))
    renderer.drain_progress(DeliverProgress(480, 922, 30.0))
    during = buffer.getvalue()
    renderer.drain_end()
    renderer.show(Say("drain.done"))

    assert f"480 / 922 · {text('eta.seconds', seconds=30)} · Ctrl-C to skip" in during
    # While it's up, the terminal shows it on exactly one row, however often it redrew.
    shown = _Screen(during).lines
    assert sum(SENDING in line for line in shown) == 1
    assert "480 / 922" in next(line for line in shown if SENDING in line)
    # Afterwards it is gone, and the wait's last line stands one blank line
    # below what came before.
    final = _Screen(buffer.getvalue()).lines
    assert not any(SENDING in line for line in final)
    done = final.index(text("drain.done"))
    assert final[done - 1] == "" and final[done - 2] != ""
    assert final[done + 1 :] in ([], [""])


def test_a_wait_that_ends_in_a_quit_says_so_on_its_line():
    buffer, renderer = _terminal()
    renderer.drain_begin(Say("drain.waiting_quit", {"remaining": 922}))
    renderer.drain_progress(DeliverProgress(480, 922, 30.0))
    renderer.drain_end()
    output = buffer.getvalue()
    assert "480 / 922 · " + text("eta.seconds", seconds=30) + " · Ctrl-C to quit now" in output
    assert "Ctrl-C to skip" not in output


def test_the_line_starts_from_the_queue_it_was_given():
    buffer, renderer = _terminal()
    renderer.drain_begin(Say("drain.waiting", {"remaining": 57}))
    started = buffer.getvalue()
    renderer.drain_end()
    assert "0 / 57 · Ctrl-C to skip" in started  # nothing measured yet: no time left


def test_input_line_open_prints_the_first_line_once_and_no_progress():
    buffer, renderer = _terminal()
    first = Say("drain.waiting_quit", {"remaining": 922})
    renderer.drain_begin(first, inline=False)
    renderer.drain_progress(DeliverProgress(480, 922, 30.0))
    renderer.drain_progress(DeliverProgress(900, 922, 5.0))
    renderer.drain_end()
    renderer.show(Say("drain.skipped", {"remaining": 22}))
    lines = _Screen(buffer.getvalue()).lines
    assert lines.count(text(first.key, **first.params)) == 1
    assert not any(SENDING in line for line in lines)
    assert text("drain.skipped", remaining=22) in lines


def test_with_a_spinner_up_the_first_line_is_printed_and_nothing_raises():
    buffer, renderer = _terminal()
    renderer.wait(Say("wait.queued"))
    first = Say("drain.waiting", {"remaining": 10})
    renderer.drain_begin(first)
    renderer.drain_progress(DeliverProgress(5, 10, 1.0))
    renderer.drain_end()
    renderer.wait_ended()
    output = buffer.getvalue()
    assert text(first.key, **first.params) in output
    assert SENDING not in output


def test_a_quiet_renderer_draws_no_wait_and_ending_one_never_raises():
    buffer, renderer = _terminal()
    renderer._quiet = True
    renderer.drain_begin(Say("drain.waiting", {"remaining": 10}))
    renderer.drain_progress(DeliverProgress(5, 10, 1.0))
    renderer.drain_end()
    renderer.drain_end()
    assert buffer.getvalue() == ""


# -- the line itself ---------------------------------------------------------------------


def test_the_line_holds_every_part_on_one_row():
    event = DeliverProgress(1_478, 2_400, 130.0)
    line = render_drain_line(event, quit_hint=False, level="none", ascii=False, width=120)
    plain = line.plain
    assert "\n" not in plain and len(plain) <= 120
    assert plain.startswith(SENDING + " ▕")
    for part in ("▏ 1,478 / 2,400", text("eta.minutes", minutes=3), "Ctrl-C to skip"):
        assert part in plain
    assert plain.count("█") + plain.count("░") == BAR_CELLS


@pytest.mark.parametrize("width", [80, 60, 30, 5])
def test_the_line_stays_one_row_on_a_narrow_terminal(width):
    event = DeliverProgress(1_478, 2_400, 130.0)
    for ascii in (False, True):
        line = render_drain_line(event, quit_hint=True, level="none", ascii=ascii, width=width)
        assert "\n" not in line.plain
        assert len(line.plain) <= width
        assert line.plain.isascii() or not ascii
    buffer, renderer = _terminal(width)
    renderer.drain_begin(Say("drain.waiting", {"remaining": 2_400}))
    renderer.drain_progress(event)
    shown = _Screen(buffer.getvalue()).lines
    renderer.drain_end()
    assert sum(bool(line) for line in shown) == 1


def test_the_bar_shrinks_before_the_line_is_cut_short():
    event = DeliverProgress(1, 2, None)
    wide = render_drain_line(event, quit_hint=False, level="none", ascii=False, width=200)
    full = len(wide.plain)
    snug = render_drain_line(event, quit_hint=False, level="none", ascii=False, width=full - 5)
    assert len(snug.plain) == full - 5 and "Ctrl-C to skip" in snug.plain
    assert snug.plain.count("█") + snug.plain.count("░") == BAR_CELLS - 5


_MIDWAY = DeliverProgress(480, 922, 30.0)
_COUNT = text("drain.count", done=480, total=922)
_ETA = eta_words(30.0)


@pytest.mark.parametrize("width", [120, 80, 60, 40])
@pytest.mark.parametrize("quit_hint", [False, True], ids=["skip", "quit"])
@pytest.mark.parametrize("ascii", [False, True], ids=["unicode", "ascii"])
@pytest.mark.parametrize(
    "event",
    [_MIDWAY, DeliverProgress(0, 1_000, None), DeliverProgress(12_345, 99_999, 3_000.0)],
    ids=["midway", "no-estimate", "long-wait"],
)
def test_the_count_and_what_ctrl_c_does_always_stay_on_one_row(width, quit_hint, ascii, event):
    line = render_drain_line(event, quit_hint=quit_hint, level="none", ascii=ascii, width=width)
    plain = line.plain
    assert "\n" not in plain and len(plain) <= width
    assert text("drain.count", done=event.done, total=event.total) in plain
    assert text("drain.quit_hint" if quit_hint else "drain.skip_hint") in plain
    assert not plain.endswith(("…", "..."))


def _cells(plain: str) -> int:
    return plain.count("█") + plain.count("░")


def test_at_120_columns_every_part_is_there():
    plain = render_drain_line(_MIDWAY, quit_hint=False, level="none", ascii=False, width=120).plain
    assert plain.startswith(SENDING + " ▕")
    assert _ETA in plain and _COUNT in plain and "Ctrl-C to skip" in plain
    assert _cells(plain) == BAR_CELLS


def test_at_80_columns_the_words_in_front_go_first():
    plain = render_drain_line(_MIDWAY, quit_hint=False, level="none", ascii=False, width=80).plain
    assert SENDING not in plain
    assert plain.startswith("▕")
    assert _ETA in plain and plain.endswith("Ctrl-C to skip")
    assert _cells(plain) == BAR_CELLS


def test_at_60_columns_the_time_left_stays_and_the_bar_narrows():
    plain = render_drain_line(_MIDWAY, quit_hint=True, level="none", ascii=False, width=60).plain
    assert SENDING not in plain
    assert _ETA in plain and plain.endswith("Ctrl-C to quit now")
    assert BAR_CELLS_MIN <= _cells(plain) < BAR_CELLS
    assert len(plain) == 60


def test_at_40_columns_the_time_left_goes_before_the_bar():
    plain = render_drain_line(_MIDWAY, quit_hint=False, level="none", ascii=False, width=40).plain
    assert _ETA not in plain and SENDING not in plain
    assert plain.startswith("▕") and _cells(plain) > 0
    assert plain.endswith(_COUNT + " · Ctrl-C to skip")


def test_the_bar_goes_last_and_only_then_is_the_line_cut():
    rest = f"{_COUNT} · Ctrl-C to skip"
    bare = render_drain_line(_MIDWAY, quit_hint=False, level="none", ascii=False, width=len(rest))
    assert bare.plain == rest
    cut = render_drain_line(_MIDWAY, quit_hint=False, level="none", ascii=False, width=20)
    assert len(cut.plain) == 20 and cut.plain.endswith("…")
    assert cut.plain.startswith(_COUNT)


def test_nothing_sent_yet_and_no_estimate():
    line = render_drain_line(
        DeliverProgress(0, 0, None), quit_hint=False, level="none", ascii=True, width=120
    )
    empty = "[" + "-" * BAR_CELLS + "]"
    assert line.plain == f"{text('drain.sending')} {empty} 0 / 0 - Ctrl-C to skip"


def test_the_line_is_coloured_at_a_colour_level_and_plain_at_none():
    event = DeliverProgress(480, 922, 30.0)
    coloured = render_drain_line(event, quit_hint=False, level="truecolor", ascii=False, width=120)
    assert coloured.spans
    plain = render_drain_line(event, quit_hint=False, level="none", ascii=False, width=120)
    assert plain.plain == coloured.plain and not plain.spans


# -- the run panel -----------------------------------------------------------------------


def _panel_text(event: RunProgress, *, ascii: bool = False) -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, width=100, color_system=None, force_terminal=True)
    console.print(
        render_panel(
            level="none",
            ascii=ascii,
            total=2_000,
            done=2_000,
            actions=2_000,
            successes=400,
            title="panel.learn.title",
            event=event,
        )
    )
    return buffer.getvalue()


def test_the_panel_shows_delivery_as_a_bar_with_the_time_left():
    event = RunProgress(2_000, 2_000, 2_000, 400, 2_400, 1_478, 1_400, None, 47.0)
    output = _panel_text(event)
    row = next(line for line in output.split("\n") if text("panel.delivered") in line)
    assert "▕" + "█" * 9 + "░" * 7 + "▏ 1,478 / 2,400 · " + text("eta.seconds", seconds=50) in row
    ascii_row = next(
        line for line in _panel_text(event, ascii=True).split("\n") if "Delivered" in line
    )
    assert "[#########-------] 1,478 / 2,400 - " in ascii_row


def test_the_panel_shows_no_time_before_it_is_known():
    event = RunProgress(1_000, 2_000, 1_000, 200, 1_200, 900, 800)
    row = next(line for line in _panel_text(event).split("\n") if "Delivered" in line)
    assert row.rstrip(" │").endswith("900 / 1,200")


def test_the_note_shows_under_the_panel_while_the_queue_drains():
    buffer, renderer = _terminal(100)
    renderer.run_started(2_000, title="panel.learn.title")
    renderer.progress(RunProgress(2_000, 2_000, 2_000, 400, 2_400, 1_478, 1_400, None, 47.0))
    renderer.drain_begin(Say("drain.waiting", {"remaining": 922}))
    renderer.drain_progress(DeliverProgress(1_500, 2_400, 40.0))  # the panel's own bar shows it
    during = _Screen(buffer.getvalue()).lines
    renderer.drain_end()
    renderer.run_ended()
    final = _Screen(buffer.getvalue()).lines

    note = text("drain.note")
    assert any(note[:40] in line for line in during)
    bottom = max(at for at, line in enumerate(during) if line.startswith("╰"))
    assert note[:40] in during[bottom + 1]  # right below the panel
    assert not any(note[:40] in line for line in final)
    assert any("1,478 / 2,400" in line for line in final)
    assert not any(SENDING in line for line in during + final)


def test_the_note_says_ctrl_c_quits_when_it_does():
    buffer, renderer = _terminal(100)
    renderer.run_started(10, title="panel.learn.title")
    renderer.drain_begin(Say("drain.waiting_quit", {"remaining": 5}))
    during = _Screen(buffer.getvalue()).lines
    renderer.drain_end()
    renderer.run_ended()
    shown = " ".join(" ".join(during).split())  # the note wraps like any paragraph
    assert text("drain.note_quit") in shown
