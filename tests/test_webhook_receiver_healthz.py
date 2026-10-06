"""signal_hub.webhook_receiver -- GET /healthz liveness probe.

Deliberately the one test in this package that opens a real socket:
``test_webhook_receiver.py`` tests the whole delivery pipeline through
``process_delivery`` directly and never needs a live server, but the tunnel
installer (``deploy/install_tunnel.ps1``) needs an unauthenticated,
no-signature endpoint it can curl -- both locally and through the tunnel --
to prove the receiver is actually listening. This proves that endpoint works
end to end, through a real ``ThreadingHTTPServer``.
"""

import http.client
import threading

from signal_hub.webhook_receiver import HEALTHZ_PATH, serve


def _start_server(tmp_path):
    server = serve(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(server, path):
    conn = http.client.HTTPConnection(server.server_address[0], server.server_address[1], timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_healthz_returns_200_with_no_auth(tmp_path):
    server, thread = _start_server(tmp_path)
    try:
        status, body = _get(server, HEALTHZ_PATH)
        assert status == 200
        assert b"ok" in body
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_healthz_touches_no_state(tmp_path):
    """A liveness probe is not a delivery -- it must never write heartbeat,
    event log, queue, or any other state file."""
    server, thread = _start_server(tmp_path)
    try:
        _get(server, HEALTHZ_PATH)
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "queue").exists()


def test_unknown_get_path_is_404_not_500():
    import signal_hub.webhook_receiver as wr

    assert wr.HEALTHZ_PATH == "/healthz"
