"""L7 CLI surface: ``python -m signal_hub tick|status|queue`` (design section 5).

The queue subcommands ARE the MVP consumer contract -- an aesop session drains
the queue through them -- so they are tested as a full claim/complete/fail
round trip, not just for exit codes.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from signal_hub import cli
from signal_hub.queue import enqueue_task

REPO_ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 9, 15, 3, 30, 0, tzinfo=timezone.utc)


def seed_task(root, rule_id="R001-bears-game-final-win", event_id="a3f91c2e8b7d4506"):
    return enqueue_task(
        Path(root) / "queue",
        rule_id=rule_id,
        event_id=event_id,
        kind="instantiate_template",
        priority=90,
        ttl="2d",
        args={"template": "gamehighlight", "account": "ballmoments_main"},
        now=T0,
    )


def files(root, box):
    d = Path(root) / "queue" / box
    return sorted(p.name for p in d.glob("*.task.json")) if d.is_dir() else []


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------

def test_no_command_is_a_usage_error(capsys):
    assert cli.main([]) == 2


def test_unknown_command_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["frobnicate"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

def test_queue_list_is_empty_on_a_fresh_hub(tmp_path, capsys):
    assert cli.main(["queue", "list", "--root", str(tmp_path)]) == 0
    # Inputs always produce outputs: an empty queue still says so.
    assert "0 pending" in capsys.readouterr().out


def test_queue_list_shows_the_task(tmp_path, capsys):
    task_id = seed_task(tmp_path)
    assert cli.main(["queue", "list", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert task_id in out
    assert "gamehighlight" in out
    assert "1 pending" in out


def test_queue_list_json(tmp_path, capsys):
    task_id = seed_task(tmp_path)
    assert cli.main(["queue", "list", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [t["task_id"] for t in payload] == [task_id]


def test_queue_claim_complete_round_trip(tmp_path, capsys):
    task_id = seed_task(tmp_path)

    assert cli.main(["queue", "claim", task_id, "--root", str(tmp_path)]) == 0
    assert files(tmp_path, "pending") == []
    assert files(tmp_path, "claimed") == ["%s.task.json" % task_id]
    assert "claimed" in capsys.readouterr().out

    assert cli.main(["queue", "complete", task_id, "--root", str(tmp_path)]) == 0
    assert files(tmp_path, "claimed") == []
    assert files(tmp_path, "consumed") == ["%s.task.json" % task_id]


def test_queue_fail_records_the_reason(tmp_path):
    task_id = seed_task(tmp_path)
    cli.main(["queue", "claim", task_id, "--root", str(tmp_path)])
    assert (
        cli.main(
            [
                "queue", "fail", task_id,
                "--reason", "template-not-ready",
                "--root", str(tmp_path),
            ]
        )
        == 0
    )
    task = json.loads(
        (tmp_path / "queue" / "failed" / ("%s.task.json" % task_id)).read_text("utf-8")
    )
    assert task["status"] == "failed"
    assert task["reason"] == "template-not-ready"


def test_queue_claim_of_a_missing_task_fails_loudly(tmp_path, capsys):
    assert cli.main(["queue", "claim", "nope", "--root", str(tmp_path)]) == 1
    assert "nope" in capsys.readouterr().err


def test_queue_accepts_the_full_filename_too(tmp_path):
    task_id = seed_task(tmp_path)
    assert (
        cli.main(
            ["queue", "claim", "%s.task.json" % task_id, "--root", str(tmp_path)]
        )
        == 0
    )


def test_queue_complete_requires_a_claim_first(tmp_path):
    task_id = seed_task(tmp_path)
    assert cli.main(["queue", "complete", task_id, "--root", str(tmp_path)]) == 1


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_before_any_tick_is_nonzero_and_explains_itself(tmp_path, capsys):
    assert cli.main(["status", "--root", str(tmp_path)]) == 1
    assert "no hub-status.json" in capsys.readouterr().err


def test_status_prints_the_status_file(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "hub-status.json").write_text(
        json.dumps({"mode": "idle", "alarms": [], "queue": {"pending": 0}}),
        encoding="utf-8",
    )
    assert cli.main(["status", "--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "idle" in out


def test_status_json_is_machine_readable(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "hub-status.json").write_text(
        json.dumps({"mode": "game_window", "alarms": ["queue_depth_high"]}),
        encoding="utf-8",
    )
    assert cli.main(["status", "--root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "game_window"


# ---------------------------------------------------------------------------
# tick
# ---------------------------------------------------------------------------

def test_tick_dry_run_writes_nothing(tmp_path, capsys):
    rc = cli.main(
        [
            "tick", "--dry-run",
            "--root", str(tmp_path),
            "--rules-dir", str(REPO_ROOT / "rules"),
            "--offline",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert not (tmp_path / "queue" / "pending").exists()
    assert not (tmp_path / "state" / "hub-status.json").exists()


def test_tick_offline_still_completes_and_writes_status(tmp_path, capsys):
    rc = cli.main(
        [
            "tick",
            "--root", str(tmp_path),
            "--rules-dir", str(REPO_ROOT / "rules"),
            "--offline",
        ]
    )
    assert rc == 0
    status = json.loads((tmp_path / "state" / "hub-status.json").read_text("utf-8"))
    # --offline means every network source records an ERROR snapshot rather than
    # being skipped: a source that could not be reached is not a source that was
    # not due, and the status file must be able to tell them apart.
    assert status["sources"]["nflverse_schedules"]["status"] == "ERROR"
    assert (tmp_path / "state" / ".signal-hub-heartbeat").is_file()


def test_tick_halted_returns_a_distinct_exit_code(tmp_path, capsys):
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / ".HALT").write_text('{"reason": "maintenance"}', encoding="utf-8")
    rc = cli.main(["tick", "--root", str(tmp_path), "--offline"])
    assert rc == 3
    assert "HALT" in capsys.readouterr().out


def test_include_preseason_flag_reaches_the_tick(tmp_path, monkeypatch):
    seen = {}

    def fake_run_tick(root, **kwargs):
        seen.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(cli.tick, "run_tick", fake_run_tick)
    with pytest.raises(SystemExit):
        cli.main(["tick", "--include-preseason", "--root", str(tmp_path)])
    assert seen["include_preseason"] is True


# ---------------------------------------------------------------------------
# python -m signal_hub
# ---------------------------------------------------------------------------

def test_module_entry_point_runs(tmp_path):
    proc = subprocess.run(
        [
            sys.executable, "-m", "signal_hub", "tick", "--dry-run",
            "--root", str(tmp_path),
            "--rules-dir", str(REPO_ROOT / "rules"),
            "--offline",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "DRY-RUN" in proc.stdout
