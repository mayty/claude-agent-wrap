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
from pathlib import Path

from agent_wrap.lib.atomic import atomic_write_json

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
    except (OSError, AttributeError, ValueError):
        return False
    else:
        return ret > 0


# ---------------------------------------------------------------------------
# JSON fallback
# ---------------------------------------------------------------------------

_TOOL_DIR = Path(__file__).resolve().parents[1]
_FALLBACK_DIR = _TOOL_DIR / ".agent-launches"


def _fallback_path() -> Path:
    return _FALLBACK_DIR / "secrets.json"


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
    print(f"Secret: {description}", file=sys.stderr)
    entered = getpass.getpass("Value: ")
    if _keyring_available():
        _keyring_set(key, entered)
    else:
        _fallback_set(key, entered)
