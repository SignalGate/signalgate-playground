"""The time left in a wait for the app's queued logs, in words (``playground.progress``)."""

from __future__ import annotations

import math

import pytest

from playground.copy.en import text
from playground.progress import RateWindow, eta_seconds, eta_words
from playground.traffic.local_constants import DRAIN_RATE_WINDOW_S, ETA_STEP_S


def test_under_a_minute_rounds_up_to_the_step():
    assert ETA_STEP_S == 5
    assert eta_words(47) == "about 50 s left"
    assert eta_words(45) == "about 45 s left"
    assert eta_words(0.2) == "about 5 s left"
    assert eta_words(0) == "about 5 s left"
    assert eta_words(47) == text("eta.seconds", seconds=50)


def test_a_minute_or_more_rounds_up_to_whole_minutes():
    assert eta_words(130) == "about 3 min left"
    assert eta_words(60) == "about 1 min left"
    assert eta_words(601) == "about 11 min left"
    assert eta_words(130) == text("eta.minutes", minutes=3)


@pytest.mark.parametrize("seconds", [None, -1.0, math.nan, math.inf])
def test_no_estimate_gives_no_words(seconds):
    assert eta_words(seconds) is None


def test_the_estimate_needs_something_left_and_a_measured_rate():
    assert eta_seconds(100, 4.0) == 25.0
    assert eta_seconds(0, 4.0) is None
    assert eta_seconds(100, 0.0) is None
    assert eta_seconds(100, -2.0) is None
    assert eta_seconds(100, math.nan) is None
    assert eta_seconds(100, math.inf) is None


def test_the_estimate_never_says_more_than_the_wait_left():
    assert eta_seconds(100, 1.0, at_most=30.0) == 30.0
    assert eta_seconds(100, 4.0, at_most=30.0) == 25.0
    assert eta_seconds(100, 1.0, at_most=0.0) is None
    assert eta_seconds(100, 1.0, at_most=-5.0) is None
    assert eta_seconds(0, 1.0, at_most=30.0) is None


def test_a_rate_window_measures_how_fast_the_backlog_falls():
    window = RateWindow(window_s=5.0)
    assert window.rate() == 0.0
    window.add(0.0, 100)
    assert window.rate() == 0.0  # one reading is no rate yet
    window.add(1.0, 90)
    assert window.rate() == pytest.approx(10.0)
    window.add(2.0, 70)
    assert window.rate() == pytest.approx(15.0)


def test_a_rate_window_forgets_readings_older_than_its_window():
    window = RateWindow(window_s=2.0)
    window.add(0.0, 1_000)  # a fast start that is long over
    window.add(1.0, 500)
    for t in range(2, 11):
        window.add(float(t), 500 - 10 * (t - 1))
    assert window.rate() == pytest.approx(10.0)


def test_a_rate_window_is_never_negative():
    window = RateWindow()
    window.add(0.0, 10)
    window.add(1.0, 50)  # the backlog grew
    assert window.rate() == 0.0
    window.add(2.0, 50)  # and then stayed put
    assert window.rate() == 0.0
    same_time = RateWindow()
    same_time.add(3.0, 10)
    same_time.add(3.0, 5)  # two readings at one instant: no rate, not a division by zero
    assert same_time.rate() == 0.0


def test_a_rate_window_starts_again_when_the_clock_goes_back():
    window = RateWindow()
    window.add(10.0, 100)
    window.add(11.0, 90)
    window.add(5.0, 80)
    assert window.rate() == 0.0
    window.add(6.0, 70)
    assert window.rate() == pytest.approx(10.0)


def test_the_default_window_is_the_local_constant():
    window = RateWindow()
    window.add(0.0, 100)
    window.add(DRAIN_RATE_WINDOW_S * 3, 100 - int(DRAIN_RATE_WINDOW_S * 3))
    assert window.rate() == pytest.approx(1.0)
