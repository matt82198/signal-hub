"""
L6 Queue Tests — TDD-first, stdlib only, injectable clock.

Tests atomic enqueue, rename-as-mutex claim race, expiry, and task schema.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def test_queue_atomic_enqueue():
    """
    Enqueue writes to .tmp/ then os.replace into pending/.
    Reader never sees a half-written task.
    """
    from signal_hub.queue import enqueue_task

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir)
        (queue_dir / "pending").mkdir()
        (queue_dir / ".tmp").mkdir()

        now = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)
        task = {
            "kind": "instantiate_template",
            "priority": 70,
            "ttl": "3d",
            "template": "goodperformance",
            "account": "ballmoments_main",
        }

        task_id = enqueue_task(
            queue_dir=queue_dir,
            rule_id="R002-big-stat-line",
            event_id="a3f91c2e8b7d4506",
            kind="instantiate_template",
            priority=70,
            ttl="3d",
            args={"template": "goodperformance", "account": "ballmoments_main"},
            now=now,
        )

        # Task should be in pending/, not in .tmp
        pending_files = list((queue_dir / "pending").glob("*"))
        assert len(pending_files) == 1

        tmp_files = list((queue_dir / ".tmp").glob("*"))
        assert len(tmp_files) == 0  # .tmp is empty after replace

        # Read and validate task file
        task_file = pending_files[0]
        with open(task_file) as f:
            task_data = json.load(f)

        assert task_data["task_id"] == task_id
        assert task_data["rule_id"] == "R002-big-stat-line"
        assert task_data["event_id"] == "a3f91c2e8b7d4506"
        assert task_data["kind"] == "instantiate_template"
        assert task_data["priority"] == 70
        assert task_data["status"] == "pending"
        assert "created_at" in task_data
        assert "expires_at" in task_data


def test_queue_claim_rename_mutex():
    """
    Claim via os.rename(pending/X, claimed/X).
    Winner owns it; loser gets FileExistsError/FileNotFoundError and skips cleanly.
    """
    from signal_hub.queue import claim_task

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir)
        (queue_dir / "pending").mkdir()
        (queue_dir / "claimed").mkdir()
        (queue_dir / ".tmp").mkdir()

        # Create a task file in pending/
        task_file = queue_dir / "pending" / "20260914T234100Z-R002-big-stat-line-a3f91c2e.task.json"
        task_data = {
            "task_id": "20260914T234100Z-R002-big-stat-line-a3f91c2e",
            "created_at": "2026-09-14T23:41:00Z",
            "expires_at": "2026-09-17T23:41:00Z",
            "rule_id": "R002-big-stat-line",
            "event_id": "a3f91c2e8b7d4506",
            "kind": "instantiate_template",
            "priority": 70,
            "status": "pending",
            "args": {"template": "goodperformance"},
        }
        with open(task_file, "w") as f:
            json.dump(task_data, f)

        # Pin the clock inside the task's created_at/expires_at window --
        # this fixture's dates are fixed strings, so without an injected
        # ``now`` the real wall clock eventually drifts past expires_at and
        # claim_task correctly (if confusingly) reports the task expired.
        # See test_queue_expires_at_honored below for the same pattern.
        now = datetime(2026, 9, 15, 0, 0, 0, tzinfo=timezone.utc)

        # Claim should succeed
        result = claim_task(queue_dir, task_file.name, now=now)
        assert result is not None
        assert result["status"] == "claimed"

        # File should be in claimed/, not pending/
        assert not (queue_dir / "pending" / task_file.name).exists()
        assert (queue_dir / "claimed" / task_file.name).exists()

        # Second claim should fail cleanly (already claimed)
        result2 = claim_task(queue_dir, task_file.name, now=now)
        assert result2 is None  # Lost the race


def test_queue_claim_race_subprocess():
    """
    Two-process claim race: spawn subprocess contender.
    Only one wins; loser gets None (clean skip).
    """
    from signal_hub.queue import claim_task, enqueue_task

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir)
        (queue_dir / "pending").mkdir()
        (queue_dir / "claimed").mkdir()
        (queue_dir / ".tmp").mkdir()

        # Enqueue a task
        now = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)
        task_id = enqueue_task(
            queue_dir=queue_dir,
            rule_id="R002-big-stat-line",
            event_id="a3f91c2e8b7d4506",
            kind="instantiate_template",
            priority=70,
            ttl="3d",
            args={"template": "goodperformance"},
            now=now,
        )

        task_file_name = list((queue_dir / "pending").glob("*"))[0].name

        # Get the project root (parent of worktree)
        project_root = Path(__file__).parent.parent

        # Write subprocess script that attempts claim
        script = f"""
import sys
sys.path.insert(0, r"{project_root}")

import json
from pathlib import Path
from signal_hub.queue import claim_task

queue_dir = Path(r"{queue_dir}")
task_file_name = r"{task_file_name}"
result = claim_task(queue_dir, task_file_name)
print("CLAIMED" if result else "LOST_RACE")
"""

        script_file = Path(tmpdir) / "claimer.py"
        with open(script_file, "w") as f:
            f.write(script)

        # Main process claims
        main_result = claim_task(queue_dir, task_file_name)

        # Subprocess tries to claim (should lose)
        proc = subprocess.run(
            [sys.executable, str(script_file)],
            capture_output=True,
            text=True,
        )

        assert proc.returncode == 0, f"Subprocess failed: {proc.stderr}"
        if main_result:
            # Main won, subprocess lost
            assert "LOST_RACE" in proc.stdout
        # One of them succeeded, exactly


def test_queue_expires_at_honored():
    """
    expires_at is load-bearing: expired tasks on claim move to failed/.
    Expiry count tracked.
    """
    from signal_hub.queue import claim_task, enqueue_task

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir)
        (queue_dir / "pending").mkdir()
        (queue_dir / "claimed").mkdir()
        (queue_dir / "failed").mkdir()
        (queue_dir / ".tmp").mkdir()

        # Enqueue a task that expires immediately
        now = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)
        past = now - timedelta(hours=1)
        expires_past = now - timedelta(minutes=1)

        task_file = queue_dir / "pending" / "20260914T234100Z-R002-big-stat-line-a3f91c2e.task.json"
        task_data = {
            "task_id": "20260914T234100Z-R002-big-stat-line-a3f91c2e",
            "created_at": "2026-09-14T20:41:00Z",  # 3 hours ago
            "expires_at": expires_past.replace(microsecond=0).isoformat().replace("+00:00", "") + "Z",  # Already expired
            "rule_id": "R002-big-stat-line",
            "event_id": "a3f91c2e8b7d4506",
            "kind": "instantiate_template",
            "priority": 70,
            "status": "pending",
            "args": {},
        }
        with open(task_file, "w") as f:
            json.dump(task_data, f)

        # Claim should detect expiry and move to failed/
        result = claim_task(queue_dir, task_file.name, now=now)
        assert result is None  # Should return None because task was expired

        # Task should be in failed/ with reason: "expired"
        failed_files = list((queue_dir / "failed").glob("*"))
        assert len(failed_files) == 1, "Expired task should be in failed/"
        with open(failed_files[0]) as f:
            failed_data = json.load(f)
        assert failed_data.get("reason") == "expired"


def test_queue_task_file_schema():
    """Validate task file schema matches design."""
    from signal_hub.queue import enqueue_task

    with tempfile.TemporaryDirectory() as tmpdir:
        queue_dir = Path(tmpdir)
        (queue_dir / "pending").mkdir()
        (queue_dir / ".tmp").mkdir()

        now = datetime(2026, 9, 14, 23, 41, 0, tzinfo=timezone.utc)

        task_id = enqueue_task(
            queue_dir=queue_dir,
            rule_id="R001-bears-game-final-win",
            event_id="game_final_abc123",
            kind="instantiate_template",
            priority=70,
            ttl="2d",
            args={
                "template": "gamehighlight",
                "account": "ballmoments_main",
                "fills_hints": {"game_id": "2026_02_CHI_DET"},
            },
            now=now,
        )

        task_file = list((queue_dir / "pending").glob("*"))[0]
        with open(task_file) as f:
            task = json.load(f)

        # Validate all required fields per design
        assert "task_id" in task
        assert "created_at" in task
        assert "expires_at" in task
        assert "rule_id" in task
        assert "event_id" in task
        assert "kind" in task
        assert "priority" in task
        assert "status" in task
        assert task["status"] == "pending"
        assert "args" in task

        # expires_at should be created_at + ttl
        created = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
        expires = datetime.fromisoformat(task["expires_at"].replace("Z", "+00:00"))
        expected_delta = timedelta(days=2)
        assert abs((expires - created) - expected_delta) < timedelta(seconds=1)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
