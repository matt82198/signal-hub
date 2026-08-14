"""
Orchestrates trend-indicator subprocess and snapshots rankings.json.

Does not reimplement scoring; invokes the owning system's CLI and reads its artifacts.
"""
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path


class TrendIndicatorAdapter:
    """Captures trend-indicator rankings via subprocess orchestration."""

    def __init__(self, trend_indicator_repo: Path | None = None):
        """
        Initialize adapter.

        Args:
            trend_indicator_repo: Path to trend-indicator repo (defaults to ~/trend-indicator)
        """
        if trend_indicator_repo is None:
            trend_indicator_repo = Path.home() / "trend-indicator"
        self.repo_path = Path(trend_indicator_repo)

    def capture(self, now: datetime, runner, file_reader) -> dict:
        """
        Capture trend-indicator rankings via subprocess.

        Args:
            now: Frozen datetime (UTC)
            runner: Injected subprocess runner (cmd_args) -> exit_code
            file_reader: Injected file reader (path) -> dict

        Returns:
            Snapshot dict with status OK|STALE and optional payload/error.
        """
        snapshot = {
            "source": "trend_indicator",
            "schema_version": 1,
            "captured_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "etag": self._compute_etag(now),
        }

        try:
            # Run refresh and rank subprocesses
            refresh_cmd = [sys.executable, "-m", "trend_indicator", "refresh"]
            refresh_exit = runner(refresh_cmd, cwd=self.repo_path)

            if refresh_exit != 0:
                snapshot["status"] = "STALE"
                snapshot["error"] = f"refresh exited with code {refresh_exit}"
                return snapshot

            rank_cmd = [sys.executable, "-m", "trend_indicator", "rank"]
            rank_exit = runner(rank_cmd, cwd=self.repo_path)

            if rank_exit != 0:
                snapshot["status"] = "STALE"
                snapshot["error"] = f"rank exited with code {rank_exit}"
                return snapshot

            # Read rankings.json
            rankings_path = self.repo_path / "state" / "rankings.json"
            payload = file_reader(rankings_path)

            snapshot["status"] = "OK"
            snapshot["payload"] = payload
            return snapshot

        except (FileNotFoundError, json.JSONDecodeError) as e:
            snapshot["status"] = "STALE"
            snapshot["error"] = f"{type(e).__name__}: {e}"
            return snapshot
        except Exception as e:
            snapshot["status"] = "STALE"
            snapshot["error"] = f"Unexpected error: {type(e).__name__}: {e}"
            return snapshot

    def _compute_etag(self, now: datetime) -> str:
        """
        Compute a deterministic etag for this snapshot.

        In a real implementation, this would be the ETag from the HTTP response
        or a hash of the content. For subprocess-based sources, use the timestamp.
        """
        ts_str = now.isoformat()
        digest = hashlib.sha256(ts_str.encode()).hexdigest()[:16]
        return f'W/"{digest}"'
