"""Tests for the injectable time source.

Time is the one input a safety control cannot validate. A circuit-breaker
cooldown, a rate window, and a staleness check all reduce to "how long since",
and every one of them is wrong if the clock can move backwards underneath it.
So the module makes two separate promises, and both are tested here:

* wall clock and monotonic clock are *different* readings, and durations use the
  monotonic one;
* :class:`SystemClock` -- the only clock that runs in production, and until now
  the only one with no test -- returns aware UTC and a non-decreasing monotonic.

:class:`ManualClock.set_wall_clock` moving time backwards is not a curiosity: it
is the mechanism by which the other test modules prove their timers are immune,
so its own behaviour needs pinning here.
"""

from __future__ import annotations

import datetime as _dt
import time
import unittest

from trading.core.clock import UTC, Clock, ManualClock, SystemClock


class TestClockContract(unittest.TestCase):
    def test_the_base_clock_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            Clock()

    def test_now_is_abstract(self):
        self.assertIn("now", Clock.__abstractmethods__)

    def test_monotonic_is_not_abstract_but_has_no_default(self):
        """A subclass that forgets it fails loudly rather than guessing.

        Leaving it concrete-but-raising means a clock is usable for timestamps
        alone, while any duration measurement stops the caller instead of
        silently falling back to wall clock.
        """
        self.assertNotIn("monotonic_seconds", Clock.__abstractmethods__)

        class TimestampsOnly(Clock):
            def now(self):
                return _dt.datetime(2026, 1, 1, tzinfo=UTC)

        with self.assertRaises(NotImplementedError):
            TimestampsOnly().monotonic_seconds()


class TestSystemClock(unittest.TestCase):
    """The clock that actually runs in production."""

    def setUp(self):
        self.clock = SystemClock()

    def test_now_is_timezone_aware(self):
        self.assertIsNotNone(self.clock.now().tzinfo)

    def test_now_is_utc(self):
        self.assertEqual(self.clock.now().utcoffset(), _dt.timedelta(0))

    def test_now_is_roughly_the_current_time(self):
        """Guards against a clock that returns a constant or a local time."""
        delta = abs(
            (self.clock.now() - _dt.datetime.now(tz=UTC)).total_seconds()
        )
        self.assertLess(delta, 5)

    def test_monotonic_is_a_float(self):
        self.assertIsInstance(self.clock.monotonic_seconds(), float)

    def test_monotonic_does_not_decrease(self):
        readings = [self.clock.monotonic_seconds() for _ in range(50)]
        self.assertEqual(readings, sorted(readings))

    def test_monotonic_advances_over_a_real_sleep(self):
        before = self.clock.monotonic_seconds()
        time.sleep(0.01)
        self.assertGreater(self.clock.monotonic_seconds(), before)

    def test_monotonic_is_not_the_wall_clock_epoch(self):
        """They must be independent readings, not one derived from the other."""
        wall_epoch = self.clock.now().timestamp()
        self.assertNotAlmostEqual(
            self.clock.monotonic_seconds(), wall_epoch, delta=1.0
        )

    def test_it_is_stateless(self):
        self.assertEqual(SystemClock.__slots__, ())


class TestManualClockDefaults(unittest.TestCase):
    def test_it_starts_at_a_fixed_instant(self):
        """A fixed default is what makes audit-hash tests reproducible."""
        self.assertEqual(
            ManualClock().now(), _dt.datetime(2026, 1, 1, tzinfo=UTC)
        )

    def test_monotonic_starts_at_zero(self):
        self.assertEqual(ManualClock().monotonic_seconds(), 0.0)

    def test_a_naive_start_is_refused(self):
        with self.assertRaises(ValueError):
            ManualClock(_dt.datetime(2026, 1, 1))

    def test_an_explicit_start_is_honoured(self):
        start = _dt.datetime(2020, 6, 5, 12, 0, tzinfo=UTC)
        self.assertEqual(ManualClock(start).now(), start)

    def test_a_non_utc_start_is_converted(self):
        tz = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
        start = _dt.datetime(2026, 1, 1, 5, 30, tzinfo=tz)
        clock = ManualClock(start)
        self.assertEqual(clock.now(), _dt.datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(clock.now().utcoffset(), _dt.timedelta(0))

    def test_it_does_not_move_on_its_own(self):
        clock = ManualClock()
        first = clock.now()
        time.sleep(0.01)
        self.assertEqual(clock.now(), first)


class TestManualClockAdvance(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()

    def test_advance_moves_both_clocks(self):
        before_wall, before_mono = self.clock.now(), self.clock.monotonic_seconds()
        self.clock.advance(60)
        self.assertEqual(
            (self.clock.now() - before_wall).total_seconds(), 60
        )
        self.assertEqual(self.clock.monotonic_seconds() - before_mono, 60)

    def test_advance_accumulates(self):
        for _ in range(3):
            self.clock.advance(10)
        self.assertEqual(self.clock.monotonic_seconds(), 30)

    def test_advance_accepts_a_fraction(self):
        self.clock.advance(0.5)
        self.assertEqual(self.clock.monotonic_seconds(), 0.5)

    def test_advance_accepts_zero(self):
        self.clock.advance(0)
        self.assertEqual(self.clock.monotonic_seconds(), 0.0)

    def test_advance_refuses_to_go_backwards(self):
        """The monotonic clock must be monotonic even in a test."""
        with self.assertRaises(ValueError):
            self.clock.advance(-1)

    def test_a_refused_advance_changes_nothing(self):
        with self.assertRaises(ValueError):
            self.clock.advance(-1)
        self.assertEqual(self.clock.monotonic_seconds(), 0.0)
        self.assertEqual(self.clock.now(), ManualClock().now())

    def test_the_result_stays_timezone_aware(self):
        self.clock.advance(86400)
        self.assertEqual(self.clock.now().utcoffset(), _dt.timedelta(0))


class TestWallClockMovesIndependently(unittest.TestCase):
    """The mechanism other modules use to prove timers ignore wall clock."""

    def setUp(self):
        self.clock = ManualClock()

    def test_setting_the_wall_clock_does_not_move_the_monotonic_clock(self):
        self.clock.set_wall_clock(_dt.datetime(2030, 1, 1, tzinfo=UTC))
        self.assertEqual(self.clock.monotonic_seconds(), 0.0)

    def test_the_wall_clock_can_be_moved_backwards(self):
        """NTP correction, or a misconfigured host. It has to be modellable."""
        self.clock.advance(3600)
        self.clock.set_wall_clock(_dt.datetime(2020, 1, 1, tzinfo=UTC))
        self.assertEqual(self.clock.now(), _dt.datetime(2020, 1, 1, tzinfo=UTC))

    def test_a_backwards_wall_clock_leaves_the_monotonic_clock_untouched(self):
        """The property every cooldown and staleness check depends on."""
        self.clock.advance(3600)
        self.clock.set_wall_clock(_dt.datetime(2020, 1, 1, tzinfo=UTC))
        self.assertEqual(self.clock.monotonic_seconds(), 3600)

    def test_a_naive_wall_clock_is_refused(self):
        with self.assertRaises(ValueError):
            self.clock.set_wall_clock(_dt.datetime(2030, 1, 1))

    def test_a_non_utc_wall_clock_is_converted(self):
        tz = _dt.timezone(_dt.timedelta(hours=-5))
        self.clock.set_wall_clock(_dt.datetime(2030, 1, 1, 19, 0, tzinfo=tz))
        self.assertEqual(
            self.clock.now(), _dt.datetime(2030, 1, 2, 0, 0, tzinfo=UTC)
        )

    def test_advancing_after_a_backwards_jump_still_works(self):
        self.clock.set_wall_clock(_dt.datetime(2020, 1, 1, tzinfo=UTC))
        self.clock.advance(60)
        self.assertEqual(
            self.clock.now(), _dt.datetime(2020, 1, 1, 0, 1, tzinfo=UTC)
        )
        self.assertEqual(self.clock.monotonic_seconds(), 60)


class TestBothClocksSatisfyTheSameInterface(unittest.TestCase):
    """A test that passes with ManualClock must mean something for SystemClock."""

    def test_both_are_clocks(self):
        for clock in (SystemClock(), ManualClock()):
            with self.subTest(clock=type(clock).__name__):
                self.assertIsInstance(clock, Clock)

    def test_both_return_aware_utc(self):
        for clock in (SystemClock(), ManualClock()):
            with self.subTest(clock=type(clock).__name__):
                self.assertEqual(clock.now().utcoffset(), _dt.timedelta(0))

    def test_both_return_a_float_monotonic(self):
        for clock in (SystemClock(), ManualClock()):
            with self.subTest(clock=type(clock).__name__):
                self.assertIsInstance(clock.monotonic_seconds(), float)


if __name__ == "__main__":
    unittest.main()
