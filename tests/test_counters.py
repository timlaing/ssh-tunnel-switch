from tunnel_switch.counters import RequestCounter


def test_totals_move_to_last_when_the_next_connection_begins():
    counter = RequestCounter()
    counter.begin()
    counter.record()
    counter.record()
    assert counter.snapshot(active=True) == {"current": 2, "last": 0}
    counter.begin()
    assert counter.snapshot(active=True) == {"current": 0, "last": 2}


def test_a_tunnel_that_exited_reports_its_own_counts_as_last():
    counter = RequestCounter()
    counter.begin()
    counter.record()
    # ssh left without a rotation following it, so nothing is active and the
    # counts still held belong to the connection that just finished.
    assert counter.snapshot(active=False) == {"current": 0, "last": 1}
    counter.begin()
    assert counter.snapshot(active=True) == {"current": 0, "last": 1}


def test_a_quiet_connection_still_replaces_the_previous_total():
    counter = RequestCounter()
    counter.begin()
    for _ in range(4):
        counter.record()
    counter.begin()
    # Alive, the quiet connection shows the previous total as last...
    assert counter.snapshot(active=True) == {"current": 0, "last": 4}
    # ...and finished, it replaces it with its own total of zero.
    assert counter.snapshot(active=False) == {"current": 0, "last": 0}


def test_counts_before_any_connection_are_reported_once_started():
    counter = RequestCounter()
    counter.record()
    assert counter.snapshot(active=False) == {"current": 0, "last": 1}
    counter.begin()
    assert counter.snapshot(active=True) == {"current": 0, "last": 1}
