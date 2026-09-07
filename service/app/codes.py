"""Pickup code generation and normalization.

Codes look like  SL-7K3Q-9RT2 : a fixed prefix plus 8 characters from the
Crockford base32 alphabet (no 0/O, 1/I/L, or U), so they are unambiguous when
read aloud at a noisy GBM or scribbled on a phone. Randomness comes from
`secrets`; uniqueness is enforced by the database's unique index, with the
caller retrying on collision (2^40 space, so collisions are essentially
never seen at this volume).
"""
from __future__ import annotations

import re
import secrets
from typing import Optional

ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"  # 30 chars, Crockford-ish minus ambiguous glyphs
PREFIX = "SL"
BODY_LENGTH = 8

# Accept anything a human might type: lower case, missing dashes, spaces,
# O for 0, I/L for 1 (we never emit those, so map them back defensively).
_SUBSTITUTIONS = str.maketrans({"O": "0", "I": "1", "L": "1"})
_CODE_RE = re.compile(rf"^{PREFIX}([{ALPHABET}]{{{BODY_LENGTH}}})$")


def generate_code() -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(BODY_LENGTH))
    return format_code(PREFIX + body)


def format_code(compact: str) -> str:
    """SL7K3Q9RT2 -> SL-7K3Q-9RT2 (the canonical stored/displayed form)."""
    compact = compact.upper().replace("-", "").replace(" ", "")
    body = compact[len(PREFIX):]
    return f"{PREFIX}-{body[:4]}-{body[4:]}"


def normalize_code(raw: Optional[str]) -> Optional[str]:
    """Turn user input into the canonical form, or None if it cannot be a code."""
    if not raw:
        return None
    compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if not compact.startswith(PREFIX):
        # Allow people to type just the 8-char body.
        if len(compact) == BODY_LENGTH:
            compact = PREFIX + compact
        else:
            return None
    compact = compact[:2] + compact[2:].translate(_SUBSTITUTIONS)
    if not _CODE_RE.match(compact):
        return None
    return format_code(compact)
