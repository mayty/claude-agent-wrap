# This file has been created with the assistance of an AI tool.
"""Constants for the secrets domain subpackage."""

from agent_wrap.constants import AGENT_LAUNCHES_DIR

#: Path to the random keyfile used in key derivation.
SECRETS_KEYFILE_PATH = AGENT_LAUNCHES_DIR / ".secrets-key"

#: Path to the encrypted secrets store.
SECRETS_ENCRYPTED_FILE_PATH = AGENT_LAUNCHES_DIR / "secrets.enc"

#: HKDF label separating the Fernet token key from the master key it expands.
FERNET_SUBKEY_LABEL = b"agent-wrap-secrets"

#: HMAC label for the superseded format's encryption sub-key.
LEGACY_ENCRYPTION_SUBKEY_LABEL = b"enc"

#: HMAC label for the superseded format's authentication sub-key.
LEGACY_AUTH_SUBKEY_LABEL = b"auth"

#: Shortest possible superseded payload: 16-byte nonce, empty ciphertext, 32-byte MAC.
LEGACY_MIN_PAYLOAD_LEN = 48
