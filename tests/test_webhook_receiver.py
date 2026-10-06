# secretscan: allow-pattern-docs -- os.environ[SIGNAL_HUB_GH_WEBHOOK_SECRET] below sets a
# fixture value for a test, never a real credential; see tools/secret_scan.py's own pragma.
"""GitHub -> signal-hub webhook receiver (Phase 1, aesop event-bridge).

Red-first: signature verification, delivery dedup, one typed event per
delivery in the existing JSONL event-log format, heartbeat-written-last, and
the three github.* rules firing on their own fixture and nothing else.

Nothing here opens a real socket -- ``process_delivery`` is the whole pipeline
minus the stdlib HTTP server wrapper, so it is testable with dict fixtures and
a frozen clock exactly like ``test_tick.py``.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from signal_hub import webhook_receiver as wh
from signal_hub.clock import FrozenClock
from signal_hub.events import EventLog
from signal_hub.queue import list_pending_tasks

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_RULES_DIR = REPO_ROOT / "rules"

SECRET = b"itsasecrettoeverybody"
T0 = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


def sign(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def clock(moment=T0):
    return FrozenClock(moment)


# ---------------------------------------------------------------------------
# fixture payloads
# ---------------------------------------------------------------------------

def workflow_run_payload(
    action="completed",
    conclusion="failure",
    head_branch="main",
    run_id=123456,
    repo="matt82198/aesop",
    head_sha="abc123abc123",
    workflow="main-full",
    html_url="https://github.com/matt82198/aesop/actions/runs/123456",
):
    return {
        "action": action,
        "workflow_run": {
            "id": run_id,
            "name": workflow,
            "conclusion": conclusion,
            "head_sha": head_sha,
            "head_branch": head_branch,
            "html_url": html_url,
        },
        "repository": {"full_name": repo},
    }


def check_suite_payload(
    action="completed",
    conclusion="success",
    check_suite_id=999,
    repo="matt82198/aesop",
    head_sha="def456def456",
    head_branch="feat/x",
    pr_numbers=(42,),
):
    return {
        "action": action,
        "check_suite": {
            "id": check_suite_id,
            "conclusion": conclusion,
            "head_sha": head_sha,
            "head_branch": head_branch,
            "pull_requests": [{"number": n} for n in pr_numbers],
        },
        "repository": {"full_name": repo},
    }


def pull_request_payload(
    action="closed",
    number=42,
    state="closed",
    merged=True,
    mergeable_state="unknown",
    repo="matt82198/aesop",
    head_sha="ghi789ghi789",
):
    pr = {"number": number, "state": state, "merged": merged, "head": {"sha": head_sha}}
    if mergeable_state is not None:
        pr["mergeable_state"] = mergeable_state
    return {
        "action": action,
        "number": number,
        "pull_request": pr,
        "repository": {"full_name": repo},
    }


def push_payload(ref="refs/heads/main", after="jkl012jkl012", repo="matt82198/aesop"):
    return {"ref": ref, "after": after, "repository": {"full_name": repo}}


def deliver(
    tmp_path,
    gh_event,
    payload,
    *,
    delivery_id="d-0001",
    secret=SECRET,
    bad_signature=False,
    no_signature=False,
    rules_dir=None,
    conductor_queue_path=None,
    now=None,
    root=None,
):
    root = root or tmp_path / "hub"
    body = json.dumps(payload).encode("utf-8")
    if no_signature:
        signature = None
    elif bad_signature:
        signature = "sha256=" + "0" * 64
    else:
        signature = sign(secret, body)
    import os

    os.environ["SIGNAL_HUB_GH_WEBHOOK_SECRET"] = secret.decode("utf-8")
    try:
        return wh.process_delivery(
            root,
            gh_event=gh_event,
            delivery_id=delivery_id,
            body=body,
            signature_header=signature,
            now=now or clock(),
            rules_dir=rules_dir if rules_dir is not None else (tmp_path / "empty-rules"),
            conductor_queue_path=conductor_queue_path or (tmp_path / "conductor-queue.jsonl"),
        )
    finally:
        os.environ.pop("SIGNAL_HUB_GH_WEBHOOK_SECRET", None)


def all_events(root):
    log = EventLog(root / "state", clock=clock())
    return list(log.iter_events())


# ---------------------------------------------------------------------------
# signature verification (pure unit tests)
# ---------------------------------------------------------------------------

def test_verify_signature_accepts_correct_hmac():
    body = b'{"a":1}'
    header = sign(SECRET, body)
    assert wh.verify_signature(SECRET, body, header) is True


def test_verify_signature_rejects_wrong_secret():
    body = b'{"a":1}'
    header = sign(b"wrong-secret", body)
    assert wh.verify_signature(SECRET, body, header) is False


def test_verify_signature_rejects_missing_header():
    assert wh.verify_signature(SECRET, b"{}", None) is False


def test_verify_signature_rejects_malformed_header():
    assert wh.verify_signature(SECRET, b"{}", "not-a-signature") is False


def test_verify_signature_rejects_empty_secret():
    body = b"{}"
    header = sign(b"", body)
    assert wh.verify_signature(b"", body, header) is False


# ---------------------------------------------------------------------------
# secret loading
# ---------------------------------------------------------------------------

def test_load_secret_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv(wh.SECRET_ENV_VAR, "env-secret")
    assert wh.load_webhook_secret(tmp_path / "state") == b"env-secret"


def test_load_secret_from_file(tmp_path, monkeypatch):
    monkeypatch.delenv(wh.SECRET_ENV_VAR, raising=False)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / wh.SECRET_FILENAME).write_text("file-secret\n", encoding="utf-8")
    assert wh.load_webhook_secret(state_dir) == b"file-secret"


def test_load_secret_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv(wh.SECRET_ENV_VAR, raising=False)
    assert wh.load_webhook_secret(tmp_path / "state") == b""


# ---------------------------------------------------------------------------
# core delivery pipeline: signature, dedup, one event per delivery
# ---------------------------------------------------------------------------

def test_valid_signature_appends_one_event(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(tmp_path, "push", push_payload(), root=root)
    assert outcome.status_code == 200
    events = all_events(root)
    assert len(events) == 1
    assert events[0]["type"] == "github.push"


def test_bad_signature_rejected_and_nothing_appended(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(tmp_path, "push", push_payload(), bad_signature=True, root=root)
    assert outcome.status_code == 401
    assert not (root / "state" / "events").exists()
    assert not (root / "state" / wh.HEARTBEAT_FILE).exists()


def test_missing_signature_rejected(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(tmp_path, "push", push_payload(), no_signature=True, root=root)
    assert outcome.status_code == 401
    assert not (root / "state" / "events").exists()


def test_replayed_delivery_id_appends_no_duplicate(tmp_path):
    root = tmp_path / "hub"
    payload = push_payload()
    first = deliver(tmp_path, "push", payload, delivery_id="dup-1", root=root)
    second = deliver(tmp_path, "push", payload, delivery_id="dup-1", root=root)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.duplicate is True
    assert len(all_events(root)) == 1


def test_different_delivery_ids_each_append(tmp_path):
    root = tmp_path / "hub"
    deliver(tmp_path, "push", push_payload(after="sha-a"), delivery_id="d-a", root=root)
    deliver(tmp_path, "push", push_payload(after="sha-b"), delivery_id="d-b", root=root)
    assert len(all_events(root)) == 2


def test_heartbeat_written_after_successful_delivery(tmp_path):
    root = tmp_path / "hub"
    deliver(tmp_path, "push", push_payload(), root=root)
    hb = root / "state" / wh.HEARTBEAT_FILE
    assert hb.exists()
    assert int(hb.read_text(encoding="ascii")) > 0


def test_unsupported_event_name_is_not_an_error(tmp_path):
    """e.g. GitHub's own 'ping' delivery: accepted, signed, but no typed event."""
    root = tmp_path / "hub"
    outcome = deliver(tmp_path, "ping", {"zen": "hello"}, root=root)
    assert outcome.status_code == 200
    assert outcome.event is None
    assert len(all_events(root)) == 0


# ---------------------------------------------------------------------------
# typed event field contracts
# ---------------------------------------------------------------------------

def test_workflow_run_completed_fields(tmp_path):
    root = tmp_path / "hub"
    deliver(tmp_path, "workflow_run", workflow_run_payload(), root=root)
    [record] = all_events(root)
    assert record["type"] == "github.workflow_run.completed"
    payload = record["payload"]
    assert payload["repo"] == "matt82198/aesop"
    assert payload["workflow"] == "main-full"
    assert payload["conclusion"] == "failure"
    assert payload["head_sha"] == "abc123abc123"
    assert payload["head_branch"] == "main"
    assert payload["run_id"] == 123456
    assert payload["html_url"].endswith("/runs/123456")


def test_workflow_run_non_completed_action_emits_nothing(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(tmp_path, "workflow_run", workflow_run_payload(action="in_progress"), root=root)
    assert outcome.event is None
    assert len(all_events(root)) == 0


def test_check_suite_completed_fields(tmp_path):
    root = tmp_path / "hub"
    deliver(tmp_path, "check_suite", check_suite_payload(), root=root)
    [record] = all_events(root)
    assert record["type"] == "github.check_suite.completed"
    payload = record["payload"]
    assert payload["repo"] == "matt82198/aesop"
    assert payload["conclusion"] == "success"
    assert payload["head_sha"] == "def456def456"
    assert payload["head_branch"] == "feat/x"
    assert payload["check_suite_id"] == 999
    assert payload["pr_numbers"] == [42]
    assert payload["pr_count"] == 1


def test_pull_request_action_fields(tmp_path):
    root = tmp_path / "hub"
    deliver(
        tmp_path,
        "pull_request",
        pull_request_payload(action="closed", number=42, state="closed", merged=True, mergeable_state="clean"),
        root=root,
    )
    [record] = all_events(root)
    assert record["type"] == "github.pull_request.closed"
    payload = record["payload"]
    assert payload["number"] == 42
    assert payload["state"] == "closed"
    assert payload["merged"] is True
    assert payload["mergeable_state"] == "clean"


def test_pull_request_mergeable_state_omitted_when_absent(tmp_path):
    root = tmp_path / "hub"
    deliver(
        tmp_path,
        "pull_request",
        pull_request_payload(action="opened", merged=False, mergeable_state=None),
        root=root,
    )
    [record] = all_events(root)
    assert record["type"] == "github.pull_request.opened"
    assert record["payload"].get("mergeable_state") is None


def test_push_fields(tmp_path):
    root = tmp_path / "hub"
    deliver(tmp_path, "push", push_payload(ref="refs/heads/main", after="deadbeefcafe"), root=root)
    [record] = all_events(root)
    assert record["type"] == "github.push"
    assert record["payload"]["ref"] == "refs/heads/main"
    assert record["payload"]["after"] == "deadbeefcafe"


# ---------------------------------------------------------------------------
# rules: each fires on its own fixture, and only on its own fixture
# ---------------------------------------------------------------------------

def test_rule_gh001_fires_on_failed_main_full(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(
        tmp_path, "workflow_run", workflow_run_payload(conclusion="failure", head_branch="main"),
        rules_dir=REAL_RULES_DIR, root=root,
    )
    assert outcome.enqueued, "R-gh-001 should have fired"
    [task] = list_pending_tasks(root / "queue")
    assert task["rule_id"].startswith("R-gh-001")


def test_rule_gh001_does_not_fire_on_success(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(
        tmp_path, "workflow_run", workflow_run_payload(conclusion="success", head_branch="main"),
        rules_dir=REAL_RULES_DIR, root=root,
    )
    assert outcome.enqueued == []


def test_rule_gh001_does_not_fire_off_main(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(
        tmp_path, "workflow_run", workflow_run_payload(conclusion="failure", head_branch="feat/x"),
        rules_dir=REAL_RULES_DIR, root=root,
    )
    assert outcome.enqueued == []


def test_rule_gh002_fires_on_green_check_suite_with_pr(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(
        tmp_path, "check_suite", check_suite_payload(conclusion="success", pr_numbers=(7,)),
        rules_dir=REAL_RULES_DIR, root=root,
    )
    assert outcome.enqueued, "R-gh-002 should have fired"
    [task] = list_pending_tasks(root / "queue")
    assert task["rule_id"].startswith("R-gh-002")


def test_rule_gh002_does_not_fire_without_pr(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(
        tmp_path, "check_suite", check_suite_payload(conclusion="success", pr_numbers=()),
        rules_dir=REAL_RULES_DIR, root=root,
    )
    assert outcome.enqueued == []


def test_rule_gh002_does_not_fire_on_failure(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(
        tmp_path, "check_suite", check_suite_payload(conclusion="failure", pr_numbers=(7,)),
        rules_dir=REAL_RULES_DIR, root=root,
    )
    assert outcome.enqueued == []


def test_rule_gh003_fires_on_pull_request_closed_merged(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(
        tmp_path, "pull_request", pull_request_payload(action="closed", merged=True, number=101),
        rules_dir=REAL_RULES_DIR, root=root,
    )
    assert outcome.enqueued, "R-gh-003 should have fired"
    [task] = list_pending_tasks(root / "queue")
    assert task["rule_id"].startswith("R-gh-003")
    assert task["args"]["number"] == 101


def test_rule_gh003_does_not_fire_on_closed_unmerged(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(
        tmp_path, "pull_request", pull_request_payload(action="closed", merged=False),
        rules_dir=REAL_RULES_DIR, root=root,
    )
    assert outcome.enqueued == []


def test_rule_gh003_does_not_fire_on_opened(tmp_path):
    root = tmp_path / "hub"
    outcome = deliver(
        tmp_path, "pull_request", pull_request_payload(action="opened", merged=False),
        rules_dir=REAL_RULES_DIR, root=root,
    )
    assert outcome.enqueued == []


def test_real_rules_dir_loads_with_no_invalid_rules(tmp_path):
    """The three new github.* rules must coexist with the three sports rules."""
    from signal_hub.rules_engine import load_rules

    loaded = load_rules(str(REAL_RULES_DIR))
    assert loaded.errors == []
    assert len(loaded.rules) == 6


# ---------------------------------------------------------------------------
# conductor3 orchestrator wake-up mirror
# ---------------------------------------------------------------------------

def test_fired_task_is_mirrored_to_conductor_queue_file(tmp_path):
    root = tmp_path / "hub"
    conductor_queue = tmp_path / "conductor-queue.jsonl"
    deliver(
        tmp_path, "workflow_run", workflow_run_payload(conclusion="failure", head_branch="main"),
        rules_dir=REAL_RULES_DIR, conductor_queue_path=conductor_queue, root=root,
    )
    lines = conductor_queue.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    mirrored = json.loads(lines[0])
    assert mirrored["rule_id"].startswith("R-gh-001")
    assert "task_id" in mirrored


def test_conductor_queue_file_is_append_only_across_deliveries(tmp_path):
    root = tmp_path / "hub"
    conductor_queue = tmp_path / "conductor-queue.jsonl"
    deliver(
        tmp_path, "workflow_run", workflow_run_payload(conclusion="failure", head_branch="main", run_id=1),
        rules_dir=REAL_RULES_DIR, conductor_queue_path=conductor_queue, root=root,
    )
    deliver(
        tmp_path, "workflow_run", workflow_run_payload(conclusion="failure", head_branch="main", run_id=2),
        rules_dir=REAL_RULES_DIR, conductor_queue_path=conductor_queue, root=root,
        delivery_id="d-0002",
    )
    lines = conductor_queue.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_no_fire_means_no_conductor_mirror_line(tmp_path):
    root = tmp_path / "hub"
    conductor_queue = tmp_path / "conductor-queue.jsonl"
    deliver(
        tmp_path, "push", push_payload(),
        rules_dir=REAL_RULES_DIR, conductor_queue_path=conductor_queue, root=root,
    )
    assert not conductor_queue.exists()
