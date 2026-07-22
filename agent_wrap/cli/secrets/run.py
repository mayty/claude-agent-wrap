# This file has been edited with the assistance of an AI tool.
"""``agent secrets check|set|clear <sidecar>`` and ``agent secrets cleanup``."""

from __future__ import annotations

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
        """Verify all required secrets for *sidecar_name* are present."""
        dsp = services.display_service
        srv = services.secrets_service
        required = srv.get_required_secrets(sidecar_name)
        if not required:
            dsp.info(f"Sidecar '{sidecar_name}' declares no secrets.")
            return 0

        results = srv.check_secrets(sidecar_name)
        all_ok = True
        max_secret_length = max(map(len, results.keys()))
        for namespaced, present in results.items():
            if present:
                dsp.success(f"{namespaced:{max_secret_length}s}  OK")
            else:
                dsp.error(f"{namespaced:{max_secret_length}s}  MISSING")
            all_ok = all_ok and present
        return 0 if all_ok else 1

    @staticmethod
    def set(sidecar_name: str) -> int:
        """Prompt and persist all required secrets for *sidecar_name*."""
        dsp = services.display_service
        srv = services.secrets_service
        try:
            keys_set = srv.set_secrets(sidecar_name)
        except RuntimeError as e:
            dsp.error(str(e))
            return 1
        if not keys_set:
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
