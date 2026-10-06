# Durable tunnel + receiver install

Exposes `signal_hub/webhook_receiver.py` (loopback-only, `127.0.0.1:8787`) at
`https://hooks.dynastywrapped.com/github` through a named Cloudflare Tunnel,
and keeps both the tunnel and the receiver running across reboots via a
Windows service (cloudflared) and a Windows Scheduled Task (the receiver).

## Two human steps (one-time, cannot be scripted)

**1. Move `dynastywrapped.com` onto Cloudflare nameservers.**

- Cloudflare dashboard -> **Add a site** -> `dynastywrapped.com` -> **Free**
  plan. Cloudflare will show you two nameservers (e.g.
  `aron.ns.cloudflare.com` / `beth.ns.cloudflare.com` -- yours will differ).
- GoDaddy -> `dynastywrapped.com` -> **DNS** -> **Nameservers** -> change from
  GoDaddy's defaults (`ns03`/`ns04.domaincontrol.com`) to the two Cloudflare
  gave you.
- This can take anywhere from a few minutes to ~24h to propagate. The
  install script checks for you (`-DryRun` reports current status without
  waiting).

**2. Authenticate cloudflared.**

```
cloudflared tunnel login
```

Opens a browser, you pick the zone, and it writes
`%USERPROFILE%\.cloudflared\cert.pem`. One-time per machine.

## Then: one command

```
cd C:\Users\matt8\signal-hub
.\deploy\install_tunnel.ps1 -DryRun     # preview everything first
.\deploy\install_tunnel.ps1             # the real thing
```

If either human step above isn't done yet, the real run (no `-DryRun`) exits
**2** and prints exactly what's still missing -- it never attempts a
Cloudflare-authenticated action without the prerequisites in place.

What it does, in order:

1. Checks prerequisites: cloudflared installed, `cert.pem` present, the zone
   resolving on Cloudflare nameservers.
2. Creates the named tunnel `aesop-hooks` if it doesn't already exist.
3. Writes `%USERPROFILE%\.cloudflared\config.yml` (tunnel id, credentials
   file, ingress rule for `hooks.dynastywrapped.com` ->
   `http://127.0.0.1:8787`, catch-all 404).
4. Routes DNS: `cloudflared tunnel route dns aesop-hooks hooks.dynastywrapped.com`.
5. Installs + starts cloudflared as a Windows service (survives reboot).
6. Registers a Windows Scheduled Task, `AesopSignalHubReceiver` (runs at
   logon, restarts on failure, logs to `state\webhook-receiver.log`), running
   `python -m signal_hub.webhook_receiver --root <repo>`. The webhook secret
   is read from the **`SIGNAL_HUB_GH_WEBHOOK_SECRET`** user-scope environment
   variable at process start -- it is never written into the task
   definition or this script. Set it once with:
   ```
   setx SIGNAL_HUB_GH_WEBHOOK_SECRET "<your secret>"
   ```
   (requires a fresh logon to take effect for scheduled tasks).
7. Verifies: `http://127.0.0.1:8787/healthz` -> 200, then
   `https://hooks.dynastywrapped.com/healthz` -> 200 through the live
   tunnel.

## Uninstalling

```
.\deploy\uninstall_tunnel.ps1 -DryRun   # preview
.\deploy\uninstall_tunnel.ps1           # stop + unregister the local pieces
.\deploy\uninstall_tunnel.ps1 -Purge    # also delete the Cloudflare tunnel + config.yml
```

Conservative by default: stops/unregisters the scheduled task and the
cloudflared service, but leaves the tunnel, its DNS route, and config.yml in
place unless `-Purge` is passed. **Never** touches the signal-hub queue
(`queue/`, `~/conductor3/state/signal-hub-queue.jsonl`), with or without
`-Purge`, and never reverts the GoDaddy nameserver change.

## Notes

- Port, path, and secret env var name come straight from
  `signal_hub/webhook_receiver.py` (`DEFAULT_PORT = 8787`,
  `SECRET_ENV_VAR = "SIGNAL_HUB_GH_WEBHOOK_SECRET"`) -- this README and the
  install script are kept in sync with that module, not the other way
  around.
- `/healthz` is an unauthenticated liveness probe added alongside this
  install (see `signal_hub/webhook_receiver.py`'s `HEALTHZ_PATH`) -- it only
  proves the process is up and reachable, not that the pipeline is healthy
  (that's still `state/.signal-hub-webhook-heartbeat`, written per-delivery).
- See `STATE.md` ("GitHub webhook receiver -- service design") for the
  original design sketch this script implements, and for how to register the
  GitHub webhook itself (`gh api repos/<owner>/<repo>/hooks ...`) once the
  tunnel is live.
