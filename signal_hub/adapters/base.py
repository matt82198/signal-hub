"""Adapter contract: injectable HTTP, result envelopes, phase mapping.

Deviation from the design's minimal ``http_get(url, timeout) -> bytes``
signature, documented here per CLAUDE.md: conditional GET (ETag /
If-None-Match / 304) cannot be expressed by a bytes-only return, so the
injected callable is::

    http_get(url, headers=None, timeout=30) -> HttpResponse

where HttpResponse carries (status, headers, body). 304 must be returned
as a normal response, never raised. urllib_http_get below is the real
implementation; tests inject doubles and never touch the network.
"""

import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_TIMEOUT = 30

# game_type / season_type -> normalized phase (design: REG | PRE | POST).
_PHASE_MAP = {
    "REG": "REG",
    "PRE": "PRE",
    "POST": "POST",
    "WC": "POST",
    "DIV": "POST",
    "CON": "POST",
    "SB": "POST",
}


@dataclass
class HttpResponse:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def header(self, name):
        """Case-insensitive header lookup."""
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


def urllib_http_get(url, headers=None, timeout=DEFAULT_TIMEOUT):
    """Real http_get: stdlib urllib, redirects followed, 3xx/4xx/5xx and
    304 all returned as HttpResponse (never raised)."""
    request = urllib.request.Request(url, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return HttpResponse(
                status=resp.status, headers=dict(resp.headers), body=resp.read()
            )
    except urllib.error.HTTPError as exc:  # includes 304 and 404
        return HttpResponse(
            status=exc.code, headers=dict(exc.headers or {}), body=b""
        )


def normalize_phase(value):
    """Map an nflverse game_type/season_type to REG|PRE|POST, None if unknown."""
    return _PHASE_MAP.get((value or "").strip().upper())


def ok_result(payload, etag=None):
    return {"status": "OK", "etag": etag, "payload": payload, "error": None}


def unchanged_result(etag=None):
    return {"status": "UNCHANGED", "etag": etag, "payload": None, "error": None}


def error_result(error):
    return {"status": "ERROR", "etag": None, "payload": None, "error": error}


def conditional_get(http_get, url, etag=None, timeout=DEFAULT_TIMEOUT):
    """Perform a GET with If-None-Match when an etag is known.

    Returns (response, result): result is a terminal envelope
    (UNCHANGED / ERROR) when the fetch cannot yield a body, else None and
    the caller parses response.body.
    """
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    try:
        response = http_get(url, headers=headers, timeout=timeout)
    except Exception as exc:  # injected callables may raise anything
        return None, error_result("GET %s failed: %s" % (url, exc))
    if response.status == 304:
        return response, unchanged_result(etag)
    if response.status != 200:
        return response, error_result(
            "GET %s returned HTTP %d" % (url, response.status)
        )
    return response, None
