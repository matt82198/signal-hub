# signal-hub

The aesop ecosystem's nervous system: world -> snapshots -> typed events -> data-driven
trigger rules -> queued aesop tasks. Local-first, stdlib-only Python 3.14, one Windows
scheduled task (5-min tick), filesystem queue with rename-as-mutex.

AUTHORITY: C:\Users\matt8\conductor3\plans\signal-hub-design.md — every module implements
its section; deviations get documented in STATE.md, never improvised silently.

Layout (per design L1-L7): signal_hub/{snapshots,clock,due,status,rotate}.py | adapters/ |
events/ | rules_engine/ | queue/ | tick.py | cli.py. rules/ is committed SOURCE (the rules
are the product); state/ and queue/ are gitignored runtime output.

## Running it

```
python -m signal_hub tick [--include-preseason] [--dry-run] [--offline] [--root DIR]
python -m signal_hub status [--json]
python -m signal_hub queue list [--json] | claim <id> | complete <id> | fail <id> --reason R
```

`tick` is the whole pipeline, once: HALT check -> due sources -> adapters -> snapshots ->
delta -> event log + seen-index -> rules -> queue -> hub-status.json -> rotation ->
heartbeat (written LAST, so a crashed tick never looks healthy). It is idempotent and cheap;
which sources actually fetch is decided inside it against `state/last-capture.json`.

`--dry-run` lands **nothing** — no snapshot, log line, seen mark, throttle record, task,
status or heartbeat. Payloads are diffed in memory against what is on disk, so it is
repeatable and safe to run against a live hub. `--include-preseason` keeps PRE rows so the
pipe can be exercised out of season; all three MVP rules still filter to REG/POST, so events
flow and no content task fires. `--offline` refuses every fetch (sources record ERROR
snapshots) — how to exercise the pipeline with no network.

Exit codes: 0 ran, 1 command failed, 2 usage, 3 halted by `state/.HALT`. A tick that ran but
hit a dead source still exits 0 — that is a normal operating condition with its own channels
(ERROR snapshot, stderr line, `hub-status.json` alarm), and a task that goes red every five
minutes when GitHub blips is a task people learn to ignore.

**Kill switch**: write `state/.HALT` (JSON `{"reason": "..."}`). The next tick short-circuits
before any capture and writes no heartbeat.

## Installing the scheduled task (user-gated)

```
install\install-tasks.ps1              # PREVIEW; prints the schtasks command, changes nothing
install\install-tasks.ps1 -Status      # read-only: is it installed?
install\install-tasks.ps1 -Register    # actually register AesopSignalHub (5-min)
install\install-tasks.ps1 -Uninstall
```

The default is preview, not install — `-Register` is the only path that touches the
scheduler. One task, 5 minutes, hidden via `run-hidden.vbs`, `StartWhenAvailable`,
`MultipleInstances IgnoreNew`.

## Consuming the queue

There is no headless drainer at MVP. An aesop session drains `queue/pending` through the CLI:
`claim` is `os.rename` (the rename IS the mutex — the loser gets nothing and skips), then
`complete` or `fail --reason`. `expires_at` is load-bearing: a recap task three days late is
worse than no task, so an expired task moves to `failed/` without executing, and a rising
`queue.expired_today` is the measurement that would justify building the drainer.

Templates `gamehighlight` / `goodperformance` are `status: design` in passive-income-dev, so
R001/R002 enqueue **candidate** tasks; fail them with `--reason template-not-ready` rather
than disabling the rule — the visible backlog is the point.
