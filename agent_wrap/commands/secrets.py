# This file has been edited with the assistance of an AI tool.
"""``agent secrets check|set|clear <sidecar>`` and ``agent secrets cleanup``."""

from __future__ import annotations

import sys

from agent_wrap.lib.argparsing import make_parser, parse_or_code
from agent_wrap.providers import get_provider

USAGE = "check|set|clear <sidecar>  |  cleanup"
SUMMARY = "Manage sidecar secrets"


# ---------------------------------------------------------------------------
# Sidecar name → required secrets resolution
# ---------------------------------------------------------------------------


def _known_sidecars() -> list[str]:
    """Return the list of known sidecar names for the usage message."""
    from agent_wrap.providers import _discover_providers

    names = list(_discover_providers().keys())
    names.append("telegram")
    return sorted(names)


def _get_required_secrets_safe(sidecar_name: str) -> list[tuple[str, str]]:
    """Like :func:`_get_required_secrets` but returns empty on unknown sidecars."""
    try:
        return _get_required_secrets(sidecar_name)
    except SystemExit:
        return []


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


# ---------------------------------------------------------------------------
# Per-action helpers
# ---------------------------------------------------------------------------


def _action_check(sidecar_name: str) -> int:
    """Verify all required secrets for *sidecar_name* are present."""
    from agent_wrap import secrets
    from agent_wrap.secrets import SecretNotFoundError

    required = _get_required_secrets(sidecar_name)
    if not required:
        print(f"Sidecar '{sidecar_name}' declares no secrets.", file=sys.stderr)
        return 0

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


def _action_set(sidecar_name: str) -> int:
    """Prompt and persist all required secrets for *sidecar_name*."""
    from agent_wrap import secrets

    if not sys.stdin.isatty():
        print(
            "Cannot prompt for secrets in a non-interactive session.",
            file=sys.stderr,
        )
        return 1

    required = _get_required_secrets(sidecar_name)
    if not required:
        print(f"Sidecar '{sidecar_name}' declares no secrets.", file=sys.stderr)
        return 0

    for key, desc in required:
        namespaced = f"{sidecar_name}:{key}"
        secrets.write(namespaced, desc)
    return 0


def _action_clear(sidecar_name: str) -> int:
    """Delete all secrets for *sidecar_name*."""
    from agent_wrap import secrets

    prefix = f"{sidecar_name}:"
    removed = 0
    for key in secrets.list_keys():
        if key.startswith(prefix):
            secrets.delete(key)
            print(f"  {key:45s}  REMOVED")
            removed += 1

    if removed == 0:
        print(f"No secrets found for sidecar '{sidecar_name}'.")
    return 0


def _action_cleanup() -> int:
    """Remove all keys not belonging to any known sidecar/provider."""
    from agent_wrap import secrets

    # Collect every known namespaced key across all sidecars/providers.
    known_keys: set[str] = set()
    for name in _known_sidecars():
        required = _get_required_secrets_safe(name)
        for key, _desc in required:
            known_keys.add(f"{name}:{key}")

    removed = 0
    for key in secrets.list_keys():
        if key not in known_keys:
            secrets.delete(key)
            print(f"  {key:45s}  REMOVED (unknown)")
            removed += 1

    if removed == 0:
        print("No unknown keys found.")
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run(args: list[str]) -> int:
    """Entry point for ``agent secrets``."""
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
    ns = parse_or_code(parser, args)
    if isinstance(ns, int):
        return ns

    action: str = ns.action
    sidecar: str | None = ns.sidecar

    # ── cleanup (no sidecar) ───────────────────────────────────────────
    if action == "cleanup":
        if sidecar is not None:
            print(
                "The 'cleanup' action does not take a sidecar argument.",
                file=sys.stderr,
            )
            return 1
        return _action_cleanup()

    # ── check / set / clear (require sidecar) ──────────────────────────
    if sidecar is None:
        print(
            f"The '{action}' action requires a sidecar name."
            f"  Usage: agent secrets {action} <sidecar>",
            file=sys.stderr,
        )
        return 1

    dispatch = {"check": _action_check, "set": _action_set, "clear": _action_clear}
    return dispatch[action](sidecar)
