# STATE — signal-hub
## Intent
Signal/trend/noise capture layer that triggers aesop tasks (design: conductor3/plans/
signal-hub-design.md, approved 2026-08-13). MVP: nflverse schedules + player stats +
trend-indicator orchestration; 3 rules (R001 game-final-win, R002 big-stat-line,
R003 demand-delta); queue consumed by aesop sessions.
## Phase
BUILD — lane fleet L1-L6 parallel, L7 integration merges last.

Phase 1 of the GitHub -> box event bridge landed on `feat/github-webhook-receiver`
(2026-10-06): `signal_hub/webhook_receiver.py` + 3 new rules (`R-gh-001/002/003`) +
conductor3 queue mirror. See CLAUDE.md "GitHub webhook receiver" for the running
contract. 592 tests total (558 pre-existing + 34 new); the 4 pre-existing
`test_queue.py`/`test_cli.py` claim-race failures are a baseline flake unconnected to
this change (reproduced on `origin/master` before this branch existed).

## GitHub webhook receiver — service design (report; NOT installed by this change)

**How it should run durably on this box:**
- A Windows Scheduled Task at logon (parallel to, not replacing, `AesopSignalHub`'s
  5-min tick task), `Action: python -m signal_hub.webhook_receiver --root <repo>`,
  `Trigger: At log on`, `Settings: Restart on failure, run whether user is logged on
  or not` if a service account is available, else "run only when logged on" is
  acceptable for a dev box. Hidden via the same `run-hidden.vbs` wrapper
  `install/install-tasks.ps1` already uses for the tick task, so it does not pop a
  console window at every logon.
- **Port**: 8787 (`DEFAULT_PORT` in `webhook_receiver.py`), loopback-only
  (`127.0.0.1`) — a tunnel (cloudflared Quick or named) or reverse proxy terminates
  the public side; the receiver itself never listens on `0.0.0.0`.
- **Logs**: stderr only today (`log_message` override never prints headers/body/
  secrets, just client IP + timestamp + status line) — redirect to
  `state/webhook-receiver.log` in the scheduled-task Action
  (`... --root <repo> >> state\webhook-receiver.log 2>&1`, hidden VBS wrapper handles
  the redirection the same way the tick task's log works).
- **Kill switch**: `state/.HALT` is honoured per-delivery — the process keeps running
  and keeps verifying signatures (so HALT cannot be probed by an unauthenticated
  request), but once HALTed nothing is appended, enqueued, mirrored or heartbeated.
  Removing `.HALT` resumes processing on the very next delivery; no restart needed.
  To actually stop the process: `schtasks /End /TN <TaskName>` (scheduled task) or
  kill the PID — same as any other signal-hub process, there is no separate stop
  flag because the HTTP server has no polling loop to race.
- **Secret rotation**: rewrite `state/.github-webhook-secret` (or the env var) and
  restart the task; no code change needed.

**Durable tunnel (hostname is Matt's call, not made here)** — once a name is picked,
these two commands are the whole wiring (cloudflared is NOT installed on this box
today; `choco install cloudflared` or the MSI first):
```
cloudflared tunnel login
cloudflared tunnel create signal-hub-webhook
# add a DNS route for the chosen hostname, e.g.:
cloudflared tunnel route dns signal-hub-webhook webhook.<Matt's domain>
# config.yml:
#   tunnel: <tunnel-id-from-create>
#   credentials-file: C:\Users\matt8\.cloudflared\<tunnel-id>.json
#   ingress:
#     - hostname: webhook.<Matt's domain>
#       service: http://127.0.0.1:8787
#     - service: http_status:404
cloudflared tunnel run signal-hub-webhook
```
Then `gh api -X POST repos/matt82198/aesop/hooks -f name=web -f active=true \
-f config[url]=https://webhook.<Matt's domain>/github -f config[content_type]=json \
-f config[secret]=<fresh random secret, stored only in state/.github-webhook-secret> \
-f events[]=workflow_run -f events[]=check_suite -f events[]=pull_request -f events[]=push`.

## Tunnel proof (this session)

`cloudflared` is not on PATH on this box — the Quick Tunnel + ephemeral
`matt82198/aesop` webhook step was skipped per the task's documented fallback. Proved
locally instead: ran `python -m signal_hub.webhook_receiver --root <tmp>` on
127.0.0.1:8787/8788, POSTed a correctly-HMAC-signed synthetic `workflow_run.completed`
payload to `/github` -> HTTP 200, one `github.workflow_run.completed` line landed in
`state/events/*.jsonl`, `R-gh-001-main-full-failed` fired, one task landed in
`queue/pending/`, and the task was mirrored to the REAL
`~/conductor3/state/signal-hub-queue.jsonl` (one demo line, left in place — delete it
if it should not be there). A second POST with a wrong signature over a separate
instance returned HTTP 401. No webhook was ever created on `matt82198/aesop` (the
tunnel step never ran, so there was nothing to delete).

## NEXT STEPS
1. Lanes land -> merge train with test proof -> L7 integration -> task installer.
2. Register scheduled task (user-visible change, announce), ECOSYSTEM.md row.
3. Preseason dry-run week: --include-preseason, verify events flow, no task fires.
4. Phase 2 of the webhook bridge (not started): pick the durable hostname, install
   cloudflared, register the scheduled task for `webhook_receiver.py`, and swap the
   `R-gh-002` auto-merge-armed proxy for a real check once a PR-fetch step (or the
   GraphQL `autoMergeRequest` field) is wired in.
