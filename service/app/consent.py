"""Call/text consent revocations: detect them, record them, look them up.

The checkout prompt promises "Opt out anytime, any way". Revocations therefore live
in their own table (CallOptOut), keyed by email and phone, and are recorded even when
no order matches yet. Every place that could grant consent (the export import) asks
latest_revocation() first. Detection errs toward revoking: a false revocation only
costs a reminder, a missed one is a consent violation.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Iterable, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .checkout_answer import normalize, parse_checkout_answer
from .db import CallOptOut, Order

_TARGET = r"(?:robo\s*-?\s*)?(?:call(?:s|ed|ing)?|text(?:s|ed|ing)?|sms|voice|reminders?|automated|calls\s*ok)"
OPT_OUT_RE = re.compile(
    r"\b(?:stop|quit|cancel|end|unsubscribe|opt(?:\s*-?\s*me)?\s*-?\s*out|remove(?:\s+me)?|take\s+me\s+off|no\s+longer|revoke|withdraw)\b[^\n]{0,60}?\b" + _TARGET + r"\b"
    r"|\b(?:don'?t|dont|do\s+not|never)\b[^\n]{0,25}?\b" + _TARGET + r"\b"
    r"|\bno\s+(?:more\s+)?" + _TARGET + r"\b"
    r"|\b" + _TARGET + r"\b[^\n]{0,30}?\b(?:stop|opt\s*-?\s*out|unsubscribe|no\s+longer|not\s+(?:ok|okay|wanted))\b"
    r"|\b(?:revoke|withdraw)\b[^\n]{0,40}?\bconsent\b",
    re.IGNORECASE,
)
BARE_KEYWORD_RE = re.compile(r"^\s*(?:stop|stopall|stop\s+all|unsubscribe|cancel|quit|end|revoke|opt\s*-?\s*out)\s*[.!]*\s*$", re.IGNORECASE)
_QUOTE_START_RE = re.compile(r"^(?:On .+wrote:\s*$|-{2,}\s*Original Message|_{5,}|From:\s.+|Begin forwarded message)", re.IGNORECASE)
_SIGNATURE_RE = re.compile(r"^(?:Sent from my .*|Get Outlook for .*|--\s*)$", re.IGNORECASE)


def own_words(text: str) -> str:
    """The sender's own text: quoted history, forwarded blocks and signatures removed."""
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if _QUOTE_START_RE.match(s):
            break
        if s.startswith(">"):
            continue
        if _SIGNATURE_RE.match(s):
            if s.startswith("--"):
                break
            continue
        out.append(line)
    return "\n".join(out)


def detect_opt_out(subject: str, text: str) -> bool:
    body = normalize(own_words(text)[:4000])
    subj = normalize(subject or "")
    if BARE_KEYWORD_RE.match(body) or BARE_KEYWORD_RE.match(subj):
        return True
    return bool(OPT_OUT_RE.search(subj) or OPT_OUT_RE.search(body))


def _utc(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)


def record_opt_out(
    session: Session,
    *,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    revoked_at: Optional[dt.datetime] = None,
    source: str,
    note: Optional[str] = None,
    inbound_email_id: Optional[int] = None,
) -> int:
    """Record a revocation and turn consent off on every matching order. Returns orders changed."""
    email = (email or "").strip().lower() or None
    phone = (phone or "").strip() or None
    if not email and not phone:
        return 0
    when = _utc(revoked_at) or dt.datetime.now(dt.timezone.utc)
    session.add(CallOptOut(email=email, phone=phone, revoked_at=when, source=source, note=(note or "")[:2000] or None, inbound_email_id=inbound_email_id))
    conds = []
    if email:
        conds += [func.lower(Order.buyer_email) == email, func.lower(Order.contact_email) == email]
    if phone:
        conds.append(Order.contact_phone == phone)
    changed = 0
    for o in session.scalars(select(Order).where(or_(*conds))).all():
        o.calls_opt_in = False
        if o.calls_opt_out_at is None or _utc(o.calls_opt_out_at) > when:
            o.calls_opt_out_at = when
        changed += 1
    session.commit()
    return changed


def latest_revocation(session: Session, emails: Iterable[Optional[str]], phone: Optional[str]) -> Optional[dt.datetime]:
    """The most recent revocation for any of these emails or this phone, from both the table and orders."""
    addrs = sorted({e.strip().lower() for e in emails if e and e.strip()})
    stamps = []
    conds = [func.lower(CallOptOut.email).in_(addrs)] if addrs else []
    if phone:
        conds.append(CallOptOut.phone == phone)
    if conds:
        stamps += [_utc(r) for r in session.scalars(select(CallOptOut.revoked_at).where(or_(*conds))).all()]
    oconds = []
    if addrs:
        oconds += [func.lower(Order.buyer_email).in_(addrs), func.lower(Order.contact_email).in_(addrs)]
    if phone:
        oconds.append(Order.contact_phone == phone)
    if oconds:
        stamps += [_utc(r) for r in session.scalars(select(Order.calls_opt_out_at).where(Order.calls_opt_out_at.is_not(None)).where(or_(*oconds))).all()]
    stamps = [s for s in stamps if s is not None]
    return max(stamps) if stamps else None


def phone_in(text: str) -> Optional[str]:
    """A phone number mentioned in free text (e.g. an opt-out email), normalized like checkout answers."""
    return parse_checkout_answer(text or "").phone
