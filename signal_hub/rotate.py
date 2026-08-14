"""
L6 Rotate — Log rotation per cardinal rules.

Append-only logs: when >200 lines or >20KB, roll oldest to dated archive.
ASCII, UTF-8 encoding. Date-partitioned logs (YYYY-MM-DD.jsonl, etc.) self-rotate
and prune partitions older than 90 days.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


# Thresholds per cardinal rules
MAX_LINES = 200
MAX_BYTES = 20 * 1024  # 20 KB


def should_rotate(log_path: Path) -> bool:
    """Check if log file should be rotated (>200 lines or >20KB)."""
    if not log_path.exists():
        return False

    # Check file size
    size_bytes = log_path.stat().st_size
    if size_bytes > MAX_BYTES:
        return True

    # Check line count
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            line_count = sum(1 for _ in f)
        if line_count > MAX_LINES:
            return True
    except (OSError, UnicodeDecodeError):
        return False

    return False


def rotate_log(
    log_path: Path,
    archive_dir: Path = None,
    now: datetime = None,
) -> None:
    """
    Rotate log: rename to dated archive, create new empty log.

    Args:
        log_path: Path to log file (e.g., state/TICK.log)
        archive_dir: Directory for archives (defaults to log_path.parent / 'archive')
        now: Injected clock (defaults to now)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not log_path.exists():
        return

    if archive_dir is None:
        archive_dir = log_path.parent / "archive"

    archive_dir.mkdir(parents=True, exist_ok=True)

    # Create dated archive filename: {original_name}.{YYYYMMDD}.log
    date_suffix = now.strftime("%Y%m%d")
    archive_name = f"{log_path.stem}.{date_suffix}.log"
    archive_path = archive_dir / archive_name

    # Handle collisions by appending .N
    counter = 1
    while archive_path.exists():
        archive_name = f"{log_path.stem}.{date_suffix}.{counter}.log"
        archive_path = archive_dir / archive_name
        counter += 1

    # Move log to archive
    log_path.rename(archive_path)

    # Create new empty log (so append operations work immediately)
    log_path.touch()


def prune_archives(
    archive_dir: Path,
    retention_days: int = 90,
    now: datetime = None,
) -> None:
    """
    Prune archive files older than retention_days.

    Args:
        archive_dir: Directory containing archived logs
        retention_days: Keep archives younger than this (default 90 days)
        now: Injected clock (defaults to now)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if not archive_dir.exists():
        return

    cutoff = now - timedelta(days=retention_days)

    for archive_file in archive_dir.glob("*.log"):
        try:
            mtime = datetime.fromtimestamp(archive_file.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                archive_file.unlink()
        except (OSError, ValueError):
            pass


def rotate_if_needed(
    log_path: Path,
    archive_dir: Path = None,
    now: datetime = None,
) -> bool:
    """
    Check and rotate log if threshold exceeded.

    Args:
        log_path: Path to log file
        archive_dir: Directory for archives
        now: Injected clock

    Returns:
        True if rotation occurred
    """
    if should_rotate(log_path):
        rotate_log(log_path, archive_dir, now)
        prune_archives(archive_dir or log_path.parent / "archive", now=now)
        return True

    return False


def append_line(
    log_path: Path,
    line: str,
    archive_dir: Path = None,
    now: datetime = None,
) -> None:
    """
    Append line to log and rotate if needed.

    Args:
        log_path: Path to log file
        line: Line to append (will add newline if not present)
        archive_dir: Directory for archives
        now: Injected clock
    """
    # Ensure line ends with newline
    if not line.endswith("\n"):
        line = line + "\n"

    # Create parent dirs if needed
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Append line
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)
        try:
            os.fsync(f.fileno())
        except (AttributeError, OSError):
            pass

    # Check and rotate if needed
    rotate_if_needed(log_path, archive_dir, now)
