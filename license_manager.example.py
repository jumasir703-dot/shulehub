"""
license_manager.py — offline passkey generation & verification.

IMPORTANT: This file contains SECRET_KEY, which is what makes a passkey
"genuine". Anyone with this file can mint their own valid passkeys.

- Do NOT commit this file to a public GitHub repo. It's listed in
  .gitignore for exactly that reason.
- Keep a private backup of it somewhere safe (e.g. a password manager or
  private note) -- if you lose it, you can't generate new passkeys for
  old customers with a passkey signed by the old secret, and you'd have
  to reissue everyone a new one signed by a new secret.
- Change SECRET_KEY below to your own random value before shipping this
  to real customers. The value here is just a randomly generated example
  so the app works out of the box for testing.

HOW IT WORKS
A passkey looks like:  GT-STANDARD-20261231-AB12CD34
                        |   |          |         |
                        |   tier       expiry    signature
                        prefix         (YYYYMMDD) (HMAC of tier+expiry,
                                                    truncated)

The signature is an HMAC-SHA256 of the tier and expiry date, keyed with
SECRET_KEY. Nobody without SECRET_KEY can produce a signature that
verify_key() will accept, so the tier/expiry embedded in the key can't be
tampered with (e.g. changing the date to extend a subscription) without
invalidating the key.

LIMITATION (inherent to any fully offline license check): verification
relies on the customer's own system clock. A technically determined
person could set their clock backwards to keep using an expired key.
run_desktop.py adds a simple "clock didn't just jump backwards" check to
raise the bar against casual clock tampering, but this is not
bulletproof -- true tamper-resistance would require an internet check
against a trusted time source, which conflicts with "fully offline".
"""

import hashlib
import hmac
from datetime import date, datetime

# CHANGE THIS before issuing real passkeys. Keep it secret.
SECRET_KEY = b"REPLACE-ME-WITH-YOUR-OWN-RANDOM-SECRET-BEFORE-USE"

VALID_TIERS = {"basic", "standard", "premium"}


def _sign(tier, expiry_str):
    message = f"{tier}:{expiry_str}".encode("utf-8")
    return hmac.new(SECRET_KEY, message, hashlib.sha256).hexdigest()[:8].upper()


def generate_key(tier, expiry_date):
    """tier: 'basic' | 'standard' | 'premium'.
    expiry_date: a date object, or a 'YYYY-MM-DD' string.
    Returns the passkey string."""
    tier = tier.lower().strip()
    if tier not in VALID_TIERS:
        raise ValueError(f"tier must be one of {sorted(VALID_TIERS)}")
    expiry_str = expiry_date if isinstance(expiry_date, str) else expiry_date.strftime("%Y-%m-%d")
    datetime.strptime(expiry_str, "%Y-%m-%d")  # validate format early
    sig = _sign(tier, expiry_str)
    return f"GT-{tier.upper()}-{expiry_str.replace('-', '')}-{sig}"


def verify_key(passkey):
    """Returns a dict:
    {valid, tier, expiry (date obj), expired (bool), error (str or None)}
    """
    try:
        parts = (passkey or "").strip().upper().split("-")
        if len(parts) != 4 or parts[0] != "GT":
            return {"valid": False, "error": "That passkey doesn't look right."}
        _, tier, expiry_compact, sig = parts
        tier_lower = tier.lower()
        if tier_lower not in VALID_TIERS:
            return {"valid": False, "error": "Unrecognized package in this passkey."}
        if len(expiry_compact) != 8 or not expiry_compact.isdigit():
            return {"valid": False, "error": "That passkey doesn't look right."}
        expiry_str = f"{expiry_compact[0:4]}-{expiry_compact[4:6]}-{expiry_compact[6:8]}"
        expiry_dt = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        expected_sig = _sign(tier_lower, expiry_str)
        if not hmac.compare_digest(sig, expected_sig):
            return {"valid": False, "error": "This passkey isn't valid."}
        expired = date.today() > expiry_dt
        return {
            "valid": True,
            "tier": tier_lower,
            "expiry": expiry_dt,
            "expired": expired,
            "error": "This subscription has expired." if expired else None,
        }
    except Exception:
        return {"valid": False, "error": "That passkey doesn't look right."}
