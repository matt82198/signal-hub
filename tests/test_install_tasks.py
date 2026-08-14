"""install/install-tasks.ps1 -- the scheduled-task installer, tested WITHOUT installing.

Registration is a user-gated, user-visible change to the machine, so the script
is built the other way round from a normal installer: doing nothing is the
default, and ``-Register`` is the only path that touches the task scheduler.
Every test here runs the preview path and then asserts the task still does not
exist -- if any of these ever registers something, the last assertion fails
loudly rather than leaving a stray five-minute job behind.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "install" / "install-tasks.ps1"
VBS = REPO_ROOT / "install" / "run-hidden.vbs"
TASK_NAME = "AesopSignalHub"

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
SCHTASKS = shutil.which("schtasks")

pytestmark = pytest.mark.skipif(
    POWERSHELL is None, reason="no PowerShell on PATH (non-Windows runner)"
)


def run_script(*args):
    proc = subprocess.run(
        [
            POWERSHELL, "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT), *args,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc


def task_exists():
    """Read-only probe. True only if the scheduler really knows the task."""
    if SCHTASKS is None:
        return False
    probe = subprocess.run(
        [SCHTASKS, "/Query", "/TN", TASK_NAME],
        capture_output=True, text=True, timeout=60,
    )
    return probe.returncode == 0


@pytest.fixture(autouse=True)
def never_registers():
    """Nothing in this module may leave a scheduled task behind."""
    before = task_exists()
    yield
    assert task_exists() == before, (
        "install-tasks.ps1 changed the real task scheduler during a preview test"
    )


# ---------------------------------------------------------------------------
# the files exist and are what the design asks for
# ---------------------------------------------------------------------------

def test_installer_files_exist():
    assert SCRIPT.is_file()
    assert VBS.is_file()


def test_vbs_runs_hidden_and_propagates_the_exit_code():
    text = VBS.read_text(encoding="utf-8")
    # Window style 0 + wait(True) is the whole point: a raw bash action flashes
    # a console every five minutes, and a non-waiting launcher makes
    # MultipleInstances and LastTaskResult meaningless.
    assert "WScript.Quit" in text
    assert "shell.Run" in text or "Shell.Run" in text


# ---------------------------------------------------------------------------
# preview is the default
# ---------------------------------------------------------------------------

def test_no_arguments_previews_and_registers_nothing():
    proc = run_script()
    assert proc.returncode == 0, proc.stderr
    assert "DRYRUN" in proc.stdout
    assert TASK_NAME in proc.stdout


def test_dryrun_prints_the_schtasks_command():
    proc = run_script("-DryRun")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "schtasks.exe /Create" in out
    assert "/TN AesopSignalHub" in out
    assert "/SC MINUTE" in out
    assert "/MO 5" in out
    assert "run-hidden.vbs" in out
    assert "-m signal_hub tick" in out


def test_whatif_is_the_same_preview():
    assert run_script("-WhatIf").stdout.strip() == run_script("-DryRun").stdout.strip()


def test_interval_override_reaches_the_command():
    proc = run_script("-DryRun", "-IntervalMinutes", "15")
    assert proc.returncode == 0, proc.stderr
    assert "/MO 15" in proc.stdout


def test_task_name_override_reaches_the_command():
    proc = run_script("-DryRun", "-TaskName", "SignalHubProbe")
    assert proc.returncode == 0, proc.stderr
    assert "/TN SignalHubProbe" in proc.stdout


def test_root_and_python_overrides_are_used_verbatim(tmp_path):
    fake_python = tmp_path / "python.exe"
    fake_python.write_bytes(b"")
    proc = run_script(
        "-DryRun", "-Root", str(tmp_path), "-Python", str(fake_python)
    )
    assert proc.returncode == 0, proc.stderr
    assert str(tmp_path) in proc.stdout
    assert str(fake_python) in proc.stdout


def test_preview_reports_whether_the_task_already_exists():
    proc = run_script("-DryRun")
    assert ("would REGISTER" in proc.stdout) or ("would REPLACE" in proc.stdout)


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------

def test_a_double_quote_in_a_path_is_rejected():
    proc = run_script("-DryRun", "-Root", 'C:\\we"ird')
    assert proc.returncode == 1
    assert "double quote" in (proc.stdout + proc.stderr).lower()


def test_a_nonpositive_interval_is_rejected():
    proc = run_script("-DryRun", "-IntervalMinutes", "0")
    assert proc.returncode == 1


def test_the_previewed_action_really_runs_a_tick(tmp_path):
    """The printed action is not a fiction -- run it and check the exit code.

    Also proves --cwd is load-bearing: `python -m signal_hub` only resolves the
    package from the repo root, and a scheduled task starts wherever Windows
    feels like.  The tick is --dry-run --offline, so this writes nothing and
    touches no network.
    """
    wscript = shutil.which("wscript")
    if wscript is None:
        pytest.skip("no wscript.exe (non-Windows runner)")

    def action(cwd):
        return subprocess.run(
            [
                wscript, "//B", "//Nologo", str(VBS), "--cwd", cwd,
                sys.executable, "-m", "signal_hub", "tick",
                "--dry-run", "--offline", "--root", str(tmp_path),
            ],
            capture_output=True, timeout=180,
        ).returncode

    assert action(str(REPO_ROOT)) == 0
    assert action(str(tmp_path)) != 0  # wrong cwd -> the package is not importable
    assert not (tmp_path / "state").exists()  # and the dry run still wrote nothing


def test_status_is_read_only_and_always_answers():
    proc = run_script("-Status")
    assert proc.returncode == 0, proc.stderr
    assert TASK_NAME in proc.stdout
