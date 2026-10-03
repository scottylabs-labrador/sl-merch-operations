"""Access codes: per-person sign-in credentials an officer mints and revokes.

A code looks like  VOL-7K3Q9-RT2VX  (volunteer) or  ADM-7K3Q9-RT2VX  (admin):
a 3-letter role prefix plus 10 characters from the same unambiguous alphabet the
pickup codes use (no 0/O, 1/I/L, or U). It is a per-person credential — handed
out, typed at /login, and revocable one at a time — unlike the single shared
VOLUNTEER_PASSCODE / ADMIN_PASSCODE, which can only be cut off by rotating them
for everyone at once. The role a code grants is the role stored on its row, not
anything the holder can change; the prefix is only there so a human can tell an
admin code from a volunteer one at a glance.

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

ROLES = ("volunteer", "admin")
PREFIXES = {"volunteer": "VOL", "admin": "ADM"}
BODY_LENGTH = 10
MAX_BATCH = 200  # one officer action should not be able to mint thousands

_CODE_RE = re.compile(rf"^({'|'.join(PREFIXES.values())})([{ALPHABET}]{{{BODY_LENGTH}}})$")


def format_code(compact: str) -> str:
    """VOL7K3Q9RT2VX -> VOL-7K3Q9-RT2VX (the canonical stored/displayed form)."""
    compact = compact.upper().replace("-", "").replace(" ", "")
    return f"{compact[:3]}-{compact[3:8]}-{compact[8:]}"


def generate_code(role: str = "volunteer") -> str:
    prefix = PREFIXES.get(role, PREFIXES["volunteer"])
    body = "".join(secrets.choice(ALPHABET) for _ in range(BODY_LENGTH))
    return format_code(prefix + body)


def normalize_code(raw: Optional[str]) -> Optional[str]:
    """Turn user input into the canonical form, or None if it cannot be a code.

    Tolerant of case, missing dashes, and surrounding whitespace. The full role
    prefix is required (there are two of them, so a bare body would be ambiguous).
    """
    if not raw:
        return None
    compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if not _CODE_RE.match(compact):
        return None
    return format_code(compact)


def create_codes(session: Session, count: int, label: str = "", role: str = "volunteer", created_by: str = "admin") -> List[VolunteerCode]:
    """Mint `count` fresh codes (clamped to [1, MAX_BATCH]) of `role` and commit them."""
    if role not in ROLES:
        role = "volunteer"
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 1
    n = max(1, min(n, MAX_BATCH))
    clean_label = (label or "").strip()[:120] or None
    made: List[VolunteerCode] = []
    for _ in range(n):
        for _attempt in range(6):  # retry on the (vanishingly rare) collision
            vc = VolunteerCode(code=generate_code(role), label=clean_label, role=role, created_by=created_by)
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
    """Return the active (non-revoked) code equal to `raw`, recording the use.

    The returned row carries the role to grant (.role); the caller trusts that,
    never the prefix the holder typed.
    """
    code = normalize_code(raw)
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
