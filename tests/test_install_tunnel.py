"""deploy/install_tunnel.ps1 and deploy/uninstall_tunnel.ps1 -- tested entirely
through -DryRun.

Neither script is ever exercised for real here: the real path needs
Cloudflare auth (``cloudflared tunnel login``) and a zone already moved onto
Cloudflare nameservers, neither of which a test box can assume, and this
suite must never attempt anything that touches the real task scheduler, the
real cloudflared service, or the network. Every test below passes -DryRun and
asserts against the printed plan (and, for install, the actual file content
it builds for a fake tunnel id).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "deploy" / "install_tunnel.ps1"
UNINSTALL_SCRIPT = REPO_ROOT / "deploy" / "uninstall_tunnel.ps1"
README = REPO_ROOT / "deploy" / "README.md"

TASK_NAME = "AesopSignalHubReceiver"
TUNNEL_NAME = "aesop-hooks"
HOSTNAME = "hooks.dynastywrapped.com"

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
SCHTASKS = shutil.which("schtasks")

pytestmark = pytest.mark.skipif(
    POWERSHELL is None, reason="no PowerShell on PATH (non-Windows runner)"
)


def run_script(script, *args):
    proc = subprocess.run(
        [
            POWERSHELL, "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", str(script), *args,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return proc


def task_exists(name=TASK_NAME):
    """Read-only probe. True only if the scheduler really knows the task."""
    if SCHTASKS is None:
        return False
    probe = subprocess.run(
        [SCHTASKS, "/Query", "/TN", name],
        capture_output=True, text=True, timeout=60,
    )
    return probe.returncode == 0


def service_exists(name="cloudflared"):
    probe = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         f"(Get-Service -Name {name} -ErrorAction SilentlyContinue) -ne $null"],
        capture_output=True, text=True, timeout=30,
    )
    return probe.stdout.strip() == "True"


@pytest.fixture(autouse=True)
def never_mutates_the_machine():
    """Nothing in this module may leave a scheduled task or service behind."""
    task_before = task_exists()
    service_before = service_exists()
    yield
    assert task_exists() == task_before, (
        "a -DryRun test registered/removed the real scheduled task"
    )
    assert service_exists() == service_before, (
        "a -DryRun test touched the real cloudflared service"
    )


# ---------------------------------------------------------------------------
# files exist
# ---------------------------------------------------------------------------

def test_scripts_and_readme_exist():
    assert INSTALL_SCRIPT.is_file()
    assert UNINSTALL_SCRIPT.is_file()
    assert README.is_file()


def test_no_double_ampersand_powershell_51_compat():
    # && is not valid PowerShell 5.1 syntax; the task requires 5.1 compat.
    for script in (INSTALL_SCRIPT, UNINSTALL_SCRIPT):
        text = script.read_text(encoding="utf-8")
        assert "&&" not in text, f"{script.name} uses && (not PowerShell 5.1 compatible)"


# ---------------------------------------------------------------------------
# install_tunnel.ps1 -DryRun
# ---------------------------------------------------------------------------

def test_dryrun_runs_cleanly_and_touches_nothing():
    proc = run_script(INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-tunnel-id-1234")
    assert proc.returncode == 0, proc.stderr
    assert "DRYRUN" in proc.stdout
    assert "nothing was executed" in proc.stdout


def test_dryrun_reports_prerequisites():
    proc = run_script(INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-tunnel-id-1234")
    out = proc.stdout
    assert "cloudflared present" in out
    assert "cert.pem present" in out
    assert "on Cloudflare NS" in out


def test_dryrun_plans_tunnel_create_and_route_dns():
    proc = run_script(INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-tunnel-id-1234")
    out = proc.stdout
    assert f"tunnel list -o json" in out
    assert f"tunnel create {TUNNEL_NAME}" in out
    assert f"tunnel route dns {TUNNEL_NAME} {HOSTNAME}" in out


def test_dryrun_plans_service_install_and_start():
    proc = run_script(INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-tunnel-id-1234")
    out = proc.stdout
    assert "service install" in out
    assert "Start-Service -Name cloudflared" in out


def test_dryrun_plans_the_scheduled_task():
    proc = run_script(INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-tunnel-id-1234")
    out = proc.stdout
    assert TASK_NAME in out
    assert "AtLogOn" in out
    assert "RestartCount=3" in out
    assert "signal_hub.webhook_receiver" in out
    assert "SIGNAL_HUB_GH_WEBHOOK_SECRET" in out
    # the secret must never be embedded as a value, only named as a source
    assert "never written into the task definition" in out


def test_dryrun_plans_health_verification():
    proc = run_script(INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-tunnel-id-1234")
    out = proc.stdout
    assert "/healthz -> 200" in out
    assert f"https://{HOSTNAME}/healthz" in out
    assert "http://127.0.0.1:8787/healthz" in out


def test_dryrun_renders_exact_config_yaml_for_a_fake_tunnel_id():
    proc = run_script(INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-tunnel-id-1234")
    out = proc.stdout
    assert "tunnel: fake-tunnel-id-1234" in out
    assert "credentials-file:" in out
    assert "fake-tunnel-id-1234.json" in out
    assert "- hostname: hooks.dynastywrapped.com" in out
    assert "service: http://127.0.0.1:8787" in out
    assert "- service: http_status:404" in out
    # ingress order: the specific hostname rule before the catch-all
    hostname_idx = out.index("- hostname: hooks.dynastywrapped.com")
    catchall_idx = out.index("- service: http_status:404")
    assert hostname_idx < catchall_idx


def test_dryrun_respects_local_port_and_hostname_overrides():
    proc = run_script(
        INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-id",
        "-LocalPort", "9999", "-Hostname", "other.example.com",
    )
    out = proc.stdout
    assert "service: http://127.0.0.1:9999" in out
    assert "- hostname: other.example.com" in out


def test_dryrun_never_prints_a_literal_secret_value():
    """The script must only ever name the env var, never a value for it."""
    import os

    proc = run_script(
        INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-id",
        "-SecretEnvVar", "SIGNAL_HUB_GH_WEBHOOK_SECRET",
    )
    out = proc.stdout
    # The script reads $env:SIGNAL_HUB_GH_WEBHOOK_SECRET at runtime; it must
    # never shell out to echo its current value during a preview.
    current = os.environ.get("SIGNAL_HUB_GH_WEBHOOK_SECRET")
    if current:
        assert current not in out


def test_a_quote_character_in_repo_root_is_rejected():
    proc = run_script(
        INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-id",
        "-RepoRoot", "C:\\we'ird",
    )
    assert proc.returncode == 1
    assert "quote" in (proc.stdout + proc.stderr).lower()


def test_dryrun_is_idempotent_output():
    first = run_script(INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-tunnel-id-1234").stdout
    second = run_script(INSTALL_SCRIPT, "-DryRun", "-TunnelId", "fake-tunnel-id-1234").stdout
    assert first == second


# ---------------------------------------------------------------------------
# uninstall_tunnel.ps1 -DryRun
# ---------------------------------------------------------------------------

def test_uninstall_dryrun_runs_cleanly():
    proc = run_script(UNINSTALL_SCRIPT, "-DryRun")
    assert proc.returncode == 0, proc.stderr
    assert "DRYRUN" in proc.stdout
    assert "nothing was executed" in proc.stdout


def test_uninstall_dryrun_never_touches_the_queue():
    proc = run_script(UNINSTALL_SCRIPT, "-DryRun")
    out = proc.stdout
    assert "signal-hub-queue.jsonl" in out
    assert "never touched" in out


def test_uninstall_default_leaves_tunnel_and_config_in_place():
    proc = run_script(UNINSTALL_SCRIPT, "-DryRun")
    out = proc.stdout
    assert "left in place" in out
    assert "tunnel delete" not in out


def test_uninstall_purge_plans_tunnel_delete_and_config_removal():
    proc = run_script(UNINSTALL_SCRIPT, "-DryRun", "-Purge")
    out = proc.stdout
    assert f"tunnel delete {TUNNEL_NAME}" in out
    assert "config.yml" in out
    # even with -Purge, DNS/registrar and the queue stay untouched
    assert "NOT reverted" in out
    assert "signal-hub-queue.jsonl" in out


def test_uninstall_reports_whether_task_and_service_exist():
    proc = run_script(UNINSTALL_SCRIPT, "-DryRun")
    out = proc.stdout
    assert TASK_NAME in out
    assert "cloudflared" in out


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

def test_readme_documents_the_two_human_steps_and_one_command():
    text = README.read_text(encoding="utf-8")
    assert "dynastywrapped.com" in text
    assert "GoDaddy" in text
    assert "cloudflared tunnel login" in text
    assert "install_tunnel.ps1" in text
    assert "-DryRun" in text
