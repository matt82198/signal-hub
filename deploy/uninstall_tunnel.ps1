# uninstall_tunnel.ps1 -- reverse of install_tunnel.ps1.
#
# Conservative by design: stops and unregisters the two LOCAL things this
# install created (the receiver scheduled task, the cloudflared Windows
# service), but by default leaves the cloudflared tunnel, its DNS route, and
# config.yml alone -- deleting a named tunnel/DNS route is a Cloudflare-side
# change that is cheap to redo and not something an uninstall should do
# silently. Pass -Purge to also remove those.
#
# NEVER touches queue state: neither this repo's queue/ dir nor
# ~/conductor3/state/signal-hub-queue.jsonl are read, written, or deleted by
# this script, with or without -Purge.
#
#   .\uninstall_tunnel.ps1            stop + unregister the local pieces
#   .\uninstall_tunnel.ps1 -DryRun    preview; changes nothing
#   .\uninstall_tunnel.ps1 -Purge     also: cloudflared tunnel delete + remove
#                                     config.yml (still never touches DNS
#                                     registrar settings or the queue file)
#
# PowerShell 5.1 compatible: no double-ampersand chaining, no null-coalescing, no ternary.

param(
    [string]$TunnelName = 'aesop-hooks',
    [string]$ReceiverTaskName = 'AesopSignalHubReceiver',
    [string]$CloudflaredExe = 'C:\Program Files (x86)\cloudflared\cloudflared.exe',
    [switch]$Purge,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Resolve-Cloudflared {
    if (Test-Path $CloudflaredExe) { return $CloudflaredExe }
    $found = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return $null
}

function Test-TaskPresent($name) {
    try {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
        if ($task) { return $true }
    }
    catch { }
    return $false
}

function Test-ServicePresent($name) {
    try {
        $svc = Get-Service -Name $name -ErrorAction Stop
        if ($svc) { return $true }
    }
    catch { }
    return $false
}

$cloudflaredExe = Resolve-Cloudflared
$taskPresent = Test-TaskPresent $ReceiverTaskName
$servicePresent = Test-ServicePresent 'cloudflared'
$configPath = Join-Path $env:USERPROFILE '.cloudflared\config.yml'
$configPresent = Test-Path $configPath

if ($DryRun) {
    Write-Host "DRYRUN: uninstall_tunnel.ps1 plan"
    if ($taskPresent) {
        Write-Host "DRYRUN: would run: Unregister-ScheduledTask -TaskName $ReceiverTaskName -Confirm:`$false"
    }
    else {
        Write-Host "DRYRUN: scheduled task '$ReceiverTaskName' not present -- nothing to unregister"
    }
    if ($servicePresent) {
        Write-Host "DRYRUN: would run: Stop-Service -Name cloudflared"
        if ($cloudflaredExe) {
            Write-Host "DRYRUN: would run: $cloudflaredExe service uninstall"
        }
        else {
            Write-Host "DRYRUN: cloudflared.exe not found -- would stop the 'cloudflared' service only, service uninstall skipped"
        }
    }
    else {
        Write-Host "DRYRUN: 'cloudflared' service not present -- nothing to stop/uninstall"
    }
    if ($Purge) {
        if ($cloudflaredExe) {
            Write-Host "DRYRUN: (-Purge) would run: $cloudflaredExe tunnel delete $TunnelName"
        }
        if ($configPresent) {
            Write-Host "DRYRUN: (-Purge) would remove: $configPath"
        }
        else {
            Write-Host "DRYRUN: (-Purge) config.yml not present at $configPath -- nothing to remove"
        }
        Write-Host "DRYRUN: (-Purge) DNS route and the GoDaddy/Cloudflare nameserver change are NOT reverted -- do that in the Cloudflare dashboard / registrar if truly decommissioning."
    }
    else {
        Write-Host "DRYRUN: -Purge not set -- the tunnel, its DNS route, and config.yml are left in place"
    }
    Write-Host "DRYRUN: the signal-hub queue (queue/, ~/conductor3/state/signal-hub-queue.jsonl) is never touched by this script."
    Write-Host "DRYRUN: nothing was executed."
    exit 0
}

Write-Host "1/3 scheduled task '$ReceiverTaskName' ..."
if ($taskPresent) {
    Unregister-ScheduledTask -TaskName $ReceiverTaskName -Confirm:$false
    Write-Host "    unregistered"
}
else {
    Write-Host "    not present; nothing to do"
}

Write-Host "2/3 cloudflared service ..."
if ($servicePresent) {
    try { Stop-Service -Name cloudflared -ErrorAction Stop } catch { Write-Host "    (stop failed/not running: $_)" }
    if ($cloudflaredExe) {
        & $cloudflaredExe service uninstall
        Write-Host "    service uninstalled"
    }
    else {
        Write-Host "    cloudflared.exe not found -- stopped the service but could not run 'service uninstall'"
    }
}
else {
    Write-Host "    not present; nothing to do"
}

Write-Host "3/3 tunnel + config.yml ..."
if ($Purge) {
    if ($cloudflaredExe) {
        & $cloudflaredExe tunnel delete $TunnelName
        Write-Host "    deleted tunnel '$TunnelName' (DNS route and registrar nameservers are untouched)"
    }
    else {
        Write-Host "    cloudflared.exe not found -- could not run 'tunnel delete $TunnelName'"
    }
    if ($configPresent) {
        Remove-Item -Path $configPath -Force
        Write-Host "    removed $configPath"
    }
}
else {
    Write-Host "    -Purge not set; tunnel, DNS route, and config.yml left in place"
}

Write-Host "Done. The signal-hub queue (queue/, ~/conductor3/state/signal-hub-queue.jsonl) was not touched."
exit 0
