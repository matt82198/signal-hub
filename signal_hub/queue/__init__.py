"""
L6 Queue — Task enqueue/claim with rename-as-mutex and expiry.

Atomic enqueue: write to .tmp/, fsync, os.replace into pending/.
Claim is os.rename(pending/X, claimed/X) — Windows rename fails if dst exists.
Expired tasks auto-move to failed/ on claim.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def parse_ttl(ttl_str: str) -> timedelta:
    """Parse TTL string like '2d', '3h', '1h' to timedelta."""
    value = int(ttl_str[:-1])
    unit = ttl_str[-1]
    if unit == "d":
        return timedelta(days=value)
    elif unit == "h":
        return timedelta(hours=value)
    elif unit == "m":
        return timedelta(minutes=value)
    else:
        raise ValueError(f"Unknown TTL unit: {unit}")


def iso_ts_now() -> str:
    """Get current ISO timestamp (UTC) for task IDs."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def enqueue_task(
    queue_dir: Path,
    rule_id: str,
    event_id: str,
    kind: str,
    priority: int,
    ttl: str,
    args: dict,
    now: datetime = None,
) -> str:
    """
    Atomically enqueue a task: write to .tmp, fsync, os.replace into pending/.

    Args:
        queue_dir: Path to queue/ directory
        rule_id: Rule that fired
        event_id: Event that triggered the rule
        kind: Task kind (instantiate_template, run_workflow, notify)
        priority: Priority (1-100)
        ttl: TTL string like '2d', '3h'
        args: Task args dict (template, account, fills_hints, etc.)
        now: Injected clock (defaults to now)

    Returns:
        task_id in format {ISO ts}-{rule_id}-{event_id[:8]}
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Parse TTL and compute expires_at
    ttl_delta = parse_ttl(ttl)
    expires_at = now + ttl_delta

    # Build task_id: {ISO datetime}-{rule_id}-{event_id[:8]}
    iso_ts = now.strftime("%Y%m%dT%H%M%SZ")
    event_id_short = event_id[:8]
    task_id = f"{iso_ts}-{rule_id}-{event_id_short}"

    # Build task object
    task = {
        "task_id": task_id,
        "created_at": now.isoformat() + "Z" if now.isoformat().endswith("+00:00") else now.isoformat(),
        "expires_at": expires_at.isoformat() + "Z" if expires_at.isoformat().endswith("+00:00") else expires_at.isoformat(),
        "rule_id": rule_id,
        "event_id": event_id,
        "kind": kind,
        "priority": priority,
        "status": "pending",
        "args": args,
    }

    # Normalize timestamps to UTC ISO format with Z suffix
    task["created_at"] = now.replace(microsecond=0).isoformat().replace("+00:00", "") + "Z"
    task["expires_at"] = expires_at.replace(microsecond=0).isoformat().replace("+00:00", "") + "Z"

    # Write atomically: .tmp -> pending via os.replace
    tmp_dir = queue_dir / ".tmp"
    pending_dir = queue_dir / "pending"

    tmp_dir.mkdir(parents=True, exist_ok=True)
    pending_dir.mkdir(parents=True, exist_ok=True)

    task_filename = f"{task_id}.task.json"
    tmp_path = tmp_dir / task_filename
    pending_path = pending_dir / task_filename

    # Write to .tmp with fsync
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(task, f, separators=(",", ":"))
        # Fsync on POSIX; on Windows, close flushes
        try:
            os.fsync(f.fileno())
        except (AttributeError, OSError):
            pass  # Windows or read-only file

    # Atomic replace
    os.replace(str(tmp_path), str(pending_path))

    return task_id


def claim_task(
    queue_dir: Path,
    task_filename: str,
    now: datetime = None,
) -> dict | None:
    """
    Claim a task: os.rename(pending/X, claimed/X).
    On Windows, rename fails if dst exists — the loser returns None.
    If task is expired, move to failed/ instead and return None.

    Args:
        queue_dir: Path to queue/ directory
        task_filename: Filename (e.g., '20260914T234100Z-R002-a3f91c2e.task.json')
        now: Injected clock (defaults to now)

    Returns:
        Task dict with status=claimed, or None if lost race or expired
    """
    if now is None:
        now = datetime.now(timezone.utc)

    pending_path = queue_dir / "pending" / task_filename
    claimed_path = queue_dir / "claimed" / task_filename
    failed_path = queue_dir / "failed" / task_filename

    (queue_dir / "claimed").mkdir(parents=True, exist_ok=True)
    (queue_dir / "failed").mkdir(parents=True, exist_ok=True)

    # Check if task exists in pending
    if not pending_path.exists():
        # Already claimed or moved
        return None

    # Read task to check expiry
    with open(pending_path, "r", encoding="utf-8") as f:
        task = json.load(f)

    # Parse expires_at and check expiry
    expires_at_str = task.get("expires_at", "").replace("Z", "+00:00")
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
    except ValueError:
        expires_at = now + timedelta(days=1)  # Default to future if parse fails

    if expires_at <= now:
        # Task is expired, move to failed/
        task["status"] = "failed"
        task["reason"] = "expired"

        # Move from pending to failed
        with open(failed_path, "w", encoding="utf-8") as f:
            json.dump(task, f, separators=(",", ":"))
            try:
                os.fsync(f.fileno())
            except (AttributeError, OSError):
                pass

        # Remove from pending
        pending_path.unlink()
        return None

    # Try to claim: rename pending -> claimed
    try:
        os.rename(str(pending_path), str(claimed_path))
    except FileExistsError:
        # Lost race: another process already claimed it
        return None
    except FileNotFoundError:
        # Lost race or already moved
        return None

    # Claim succeeded
    task["status"] = "claimed"
    return task


def list_pending_tasks(queue_dir: Path) -> list[dict]:
    """List all pending tasks."""
    pending_dir = queue_dir / "pending"
    if not pending_dir.exists():
        return []

    tasks = []
    for task_file in pending_dir.glob("*.task.json"):
        with open(task_file, "r", encoding="utf-8") as f:
            tasks.append(json.load(f))

    return tasks


def complete_task(
    queue_dir: Path,
    task_filename: str,
    result: dict = None,
) -> bool:
    """
    Move task from claimed/ to consumed/ and append result.

    Args:
        queue_dir: Path to queue/ directory
        task_filename: Filename (e.g., '20260914T234100Z-R002-a3f91c2e.task.json')
        result: Result dict to append to task

    Returns:
        True if success, False if task not found
    """
    claimed_path = queue_dir / "claimed" / task_filename
    consumed_path = queue_dir / "consumed" / task_filename

    (queue_dir / "consumed").mkdir(parents=True, exist_ok=True)

    if not claimed_path.exists():
        return False

    with open(claimed_path, "r", encoding="utf-8") as f:
        task = json.load(f)

    task["status"] = "consumed"
    if result:
        task["result"] = result

    with open(consumed_path, "w", encoding="utf-8") as f:
        json.dump(task, f, separators=(",", ":"))
        try:
            os.fsync(f.fileno())
        except (AttributeError, OSError):
            pass

    claimed_path.unlink()
    return True


def fail_task(
    queue_dir: Path,
    task_filename: str,
    reason: str = "unknown",
) -> bool:
    """
    Move task from claimed/ to failed/ with reason.

    Args:
        queue_dir: Path to queue/ directory
        task_filename: Filename
        reason: Failure reason

    Returns:
        True if success, False if task not found
    """
    claimed_path = queue_dir / "claimed" / task_filename
    failed_path = queue_dir / "failed" / task_filename

    (queue_dir / "failed").mkdir(parents=True, exist_ok=True)

    if not claimed_path.exists():
        return False

    with open(claimed_path, "r", encoding="utf-8") as f:
        task = json.load(f)

    task["status"] = "failed"
    task["reason"] = reason

    with open(failed_path, "w", encoding="utf-8") as f:
        json.dump(task, f, separators=(",", ":"))
        try:
            os.fsync(f.fileno())
        except (AttributeError, OSError):
            pass

    claimed_path.unlink()
    return True
