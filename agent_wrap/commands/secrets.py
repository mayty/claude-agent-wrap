# This file has been created with the assistance of an AI tool.
"""``agent secrets <sidecar> check|set`` — check or initialize sidecar secrets."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from agent_wrap.lib.argparsing import make_parser, parse_or_code
from agent_wrap.providers import get_provider

if TYPE_CHECKING:
    from pathlib import Path

USAGE = "<sidecar> check|set"
SUMMARY = "Check or initialize sidecar secrets"


# ---------------------------------------------------------------------------
# Sidecar name → required secrets resolution
# ---------------------------------------------------------------------------


def _known_sidecars() -> list[str]:
    """Return the list of known sidecar names for the usage message."""
    from agent_wrap.providers import _discover_providers

    names = list(_discover_providers().keys())
    names.append("telegram")
    return sorted(names)


def _get_required_secrets(sidecar_name: str) -> list[tuple[str, str]]:
    """Return the required-secret ``(key, description)`` tuples for a sidecar."""
    if sidecar_name == "telegram":
        from agent_wrap.sidecars.telegram import TelegramSidecar

        return TelegramSidecar.required_secrets()

    try:
        provider = get_provider(sidecar_name)
    except SystemExit:
        known = ", ".join(_known_sidecars())
        print(f"Unknown sidecar: {sidecar_name}  (known: {known})", file=sys.stderr)
        raise SystemExit(1) from None

    return provider.required_secrets()


def run(args: list[str], tool_dir: Path) -> int:  # noqa: ARG001
    """Entry point for ``agent secrets``."""
    parser = make_parser(
        "secrets",
        usage_summary=USAGE,
        description="Check or initialize secrets for a sidecar.",
    )
    parser.add_argument(
        "sidecar",
        help=f"Sidecar name (e.g. {', '.join(_known_sidecars()[:3])}, …)",
    )
    parser.add_argument(
        "action",
        choices=("check", "set"),
        help="check — verify all secrets are present; set — prompt and store secrets",
    )
    ns = parse_or_code(parser, args)
    if isinstance(ns, int):
        return ns

    required = _get_required_secrets(ns.sidecar)
    if not required:
        print(f"Sidecar '{ns.sidecar}' declares no secrets.", file=sys.stderr)
        return 0

    sidecar_name: str = ns.sidecar
    action: str = ns.action

    # ------------------------------------------------------------------
    # check
    # ------------------------------------------------------------------
    if action == "check":
        from agent_wrap import secrets
        from agent_wrap.secrets import SecretNotFoundError

        all_ok = True
        for key, desc in required:
            namespaced = f"{sidecar_name}:{key}"
            try:
                secrets.read(namespaced, desc, prompt_on_missing=False)
                print(f"  {namespaced:45s}  OK")
            except SecretNotFoundError:
                print(f"  {namespaced:45s}  MISSING")
                all_ok = False
        return 0 if all_ok else 1

    # ------------------------------------------------------------------
    # set
    # ------------------------------------------------------------------
    if action == "set":
        if not sys.stdin.isatty():
            print(
                "Cannot prompt for secrets in a non-interactive session.",
                file=sys.stderr,
            )
            return 1

        from agent_wrap import secrets

        for key, desc in required:
            namespaced = f"{sidecar_name}:{key}"
            secrets.write(namespaced, desc)
        return 0

    return 1  # unreachable
