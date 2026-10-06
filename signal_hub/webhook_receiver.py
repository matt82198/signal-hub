"""GitHub -> signal-hub webhook receiver (Phase 1 of the box event bridge).

A tiny stdlib HTTP server: ``POST /github`` -> verify ``X-Hub-Signature-256``
(HMAC-SHA256 over the raw body) -> dedup on ``X-GitHub-Delivery`` -> build ONE
typed event -> append it to signal-hub's existing event log (design section 3,
``signal_hub.events``) -> evaluate it against the committed rules (design
section 4, ``signal_hub.rules_engine``) -> enqueue any fired task into
``queue/`` (design section 5, ``signal_hub.queue``) -> mirror the fired task
into ``~/conductor3/state/signal-hub-queue.jsonl`` so a live orchestrator
session can ``Monitor`` that file instead of polling GitHub.

No new third-party dependency: ``http.server`` + ``hmac`` + ``hashlib``, all
stdlib. No ``eval``/``exec`` anywhere (same hard constraint as the rules
engine).

SECRET
------
Read from the ``SIGNAL_HUB_GH_WEBHOOK_SECRET`` env var first, else a file
``state/.github-webhook-secret`` (one line, trailing whitespace stripped).
Neither is ever committed -- ``state/`` is gitignored, and the receiver never
logs or echoes the secret, the signature header, or the raw body.

DEDUP
-----
Two independent gates, same as the sports pipeline:

* **Delivery-level** -- ``X-GitHub-Delivery`` is a UUID GitHub assigns per
  webhook attempt (retries reuse it).  Tracked in its own date-partitioned
  index (``state/webhook-deliveries/.events-seen``), reusing
  :class:`signal_hub.events.SeenIndex` verbatim rather than inventing a
  second seen-index format.
* **Event-level** -- the typed event's derived ``event_id`` goes through the
  SAME event log and seen-index the 5-minute tick uses
  (``state/events/*.jsonl``, ``state/.events-seen``), so a webhook-sourced
  event and a tick-sourced event share one dedup domain.

HEARTBEAT
---------
``state/.signal-hub-webhook-heartbeat`` is written LAST, after the event is
logged, rules evaluated and any task enqueued+mirrored -- the same
backup-fleet lesson ``tick.py`` encodes: a heartbeat written up front would
leave a wedged receiver looking healthy.  It is only written for an
authenticated, successfully processed delivery; a rejected (401) request
touches no state at all.

The ``.HALT`` kill switch (``state/.HALT``) is honoured the same way tick.py
honours it: present -> every delivery is still signature-checked (so an
attacker cannot use HALT to probe) but nothing is appended, enqueued, mirrored
or heartbeated.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from signal_hub.clock import iso_utc, system_now
from signal_hub.events import Event, EventLog, SeenIndex, record_events
from signal_hub.queue import enqueue_task
from signal_hub.rules_engine import RuleEngine, ThrottleState, load_rules

__all__ = [
    "SECRET_ENV_VAR",
    "SECRET_FILENAME",
    "HEARTBEAT_FILE",
    "HEALTHZ_PATH",
    "DeliveryOutcome",
    "load_webhook_secret",
    "verify_signature",
    "build_github_event",
    "process_delivery",
    "default_root",
    "default_conductor_queue_path",
    "serve",
    "main",
]

#: Env var carrying the webhook secret. Never printed, never logged.
SECRET_ENV_VAR = "SIGNAL_HUB_GH_WEBHOOK_SECRET"
#: Fallback file under state/ (gitignored, never committed) if the env var is unset.
SECRET_FILENAME = ".github-webhook-secret"

HEARTBEAT_FILE = ".signal-hub-webhook-heartbeat"
HALT_FILE = ".HALT"
#: Shared with tick.py -- one throttle/dedup ledger for both tick- and
#: webhook-sourced events, so a rule cannot fire twice from two producers.
THROTTLE_FILE = ".rules-fired.jsonl"

DEFAULT_PORT = 8787
DEFAULT_PATH = "/github"
#: Unauthenticated liveness probe -- "is the process up and listening", not
#: "is the pipeline healthy" (that is state/.signal-hub-webhook-heartbeat).
#: Exists so a tunnel/service installer can verify end-to-end reachability
#: without needing a signed payload. Touches no state, logs nothing new.
HEALTHZ_PATH = "/healthz"

_SIG_PREFIX = "sha256="


# ---------------------------------------------------------------------------
# secret + signature
# ---------------------------------------------------------------------------

def load_webhook_secret(state_root) -> bytes:
    """Env var wins; else a one-line file under state/; else empty (refuses everything)."""
    env = os.environ.get(SECRET_ENV_VAR)
    if env:
        return env.encode("utf-8")
    path = Path(state_root) / SECRET_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return b""
    return text.strip().encode("utf-8")


def verify_signature(secret: bytes, body: bytes, header_value: Optional[str]) -> bool:
    """HMAC-SHA256 over the raw body, compared with ``hmac.compare_digest``.

    Never raises. An empty secret, a missing header, or a header not spelled
    ``sha256=<hex>`` are all simply "not verified" -- the design's "absent
    data can never satisfy a rule" principle, applied to auth.
    """
    if not secret or not header_value or not header_value.startswith(_SIG_PREFIX):
        return False
    provided = header_value[len(_SIG_PREFIX):]
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, provided)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# typed event builders
# ---------------------------------------------------------------------------

def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _entity(kind: str, ident: str) -> dict:
    return {"kind": kind, "id": ident}


def _repo_full_name(payload: dict) -> str:
    return _text((payload.get("repository") or {}).get("full_name"))


def _event_workflow_run(payload: dict, now: datetime) -> Optional[Event]:
    if _text(payload.get("action")) != "completed":
        return None
    run = payload.get("workflow_run") or {}
    repo = _repo_full_name(payload)
    run_id = run.get("id")
    return Event(
        type="github.workflow_run.completed",
        ts=iso_utc(now),
        source="github",
        confidence=1.0,
        entities=[_entity("repo", repo), _entity("workflow_run", _text(run_id))],
        payload={
            "repo": repo or None,
            "workflow": run.get("name"),
            "conclusion": run.get("conclusion"),
            "head_sha": run.get("head_sha"),
            "head_branch": run.get("head_branch"),
            "run_id": run_id,
            "html_url": run.get("html_url"),
        },
        identity=(repo, _text(run_id)),
    )


def _event_check_suite(payload: dict, now: datetime) -> Optional[Event]:
    if _text(payload.get("action")) != "completed":
        return None
    suite = payload.get("check_suite") or {}
    repo = _repo_full_name(payload)
    suite_id = suite.get("id")
    pulls = suite.get("pull_requests") or []
    pr_numbers = [
        pr.get("number") for pr in pulls if isinstance(pr, dict) and pr.get("number") is not None
    ]
    return Event(
        type="github.check_suite.completed",
        ts=iso_utc(now),
        source="github",
        confidence=1.0,
        entities=[_entity("repo", repo), _entity("check_suite", _text(suite_id))],
        payload={
            "repo": repo or None,
            "conclusion": suite.get("conclusion"),
            "head_sha": suite.get("head_sha"),
            "head_branch": suite.get("head_branch"),
            "check_suite_id": suite_id,
            "pr_numbers": pr_numbers,
            "pr_count": len(pr_numbers),
        },
        identity=(repo, _text(suite_id)),
    )


def _event_pull_request(payload: dict, now: datetime) -> Optional[Event]:
    action = _text(payload.get("action"))
    if not action:
        return None
    pr = payload.get("pull_request") or {}
    repo = _repo_full_name(payload)
    number = payload.get("number", pr.get("number"))
    head_sha = (pr.get("head") or {}).get("sha")
    event_payload: dict[str, Any] = {
        "repo": repo or None,
        "number": number,
        "state": pr.get("state"),
        "merged": bool(pr.get("merged", False)),
    }
    if "mergeable_state" in pr:
        event_payload["mergeable_state"] = pr.get("mergeable_state")
    return Event(
        type="github.pull_request.%s" % action,
        ts=iso_utc(now),
        source="github",
        confidence=1.0,
        entities=[_entity("repo", repo), _entity("pull_request", _text(number))],
        payload=event_payload,
        identity=(repo, _text(number), action, _text(head_sha)),
    )


def _event_push(payload: dict, now: datetime) -> Optional[Event]:
    repo = _repo_full_name(payload)
    ref = payload.get("ref")
    after = payload.get("after")
    return Event(
        type="github.push",
        ts=iso_utc(now),
        source="github",
        confidence=1.0,
        entities=[_entity("repo", repo)],
        payload={"repo": repo or None, "ref": ref, "after": after},
        identity=(repo, _text(ref), _text(after)),
    )


_EVENT_BUILDERS: dict[str, Callable[[dict, datetime], Optional[Event]]] = {
    "workflow_run": _event_workflow_run,
    "check_suite": _event_check_suite,
    "pull_request": _event_pull_request,
    "push": _event_push,
}


def build_github_event(gh_event: str, payload: dict, now: datetime) -> Optional[Event]:
    """Build the one typed event for this delivery, or ``None``.

    ``None`` is a legitimate, non-error outcome: an unrecognized
    ``X-GitHub-Event`` (``ping``, a future event type we have not wired) or a
    recognized one whose ``action`` is not the completed/settled state we
    track (e.g. ``workflow_run`` still ``in_progress``). Inputs always produce
    outputs -- here, the output of "nothing to derive yet" is silence, not an
    error.
    """
    builder = _EVENT_BUILDERS.get(gh_event)
    if builder is None or not isinstance(payload, dict):
        return None
    return builder(payload, now)


# ---------------------------------------------------------------------------
# delivery-level dedup (X-GitHub-Delivery)
# ---------------------------------------------------------------------------

def _delivery_index(state_dir, now) -> SeenIndex:
    """Reuses SeenIndex verbatim, rooted at its own subdirectory so delivery
    ids never share a namespace with sports event ids."""
    return SeenIndex(Path(state_dir) / "webhook-deliveries", clock=now)


# ---------------------------------------------------------------------------
# conductor3 orchestrator wake-up mirror
# ---------------------------------------------------------------------------

def default_conductor_queue_path() -> Path:
    return Path.home() / "conductor3" / "state" / "signal-hub-queue.jsonl"


def _mirror_to_conductor_queue(path: Path, record: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
    with open(path, "a", encoding="ascii", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------

@dataclass
class DeliveryOutcome:
    """Everything one delivery produced. Always returned, never raised."""

    status_code: int
    reason: str
    event: Optional[Event] = None
    duplicate: bool = False
    halted: bool = False
    actions: list = field(default_factory=list)
    enqueued: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status_code < 400


def process_delivery(
    root,
    *,
    gh_event: str,
    delivery_id: str,
    body: bytes,
    signature_header: Optional[str],
    now: Callable[[], datetime] = system_now,
    rules_dir=None,
    conductor_queue_path=None,
) -> DeliveryOutcome:
    """Handle one webhook delivery end to end. Always returns a DeliveryOutcome."""
    root = Path(root)
    state_dir = root / "state"
    queue_dir = root / "queue"
    rules_dir = Path(rules_dir) if rules_dir is not None else root / "rules"

    secret = load_webhook_secret(state_dir)
    if not verify_signature(secret, body, signature_header):
        # Unsigned/invalid: touch nothing, not even the dedup index.
        return DeliveryOutcome(401, "invalid signature")

    if not delivery_id:
        return DeliveryOutcome(400, "missing X-GitHub-Delivery")

    moment = now()

    halt = state_dir / HALT_FILE
    if halt.exists():
        return DeliveryOutcome(200, "halted", halted=True)

    dedup = _delivery_index(state_dir, now)
    if dedup.has(delivery_id):
        return DeliveryOutcome(200, "duplicate delivery", duplicate=True)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        dedup.mark(delivery_id)
        return DeliveryOutcome(400, "invalid JSON body")
    if not isinstance(payload, dict):
        dedup.mark(delivery_id)
        return DeliveryOutcome(400, "payload must be a JSON object")

    outcome = DeliveryOutcome(200, "ok")
    event = build_github_event(gh_event, payload, moment)

    if event is not None:
        event_log = EventLog(state_dir, clock=now)
        seen = SeenIndex(state_dir, clock=now)
        recorded = record_events(event_log, seen, [event])
        outcome.event = recorded[0] if recorded else event

        loaded = load_rules(str(rules_dir))
        for rule_error in loaded.errors:
            outcome.errors.append("rule invalid: %s" % rule_error)
        throttle = ThrottleState.load(str(state_dir / THROTTLE_FILE), now=now)
        engine = RuleEngine(loaded.rules, throttle, now=now)
        evaluation = engine.evaluate([outcome.event.to_dict()])
        outcome.actions = evaluation.actions

        conductor_path = (
            Path(conductor_queue_path)
            if conductor_queue_path is not None
            else default_conductor_queue_path()
        )
        for action in outcome.actions:
            task = action.get("task") or {}
            args = {k: v for k, v in task.items() if k != "kind"}
            try:
                task_id = enqueue_task(
                    queue_dir,
                    rule_id=action["rule_id"],
                    event_id=action["event_id"],
                    kind=task.get("kind") or action["kind"],
                    priority=action["priority"],
                    ttl=action["ttl"] or "1d",
                    args=args,
                    now=moment,
                )
            except Exception as exc:  # enqueue failures are visible, never silent
                outcome.errors.append(
                    "enqueue failed for %s/%s: %s" % (action["rule_id"], action["event_id"], exc)
                )
                continue
            outcome.enqueued.append(task_id)
            try:
                _mirror_to_conductor_queue(
                    conductor_path,
                    {
                        "task_id": task_id,
                        "rule_id": action["rule_id"],
                        "event_id": action["event_id"],
                        "event_type": action["event_type"],
                        "kind": task.get("kind") or action["kind"],
                        "priority": action["priority"],
                        "ttl": action["ttl"],
                        "args": args,
                        "fired_at": action["fired_at"],
                        "source": "signal-hub-webhook",
                    },
                )
            except OSError as exc:
                outcome.errors.append("conductor queue mirror failed: %s" % exc)

    dedup.mark(delivery_id)

    # Heartbeat LAST: only after the event is logged, rules evaluated, and
    # any task enqueued+mirrored. See module docstring.
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / HEARTBEAT_FILE).write_text(str(int(moment.timestamp())), encoding="ascii")
    except OSError as exc:
        outcome.errors.append("heartbeat write failed: %s" % exc)

    return outcome


# ---------------------------------------------------------------------------
# stdlib HTTP server (the only part that is NOT unit tested with sockets)
# ---------------------------------------------------------------------------

def default_root() -> Path:
    env = os.environ.get("SIGNAL_HUB_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def _make_handler(root, *, now, rules_dir, conductor_queue_path, path):
    class _Handler(BaseHTTPRequestHandler):
        server_version = "signal-hub-webhook/1.0"

        def do_POST(self):  # noqa: N802 (stdlib naming)
            if self.path != path:
                self._reply(404, "not found")
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            outcome = process_delivery(
                root,
                gh_event=self.headers.get("X-GitHub-Event", ""),
                delivery_id=self.headers.get("X-GitHub-Delivery", ""),
                body=raw,
                signature_header=self.headers.get("X-Hub-Signature-256"),
                now=now,
                rules_dir=rules_dir,
                conductor_queue_path=conductor_queue_path,
            )
            self._reply(outcome.status_code, outcome.reason)

        def do_GET(self):  # noqa: N802 (stdlib naming)
            if self.path == HEALTHZ_PATH:
                self._reply(200, "ok")
                return
            self._reply(404, "not found")

        def _reply(self, code, reason):
            body = json.dumps({"status": reason}).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):  # never print headers/body/secrets
            sys.stderr.write(
                "%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args)
            )

    return _Handler


def serve(
    root,
    *,
    port: int = DEFAULT_PORT,
    now: Callable[[], datetime] = system_now,
    rules_dir=None,
    conductor_queue_path=None,
    path: str = DEFAULT_PATH,
) -> ThreadingHTTPServer:
    """Build (but do not run) the HTTP server. Bound to loopback only --
    a tunnel or reverse proxy terminates the public side."""
    handler = _make_handler(
        Path(root), now=now, rules_dir=rules_dir, conductor_queue_path=conductor_queue_path, path=path
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m signal_hub.webhook_receiver",
        description="GitHub -> signal-hub webhook receiver (POST /github).",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root", default=None, help="hub root (default: $SIGNAL_HUB_ROOT, else the repo)")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else default_root()
    server = serve(root, port=args.port)
    sys.stderr.write(
        "signal-hub webhook receiver listening on http://127.0.0.1:%d%s (root=%s)\n"
        % (args.port, DEFAULT_PATH, root)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
