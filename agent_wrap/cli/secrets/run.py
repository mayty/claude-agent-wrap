# This file has been edited with the assistance of an AI tool.
"""``agent secrets check|set|clear <sidecar>`` and ``agent secrets cleanup``."""

from typing import TYPE_CHECKING

import click

from agent_wrap.cli.secrets.constants import (
    NO_SIDECAR,
    SECRETS_CHECK_TABLE,
    SECRETS_CHECK_TITLE,
    SECRETS_CLEANUP_TITLE,
    SECRETS_CLEAR_TITLE,
    SECRETS_REMOVED_TABLE,
    STATE_MISSING,
    STATE_OK,
    STATE_REMOVED,
)
from agent_wrap.containers import services
from agent_wrap.domain.display.constants import Style
from agent_wrap.domain.display.models import RowItem

if TYPE_CHECKING:
    from rich.console import Group

    from agent_wrap.domain.display.models import RowItemOrDivider
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.secrets.models import SecretsCheckReport


def complete_sidecar(
    ctx: click.Context,  # noqa: ARG001
    param: click.Parameter,  # noqa: ARG001
    incomplete: str,
) -> list[str]:
    """Complete the ``<sidecar>`` argument from the sidecars the wrapper knows about."""
    return [
        name for name in services.secrets_service.known_sidecars() if name.startswith(incomplete)
    ]


@click.group("secrets")
def secrets_group() -> None:
    """> Manage sidecar secrets"""


class _SecretsReport:
    """What `agent secrets` prints once the store has answered."""

    @staticmethod
    def _sidecar_and_key(namespaced: str) -> tuple[str, str]:
        sidecar, sep, key = namespaced.partition(":")
        return (sidecar, key) if sep else (NO_SIDECAR, namespaced)

    @staticmethod
    def check_table(reports: dict[str, SecretsCheckReport], dsp: DisplayService) -> Group:
        """
        Render one row per required secret, in declaration order within each sidecar.

        An absent key leaves LENGTH and HINT empty rather than showing a zero and an empty
        mask. A secret stored as the empty string is present, at length 0 with a ``***``
        hint, and the two readings must not render alike.
        """
        body: list[RowItemOrDivider] = []
        for report in reports.values():
            for namespaced, entry in report.entries.items():
                sidecar, key = _SecretsReport._sidecar_and_key(namespaced)
                cells = (
                    [sidecar, key, STATE_OK, str(entry.length), entry.hint]
                    if entry.present
                    else [sidecar, key, STATE_MISSING, "", ""]
                )
                body.append(
                    RowItem(
                        cells=cells,
                        style=Style.BOLD_GREEN if entry.present else Style.DIM,
                        prefix_len=0,
                    )
                )
        return dsp.render_table(SECRETS_CHECK_TITLE, SECRETS_CHECK_TABLE, body)

    @staticmethod
    def removed_table(title: str, removed: list[str], dsp: DisplayService) -> Group:
        body: list[RowItemOrDivider] = [
            RowItem(
                cells=[*_SecretsReport._sidecar_and_key(key), STATE_REMOVED],
                style=Style.BOLD_YELLOW,
                prefix_len=0,
            )
            for key in removed
        ]
        return dsp.render_table(title, SECRETS_REMOVED_TABLE, body)


class _SecretsChecks:
    @staticmethod
    def one_sidecar(dsp: DisplayService, sidecar: str) -> int:
        report = services.secrets_service.check_secrets(sidecar)
        if report.declares_none:
            dsp.info(f"Sidecar '{sidecar}' declares no secrets.")
            return 0

        dsp.show(_SecretsReport.check_table({sidecar: report}, dsp))
        if report.all_present:
            return 0

        missing = sum(1 for entry in report.entries.values() if not entry.present)
        dsp.error(
            f"{missing} of {len(report.entries)} secrets missing for "
            f"'{sidecar}'\nRun 'agent secrets set {sidecar}' to set them."
        )
        return 1

    @staticmethod
    def every_sidecar(dsp: DisplayService) -> int:
        """Skip the table when no sidecar has rows: a header over nothing reads as broken."""
        reports = services.secrets_service.check_all_sidecars()
        if any(report.entries for report in reports.values()):
            dsp.show(_SecretsReport.check_table(reports, dsp))

        silent = [name for name, report in reports.items() if report.declares_none]
        if silent:
            dsp.info(f"Declares no secrets: {', '.join(silent)}.")

        failing = {
            name: sum(1 for entry in report.entries.values() if not entry.present)
            for name, report in reports.items()
            if not report.all_present
        }
        if not failing:
            return 0

        missing, sidecars = sum(failing.values()), len(failing)
        dsp.error(
            f"{missing} secret{'' if missing == 1 else 's'} missing across "
            f"{sidecars} sidecar{'' if sidecars == 1 else 's'}: {', '.join(failing)}\n"
            f"Run 'agent secrets set <sidecar>' to set them."
        )
        return 1


@secrets_group.command("check")
@click.argument("sidecar", required=False, shell_complete=complete_sidecar)
@click.pass_context
def secrets_check(ctx: click.Context, sidecar: str | None) -> None:
    """
    Report which of a sidecar's secrets are present

    Print a table of SIDECAR's required secrets: the key, whether it is present, and the
    length and a masked hint of each present one. The value itself is never printed.
    Omit SIDECAR to table every sidecar the wrapper knows about at once.
    """
    dsp = services.display_service
    ctx.exit(
        _SecretsChecks.one_sidecar(dsp, sidecar)
        if sidecar is not None
        else _SecretsChecks.every_sidecar(dsp)
    )


@secrets_group.command("set")
@click.argument("sidecar", shell_complete=complete_sidecar)
@click.pass_context
def secrets_set(ctx: click.Context, sidecar: str) -> None:
    """
    Prompt for and persist a sidecar's secrets

    Prompt for and persist every secret SIDECAR requires.
    """
    dsp = services.display_service
    result = services.secrets_service.set_secrets(sidecar)
    if result.error is not None:
        dsp.error(result.error)
        ctx.exit(1)
    if not result.keys_set:
        dsp.info(f"Sidecar '{sidecar}' declares no secrets.")
    ctx.exit(0)


@secrets_group.command("clear")
@click.argument("sidecar", shell_complete=complete_sidecar)
@click.pass_context
def secrets_clear(ctx: click.Context, sidecar: str) -> None:
    """
    Delete all secrets stored for a sidecar

    Delete every secret stored for SIDECAR.
    """
    dsp = services.display_service
    removed = services.secrets_service.clear_secrets(sidecar)
    if not removed:
        dsp.info(f"No secrets found for sidecar '{sidecar}'.")
        ctx.exit(0)
    title = SECRETS_CLEAR_TITLE.format(sidecar=sidecar, count=len(removed))
    dsp.show(_SecretsReport.removed_table(title, removed, dsp))
    ctx.exit(0)


@secrets_group.command("cleanup")
@click.pass_context
def secrets_cleanup(ctx: click.Context) -> None:
    """
    Remove keys not belonging to any known sidecar

    Remove every stored key that does not belong to a known sidecar or provider.
    """
    dsp = services.display_service
    removed = services.secrets_service.cleanup_secrets()
    if not removed:
        dsp.info("No unknown keys found.")
        ctx.exit(0)
    title = SECRETS_CLEANUP_TITLE.format(count=len(removed))
    dsp.show(_SecretsReport.removed_table(title, removed, dsp))
    ctx.exit(0)
