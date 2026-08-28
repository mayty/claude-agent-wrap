# This file has been edited with the assistance of an AI tool.
"""Tests for the `inspect` CLI command — argument parsing and service protocol."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

import pytest

from agent_wrap.cli.constants import COMMANDS
from agent_wrap.cli.inspect.run import build_parser, run
from agent_wrap.containers import services
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.status.models import (
    AgentRow,
    DockerStatus,
    EnvironmentRow,
    InspectReport,
    ProjectImageRow,
    ProviderRow,
    SidecarRow,
    StorageRow,
    ViewerRow,
    WrapperRow,
)

if TYPE_CHECKING:
    from unittest.mock import Mock

    import pytest_mock

_SIDECAR = SidecarRow(
    name="agent-wrap-litellm-bedrock",
    role="litellm",
    provider="litellm-bedrock",
    status="running",
    health="healthy",
    uptime_sec=11520,
    port=48620,
    exit_code=0,
    image=(
        "ghcr.io/berriai/litellm:v1.96.2"
        "@sha256:154e23bb5f31b1f10e16392a8ef299bd2cde08de3a64a6849002cfcc25ce3c63"
    ),
    stale_image=False,
    networks=["agent-wrap-net"],
    attached_agents=1,
)

_AGENT = AgentRow(
    name="claude-agent-wrap-abc",
    instance_id="wrap-abc",
    status="running",
    uptime_sec=1320,
    cwd="/home/me/agent-wrap",
    image="claude-agent-wrap",
    provider="litellm-bedrock",
    sidecars=["agent-wrap-litellm-bedrock"],
)


_PROJECT = ProjectImageRow(
    image="claude-agent-wrap",
    dockerfile="/home/me/agent-wrap/.claude-agent-wrap/Dockerfile",
    is_legacy=False,
    present=True,
    claude_version="2.0.50",
    claude_update_available=False,
)


def _report(  # noqa: PLR0913
    *,
    docker_available: bool = True,
    queued: list[str] | None = None,
    day_start_hours: int = -3,
    day_start_overridden: bool = True,
    day_start_timezone: str | None = None,
    project: ProjectImageRow | None = None,
    lite: bool = False,
    logs_bytes: int | None = 1_033_465_471,
    warnings: list[str] | None = None,
    viewer: ViewerRow | None = None,
) -> InspectReport:
    return InspectReport(
        docker=DockerStatus(
            available=docker_available, error="" if docker_available else "no docker"
        ),
        sidecars=[_SIDECAR] if docker_available else [],
        agents=[_AGENT] if docker_available else [],
        queued_launches=queued or [],
        viewer=viewer
        or ViewerRow(
            running=True,
            pid=41233,
            port=8765,
            starting=False,
            connect_line="LiteLLM log viewer running at http://127.0.0.1:8765",
            log_size=42_000,
            log_mtime=1_700_000_000.0,
        ),
        providers=[
            ProviderRow(name="litellm-bedrock", is_default=True, secrets_ok=True, missing_keys=[]),
            ProviderRow(
                name="telegram",
                is_default=False,
                secrets_ok=False,
                missing_keys=["telegram:TelegramBotToken"],
            ),
        ],
        wrapper=WrapperRow(branch="master", commit="7e8ef2f", describe="0.8.0", dirty=False),
        environment=EnvironmentRow(
            base_image="claude-agent",
            base_image_present=True,
            base_image_version="2.0.50",
            latest_claude_version=None,
            claude_update_available=False,
            network_name="agent-wrap-net",
            network_present=True,
            host_network_requested=False,
            host_network_effective=False,
            day_start_hours=day_start_hours,
            day_start_overridden=day_start_overridden,
            day_start_timezone=day_start_timezone,
        ),
        storage=StorageRow(logs_bytes=logs_bytes, projects_registered=24, projects_stale=2),
        project=project,
        lite=lite,
        warnings=warnings or [],
    )


@pytest.fixture
def inspect_mock() -> Mock:
    mock = services.inspect_service
    mock.build_report.return_value = _report()  # pyrefly: ignore [missing-attribute]
    return mock


@pytest.fixture
def display_mock_service(mocker: pytest_mock.MockFixture) -> Mock:
    """Return the mocked display service with real formatters and a pass-through spinner."""
    dsp = mocker.Mock(spec=DisplayService, wraps=DisplayService())
    dsp.spin_while.side_effect = lambda **kw: kw["work"]()  # pyrefly: ignore [implicit-any-lambda]
    mocker.patch.object(services, "display_service", dsp)
    return dsp


def _lines(dsp: Mock) -> list[str]:
    return [str(call.args[0]) for call in dsp.info.call_args_list]


def _stdout(dsp: Mock) -> str:
    return "\n".join(_lines(dsp))


def _details_lines(dsp: Mock) -> list[str]:
    """Return the rendered details table, from its title to the end of the output."""
    lines = _lines(dsp)
    start = next(i for i, line in enumerate(lines) if line.startswith("Details:"))
    return lines[start:]


# --- parsing ---


def test_parser_defaults_to_human_output() -> None:
    assert build_parser().parse_args([]).as_json is False


def test_parser_accepts_json_flag() -> None:
    assert build_parser().parse_args(["--json"]).as_json is True


def test_parser_accepts_j_flag() -> None:
    """-j is the shorthand for --json."""
    assert build_parser().parse_args(["-j"]).as_json is True


def test_parser_defaults_to_the_full_report() -> None:
    assert build_parser().parse_args([]).lite is False


def test_parser_accepts_lite_flag() -> None:
    assert build_parser().parse_args(["--lite"]).lite is True


def test_parser_accepts_l_flag() -> None:
    """-l is the shorthand for --lite."""
    assert build_parser().parse_args(["-l"]).lite is True


def test_parser_combines_lite_and_json() -> None:
    ns = build_parser().parse_args(["-j", "-l"])
    assert (ns.as_json, ns.lite) == (True, True)


def test_help_returns_zero() -> None:
    assert run(["--help"]) == 0


def test_unknown_flag_reports_the_reason(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["--bogus"]) == 1
    assert "unrecognized arguments" in capsys.readouterr().err


def test_positional_argument_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["extra"]) == 1
    assert "unrecognized arguments" in capsys.readouterr().err


# --- service protocol ---


@pytest.mark.usefixtures("display_mock_service")
def test_run_builds_the_report_once(inspect_mock: Mock) -> None:
    assert run([]) == 0
    inspect_mock.build_report.assert_called_once_with(lite=False)


@pytest.mark.usefixtures("display_mock_service")
def test_run_forwards_the_lite_flag(inspect_mock: Mock) -> None:
    assert run(["--lite"]) == 0
    inspect_mock.build_report.assert_called_once_with(lite=True)


def test_json_run_forwards_the_lite_flag(inspect_mock: Mock, display_mock_service: Mock) -> None:
    """The spinner-free path takes the flag too — it is a separate call site."""
    del display_mock_service
    assert run(["--json", "--lite"]) == 0
    inspect_mock.build_report.assert_called_once_with(lite=True)


@pytest.mark.usefixtures("inspect_mock")
def test_run_shows_a_spinner_while_collecting(display_mock_service: Mock) -> None:
    run([])
    display_mock_service.spin_while.assert_called_once()


@pytest.mark.usefixtures("inspect_mock")
def test_json_mode_uses_no_spinner(display_mock_service: Mock) -> None:
    """The spinner animates on stdout, which would corrupt the JSON document."""
    run(["--json"])
    display_mock_service.spin_while.assert_not_called()


# --- human output ---


@pytest.mark.usefixtures("inspect_mock")
def test_human_output_lists_sidecars_and_agents(display_mock_service: Mock) -> None:
    run([])
    out = _stdout(display_mock_service)
    assert "agent-wrap-litellm-bedrock" in out
    assert "48620" in out
    assert "claude-agent-wrap" in out
    assert "/home/me/agent-wrap" in out


@pytest.mark.usefixtures("inspect_mock")
def test_sidecar_table_column_order(display_mock_service: Mock) -> None:
    run([])
    header = next(line for line in _lines(display_mock_service) if "CONTAINER" in line)
    assert [cell.strip() for cell in header.strip("│").split("│")] == [
        "CONTAINER",
        "ROLE",
        "IMAGE",
        "STATUS",
        "HEALTH",
        "UPTIME",
        "PORT",
        "AGENTS",
    ]


@pytest.mark.usefixtures("inspect_mock")
def test_sidecar_image_drops_the_registry_and_digest(display_mock_service: Mock) -> None:
    """A digest-pinned reference is ~110 chars and would wrap the row."""
    run([])
    out = _stdout(display_mock_service)
    assert "litellm:v1.96.2" in out
    assert "sha256" not in out
    assert "ghcr.io" not in out


@pytest.mark.usefixtures("inspect_mock")
def test_agent_table_column_order(display_mock_service: Mock) -> None:
    run([])
    header = next(
        line for line in _lines(display_mock_service) if "IMAGE" in line and "PROVIDER" in line
    )
    assert [cell.strip() for cell in header.strip("│").split("│")] == [
        "IMAGE",
        "CWD",
        "PROVIDER",
        "STATUS",
        "UPTIME",
    ]


@pytest.mark.usefixtures("inspect_mock")
def test_agent_row_shows_the_provider_not_the_container_names(
    display_mock_service: Mock,
) -> None:
    """The sidecar container names are already listed in the table above."""
    run([])
    out = _stdout(display_mock_service)
    assert "│ litellm-bedrock" in out
    assert "SIDECARS" not in out


@pytest.mark.usefixtures("inspect_mock")
def test_human_output_includes_every_details_row(display_mock_service: Mock) -> None:
    run([])
    out = _stdout(display_mock_service)
    for label in (
        "logs viewer",
        "logs storage",
        "wrapper",
        "base image",
        "network",
        "host network",
        "day boundary",
    ):
        assert label in out


def test_human_output_reports_a_starting_viewer_without_a_connect_line(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    """A viewer that has not bound its port yet has no address to advertise."""
    inspect_mock.build_report.return_value = _report(
        viewer=ViewerRow(
            running=True,
            pid=41233,
            port=8765,
            starting=True,
            connect_line="",
            log_size=42_000,
            log_mtime=1_700_000_000.0,
        )
    )
    run([])
    out = _stdout(display_mock_service)
    assert "starting" in out
    assert "http://127.0.0.1" not in out


@pytest.mark.usefixtures("inspect_mock")
def test_human_output_shows_base_image_version(display_mock_service: Mock) -> None:
    run([])
    out = _stdout(display_mock_service)
    assert "claude-agent present (Claude Code v2.0.50)" in out


def test_human_output_shows_the_project_image(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    inspect_mock.build_report.return_value = _report(project=_PROJECT)
    run([])
    out = _stdout(display_mock_service)
    assert "project image" in out
    assert "claude-agent-wrap present (Claude Code v2.0.50)" in out


@pytest.mark.usefixtures("inspect_mock")
def test_human_output_omits_the_project_row_without_a_project_image(
    display_mock_service: Mock,
) -> None:
    """A project that customizes nothing has nothing to say here."""
    run([])
    assert "project image" not in _stdout(display_mock_service)


def test_human_output_flags_a_project_image_that_was_never_built(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    inspect_mock.build_report.return_value = _report(
        project=dataclasses.replace(_PROJECT, present=False, claude_version=None)
    )
    run([])
    out = _stdout(display_mock_service)
    assert "claude-agent-wrap MISSING (run `agent rebuild`)" in out


def test_human_output_flags_a_stale_project_image(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    """Both image rows name the same available version — there is one registry answer."""
    report = _report(project=dataclasses.replace(_PROJECT, claude_update_available=True))
    inspect_mock.build_report.return_value = dataclasses.replace(
        report,
        environment=dataclasses.replace(
            report.environment, latest_claude_version="2.0.51", claude_update_available=True
        ),
    )
    run([])
    out = _stdout(display_mock_service)
    assert "claude-agent-wrap present (Claude Code v2.0.50) → v2.0.51 available" in out


def test_human_output_never_claims_an_update_it_did_not_check_for(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    """In lite mode the latest version is unknown, and unknown must not read as stale."""
    inspect_mock.build_report.return_value = _report(lite=True, logs_bytes=None, project=_PROJECT)
    run([])
    assert "available" not in _stdout(display_mock_service)


def test_human_output_notes_a_legacy_project_dockerfile(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    inspect_mock.build_report.return_value = _report(
        project=dataclasses.replace(_PROJECT, is_legacy=True)
    )
    run([])
    assert "deprecated Dockerfile.agent" in _stdout(display_mock_service)


def test_human_output_marks_an_unmeasured_logs_footprint(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    """A blank cell beside the project count would read as zero bytes."""
    inspect_mock.build_report.return_value = _report(lite=True, logs_bytes=None)
    run([])
    out = _stdout(display_mock_service)
    assert "not measured (--lite)" in out
    assert "24 project(s) registered" in out


def test_human_output_closes_a_lite_report_with_what_it_skipped(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    inspect_mock.build_report.return_value = _report(lite=True, logs_bytes=None)
    run([])
    out = _stdout(display_mock_service)
    assert "npm-registry version check" in out
    assert "logs-size walk" in out


@pytest.mark.usefixtures("inspect_mock")
def test_human_output_has_no_lite_note_in_the_full_report(display_mock_service: Mock) -> None:
    run([])
    assert "--lite" not in _stdout(display_mock_service)


def test_human_output_prints_collection_warnings(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    inspect_mock.build_report.return_value = _report(warnings=["Error: both Dockerfiles exist"])
    run([])
    assert "warning: Error: both Dockerfiles exist" in _stdout(display_mock_service)


def test_human_output_flags_an_available_update(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    report = _report()
    report = dataclasses.replace(
        report,
        environment=dataclasses.replace(
            report.environment,
            latest_claude_version="2.0.51",
            claude_update_available=True,
        ),
    )
    inspect_mock.build_report.return_value = report
    run([])
    out = _stdout(display_mock_service)
    assert "claude-agent present (Claude Code v2.0.50) → v2.0.51 available" in out


def test_human_output_shows_no_update_when_current(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    """A current version must not be flagged — that is the steady state."""
    report = _report()
    report = dataclasses.replace(
        report,
        environment=dataclasses.replace(
            report.environment,
            latest_claude_version="2.0.50",
            claude_update_available=False,
        ),
    )
    inspect_mock.build_report.return_value = report
    run([])
    out = _stdout(display_mock_service)
    assert "→" not in out


def test_human_output_omits_version_when_none(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    """A failed version probe degrades to the plain 'present' row."""
    report = _report()
    report = dataclasses.replace(
        report,
        environment=dataclasses.replace(report.environment, base_image_version=None),
    )
    inspect_mock.build_report.return_value = report
    run([])
    out = _stdout(display_mock_service)
    assert "claude-agent present" in out
    assert "Claude Code v" not in out


@pytest.mark.usefixtures("inspect_mock")
def test_json_output_includes_base_image_version(display_mock_service: Mock) -> None:
    run(["--json"])
    payload = json.loads(_stdout(display_mock_service))
    assert payload["environment"]["base_image_version"] == "2.0.50"


def test_json_output_includes_update_fields(inspect_mock: Mock, display_mock_service: Mock) -> None:
    report = _report()
    report = dataclasses.replace(
        report,
        environment=dataclasses.replace(
            report.environment,
            latest_claude_version="2.0.51",
            claude_update_available=True,
        ),
    )
    inspect_mock.build_report.return_value = report
    run(["--json"])
    payload = json.loads(_stdout(display_mock_service))
    env = payload["environment"]
    assert env["latest_claude_version"] == "2.0.51"
    assert env["claude_update_available"] is True


@pytest.mark.usefixtures("inspect_mock")
def test_details_table_divides_its_three_groups(display_mock_service: Mock) -> None:
    """Logs, secrets, and wrapper facts are separate concerns, not one run-on block."""
    run([])
    rules = [line for line in _details_lines(display_mock_service) if line.startswith("├")]
    assert len(rules) == 3  # the header rule, plus one divider between each pair of groups


@pytest.mark.usefixtures("inspect_mock")
def test_human_output_formats_uptime(display_mock_service: Mock) -> None:
    run([])
    assert "3h 12m" in _stdout(display_mock_service)


@pytest.mark.usefixtures("inspect_mock")
def test_human_output_reports_secret_readiness_as_two_states(display_mock_service: Mock) -> None:
    run([])
    out = _stdout(display_mock_service)
    assert "Secrets OK" in out
    assert "Secrets NOT SET" in out


@pytest.mark.usefixtures("inspect_mock")
def test_human_output_omits_the_missing_secret_names(display_mock_service: Mock) -> None:
    """`agent secrets check` names the keys; here they only pad the row."""
    run([])
    assert "telegram:TelegramBotToken" not in _stdout(display_mock_service)


@pytest.mark.usefixtures("inspect_mock")
def test_json_output_still_names_the_missing_secrets(display_mock_service: Mock) -> None:
    """Dropping the keys is a table decision, not a loss of information."""
    run(["--json"])
    payload = json.loads(_stdout(display_mock_service))
    assert payload["providers"][1]["missing_keys"] == ["telegram:TelegramBotToken"]


@pytest.mark.usefixtures("inspect_mock")
def test_day_boundary_states_the_offset_against_utc(display_mock_service: Mock) -> None:
    run([])
    assert "-3h UTC (AGENT_DAY_START_UTC)" in _stdout(display_mock_service)


def test_day_boundary_drops_the_sign_at_zero(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    inspect_mock.build_report.return_value = _report(day_start_hours=0, day_start_overridden=False)
    run([])
    out = _stdout(display_mock_service)
    assert "0h UTC" in out
    assert "+0h" not in out


def test_day_boundary_notes_agent_timezone_when_that_is_the_source(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    inspect_mock.build_report.return_value = _report(
        day_start_hours=-2, day_start_overridden=False, day_start_timezone="Europe/Warsaw"
    )
    run([])
    assert "-2h UTC (AGENT_TIMEZONE=Europe/Warsaw)" in _stdout(display_mock_service)


@pytest.mark.usefixtures("inspect_mock")
def test_human_output_reports_no_token_usage(display_mock_service: Mock) -> None:
    """`agent stats` owns usage; this command must not half-answer it."""
    run([])
    out = _stdout(display_mock_service)
    assert "Today" not in out
    assert "usage" not in out.lower()


def test_queued_launches_footnote_follows_the_sidecar_table(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    inspect_mock.build_report.return_value = _report(queued=["other-xyz"])
    run([])
    lines = _lines(display_mock_service)
    footnote = next(i for i, line in enumerate(lines) if "awaiting the sidecar lock" in line)
    agents_title = next(i for i, line in enumerate(lines) if line.startswith("Agents ("))
    assert footnote < agents_title


def test_human_output_never_prescribes_cleanup(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    """Staleness is only answerable from the host; acting on it deletes live logs."""
    inspect_mock.build_report.return_value = _report()
    run([])
    assert "agent cleanup" not in _stdout(display_mock_service)


# --- json output ---


@pytest.mark.usefixtures("inspect_mock")
def test_json_output_parses(display_mock_service: Mock) -> None:
    run(["--json"])
    payload = json.loads(_stdout(display_mock_service))
    assert payload["sidecars"][0]["port"] == 48620
    assert payload["agents"][0]["instance_id"] == "wrap-abc"


@pytest.mark.usefixtures("inspect_mock")
def test_json_output_carries_no_secret_values(display_mock_service: Mock) -> None:
    run(["--json"])
    out = _stdout(display_mock_service)
    for forbidden in ("LITELLM_MASTER_KEY", "AWS_BEARER_TOKEN", "TELEGRAM_BOT_TOKEN", "sk-"):
        assert forbidden not in out


def test_json_output_carries_the_lite_marker_and_nulls(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    """A consumer must be able to tell "not measured" from "measured as zero"."""
    inspect_mock.build_report.return_value = _report(lite=True, logs_bytes=None)
    run(["--json", "--lite"])
    payload = json.loads(_stdout(display_mock_service))
    assert payload["lite"] is True
    assert payload["storage"]["logs_bytes"] is None
    assert payload["environment"]["latest_claude_version"] is None


def test_json_output_carries_the_project_image(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    inspect_mock.build_report.return_value = _report(project=_PROJECT)
    run(["--json"])
    payload = json.loads(_stdout(display_mock_service))
    assert payload["project"]["image"] == "claude-agent-wrap"
    assert payload["project"]["claude_version"] == "2.0.50"


@pytest.mark.usefixtures("inspect_mock")
def test_json_output_nulls_the_project_when_there_is_none(display_mock_service: Mock) -> None:
    run(["--json"])
    assert json.loads(_stdout(display_mock_service))["project"] is None


def test_json_output_still_parses_when_docker_is_down(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    """A degraded report must stay machine-readable, not truncate."""
    inspect_mock.build_report.return_value = _report(docker_available=False)
    assert run(["--json"]) == 1
    payload = json.loads(_stdout(display_mock_service))
    assert payload["docker"]["available"] is False
    assert payload["docker"]["error"]


# --- exit codes ---


@pytest.mark.usefixtures("display_mock_service")
def test_exit_zero_when_docker_is_up(inspect_mock: Mock) -> None:
    del inspect_mock
    assert run([]) == 0


def test_exit_one_when_docker_is_down(inspect_mock: Mock, display_mock_service: Mock) -> None:
    inspect_mock.build_report.return_value = _report(docker_available=False)
    assert run([]) == 1
    assert "no docker" in _stdout(display_mock_service)


def test_docker_down_still_prints_filesystem_sections(
    inspect_mock: Mock, display_mock_service: Mock
) -> None:
    inspect_mock.build_report.return_value = _report(docker_available=False)
    run([])
    out = _stdout(display_mock_service)
    assert "wrapper" in out
    assert "logs storage" in out


# --- completion ---


def test_completion_offers_the_json_flag() -> None:
    _run_fn, complete_fn = COMMANDS["inspect"]
    assert "--json" in complete_fn(2, ["agent", "inspect", ""])
    assert "-j" in complete_fn(2, ["agent", "inspect", ""])


def test_completion_offers_the_lite_flag() -> None:
    _run_fn, complete_fn = COMMANDS["inspect"]
    assert "--lite" in complete_fn(2, ["agent", "inspect", ""])
    assert "-l" in complete_fn(2, ["agent", "inspect", ""])


def test_completion_omits_an_already_used_flag() -> None:
    _run_fn, complete_fn = COMMANDS["inspect"]
    assert "--json" not in complete_fn(3, ["agent", "inspect", "--json", ""])


def test_completion_omits_an_already_used_lite_flag() -> None:
    """Both spellings hang off one action, so either present excludes the pair."""
    _run_fn, complete_fn = COMMANDS["inspect"]
    offered = complete_fn(3, ["agent", "inspect", "-l", ""])
    assert "--lite" not in offered
    assert "--json" in offered
