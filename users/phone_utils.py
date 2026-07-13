"""
users/phone_utils.py

Normalizes Kenyan phone numbers to one canonical form so the same real
number can't be used to bypass validate_phone's uniqueness check just by
being typed differently. All of these must normalize to the SAME string:

    +254718889559
    254718889559
    0718889559
    +254 718 889 559
    0718 889 559
    +254-718-889-559

Canonical form: "+254" followed by 9 digits, no spaces/dashes — e.g.
"+254718889559". Kenyan mobile numbers start with 7 or 1 after the
country code (Safaricom/Airtel/Telkom use 7xx; some newer Airtel/Telkom
ranges use 1xx), always 9 digits total after +254.
"""
import re

from rest_framework import serializers

KENYA_PHONE_RE = re.compile(r"^\+254[17]\d{8}$")


class InvalidKenyanPhone(ValueError):
    pass


def normalize_kenyan_phone(raw):
    """
    Takes any of the accepted input formats and returns the canonical
    "+254XXXXXXXXX" form. Raises InvalidKenyanPhone if the input can't be
    confidently resolved to a valid Kenyan mobile number.
    """
    if not raw:
        raise InvalidKenyanPhone("Phone number is required.")

    # Strip everything except leading + and digits.
    cleaned = re.sub(r"[^\d+]", "", raw.strip())

    if cleaned.startswith("+254"):
        candidate = cleaned
    elif cleaned.startswith("254"):
        candidate = "+" + cleaned
    elif cleaned.startswith("0"):
        # 0718889559 -> +254718889559 (drop the leading 0, not the whole prefix)
        candidate = "+254" + cleaned[1:]
    elif cleaned.startswith("7") or cleaned.startswith("1"):
        # Bare 718889559 (9 digits, no prefix at all)
        candidate = "+254" + cleaned
    else:
        candidate = cleaned

    if not KENYA_PHONE_RE.match(candidate):
        raise InvalidKenyanPhone(
            "Enter a valid Kenyan phone number, e.g. +254712345678 or 0712345678."
        )

    return candidate


def validate_kenyan_phone_field(value):
    """DRF-style validator — raises serializers.ValidationError, returns
    the normalized value for the caller to assign back onto validated_data."""
    try:
        return normalize_kenyan_phone(value)
    except InvalidKenyanPhone as exc:
        raise serializers.ValidationError(str(exc)) from exc
