"""Source adapters (design section 2, lane L2).

Each adapter module exposes fetch(http_get, ...) -> result dict with the
shape defined in signal_hub.adapters.base (status OK|UNCHANGED|ERROR,
etag, payload, error). HTTP and the clock are always injected -- adapters
never touch the network or the filesystem on their own.
"""
