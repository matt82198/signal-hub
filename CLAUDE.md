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

## GitHub webhook receiver (Phase 1 of the box event bridge)

`signal_hub/webhook_receiver.py` is a second, independent producer into the same event log
and rules engine `tick.py` uses — webhook-sourced instead of poll-sourced, same dedup domains
(`state/events/*.jsonl`, `state/.events-seen`, `state/.rules-fired.jsonl`), same heartbeat
discipline (written LAST), own heartbeat file `state/.signal-hub-webhook-heartbeat`.

```
python -m signal_hub.webhook_receiver [--port 8787] [--root DIR]
```

`POST /github` only. Verifies `X-Hub-Signature-256` (HMAC-SHA256, secret from
`SIGNAL_HUB_GH_WEBHOOK_SECRET` env var or `state/.github-webhook-secret`, never committed) —
unsigned/invalid is 401 and touches no state. Dedups on `X-GitHub-Delivery` via a second
`SeenIndex` rooted at `state/webhook-deliveries/`. Builds exactly one typed event per
delivery (`github.workflow_run.completed`, `github.check_suite.completed`,
`github.pull_request.<action>`, `github.push`), appends it, evaluates it against `rules/`
inline (no 5-minute wait), enqueues any fired task into `queue/` exactly like `tick.py` does.
Binds to `127.0.0.1` only — a tunnel or reverse proxy terminates the public side; see
STATE.md for the durable-tunnel commands (none registered yet — that's a hostname decision
for Matt).

Three rules at Phase 1 (`rules/R-gh-00{1,2,3}-*.json`), all `task.kind: run_workflow` since
there is still no headless drainer — an aesop session (or a future drainer) runs the named
workflow:

* `R-gh-001-main-full-failed` — `workflow_run.completed`, `conclusion==failure` on `main` ->
  `aesop.gate_escape` (run_id, head_sha, html_url).
* `R-gh-002-pr-check-suite-green` — `check_suite.completed`, `conclusion==success` with at
  least one associated PR -> `aesop.merge_eligible`, informational only (native GitHub
  auto-merge does the actual merge; the webhook payload cannot see a PR's `auto_merge` flag,
  so this is a `pr_count>0` proxy, not a true armed-check — documented in the rule's `notes`).
* `R-gh-003-pr-closed-merged` — `pull_request.closed` with `merged==true` ->
  `aesop.tracker_autoclose` (PR number) — consumer is the tracker auto-close path.

**Orchestrator wake-up mirror**: every task this receiver enqueues is ALSO appended
(append-only, one JSON line per task) to `~/conductor3/state/signal-hub-queue.jsonl` —
override with `conductor_queue_path=` when calling `process_delivery` directly (tests always
override it). A live orchestrator session watches that file with `Monitor` instead of polling
GitHub or signal-hub's own `queue/` directory.

`GET /healthz` is an unauthenticated liveness probe (`HEALTHZ_PATH`) — 200 if the process is
up, nothing else; it touches no state and is distinct from `state/.signal-hub-webhook-heartbeat`
(per-delivery pipeline health).

Durable hosting: `deploy/install_tunnel.ps1` / `deploy/uninstall_tunnel.ps1` (one command each,
`-DryRun` previews everything) create the named Cloudflare Tunnel `aesop-hooks`, route
`hooks.dynastywrapped.com` -> `http://127.0.0.1:8787`, install cloudflared as a Windows service,
and register the receiver as scheduled task `AesopSignalHubReceiver` — see `deploy/README.md`
for the two one-time human steps and STATE.md "GitHub webhook receiver — service design" for the
original plan these scripts implement.
