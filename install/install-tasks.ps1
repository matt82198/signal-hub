# install-tasks.ps1 -- register (or, by default, merely PREVIEW) the
# signal-hub scheduled task.
#
# Design section 2c: exactly ONE Windows Scheduled Task, firing
# `python -m signal_hub tick` every 5 minutes. No per-source tasks, no daemon.
# Which sources actually fetch is decided inside the tick.
#
# THE DEFAULT IS TO DO NOTHING. Registering a task is a user-visible change to
# the machine, so this script previews unless it is handed -Register. That is
# the opposite of the usual installer default, and it is deliberate: a preview
# that a human reads and then approves is cheap, and a five-minute job nobody
# remembers installing is expensive.
#
#   .\install-tasks.ps1                 preview (prints the schtasks command)
#   .\install-tasks.ps1 -DryRun         same, said out loud
#   .\install-tasks.ps1 -WhatIf         same again
#   .\install-tasks.ps1 -Status         read-only: is it installed?
#   .\install-tasks.ps1 -Register       ACTUALLY register (user-gated)
#   .\install-tasks.ps1 -Uninstall      remove it
#
# Registration uses Register-ScheduledTask (the aesop watchdog pattern: hidden
# window, StartWhenAvailable, MultipleInstances IgnoreNew, idempotent -Force).
# The preview prints the equivalent schtasks.exe command line so the exact
# action can be audited, or pasted, without running this script at all.

param(
    [string]$TaskName = 'AesopSignalHub',
    [int]$IntervalMinutes = 5,
    [string]$Python = '',
    [string]$Root = '',
    [switch]$Register,
    [switch]$DryRun,
    [switch]$WhatIf,
    [switch]$Uninstall,
    [switch]$Status
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

function Test-TaskPresent($name) {
    try {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction Stop
        if ($task) { return $true }
    }
    catch { }
    return $false
}

# -- resolve inputs ---------------------------------------------------------

if ($IntervalMinutes -lt 1) {
    Fail "IntervalMinutes must be at least 1 (got $IntervalMinutes)."
}

$installDir = $PSScriptRoot
$repoRoot = $Root
if (-not $repoRoot) { $repoRoot = Split-Path -Parent $installDir }
# A trailing backslash would collide with the \" escaping inside /TR and turn
# the closing quote into a literal one -- silently corrupting the action.
$repoRoot = $repoRoot.TrimEnd('\')
$vbs = Join-Path $installDir 'run-hidden.vbs'
$pythonExe = Resolve-Python

# The vbs launcher's contract is that no argument contains a double quote.
# Check before building anything, so a bad path can never reach the scheduler.
foreach ($pair in @(@('Root', $repoRoot), @('Python', $pythonExe), @('TaskName', $TaskName))) {
    if ($pair[1] -like "*$QUOTE*") {
        Fail "$($pair[0]) contains a double quote, which the run-hidden.vbs launcher forbids: $($pair[1])"
    }
}

# -- build the action -------------------------------------------------------

$vbsArguments = "//B //Nologo $QUOTE$vbs$QUOTE --cwd $QUOTE$repoRoot$QUOTE $QUOTE$pythonExe$QUOTE -m signal_hub tick"

# The same action spelled as a single schtasks.exe command line. Inner quotes
# are backslash-escaped, which is how schtasks wants them inside /TR.
$esc = '\' + $QUOTE
$tr = 'wscript.exe //B //Nologo ' + $esc + $vbs + $esc + ' --cwd ' + $esc + $repoRoot + $esc + ' ' + $esc + $pythonExe + $esc + ' -m signal_hub tick'
$schtasksCommand = 'schtasks.exe /Create /TN ' + $TaskName + ' /SC MINUTE /MO ' + $IntervalMinutes + ' /TR ' + $QUOTE + $tr + $QUOTE + ' /F'

$present = Test-TaskPresent $TaskName

# -- modes ------------------------------------------------------------------

if ($Status) {
    if ($present) { Write-Host "$TaskName is REGISTERED" }
    else { Write-Host "$TaskName is NOT registered" }
    Write-Host "  root:   $repoRoot"
    Write-Host "  python: $pythonExe"
    exit 0
}

if ($Uninstall) {
    if (-not $present) {
        Write-Host "$TaskName is not registered; nothing to remove"
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered $TaskName"
    exit 0
}

if (-not $Register) {
    # Preview. -DryRun and -WhatIf are accepted so the intent can be stated
    # explicitly, but they change nothing: this is what happens by default.
    $verb = 'would REGISTER'
    if ($present) { $verb = 'would REPLACE existing task' }
    Write-Host "DRYRUN: $TaskName $verb (every $IntervalMinutes minute(s), hidden, StartWhenAvailable)"
    Write-Host "DRYRUN:   root:   $repoRoot"
    Write-Host "DRYRUN:   python: $pythonExe"
    Write-Host "DRYRUN:   action: wscript.exe $vbsArguments"
    Write-Host "DRYRUN: equivalent command (NOT executed):"
    Write-Host $schtasksCommand
    Write-Host "DRYRUN: nothing was registered. Re-run with -Register to install."
    exit 0
}

# -- actually register ------------------------------------------------------

if (-not (Test-Path $vbs)) { Fail "run-hidden.vbs not found at: $vbs" }
if (-not (Test-Path $repoRoot)) { Fail "repo root not found at: $repoRoot" }

$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument $vbsArguments
$trigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Registered $TaskName (every $IntervalMinutes minute(s))"
Write-Host "  kill switch: create $repoRoot\state\.HALT to stop the pipeline without unregistering"
exit 0
