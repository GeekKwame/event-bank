"""Ghana mobile number normalisation to E.164 (+233XXXXXXXXX)."""

import re

GHANA_COUNTRY_CODE = "233"
_SEPARATORS = re.compile(r"[\s\-().]")
# Ghana mobile numbers have a 9-digit national number starting with 2 or 5
# (e.g. 024, 054, 020, 050 when written with the trunk prefix 0).
_GHANA_MOBILE_E164_DIGITS = re.compile(r"233[25][0-9]{8}")


class InvalidPhoneNumber(ValueError):
    pass


def normalize_ghana_mobile(value: str) -> str:
    compact = _SEPARATORS.sub("", value.strip())
    if compact.startswith("+"):
        digits = compact[1:]
    elif compact.startswith("00"):
        digits = compact[2:]
    elif compact.startswith("0"):
        digits = GHANA_COUNTRY_CODE + compact[1:]
    else:
        digits = compact
    if not _GHANA_MOBILE_E164_DIGITS.fullmatch(digits):
        raise InvalidPhoneNumber("Not a valid Ghana mobile number")
    return f"+{digits}"


def to_ghana_national(e164: str) -> str:
    """+233241234567 -> 0241234567."""
    normalized = normalize_ghana_mobile(e164)
    return "0" + normalized[1 + len(GHANA_COUNTRY_CODE) :]


def mask_phone(e164: str) -> str:
    return e164[:6] + "*" * max(len(e164) - 9, 0) + e164[-3:] if len(e164) > 9 else "***"
