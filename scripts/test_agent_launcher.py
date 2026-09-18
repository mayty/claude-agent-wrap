# This file has been created with the assistance of an AI tool.
"""
Tests for bin/agent, the POSIX sh launcher.

Driven as a subprocess inside a throwaway checkout: the real launcher, a *stub*
bin/agent-bootstrap, and a fake interpreter that echoes its own argv. Nothing here
downloads a CPython or touches the real .python/ tree, so the provisioning branch is
exercised in milliseconds.
"""

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "agent"

# What the stub prints on stdout, standing in for the real bootstrap's `note` lines.
# The launcher must keep this off its own stdout.
CHATTER = "agent-bootstrap: pretending to download CPython"

# Stands in for bin/requirements.txt. Its content matters only through its hash.
CONSTRAINTS = "click==8.3.0 --hash=sha256:0000\n"

# The suffix the launcher must derive from CONSTRAINTS in sh. Computed here with hashlib
# rather than hardcoded, because hashlib is also what the bootstrap's slug and
# InspectService._deps_current are defined against -- so this is what holds the three
# implementations to one definition.
FRESH_SLUG = f"venv-fake-{hashlib.sha256(CONSTRAINTS.encode()).hexdigest()[:12]}"

# A venv published before the constraints moved, and a dev venv, whose slug carries no
# hash at all because uv.lock rather than bin/requirements.txt owns its contents.
STALE_SLUG = "venv-fake-000000000000"
DEV_SLUG = "venv-fake-dev"

# The stub publishes the pointer and nothing else, mirroring the real bootstrap's
# ordering: `current-venv` is written last, once an install has fully succeeded.
PUBLISH = f"printf '{FRESH_SLUG}\\n' > \"$root/.python/current-venv\""


def write_fake_interpreter(root: Path, slug: str) -> None:
    """Lay down one venv the pointer may name, minus the pointer itself."""
    py = root / ".python" / slug / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text(f"#!/bin/sh\nprintf 'FAKE_PY[{slug}] %s\\n' \"$*\"\n")
    py.chmod(0o755)


def sandbox(
    tmp_path: Path,
    extra: str = PUBLISH,
    *,
    slugs: tuple[str, ...] = (FRESH_SLUG,),
    pointer: str | None = None,
) -> Path:
    """Build a checkout around the real launcher and return its path."""
    agent = tmp_path / "bin" / "agent"
    agent.parent.mkdir()
    shutil.copy2(LAUNCHER, agent)
    (tmp_path / "bin" / "requirements.txt").write_text(CONSTRAINTS)

    bootstrap = tmp_path / "bin" / "agent-bootstrap"
    bootstrap.write_text(
        "#!/bin/sh\n"
        'root=$(cd "$(dirname "$0")/.." && pwd)\n'
        ': > "$root/bootstrap-ran"\n'
        f"printf '{CHATTER}\\n'\n"
        f"{extra}\n"
    )
    bootstrap.chmod(0o755)

    for slug in slugs:
        write_fake_interpreter(tmp_path, slug)
    if pointer is not None:
        (tmp_path / ".python" / "current-venv").write_text(f"{pointer}\n")
    return agent


def run(agent: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(agent), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=60,
    )


def test_launcher_provisions_then_execs_when_unprovisioned(tmp_path: Path) -> None:
    agent = sandbox(tmp_path)

    result = run(agent, "run", "--base")

    assert result.returncode == 0
    assert (tmp_path / "bootstrap-ran").exists()
    assert f"FAKE_PY[{FRESH_SLUG}] -m agent_wrap run --base" in result.stdout


def test_launcher_keeps_bootstrap_chatter_off_its_stdout(tmp_path: Path) -> None:
    agent = sandbox(tmp_path)

    result = run(agent, "--help")

    assert CHATTER not in result.stdout
    assert CHATTER in result.stderr
    assert result.stdout.strip() == f"FAKE_PY[{FRESH_SLUG}] -m agent_wrap --help"


def test_launcher_reports_a_failing_bootstrap(tmp_path: Path) -> None:
    agent = sandbox(tmp_path, extra="exit 3")

    result = run(agent, "--help")

    assert result.returncode == 1
    assert (tmp_path / "bootstrap-ran").exists()
    assert "provisioning failed" in result.stderr
    assert str(tmp_path / "bin" / "agent-bootstrap") in result.stderr
    assert result.stdout == ""


def test_launcher_rejects_a_bootstrap_that_publishes_nothing(tmp_path: Path) -> None:
    agent = sandbox(tmp_path, extra="")

    result = run(agent, "--help")

    assert result.returncode == 1
    assert "still not provisioned" in result.stderr
    assert result.stdout == ""


def test_completion_stays_silent_and_skips_the_bootstrap(tmp_path: Path) -> None:
    """``_AGENT_COMPLETE`` is click's variable; a TAB press must not start a 34MB download."""
    agent = sandbox(tmp_path)

    result = run(agent, _AGENT_COMPLETE="bash_complete")

    assert result.returncode == 0
    assert result.stdout == ""
    assert not (tmp_path / "bootstrap-ran").exists()


def test_launcher_skips_the_bootstrap_when_already_provisioned(tmp_path: Path) -> None:
    agent = sandbox(tmp_path, pointer=FRESH_SLUG)

    result = run(agent, "--help")

    assert result.returncode == 0
    assert not (tmp_path / "bootstrap-ran").exists()
    assert result.stdout.strip() == f"FAKE_PY[{FRESH_SLUG}] -m agent_wrap --help"


def test_launcher_reprovisions_when_the_constraints_moved(tmp_path: Path) -> None:
    """The runnable-but-outdated venv a plain `git pull` leaves behind."""
    agent = sandbox(tmp_path, slugs=(STALE_SLUG, FRESH_SLUG), pointer=STALE_SLUG)

    result = run(agent, "--help")

    assert result.returncode == 0
    assert (tmp_path / "bootstrap-ran").exists()
    assert "dependencies moved" in result.stderr
    assert result.stdout.strip() == f"FAKE_PY[{FRESH_SLUG}] -m agent_wrap --help"


def test_launcher_leaves_a_dev_venv_alone(tmp_path: Path) -> None:
    """A -dev slug can never match the hash, so comparing would republish it away."""
    agent = sandbox(tmp_path, slugs=(DEV_SLUG,), pointer=DEV_SLUG)

    result = run(agent, "--help")

    assert result.returncode == 0
    assert not (tmp_path / "bootstrap-ran").exists()
    assert result.stdout.strip() == f"FAKE_PY[{DEV_SLUG}] -m agent_wrap --help"


def test_launcher_continues_on_the_old_venv_when_reprovisioning_fails(tmp_path: Path) -> None:
    """Unlike a first run, a refresh has a working venv to fall back to."""
    agent = sandbox(tmp_path, extra="exit 3", slugs=(STALE_SLUG,), pointer=STALE_SLUG)

    result = run(agent, "--help")

    assert result.returncode == 0
    assert (tmp_path / "bootstrap-ran").exists()
    assert "re-provisioning failed" in result.stderr
    assert result.stdout.strip() == f"FAKE_PY[{STALE_SLUG}] -m agent_wrap --help"


def test_completion_skips_the_constraints_check(tmp_path: Path) -> None:
    """
    A TAB press hands the stale venv straight to click rather than re-provisioning.

    The row `agent inspect` prints is the place that drift is reported; a completion
    subshell's stderr and exit code are both discarded, so it could not report anything
    and must not spend a bootstrap trying.
    """
    agent = sandbox(tmp_path, slugs=(STALE_SLUG, FRESH_SLUG), pointer=STALE_SLUG)

    result = run(agent, _AGENT_COMPLETE="bash_complete")

    assert result.returncode == 0
    assert not (tmp_path / "bootstrap-ran").exists()
    assert result.stdout.strip() == f"FAKE_PY[{STALE_SLUG}] -m agent_wrap"
