# This file has been created with the assistance of an AI tool.
"""Constants for the secrets domain subpackage."""

from agent_wrap.constants import AGENT_LAUNCHES_DIR

#: Path to the random keyfile used in key derivation.
SECRETS_KEYFILE_PATH = AGENT_LAUNCHES_DIR / ".secrets-key"

#: Path to the encrypted secrets store.
SECRETS_ENCRYPTED_FILE_PATH = AGENT_LAUNCHES_DIR / "secrets.enc"

#: HMAC label for deriving the encryption sub-key.
ENCRYPTION_SUBKEY_LABEL = b"enc"

#: HMAC label for deriving the authentication sub-key.
AUTH_SUBKEY_LABEL = b"auth"
