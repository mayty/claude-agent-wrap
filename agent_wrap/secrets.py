# This file has been created with the assistance of an AI tool.
"""
Secrets storage for agent-wrap sidecars.

Two-tier backend, locked in once at first use:

* **Kernel keyring** (Linux only) — secrets live in kernel memory, never on disk.
  Accessed via ``add_key`` / ``request_key`` / ``keyctl`` syscalls through
  stdlib ``ctypes``.  Key descriptions use the format
  ``"agent-wrap:<sidecar>:<key_name>"``.  Detected with a throwaway
  ``add_key`` + ``keyctl_unlink`` probe.

* **JSON file fallback** (non-Linux, or when keyring is unavailable) — secrets
  stored at ``<tool_dir>/.agent-launches/secrets.json`` with ``0o600``
  permissions, written atomically via ``agent_wrap.lib.atomic.atomic_write_json``.

Only one backend is active — there is no cross-backend fallthrough on individual
operations, and no migration between them.
"""

from __future__ import annotations

import ctypes
import functools
import getpass
import json
import sys
from contextlib import suppress
from pathlib import Path

from agent_wrap.constants import AGENT_LAUNCHES_DIR
from agent_wrap.lib.atomic import atomic_write_json
from agent_wrap.lib.console import Ansi

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class SecretNotFoundError(Exception):
    """Raised when a required secret is not found in the active backend."""

    def __init__(self, key: str, description: str) -> None:
        self.key = key
        self.description = description
        super().__init__(f"Secret '{key}' ({description}) not found in secrets store")


# ---------------------------------------------------------------------------
# Keyring syscall wrappers (Linux only)
# ---------------------------------------------------------------------------

_SYS_add_key = 248
_SYS_request_key = 249
_SYS_keyctl = 250

_KEYCTL_READ = 11
_KEYCTL_UNLINK = 9

_KEY_SPEC_USER_KEYRING = ctypes.c_long(-4)

_KEY_TYPE = b"user"
_KEY_PREFIX = b"agent-wrap:"
_KEY_MAX_PAYLOAD = 4096

# Path to the old flat secrets file (pre-secrets-store era).
_OLD_SECRETS_PATH: Path = Path.home() / "claude_keys.json"

_libc: ctypes.CDLL | None = None


def _get_libc() -> ctypes.CDLL:
    """Return (cached) handle to ``libc`` for raw syscall wrappers."""
    global _libc  # noqa: PLW0603
    if _libc is None:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.syscall.argtypes = [ctypes.c_long]
        libc.syscall.restype = ctypes.c_long
        _libc = libc
    return _libc


def _keyring_set(key: str, value: str) -> bool:
    """Store *value* under the namespaced *key* in the user keyring."""
    desc = _KEY_PREFIX + key.encode("utf-8")
    payload = value.encode("utf-8")
    try:
        libc = _get_libc()
        existing = libc.syscall(
            ctypes.c_long(_SYS_request_key),
            ctypes.c_char_p(_KEY_TYPE),
            ctypes.c_char_p(desc),
            ctypes.c_void_p(0),
            _KEY_SPEC_USER_KEYRING,
        )
        if existing > 0:
            libc.syscall(
                ctypes.c_long(_SYS_keyctl),
                ctypes.c_int(_KEYCTL_UNLINK),
                ctypes.c_long(existing),
                _KEY_SPEC_USER_KEYRING,
            )
        ret = libc.syscall(
            ctypes.c_long(_SYS_add_key),
            ctypes.c_char_p(_KEY_TYPE),
            ctypes.c_char_p(desc),
            ctypes.c_char_p(payload),
            ctypes.c_size_t(len(payload)),
            _KEY_SPEC_USER_KEYRING,
        )
    except (OSError, AttributeError, ValueError):
        return False
    else:
        return ret > 0


def _keyring_get(key: str) -> str | None:
    """Retrieve the value for *key* from the user keyring, or ``None``."""
    desc = _KEY_PREFIX + key.encode("utf-8")
    try:
        libc = _get_libc()
        key_id = libc.syscall(
            ctypes.c_long(_SYS_request_key),
            ctypes.c_char_p(_KEY_TYPE),
            ctypes.c_char_p(desc),
            ctypes.c_void_p(0),
            _KEY_SPEC_USER_KEYRING,
        )
        if key_id < 0:
            return None
        buf = ctypes.create_string_buffer(_KEY_MAX_PAYLOAD)
        ret = libc.syscall(
            ctypes.c_long(_SYS_keyctl),
            ctypes.c_int(_KEYCTL_READ),
            ctypes.c_long(key_id),
            buf,
            ctypes.c_size_t(_KEY_MAX_PAYLOAD),
        )
        if ret < 0:
            return None
        return buf.raw[:ret].rstrip(b"\x00").decode("utf-8", errors="replace")
    except (OSError, AttributeError, ValueError):
        return None


def _keyring_delete(key: str) -> bool:
    """Remove *key* from the user keyring."""
    desc = _KEY_PREFIX + key.encode("utf-8")
    try:
        libc = _get_libc()
        key_id = libc.syscall(
            ctypes.c_long(_SYS_request_key),
            ctypes.c_char_p(_KEY_TYPE),
            ctypes.c_char_p(desc),
            ctypes.c_void_p(0),
            _KEY_SPEC_USER_KEYRING,
        )
        if key_id < 0:
            return False
        ret = libc.syscall(
            ctypes.c_long(_SYS_keyctl),
            ctypes.c_int(_KEYCTL_UNLINK),
            ctypes.c_long(key_id),
            _KEY_SPEC_USER_KEYRING,
        )
    except (OSError, AttributeError, ValueError):
        return False
    else:
        return ret == 0


# ---------------------------------------------------------------------------
# Backend detection (cached, probed on first call)
# ---------------------------------------------------------------------------


@functools.cache
def _keyring_available() -> bool:
    """Probe whether the kernel keyring is available.  Cached after first call."""
    try:
        libc = _get_libc()
        probe_desc = b"agent-wrap:__probe__"
        ret = libc.syscall(
            ctypes.c_long(_SYS_add_key),
            ctypes.c_char_p(_KEY_TYPE),
            ctypes.c_char_p(probe_desc),
            ctypes.c_char_p(b"x"),
            ctypes.c_size_t(1),
            _KEY_SPEC_USER_KEYRING,
        )
        if ret > 0:
            libc.syscall(
                ctypes.c_long(_SYS_keyctl),
                ctypes.c_int(_KEYCTL_UNLINK),
                ctypes.c_long(ret),
                _KEY_SPEC_USER_KEYRING,
            )
        available = ret > 0
    except (OSError, AttributeError, ValueError):
        available = False

    if not available:
        print(
            f"{Ansi.BOLD_YELLOW}Warning:{Ansi.RESET} kernel keyring unavailable —"
            " secrets will be stored on disk",
            file=sys.stderr,
        )

    return available


# ---------------------------------------------------------------------------
# JSON fallback
# ---------------------------------------------------------------------------


def _fallback_path() -> Path:
    return AGENT_LAUNCHES_DIR / "secrets.json"


def _fallback_get(key: str) -> str | None:
    try:
        data = json.loads(_fallback_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    value = data.get(key)
    return str(value) if value is not None else None


def _fallback_set(key: str, value: str) -> None:
    path = _fallback_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        existing = {}
    existing[key] = value
    atomic_write_json(path, existing)
    path.chmod(0o600)


# ---------------------------------------------------------------------------
# Migration from old-style flat ~/claude_keys.json
# ---------------------------------------------------------------------------


@functools.cache
def _migrate_secrets() -> None:  # noqa: C901, PLR0912, PLR0915
    """
    Migrate old ``~/claude_keys.json`` secrets to the active backend, once.

    Called (as a cached no-op after the first run) from :func:`read` and
    :func:`write` so every entry point transparently triggers migration.

    Mapping from old flat keys to new namespaced keys:

    ====================================  ================================
    Old key (``~/claude_keys.json``)      New namespaced key
    ====================================  ================================
    ``BedrockBearerToken``                ``litellm-bedrock:api_key``
    ``ServiceSpecificCredential``         ``litellm-bedrock:api_key``
    ``DashScopeAPIKey``                   ``litellm-dashscope:api_key``
    ``DeepSeekAPIKey``                    ``litellm-deepseek:api_key``
    ``TelegramBotToken``                  ``telegram:TelegramBotToken``
    ``TelegramChatId``                    ``telegram:TelegramChatId``
    ====================================  ================================

    ``ServiceSpecificCredential`` is a nested dict whose value is at the
    key ``ServiceCredentialSecret``.  It is only used when
    ``BedrockBearerToken`` is missing or empty (preserving the old fallback
    order from the per-provider ``read_secret_key`` methods).
    """
    old_path = _OLD_SECRETS_PATH
    if not old_path.is_file():
        return

    try:
        raw = old_path.read_text(encoding="utf-8")
        old_data: dict = json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        print(
            f"{Ansi.BOLD_YELLOW}Warning:{Ansi.RESET} {old_path}"
            f" is corrupt or unreadable ({exc}); removing",
            file=sys.stderr,
        )
        with suppress(OSError):
            old_path.unlink(missing_ok=True)
        return

    _store = _keyring_set if _keyring_available() else _fallback_set
    _fetch = _keyring_get if _keyring_available() else _fallback_get

    migrated = 0

    # --- Bedrock ---
    bedrock_new_key = "litellm-bedrock:api_key"
    bedrock_value: str = old_data.get("BedrockBearerToken", "") or ""
    if not bedrock_value:
        cred = old_data.get("ServiceSpecificCredential") or {}
        if isinstance(cred, dict):
            bedrock_value = cred.get("ServiceCredentialSecret", "") or ""
    if bedrock_value and _fetch(bedrock_new_key) is None:
        _store(bedrock_new_key, bedrock_value)
        migrated += 1

    # --- DashScope ---
    dashscope_value: str = old_data.get("DashScopeAPIKey", "") or ""
    if dashscope_value:
        ds_key = "litellm-dashscope:api_key"
        if _fetch(ds_key) is None:
            _store(ds_key, dashscope_value)
            migrated += 1

    # --- DeepSeek ---
    deepseek_value: str = old_data.get("DeepSeekAPIKey", "") or ""
    if deepseek_value:
        deepseek_key = "litellm-deepseek:api_key"
        if _fetch(deepseek_key) is None:
            _store(deepseek_key, deepseek_value)
            migrated += 1

    # --- Telegram ---
    tg_bot: str = old_data.get("TelegramBotToken", "") or ""
    if tg_bot:
        tg_bot_key = "telegram:TelegramBotToken"
        if _fetch(tg_bot_key) is None:
            _store(tg_bot_key, tg_bot)
            migrated += 1

    tg_chat: str = old_data.get("TelegramChatId", "") or ""
    if tg_chat:
        tg_chat_key = "telegram:TelegramChatId"
        if _fetch(tg_chat_key) is None:
            _store(tg_chat_key, tg_chat)
            migrated += 1

    if migrated:
        print(
            f"{Ansi.BOLD_GREEN}Migrated {migrated} secret(s) from"
            f" ~/claude_keys.json to the secrets store{Ansi.RESET}",
            file=sys.stderr,
        )

    try:
        old_path.unlink(missing_ok=True)
    except OSError as exc:
        print(
            f"{Ansi.BOLD_YELLOW}Warning:{Ansi.RESET} could not remove"
            f" {old_path} after migration ({exc})",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read(key: str, description: str, *, prompt_on_missing: bool = False) -> str:
    """
    Return the secret for *key*.

    Looks up *key* in the active backend (keyring if available, else JSON
    fallback).  If not found and *prompt_on_missing* is ``True`` the user is
    interactively prompted, and the entered value is persisted and returned.

    Raises :class:`SecretNotFoundError` when the key is absent and
    *prompt_on_missing* is ``False``.
    """
    _migrate_secrets()
    value = _keyring_get(key) if _keyring_available() else _fallback_get(key)
    if value is not None:
        return value
    if not prompt_on_missing:
        raise SecretNotFoundError(key, description)

    print(f"Secret: {description}", file=sys.stderr)
    try:
        entered = getpass.getpass("Value: ")
    except EOFError:
        raise SecretNotFoundError(key, description) from None

    if _keyring_available():
        _keyring_set(key, entered)
    else:
        _fallback_set(key, entered)

    return entered


def write(key: str, description: str) -> None:
    """Prompt the user for *key* and persist it to the active backend."""
    _migrate_secrets()
    print(f"Secret: {description}", file=sys.stderr)
    entered = getpass.getpass("Value: ")
    if _keyring_available():
        _keyring_set(key, entered)
    else:
        _fallback_set(key, entered)
