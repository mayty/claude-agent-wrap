# This file has been created with the assistance of an AI tool.
"""
Tests for bin/agent, the POSIX sh launcher.

Driven as a subprocess inside a throwaway checkout: the real launcher, a *stub*
bin/agent-bootstrap, and a fake interpreter that echoes its own argv. Nothing here
downloads a CPython or touches the real .python/ tree, so the provisioning branch is
exercised in milliseconds.
"""

import os
import shutil
import subprocess
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "agent"

# What the stub prints on stdout, standing in for the real bootstrap's `note` lines.
# The launcher must keep this off its own stdout.
CHATTER = "agent-bootstrap: pretending to download CPython"

# The stub publishes the pointer and nothing else, mirroring the real bootstrap's
# ordering: `current-venv` is written last, once an install has fully succeeded.
PUBLISH = "printf 'venv-fake\\n' > \"$root/.python/current-venv\""


def write_fake_interpreter(root: Path) -> None:
    """Lay down the venv the pointer will name, minus the pointer itself."""
    py = root / ".python" / "venv-fake" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nprintf 'FAKE_PY %s\\n' \"$*\"\n")
    py.chmod(0o755)


def sandbox(tmp_path: Path, extra: str = PUBLISH) -> Path:
    """Build a checkout around the real launcher and return its path."""
    agent = tmp_path / "bin" / "agent"
    agent.parent.mkdir()
    shutil.copy2(LAUNCHER, agent)

    bootstrap = tmp_path / "bin" / "agent-bootstrap"
    bootstrap.write_text(
        "#!/bin/sh\n"
        'root=$(cd "$(dirname "$0")/.." && pwd)\n'
        ': > "$root/bootstrap-ran"\n'
        f"printf '{CHATTER}\\n'\n"
        f"{extra}\n"
    )
    bootstrap.chmod(0o755)

    write_fake_interpreter(tmp_path)
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
    assert "FAKE_PY -m agent_wrap run --base" in result.stdout


def test_launcher_keeps_bootstrap_chatter_off_its_stdout(tmp_path: Path) -> None:
    agent = sandbox(tmp_path)

    result = run(agent, "--help")

    assert CHATTER not in result.stdout
    assert CHATTER in result.stderr
    assert result.stdout.strip() == "FAKE_PY -m agent_wrap --help"


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
    agent = sandbox(tmp_path)

    result = run(agent, "1", "agent", AGENT_COMPLETE="1")

    assert result.returncode == 0
    assert result.stdout == ""
    assert not (tmp_path / "bootstrap-ran").exists()


def test_launcher_skips_the_bootstrap_when_already_provisioned(tmp_path: Path) -> None:
    agent = sandbox(tmp_path)
    (tmp_path / ".python" / "current-venv").write_text("venv-fake\n")

    result = run(agent, "--help")

    assert result.returncode == 0
    assert not (tmp_path / "bootstrap-ran").exists()
    assert result.stdout.strip() == "FAKE_PY -m agent_wrap --help"
