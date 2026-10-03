"""Volunteer access codes: mint, normalize, match, revoke.

A volunteer code looks like  VOL-7K3Q9-RT2VX : a fixed prefix plus 10 characters
from the same unambiguous alphabet the pickup codes use (no 0/O, 1/I/L, or U).
It is a per-volunteer sign-in credential — handed out before a shift, typed at
/login, and revocable one at a time — unlike the single shared VOLUNTEER_PASSCODE,
which can only be cut off by rotating it for everyone at once.

Codes are stored in the clear (like the shared passcodes and the pickup codes):
an officer needs to see them to hand them out or re-share one, and the threat
model is an internal club's merch desk, not password storage. Randomness is from
`secrets`; 30^10 (~2^49) is ample against online guessing at this volume, and the
unique index plus a retry-on-collision loop keeps them distinct.
"""
from __future__ import annotations

import re
import secrets
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .codes import ALPHABET  # 30 unambiguous chars, shared with pickup codes
from .db import VolunteerCode, utcnow

PREFIX = "VOL"
BODY_LENGTH = 10
MAX_BATCH = 200  # one officer action should not be able to mint thousands

_CODE_RE = re.compile(rf"^{PREFIX}([{ALPHABET}]{{{BODY_LENGTH}}})$")


def format_volunteer_code(compact: str) -> str:
    """VOL7K3Q9RT2VX -> VOL-7K3Q9-RT2VX (the canonical stored/displayed form)."""
    compact = compact.upper().replace("-", "").replace(" ", "")
    body = compact[len(PREFIX):]
    return f"{PREFIX}-{body[:5]}-{body[5:]}"


def generate_volunteer_code() -> str:
    body = "".join(secrets.choice(ALPHABET) for _ in range(BODY_LENGTH))
    return format_volunteer_code(PREFIX + body)


def normalize_volunteer_code(raw: Optional[str]) -> Optional[str]:
    """Turn user input into the canonical form, or None if it cannot be a code.

    Tolerant of case, missing dashes, and surrounding whitespace. The alphabet is
    already free of ambiguous glyphs, so no character remapping is needed.
    """
    if not raw:
        return None
    compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if not compact.startswith(PREFIX):
        # Allow people to type just the 10-char body (the body can never begin
        # with "VOL" because 'O' is not in the alphabet, so this is unambiguous).
        if len(compact) == BODY_LENGTH:
            compact = PREFIX + compact
        else:
            return None
    if not _CODE_RE.match(compact):
        return None
    return format_volunteer_code(compact)


def create_codes(session: Session, count: int, label: str = "", created_by: str = "admin") -> List[VolunteerCode]:
    """Mint `count` fresh codes (clamped to [1, MAX_BATCH]) and commit them."""
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 1
    n = max(1, min(n, MAX_BATCH))
    clean_label = (label or "").strip()[:120] or None
    made: List[VolunteerCode] = []
    for _ in range(n):
        for _attempt in range(6):  # retry on the (vanishingly rare) collision
            vc = VolunteerCode(code=generate_volunteer_code(), label=clean_label, created_by=created_by)
            session.add(vc)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                continue
            session.refresh(vc)
            made.append(vc)
            break
    return made


def match_active(session: Session, raw: str) -> Optional[VolunteerCode]:
    """Return the active (non-revoked) code equal to `raw`, recording the use."""
    code = normalize_volunteer_code(raw)
    if not code:
        return None
    vc = session.scalar(
        select(VolunteerCode).where(VolunteerCode.code == code, VolunteerCode.revoked_at.is_(None))
    )
    if vc:
        vc.last_used_at = utcnow()
        vc.use_count = (vc.use_count or 0) + 1
        session.commit()
    return vc


def is_active(session: Session, code_id: int) -> bool:
    """Whether a code id still grants access (exists and is not revoked)."""
    vc = session.get(VolunteerCode, code_id)
    return bool(vc and vc.revoked_at is None)


def revoke(session: Session, code_id: int, by: str = "admin") -> Optional[VolunteerCode]:
    vc = session.get(VolunteerCode, code_id)
    if not vc or vc.revoked_at is not None:
        return None
    vc.revoked_at = utcnow()
    vc.revoked_by = by
    session.commit()
    return vc


def restore(session: Session, code_id: int) -> Optional[VolunteerCode]:
    vc = session.get(VolunteerCode, code_id)
    if not vc or vc.revoked_at is None:
        return None
    vc.revoked_at = None
    vc.revoked_by = None
    session.commit()
    return vc


def list_codes(session: Session) -> List[VolunteerCode]:
    return session.scalars(
        select(VolunteerCode).order_by(VolunteerCode.created_at.desc(), VolunteerCode.id.desc())
    ).all()
