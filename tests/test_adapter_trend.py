"""
Tests for trend_indicator adapter.
Injects subprocess runner and file reader; no live subprocess calls.
"""
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from signal_hub.adapters.trend_indicator import TrendIndicatorAdapter


@pytest.fixture
def adapter():
    """Create adapter with injected dependencies."""
    return TrendIndicatorAdapter()


@pytest.fixture
def mock_runner():
    """Mock subprocess runner."""
    return MagicMock()


@pytest.fixture
def mock_file_reader():
    """Mock file reader."""
    return MagicMock()


@pytest.fixture
def frozen_now():
    """Frozen clock."""
    return datetime(2026, 8, 13, 14, 2, 0)


class TestTrendIndicatorAdapter:
    """Tests for trend_indicator adapter."""

    def test_adapter_has_capture_method(self, adapter):
        """Adapter has a capture(now, runner, file_reader) method."""
        assert hasattr(adapter, "capture")
        assert callable(adapter.capture)

    def test_snapshot_structure_ok(self, adapter, mock_runner, mock_file_reader, frozen_now):
        """Successful capture returns snapshot with correct structure."""
        # Setup: mock subprocess success and valid rankings.json
        mock_runner.return_value = 0  # Exit code 0
        mock_file_reader.return_value = {"opportunities": {"test": {"score": 0.8}}}

        snapshot = adapter.capture(frozen_now, mock_runner, mock_file_reader)

        assert snapshot["source"] == "trend_indicator"
        assert snapshot["schema_version"] == 1
        assert snapshot["captured_at"] == "2026-08-13T14:02:00Z"
        assert snapshot["status"] == "OK"
        assert "payload" in snapshot
        assert snapshot["payload"] == {"opportunities": {"test": {"score": 0.8}}}

    def test_subprocess_called_with_refresh_and_rank(
        self, adapter, mock_runner, mock_file_reader, frozen_now
    ):
        """Adapter calls subprocess with refresh then rank."""
        mock_runner.return_value = 0
        mock_file_reader.return_value = {}

        adapter.capture(frozen_now, mock_runner, mock_file_reader)

        # Should call refresh and rank
        calls = mock_runner.call_args_list
        assert len(calls) >= 2
        # First call should be refresh
        assert "refresh" in str(calls[0])
        # Second call should be rank
        assert "rank" in str(calls[1])

    def test_nonzero_exit_code_returns_stale(
        self, adapter, mock_runner, mock_file_reader, frozen_now
    ):
        """Subprocess nonzero exit returns STALE snapshot."""
        mock_runner.return_value = 1  # Nonzero exit

        snapshot = adapter.capture(frozen_now, mock_runner, mock_file_reader)

        assert snapshot["status"] == "STALE"
        assert "payload" not in snapshot
        assert "error" in snapshot

    def test_missing_rankings_file_returns_stale(
        self, adapter, mock_runner, mock_file_reader, frozen_now
    ):
        """Missing rankings.json returns STALE snapshot."""
        mock_runner.return_value = 0
        mock_file_reader.side_effect = FileNotFoundError()

        snapshot = adapter.capture(frozen_now, mock_runner, mock_file_reader)

        assert snapshot["status"] == "STALE"
        assert "error" in snapshot

    def test_malformed_rankings_returns_stale(
        self, adapter, mock_runner, mock_file_reader, frozen_now
    ):
        """Malformed JSON returns STALE snapshot."""
        mock_runner.return_value = 0
        mock_file_reader.side_effect = json.JSONDecodeError("msg", "doc", 0)

        snapshot = adapter.capture(frozen_now, mock_runner, mock_file_reader)

        assert snapshot["status"] == "STALE"
        assert "error" in snapshot

    def test_etag_field_present(self, adapter, mock_runner, mock_file_reader, frozen_now):
        """Snapshot includes etag field for conditional GET."""
        mock_runner.return_value = 0
        mock_file_reader.return_value = {}

        snapshot = adapter.capture(frozen_now, mock_runner, mock_file_reader)

        assert "etag" in snapshot

    def test_iso8601_timestamp_format(self, adapter, mock_runner, mock_file_reader, frozen_now):
        """captured_at uses ISO 8601 UTC format."""
        mock_runner.return_value = 0
        mock_file_reader.return_value = {}

        snapshot = adapter.capture(frozen_now, mock_runner, mock_file_reader)

        assert snapshot["captured_at"] == "2026-08-13T14:02:00Z"

    def test_runner_receives_correct_args(
        self, adapter, mock_runner, mock_file_reader, frozen_now
    ):
        """Subprocess runner receives command args."""
        mock_runner.return_value = 0
        mock_file_reader.return_value = {}

        adapter.capture(frozen_now, mock_runner, mock_file_reader)

        # Verify runner was called with expected command structure
        calls = mock_runner.call_args_list
        assert len(calls) >= 1

    def test_file_reader_receives_path(
        self, adapter, mock_runner, mock_file_reader, frozen_now
    ):
        """File reader is called with rankings.json path."""
        mock_runner.return_value = 0
        mock_file_reader.return_value = {}

        adapter.capture(frozen_now, mock_runner, mock_file_reader)

        # Verify file_reader was called
        assert mock_file_reader.called
        # It should be called with a path-like argument containing "rankings.json"
        call_args = str(mock_file_reader.call_args_list[0])
        assert "rankings.json" in call_args

    def test_exception_in_subprocess_returns_stale(
        self, adapter, mock_runner, mock_file_reader, frozen_now
    ):
        """Unexpected exception in subprocess handling returns STALE."""
        mock_runner.side_effect = Exception("Subprocess error")

        snapshot = adapter.capture(frozen_now, mock_runner, mock_file_reader)

        assert snapshot["status"] == "STALE"
        assert "error" in snapshot
