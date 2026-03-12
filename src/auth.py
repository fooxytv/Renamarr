"""Time-limited delete authorisation codes and API authentication."""

import hashlib
import hmac
import os
import time

# Code validity window in seconds
CODE_VALIDITY = 120  # 2 minutes


def _derive_key(passphrase: str) -> bytes:
    """Derive a strong key from a passphrase using PBKDF2."""
    salt = b"renamarr-delete-auth-v1"
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, 100_000)


def _get_time_slot() -> int:
    """Get the current time slot (changes every CODE_VALIDITY seconds)."""
    return int(time.time()) // CODE_VALIDITY


def generate_code(passphrase: str) -> str:
    """Generate a time-limited 8-digit code from a passphrase.

    The code is valid for 2 minutes.
    """
    slot = _get_time_slot()
    key = _derive_key(passphrase)
    digest = hmac.new(key, f"{slot}".encode(), hashlib.sha256).hexdigest()
    code = str(int(digest[:10], 16) % 100_000_000).zfill(8)
    return code


def verify_code(passphrase: str, code: str) -> bool:
    """Verify a time-limited code against the passphrase.

    Accepts the current time slot and the previous one (to handle edge cases).
    """
    current_slot = _get_time_slot()
    key = _derive_key(passphrase)

    for slot in [current_slot, current_slot - 1]:
        digest = hmac.new(key, f"{slot}".encode(), hashlib.sha256).hexdigest()
        expected = str(int(digest[:10], 16) % 100_000_000).zfill(8)
        if hmac.compare_digest(code, expected):
            return True

    return False


def get_passphrase() -> str | None:
    """Get the delete passphrase from environment."""
    return os.environ.get("RENAMARR_DELETE_PASSPHRASE")


def get_api_key() -> str | None:
    """Get the API key for web UI authentication."""
    return os.environ.get("RENAMARR_API_KEY")
