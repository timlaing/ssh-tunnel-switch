"""HTTP request totals split across tunnel connections, for the status panel.

The proxy records on the service event loop while the display thread reads
``snapshot()``, so the values are plain integers: an attribute read or write
of an int is atomic under the GIL, and only one thread ever increments, so no
lock is needed to keep the two totals coherent. The one mutation that moves
totals between the buckets, ``begin()``, runs on the event loop under the
manager's state lock — the same lock ``status()`` holds while reading the
pair — so a rotation can never be caught halfway through moving them.
"""


class RequestCounter:
    """Count the HTTP requests the tunnel carried, one total per connection.

    ``current`` totals the connection the panel calls current and ``last`` the
    one before it. A connection's counts move into ``last`` when the next
    connection begins (``begin()``), which is the moment the panel can name
    both values; a tunnel that dies without a following connection keeps its
    own total in ``current`` until then, and ``snapshot()`` reports it as the
    last value while no connection is current.
    """

    def __init__(self):
        self.current = 0
        self.last = 0
        # True while "current" still belongs to a connection that has not been
        # superseded: it tells a finished connection's total, however small,
        # apart from a total that begin() has already rolled into "last".
        self._open = False

    def record(self) -> None:
        """Count one request carried over the tunnel."""
        self.current += 1
        self._open = True

    def begin(self) -> None:
        """Open a new connection's total, keeping the finished one as last."""
        if self._open:
            self.last, self.current = self.current, 0
        self._open = True

    def snapshot(self, *, active: bool) -> dict:
        """Read the two totals for the panel.

        ``active`` says the adopted connection is still the one running. While
        it is not — ssh exited on its own, or a rotation is between the old
        tunnel and the new one — the counts being collected are the finished
        connection's, so they are reported as ``last``, exactly as the
        ``begin()`` that follows will store them.
        """
        if active:
            return {"current": self.current, "last": self.last}
        return {"current": 0, "last": self.current if self._open else self.last}
