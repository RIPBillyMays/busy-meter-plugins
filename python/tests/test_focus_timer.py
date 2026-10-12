"""The focus timer on a fake clock: single blocks, Pomodoro cycles with a long break every 4th, and the command."""

import unittest
from unittest import mock

from busymeter import cli
from busymeter.recipes.focus_timer import SCHEMA, FocusTimer, _mmss


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def timer(**settings):
    clock = Clock()
    messages = []
    recipe = FocusTimer(SCHEMA.validate(settings), clock=clock, say=messages.append)
    recipe.live = False
    recipe.start()
    recipe.connected()
    return recipe, clock, messages


def timeline(recipe, clock, until, step=30):
    """(minute, active) each time the active set changes."""
    out = []
    last = None
    while clock.now <= until and not recipe.finished:
        active = recipe.poll()
        if active != last:
            out.append((clock.now / 60, active))
            last = active
        clock.now += step
    return out


class FocusTimerTests(unittest.TestCase):
    def test_one_block_then_done(self):
        recipe, clock, messages = timer(focus_minutes=25, cycles=1)
        self.assertEqual([(0, ["focus"]), (25, [])], timeline(recipe, clock, 3600))
        self.assertTrue(recipe.finished)
        self.assertIn("Done.", messages)

    def test_pomodoro_long_break_every_fourth(self):
        recipe, clock, _ = timer(cycles=0)
        changes = timeline(recipe, clock, 131 * 60)
        starts = [m for m, a in changes if a == ["focus"]]
        breaks = [m for m, a in changes if a == []]
        self.assertEqual([0, 30, 60, 90, 130], starts)  # 25 + 5, ..., the 4th break is 15
        self.assertEqual([25, 55, 85, 115], breaks)
        self.assertFalse(recipe.finished)

    def test_cycles_end_after_the_last_focus_block(self):
        recipe, clock, _ = timer(cycles=2, focus_minutes=10, short_break_minutes=2)
        changes = timeline(recipe, clock, 10_000)
        self.assertEqual([(0, ["focus"]), (10, []), (12, ["focus"]), (22, [])], changes)
        self.assertTrue(recipe.finished)

    def test_zero_length_breaks_go_straight_on(self):
        recipe, clock, _ = timer(cycles=3, focus_minutes=10, short_break_minutes=0, long_break_minutes=0)
        changes = timeline(recipe, clock, 10_000)
        self.assertEqual([(0, ["focus"]), (30, [])], changes)

    def test_a_long_sleep_skips_phases(self):
        recipe, clock, _ = timer(cycles=0)
        clock.now = 61 * 60  # e.g. the laptop slept: focus 25, break 5, focus 25, break 5 -> in focus #3
        self.assertEqual(["focus"], recipe.poll())
        self.assertEqual(3, recipe.block)

    def test_the_block_starts_when_connected_not_before(self):
        clock = Clock()
        recipe = FocusTimer(SCHEMA.validate({"cycles": 1, "focus_minutes": 10}), clock=clock, say=lambda m: None)
        recipe.live = False
        recipe.start()
        clock.now = 300  # five minutes waiting for Allow
        self.assertEqual([], recipe.poll())
        self.assertEqual(0, recipe.block)
        recipe.connected()
        clock.now = 300 + 9 * 60
        self.assertEqual(["focus"], recipe.poll())  # still 1 minute left: the wait didn't count
        clock.now = 300 + 10 * 60
        self.assertEqual([], recipe.poll())
        self.assertTrue(recipe.finished)

    def test_signal_and_suggested_level(self):
        recipe, _, _ = timer(focus_level=3)
        self.assertEqual([{"name": "focus", "label": "Focus block", "suggest": 3}], recipe.signals())

    def test_remaining_time_is_printed(self):
        recipe, clock, messages = timer(cycles=1, focus_minutes=3)
        timeline(recipe, clock, 400, step=10)
        self.assertIn("Focus: 03:00 left.", messages)
        self.assertIn("Focus: 01:50 left.", messages)
        self.assertEqual("1:00:00", _mmss(3600))

    def test_the_focus_command(self):
        with mock.patch("busymeter.recipes.cli.run_recipe", return_value=0) as run:
            self.assertEqual(0, cli.main(["focus", "25m"]))
            run.assert_called_with("focus_timer", overrides={"focus_minutes": 25.0, "cycles": 1})
            cli.main(["focus", "--pomodoro", "50m", "--break", "10m", "--cycles", "3"])
            run.assert_called_with("focus_timer", overrides={"focus_minutes": 50.0, "short_break_minutes": 10.0,
                                                             "cycles": 3})
            cli.main(["focus", "--pomodoro"])
            run.assert_called_with("focus_timer", overrides={})


if __name__ == "__main__":
    unittest.main()
