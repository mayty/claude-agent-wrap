# This file has been edited with the assistance of an AI tool.
"""``agent secrets check|set|clear <sidecar>`` and ``agent secrets cleanup``."""

from typing import TYPE_CHECKING

from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

if TYPE_CHECKING:
    import argparse

USAGE = "check|set|clear <sidecar>  |  cleanup"
SUMMARY = "Manage sidecar secrets"


def build_parser() -> argparse.ArgumentParser:
    parser = make_parser(
        "secrets",
        usage_summary=USAGE,
        description="Manage secrets for sidecars and providers.",
    )
    parser.add_argument(
        "action",
        choices=("check", "set", "clear", "cleanup"),
        help="check|set|clear — operate on a sidecar; cleanup — remove unknown keys",
    )
    parser.add_argument(
        "sidecar",
        nargs="?",
        help="Sidecar name (required for check, set, clear)",
    )
    return parser


class _SecretsActions:
    """Per-action helper functions for the secrets CLI."""

    @staticmethod
    def check(sidecar_name: str) -> int:
        """Report which of *sidecar_name*'s required secrets are present."""
        dsp = services.display_service
        report = services.secrets_service.check_secrets(sidecar_name)
        if report.declares_none:
            dsp.info(f"Sidecar '{sidecar_name}' declares no secrets.")
            return 0

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
                f"'{sidecar_name}'\nRun 'agent secrets set {sidecar_name}' to set them."
            )
            return 1
        return 0

    @staticmethod
    def set(sidecar_name: str) -> int:
        """Prompt and persist all required secrets for *sidecar_name*."""
        dsp = services.display_service
        result = services.secrets_service.set_secrets(sidecar_name)
        if result.error is not None:
            dsp.error(result.error)
            return 1
        if not result.keys_set:
            dsp.info(f"Sidecar '{sidecar_name}' declares no secrets.")
        return 0

    @staticmethod
    def clear(sidecar_name: str) -> int:
        """Delete all secrets for *sidecar_name*."""
        dsp = services.display_service
        srv = services.secrets_service
        removed = srv.clear_secrets(sidecar_name)
        for key in removed:
            dsp.info(f"  {key:45s}  REMOVED")
        if not removed:
            dsp.info(f"No secrets found for sidecar '{sidecar_name}'.")
        return 0

    @staticmethod
    def cleanup() -> int:
        """Remove all keys not belonging to any known sidecar/provider."""
        dsp = services.display_service
        srv = services.secrets_service
        removed = srv.cleanup_secrets()
        for key in removed:
            dsp.info(f"  {key:45s}  REMOVED (unknown)")
        if not removed:
            dsp.info("No unknown keys found.")
        return 0


def run(args: list[str]) -> int:
    """Entry point for ``agent secrets``."""
    ns = parse_or_code(build_parser(), args)
    if isinstance(ns, int):
        return ns

    action: str = ns.action
    sidecar: str | None = ns.sidecar

    # -- cleanup (no sidecar) --------------------------------------------------
    if action == "cleanup":
        if sidecar is not None:
            services.display_service.error("The 'cleanup' action does not take a sidecar argument.")
            return 1
        return _SecretsActions.cleanup()

    # -- check / set / clear (require sidecar) ----------------------------------
    if sidecar is None:
        services.display_service.error(
            f"The '{action}' action requires a sidecar name."
            f"  Usage: agent secrets {action} <sidecar>"
        )
        return 1

    dispatch = {
        "check": _SecretsActions.check,
        "set": _SecretsActions.set,
        "clear": _SecretsActions.clear,
    }
    return dispatch[action](sidecar)
