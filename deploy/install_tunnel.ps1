# install_tunnel.ps1 -- ONE command to finish the Cloudflare Tunnel + receiver
# install for the GitHub webhook bridge, once Matt has done the two human
# steps this script cannot do for him (see deploy/README.md):
#
#   1. Cloudflare dashboard -> Add a site -> the zone (Free) -> change the
#      zone's nameservers at the registrar to the two Cloudflare gives you.
#   2. cloudflared tunnel login   (writes cert.pem to %USERPROFILE%\.cloudflared)
#
# After that, this script:
#   - checks both of those (plus cloudflared being installed) and FAILS
#     CLOSED (exit 2, remaining steps printed) if either is still missing --
#     never attempts an action that would need Cloudflare auth that is not
#     there yet.
#   - creates the named tunnel if it does not already exist
#     (`cloudflared tunnel create`)
#   - writes %USERPROFILE%\.cloudflared\config.yml (tunnel id, credentials
#     file, ingress: Hostname -> http://127.0.0.1:<LocalPort>, catch-all 404)
#   - routes DNS (`cloudflared tunnel route dns`)
#   - installs + starts cloudflared as a Windows service
#   - registers the receiver as a scheduled task (at logon, restart on
#     failure), running `python -m signal_hub.webhook_receiver` out of this
#     repo, reading its webhook secret from the user-scope env var (never
#     embedded in the task definition)
#   - verifies: local /healthz -> 200, then the public hostname's /healthz
#     -> 200 through the tunnel
#
#   .\install_tunnel.ps1 -DryRun     preview every command; changes nothing
#   .\install_tunnel.ps1             the real thing (gated on prerequisites)
#
# PowerShell 5.1 compatible: no double-ampersand chaining, no null-coalescing, no ternary.

param(
    [string]$TunnelName = 'aesop-hooks',
    [string]$Hostname = 'hooks.dynastywrapped.com',
    [string]$Zone = 'dynastywrapped.com',
    [int]$LocalPort = 8787,
    [string]$RepoRoot = '',
    [string]$ReceiverTaskName = 'AesopSignalHubReceiver',
    [string]$SecretEnvVar = 'SIGNAL_HUB_GH_WEBHOOK_SECRET',
    [string]$CloudflaredExe = 'C:\Program Files (x86)\cloudflared\cloudflared.exe',
    [string]$Python = '',
    # Test/preview-only override: skip `cloudflared tunnel create`/`list`
    # (both need auth) and use this id to render config.yml. Never needed
    # for a real run against an already-authenticated box.
    [string]$TunnelId = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$QUOTE = [char]34

function Fail($message) {
    Write-Host "ERROR: $message"
    exit 1
}

function Resolve-Python {
    if ($Python) { return $Python }
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return 'python.exe'
}

function Resolve-Cloudflared {
    if (Test-Path $CloudflaredExe) { return $CloudflaredExe }
    $found = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return $null
}

function Get-ZoneNameServers($zoneName) {
    $cmd = Get-Command Resolve-DnsName -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $records = Resolve-DnsName -Name $zoneName -Type NS -ErrorAction Stop
            return @($records | Where-Object { $_.Type -eq 'NS' } | ForEach-Object { $_.NameHost })
        }
        catch { return @() }
    }
    $nslookup = Get-Command nslookup.exe -ErrorAction SilentlyContinue
    if (-not $nslookup) { return @() }
    try {
        $out = & $nslookup.Source -type=ns $zoneName 2>$null
        $matches = $out | Select-String 'nameserver\s*=\s*(\S+)'
        return @($matches | ForEach-Object { $_.Matches[0].Groups[1].Value.TrimEnd('.') })
    }
    catch { return @() }
}

function Test-ZoneOnCloudflare($zoneName) {
    $ns = Get-ZoneNameServers $zoneName
    if (-not $ns -or $ns.Count -eq 0) { return $false }
    $cf = @($ns | Where-Object { $_ -match 'cloudflare\.com\.?$' })
    return ($cf.Count -gt 0)
}

function Get-ConfigYamlContent($id) {
    $credsFile = Join-Path $env:USERPROFILE ('.cloudflared\' + $id + '.json')
    $lines = @(
        "tunnel: $id",
        "credentials-file: $credsFile",
        "ingress:",
        "  - hostname: $Hostname",
        "    service: http://127.0.0.1:$LocalPort",
        "  - service: http_status:404"
    )
    return ($lines -join "`n") + "`n"
}

function Test-Url200($url, $maxAttempts, $delaySeconds) {
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    for ($i = 1; $i -le $maxAttempts; $i++) {
        try {
            if ($curl) {
                $code = & $curl.Source -s -o NUL -w '%{http_code}' --max-time 10 $url
                if ($code -eq '200') { return $true }
            }
            else {
                $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 10
                if ($resp.StatusCode -eq 200) { return $true }
            }
        }
        catch { }
        if ($i -lt $maxAttempts) { Start-Sleep -Seconds $delaySeconds }
    }
    return $false
}

function Get-TunnelCreateId($cfExe) {
    if ($TunnelId) { return $TunnelId }
    $listOut = & $cfExe tunnel list -o json 2>$null
    $tunnels = @()
    try { $tunnels = @($listOut | ConvertFrom-Json) } catch { $tunnels = @() }
    $existing = $tunnels | Where-Object { $_.name -eq $TunnelName }
    if ($existing) { return $existing[0].id }
    $createOut = & $cfExe tunnel create $TunnelName 2>&1
    $createOut | ForEach-Object { Write-Host $_ }
    $joined = ($createOut | Out-String)
    $m = [regex]::Match($joined, 'with id ([0-9a-fA-F-]+)')
    if (-not $m.Success) { Fail "could not parse a tunnel id out of 'cloudflared tunnel create' output" }
    return $m.Groups[1].Value
}

# -- resolve inputs ----------------------------------------------------------

$repoRoot = $RepoRoot
if (-not $repoRoot) { $repoRoot = Split-Path -Parent $PSScriptRoot }
$repoRoot = $repoRoot.TrimEnd('\')
$pythonExe = Resolve-Python
$logPath = Join-Path $repoRoot 'state\webhook-receiver.log'

# The receiver action is built as a single `powershell.exe -WindowStyle Hidden
# -Command "..."` string (not the install/run-hidden.vbs tick-task launcher):
# that launcher re-splits its argv on whitespace and re-quotes each token
# individually, which would mangle a `>> log 2>&1`-style redirection. Paths
# are wrapped in single quotes inside the -Command string, so guard against
# both quote styles up front.
foreach ($pair in @(@('RepoRoot', $repoRoot), @('Python', $pythonExe), @('LogPath', $logPath), @('TunnelName', $TunnelName), @('Hostname', $Hostname))) {
    if (($pair[1] -like "*$QUOTE*") -or ($pair[1] -like "*'*")) {
        Fail "$($pair[0]) contains a quote character, which the receiver task command cannot safely embed: $($pair[1])"
    }
}

$cloudflaredExe = Resolve-Cloudflared
$certPath = Join-Path $env:USERPROFILE '.cloudflared\cert.pem'
$certPresent = Test-Path $certPath
$zoneOnCloudflare = Test-ZoneOnCloudflare $Zone
$prereqsOk = ($null -ne $cloudflaredExe) -and $certPresent -and $zoneOnCloudflare

function Write-RemainingHumanSteps {
    Write-Host "Remaining human steps before install_tunnel.ps1 can finish:"
    if (-not $cloudflaredExe) {
        Write-Host "  - cloudflared.exe not found (checked '$CloudflaredExe' and PATH). Install it first."
    }
    if (-not $zoneOnCloudflare) {
        Write-Host "  - $Zone is not yet on Cloudflare nameservers:"
        Write-Host "      1. Cloudflare dashboard -> Add a site -> $Zone (Free plan) -> note the two nameservers shown."
        Write-Host "      2. GoDaddy -> $Zone -> DNS -> Nameservers -> change to those two Cloudflare nameservers."
        Write-Host "      (DNS propagation can take a while; re-run this script once it has propagated.)"
    }
    if (-not $certPresent) {
        $cf = $cloudflaredExe
        if (-not $cf) { $cf = 'cloudflared' }
        Write-Host "  - Not logged in to Cloudflare: run '$cf tunnel login' (opens a browser, writes cert.pem to $certPath)."
    }
    Write-Host "Then re-run: .\deploy\install_tunnel.ps1"
}

# -- the plan (shared by -DryRun and the real run) ---------------------------

# One powershell.exe -Command string: -WindowStyle Hidden suppresses the
# console directly (no launcher/VBS indirection needed), and `*>> file`
# appends ALL streams (stdout+stderr), matching the ">> log 2>&1" redirection
# STATE.md's service design calls for. Built once so -DryRun prints exactly
# what the real registration below uses.
$innerCmd = "& '$pythonExe' -m signal_hub.webhook_receiver --root '$repoRoot' --port $LocalPort *>> '$logPath'"
$psArgument = '-NoProfile -NonInteractive -WindowStyle Hidden -Command "' + $innerCmd + '"'

if ($DryRun) {
    Write-Host "DRYRUN: install_tunnel.ps1 plan for tunnel '$TunnelName' -> $Hostname -> http://127.0.0.1:$LocalPort"
    Write-Host "DRYRUN: prerequisites:"
    Write-Host "DRYRUN:   cloudflared present:        $(if ($cloudflaredExe) { 'YES ' + $cloudflaredExe } else { 'NO' })"
    Write-Host "DRYRUN:   cert.pem present:            $(if ($certPresent) { 'YES' } else { 'NO (run: cloudflared tunnel login)' })"
    Write-Host "DRYRUN:   $Zone on Cloudflare NS:  $(if ($zoneOnCloudflare) { 'YES' } else { 'NO (nameservers not switched yet)' })"
    if (-not $prereqsOk) {
        Write-Host "DRYRUN: prerequisites NOT all met yet -- a real run would exit 2 here. Showing the rest of the plan anyway:"
    }
    $previewId = $TunnelId
    if (-not $previewId) { $previewId = '<tunnel-id-from-cloudflared-tunnel-create>' }
    Write-Host "DRYRUN: would run: $CloudflaredExe tunnel list -o json   (check whether '$TunnelName' already exists)"
    Write-Host "DRYRUN: would run (if absent): $CloudflaredExe tunnel create $TunnelName"
    Write-Host "DRYRUN: would write $(Join-Path $env:USERPROFILE '.cloudflared\config.yml'):"
    Write-Host (Get-ConfigYamlContent $previewId)
    Write-Host "DRYRUN: would run: $CloudflaredExe tunnel route dns $TunnelName $Hostname"
    Write-Host "DRYRUN: would run: $CloudflaredExe service install"
    Write-Host "DRYRUN: would run: Start-Service -Name cloudflared"
    Write-Host "DRYRUN: would register scheduled task:"
    Write-Host "DRYRUN:   TaskName: $ReceiverTaskName"
    Write-Host "DRYRUN:   Action:   powershell.exe $psArgument"
    Write-Host "DRYRUN:   WorkingDirectory: $repoRoot"
    Write-Host "DRYRUN:   Trigger:  AtLogOn"
    Write-Host "DRYRUN:   Settings: Hidden, RestartCount=3, RestartInterval=1min, ExecutionTimeLimit=Indefinite"
    Write-Host "DRYRUN:   Secret:   read from `$env:$SecretEnvVar at process start (never written into the task definition)"
    Write-Host "DRYRUN: would verify: curl http://127.0.0.1:$LocalPort/healthz -> 200"
    Write-Host "DRYRUN: would verify: curl https://$Hostname/healthz -> 200 (through the tunnel)"
    Write-Host "DRYRUN: nothing was executed. Re-run without -DryRun once prerequisites are met."
    exit 0
}

# -- real run: prerequisites are a hard gate ---------------------------------

if (-not $prereqsOk) {
    Write-RemainingHumanSteps
    exit 2
}

if (-not (Test-Path $repoRoot)) { Fail "repo root not found at: $repoRoot" }
$stateDir = Join-Path $repoRoot 'state'
if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }

Write-Host "1/6 resolving tunnel '$TunnelName' ..."
$tunnelId = Get-TunnelCreateId $cloudflaredExe
Write-Host "    tunnel id: $tunnelId"

Write-Host "2/6 writing config.yml ..."
$cloudflaredDir = Join-Path $env:USERPROFILE '.cloudflared'
if (-not (Test-Path $cloudflaredDir)) { New-Item -ItemType Directory -Path $cloudflaredDir -Force | Out-Null }
$configPath = Join-Path $cloudflaredDir 'config.yml'
Set-Content -Path $configPath -Value (Get-ConfigYamlContent $tunnelId) -Encoding ascii -NoNewline
Write-Host "    wrote $configPath"

Write-Host "3/6 routing DNS ..."
& $cloudflaredExe tunnel route dns $TunnelName $Hostname
if ($LASTEXITCODE -ne 0) { Fail "cloudflared tunnel route dns exited $LASTEXITCODE" }

Write-Host "4/6 installing + starting the cloudflared service ..."
& $cloudflaredExe service install
if ($LASTEXITCODE -ne 0) { Fail "cloudflared service install exited $LASTEXITCODE" }
try { Start-Service -Name cloudflared -ErrorAction Stop } catch { Write-Host "    (service may already be running: $_)" }

Write-Host "5/6 registering scheduled task '$ReceiverTaskName' ..."
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $psArgument -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -StartWhenAvailable
Register-ScheduledTask -TaskName $ReceiverTaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "    registered (reads `$env:$SecretEnvVar at logon; set it with: setx $SecretEnvVar <value>)"

Write-Host "6/6 verifying ..."
$localOk = Test-Url200 "http://127.0.0.1:$LocalPort/healthz" 6 5
if (-not $localOk) { Fail "local health check failed: http://127.0.0.1:$LocalPort/healthz did not return 200" }
Write-Host "    local  http://127.0.0.1:$LocalPort/healthz -> 200"

$tunnelOk = Test-Url200 "https://$Hostname/healthz" 12 10
if (-not $tunnelOk) { Fail "tunnel health check failed: https://$Hostname/healthz did not return 200 (DNS/tunnel may still be propagating -- re-run verification shortly)" }
Write-Host "    tunnel https://$Hostname/healthz -> 200"

Write-Host "Done. $Hostname -> cloudflared service -> http://127.0.0.1:$LocalPort ($ReceiverTaskName)."
exit 0
