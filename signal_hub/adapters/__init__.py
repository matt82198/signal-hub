"""
Snapshot adapters for signal-hub.

Each adapter captures state from an external source and produces a normalized snapshot.

Interface (protocol, not a base class):
    def capture(now: datetime, runner, file_reader) -> dict:
        '''
        Capture a snapshot from the source.

        Args:
            now: Frozen datetime (UTC) for this snapshot
            runner: Injected subprocess runner callable (cmd_args) -> exit_code
            file_reader: Injected file reader callable (path) -> dict

        Returns:
            Snapshot dict:
                {
                    "source": "<name>",
                    "schema_version": 1,
                    "captured_at": "ISO8601Z",
                    "status": "OK|UNCHANGED|STALE|SKIPPED|ERROR",
                    "payload": {...},  # omitted if status != OK
                    "etag": "...",     # for conditional GET
                    "error": "..."     # omitted if status == OK
                }
        '''
"""
