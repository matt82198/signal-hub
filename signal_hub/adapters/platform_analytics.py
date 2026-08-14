"""
Platform analytics adapter (STUB).

Wave-2 implementation. Captures per-account views/followers.
Requires user gate B4 (account configuration).
"""
from datetime import datetime


class PlatformAnalyticsAdapter:
    """Stub adapter for platform analytics."""

    def capture(self, now: datetime, runner, file_reader) -> dict:
        """
        Capture platform analytics (stub).

        Args:
            now: Frozen datetime (UTC)
            runner: Injected subprocess runner
            file_reader: Injected file reader

        Raises:
            NotImplementedError: Wave-2 implementation pending account access.
        """
        raise NotImplementedError(
            "Platform analytics adapter is a Wave-2 feature. "
            "Requires user gate B4 for account configuration. "
            "See design doc §2a for contract and integration notes."
        )
