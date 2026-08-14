"""
Reddit adapter (STUB).

Wave-2 implementation. Captures subreddit hot/rising velocity.
Requires OAuth app configuration.
"""
from datetime import datetime


class RedditAdapter:
    """Stub adapter for Reddit subreddit trends."""

    def capture(self, now: datetime, runner, file_reader) -> dict:
        """
        Capture Reddit subreddit trends (stub).

        Args:
            now: Frozen datetime (UTC)
            runner: Injected subprocess runner
            file_reader: Injected file reader

        Raises:
            NotImplementedError: Wave-2 implementation pending OAuth app setup.
        """
        raise NotImplementedError(
            "Reddit adapter is a Wave-2 feature. "
            "Requires OAuth app configuration for API access. "
            "Trend-indicator's design doc (conductor3/plans/trend-indicator-design.md) "
            "contains research notes; do not re-research. "
            "See design doc §2a for contract and integration notes."
        )
