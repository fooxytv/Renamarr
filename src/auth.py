"""Time-limited delete authorisation codes."""

import hashlib
import hmac
import os
import time

# Code validity window in seconds
CODE_VALIDITY = 120  # 2 minutes


def _get_time_slot() -> int:
    """Get the current time slot (changes every CODE_VALIDITY seconds)."""
    return int(time.time()) // CODE_VALIDITY


def generate_code(passphrase: str) -> str:
    """Generate a time-limited 6-digit code from a passphrase.

    The code is valid for 2 minutes.
    """
    slot = _get_time_slot()
    message = f"{slot}".encode()
    key = passphrase.encode()
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    # Take first 6 digits from hex
    code = str(int(digest[:8], 16) % 1000000).zfill(6)
    return code


def verify_code(passphrase: str, code: str) -> bool:
    """Verify a time-limited code against the passphrase.

    Accepts the current time slot and the previous one (to handle edge cases).
    """
    current_slot = _get_time_slot()

    for slot in [current_slot, current_slot - 1]:
        message = f"{slot}".encode()
        key = passphrase.encode()
        digest = hmac.new(key, message, hashlib.sha256).hexdigest()
        expected = str(int(digest[:8], 16) % 1000000).zfill(6)
        if hmac.compare_digest(code, expected):
            return True

    return False


def get_passphrase() -> str | None:
    """Get the delete passphrase from environment."""
    return os.environ.get("RENAMARR_DELETE_PASSPHRASE")
