# signal-hub

The aesop ecosystem's nervous system: world -> snapshots -> typed events -> data-driven
trigger rules -> queued aesop tasks. Local-first, stdlib-only Python 3.14, one Windows
scheduled task (5-min tick), filesystem queue with rename-as-mutex.

AUTHORITY: C:\Users\matt8\conductor3\plans\signal-hub-design.md — every module implements
its section; deviations get documented in STATE.md, never improvised silently.

Layout (per design L1-L7): signal_hub/{snapshots,clock}.py | adapters/ | events/ | rules/ |
queue/ | cli.py. state/ (snapshots, event log, seen-index, hub-status.json) is gitignored.
Commands: python -m signal_hub tick | status | queue list|claim|complete.
