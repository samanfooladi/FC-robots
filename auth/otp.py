import time
import logging
import pyotp

logger = logging.getLogger(__name__)


def generate_otp(otp_key: str) -> str:
    """Return the current TOTP code for *otp_key* (base-32 encoded)."""
    totp = pyotp.TOTP(otp_key)
    code = totp.now()
    logger.debug("OTP code generated (window expires in %ds)", remaining_seconds(otp_key))
    return code


def remaining_seconds(otp_key: str) -> int:
    """Seconds until the current TOTP window expires."""
    totp = pyotp.TOTP(otp_key)
    return totp.interval - (int(time.time()) % totp.interval)


def verify_otp(otp_key: str, code: str) -> bool:
    """Verify a code against the current (and adjacent) TOTP window."""
    return pyotp.TOTP(otp_key).verify(code, valid_window=1)
