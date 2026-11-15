"""Verify connection-scoped request totals and their rollover semantics."""

import pytest

from tunnel_switch.counters import RequestCounter


def _expect_totals(
    counter: RequestCounter, *, active: bool, current: int, last: int
) -> None:
    actual = counter.snapshot(active=active)
    expected = {"current": current, "last": last}
    if actual != expected:
        pytest.fail(f"Request totals {actual!r} do not match {expected!r}")


def test_totals_move_to_last_when_the_next_connection_begins() -> None:
    """Move the finished connection's total into the next snapshot."""
    counter = RequestCounter()
    counter.begin()
    counter.record()
    counter.record()
    _expect_totals(counter, active=True, current=2, last=0)
    counter.begin()
    _expect_totals(counter, active=True, current=0, last=2)


def test_a_tunnel_that_exited_reports_its_own_counts_as_last() -> None:
    """Report an exited tunnel's total even before another connection begins."""
    counter = RequestCounter()
    counter.begin()
    counter.record()
    # ssh left without a rotation following it, so nothing is active and the
    # counts still held belong to the connection that just finished.
    _expect_totals(counter, active=False, current=0, last=1)
    counter.begin()
    _expect_totals(counter, active=True, current=0, last=1)


def test_a_quiet_connection_still_replaces_the_previous_total() -> None:
    """Replace the previous total with zero when a quiet connection ends."""
    counter = RequestCounter()
    counter.begin()
    for _ in range(4):
        counter.record()
    counter.begin()
    # Alive, the quiet connection shows the previous total as last...
    _expect_totals(counter, active=True, current=0, last=4)
    # ...and finished, it replaces it with its own total of zero.
    _expect_totals(counter, active=False, current=0, last=0)


def test_counts_before_any_connection_are_reported_once_started() -> None:
    """Retain pre-connection counts as the last total on first connection."""
    counter = RequestCounter()
    counter.record()
    _expect_totals(counter, active=False, current=0, last=1)
    counter.begin()
    _expect_totals(counter, active=True, current=0, last=1)
