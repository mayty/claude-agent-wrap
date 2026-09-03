# This file has been edited with the assistance of an AI tool.
"""``agent secrets check|set|clear <sidecar>`` and ``agent secrets cleanup``."""

import click

from agent_wrap.containers import services


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


@secrets_group.command("check")
@click.argument("sidecar", shell_complete=complete_sidecar)
@click.pass_context
def secrets_check(ctx: click.Context, sidecar: str) -> None:
    """
    Report which of a sidecar's secrets are present

    Report which of SIDECAR's required secrets are present, without revealing their
    values.
    """
    dsp = services.display_service
    report = services.secrets_service.check_secrets(sidecar)
    if report.declares_none:
        dsp.info(f"Sidecar '{sidecar}' declares no secrets.")
        ctx.exit(0)

    # The whole report goes to stdout so the columns stay aligned and the rows stay
    # in declaration order; a severity tag on the MISSING rows alone would indent
    # them past the OK rows, and splitting the two across streams reorders them
    # under a pipe. The verdict is what carries the severity.
    width = max(map(len, report.entries))
    for namespaced, present in report.entries.items():
        if present:
            dsp.success(f"{namespaced:{width}s}  OK")
        else:
            dsp.info(f"{namespaced:{width}s}  MISSING")
    if not report.all_present:
        missing = [key for key, present in report.entries.items() if not present]
        dsp.error(
            f"{len(missing)} of {len(report.entries)} secrets missing for "
            f"'{sidecar}'\nRun 'agent secrets set {sidecar}' to set them."
        )
        ctx.exit(1)
    ctx.exit(0)


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
    for key in removed:
        dsp.info(f"  {key:45s}  REMOVED")
    if not removed:
        dsp.info(f"No secrets found for sidecar '{sidecar}'.")
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
    for key in removed:
        dsp.info(f"  {key:45s}  REMOVED (unknown)")
    if not removed:
        dsp.info("No unknown keys found.")
    ctx.exit(0)
