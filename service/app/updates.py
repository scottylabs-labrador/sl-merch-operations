"""Officer updates to buyers (/admin/updates).

An officer tells the agent what buyers need to hear ("no pickup this Saturday; a makeup pickup will be
announced"). The model drafts ONE subject and body with placeholders such as {first_name} and {items}; the app
fills those in from each order and shows the officer every personalised email; nothing is sent until the
officer approves that exact text. The officer can ask for changes or edit by hand in between.

Why a template: the model never sees a buyer's name, email, code or items, and each email can only carry its
own order's details, because the details come from the database rather than from the model. Sending claims
each message before it goes out, so a double click or a second tab cannot send anything twice.

An update can also leave a notice for the automated support desk (app/support.py), so its replies agree with
what buyers were just told until the notice expires or an officer clears it.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import re
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from sqlalchemy import select, update as sql_update
from sqlalchemy.orm import Session

from . import decide
from .agentmail import AgentMail, AgentMailError
from .config import settings
from .db import CustomerUpdate, CustomerUpdateMessage, DeskNotice, Order, SessionLocal, utcnow
from .llm import structured_json

log = logging.getLogger(__name__)

PLACEHOLDERS: Dict[str, str] = {
    "first_name": "the buyer's first name",
    "full_name": "the buyer's full name as typed at checkout",
    "items": "the items still waiting for them, one per line, like '- 1 x ScottyLabs Found T-Shirt, size M'",
    "item_summary": "the same items on one line, like '1 x ScottyLabs Found T-Shirt, size M'",
    "order_date": "the day they ordered, like 'Sep 25'",
    "pickup_code": "their pickup code (only when the officer asks for codes)",
}
_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_CODE = re.compile(r"\bSL[-\s]?[A-Z0-9]{4}[-\s]?[A-Z0-9]{4}\b", re.I)
MAX_SUBJECT, MAX_BODY = 200, 5000

DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
        "format_note": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "desk_notice": {"type": "string"},
        "desk_notice_until": {"type": "string"},
    },
    "required": ["subject", "body", "format_note", "assumptions", "desk_notice", "desk_notice_until"],
}


# --------------------------------------------------------------------------- #
# time
# --------------------------------------------------------------------------- #
def _tz():
    try:
        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001 - a bad TIMEZONE must not break the page
        return dt.timezone.utc


def _aware(when: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """SQLite hands back naive datetimes; everything here is stored in UTC."""
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def local_now() -> dt.datetime:
    return dt.datetime.now(_tz())


def end_of_day(day: str) -> Optional[dt.datetime]:
    """'2026-09-27' -> the last second of that day in the org's time zone (as UTC). None when blank or unreadable."""
    try:
        d = dt.date.fromisoformat((day or "").strip()[:10])
    except ValueError:
        return None
    return dt.datetime.combine(d, dt.time(23, 59, 59), tzinfo=_tz()).astimezone(dt.timezone.utc)


def local_day(when: Optional[dt.datetime]) -> str:
    return _aware(when).astimezone(_tz()).strftime("%Y-%m-%d") if when else ""


# --------------------------------------------------------------------------- #
# who gets it, and what their copy says
# --------------------------------------------------------------------------- #
def _item_name(product_name: str) -> str:
    """'ScottyLabs Found T-Shirt, Size S (Pittsburgh Pickup Only, No Refunds)' -> 'ScottyLabs Found T-Shirt'."""
    name = re.sub(r"\s*\([^)]*\)", "", product_name or "").strip()
    name = re.sub(r"[\s,—–-]*\bsize\s+\w+\s*$", "", name, flags=re.I).strip(" ,—–-")
    return name or "item"


def remaining_items(order: Order) -> List[Tuple[int, str]]:
    """(quantity, label) still to be collected; already-picked quantity is attributed to items in order."""
    picked = order.quantity_picked
    out = []
    for item in order.items:
        take = min(picked, item.quantity)
        picked -= take
        if item.quantity - take > 0:
            out.append((item.quantity - take, _item_name(item.product_name) + (f", size {item.size}" if item.size else "")))
    return out


def values_for(order: Order) -> Dict[str, str]:
    items = remaining_items(order) or [(i.quantity, _item_name(i.product_name) + (f", size {i.size}" if i.size else "")) for i in order.items]
    name = (order.buyer_name or "").strip()
    return {
        "first_name": name.split(" ")[0] if name else "there",
        "full_name": name or "there",
        "items": "\n".join(f"- {q} x {label}" for q, label in items),
        "item_summary": ", ".join(f"{q} x {label}" for q, label in items),
        "order_date": _aware(order.purchased_at).astimezone(_tz()).strftime("%b %-d") if order.purchased_at else "",
        "pickup_code": order.pickup_code,
    }


def render(template: str, values: Dict[str, str]) -> str:
    return _TOKEN.sub(lambda m: values.get(m.group(1), m.group(0)), template or "")


def template_problems(subject: str, body: str) -> List[str]:
    """Anything that must be fixed before the update can be sent."""
    problems = []
    if not (subject or "").strip():
        problems.append("The subject is empty.")
    if not (body or "").strip():
        problems.append("The email body is empty.")
    unknown = sorted({m.group(1) for m in _TOKEN.finditer(f"{subject}\n{body}")} - set(PLACEHOLDERS))
    if unknown:
        problems.append("Unknown placeholder(s) " + ", ".join("{" + u + "}" for u in unknown) + ". Use only " + ", ".join("{" + k + "}" for k in PLACEHOLDERS) + ".")
    for m in _CODE.finditer(f"{subject} {body}"):
        if not re.fullmatch(r"SL[-\s]?X{4}[-\s]?X{4}", m.group(0), re.I):
            problems.append(f"The text contains a literal pickup code ({m.group(0)}). Use {{pickup_code}} so each buyer only ever sees their own.")
            break
    if len(subject or "") > MAX_SUBJECT:
        problems.append(f"The subject is longer than {MAX_SUBJECT} characters.")
    if len(body or "") > MAX_BODY:
        problems.append(f"The body is longer than {MAX_BODY} characters.")
    return problems


def recipients(session: Session, audience: str, order_id: Optional[int] = None) -> Tuple[List[Order], List[Tuple[Order, str]]]:
    """(orders that get the email, [(order, why not)]). 'pending' = every order with items still to collect."""
    if audience == "order":
        order = session.get(Order, order_id) if order_id else None
        if not order:
            return [], []
        if not order.buyer_email:
            return [], [(order, "no buyer email on this order")]
        return [order], []
    rows = session.scalars(select(Order).where(Order.status.in_(["pending", "needs_email", "needs_review"])).order_by(Order.purchased_at)).all()
    ok, skipped = [], []
    for o in rows:
        if o.quantity_picked >= o.quantity_total:
            continue
        if o.status == "needs_review":
            skipped.append((o, "on hold (needs review); message it on its own if it should hear this"))
        elif not o.buyer_email or o.status == "needs_email":
            skipped.append((o, "no buyer email yet"))
        else:
            ok.append(o)
    return ok, skipped


def _describe(audience: str, ok: List[Order], skipped: List[Tuple[Order, str]]) -> str:
    """What the model is told about the recipients: counts and order state only, never who they are."""
    if audience == "order":
        o = ok[0] if ok else (skipped[0][0] if skipped else None)
        if not o:
            return "one buyer (order not found)"
        state = {"pending": "waiting for pickup", "picked_up": "already picked up", "cancelled": "cancelled"}.get(o.status, o.status.replace("_", " "))
        return f"one buyer, about their own order (status: {state})"
    return f"{len(ok)} buyer(s) whose orders are waiting for pickup"


def build_previews(session: Session, upd: CustomerUpdate) -> None:
    """Re-render every recipient's copy of the current revision (only while it is a draft)."""
    if upd.status != "draft":
        return
    upd.messages.clear()                      # delete-orphan removes the old copies
    session.flush()
    ok, skipped = recipients(session, upd.audience, upd.order_id)
    for o in ok:
        vals = values_for(o)
        upd.messages.append(CustomerUpdateMessage(order_id=o.id, to_email=o.buyer_email, subject=render(upd.subject, vals)[:300], body=render(upd.body, vals), status="preview"))
    for o, why in skipped:
        upd.messages.append(CustomerUpdateMessage(order_id=o.id, to_email=o.buyer_email or "", status="skipped", note=why))
    upd.warnings = _warnings(upd, ok)


def _warnings(upd: CustomerUpdate, ok: List[Order]) -> List[str]:
    """Advisory checks shown next to the preview; the officer decides."""
    out = []
    seen: Dict[str, int] = {}
    for o in ok:
        seen[o.buyer_email] = seen.get(o.buyer_email, 0) + 1
    repeat = [e for e, n in seen.items() if n > 1]
    if repeat:
        out.append(f"{len(repeat)} buyer(s) have more than one order waiting; each order gets its own email.")
    if "{pickup_code}" in f"{upd.subject}{upd.body}":
        out.append("Includes pickup codes: each buyer gets only their own, at the address on that order.")
    if upd.body.strip() and decide.enabled():
        guard = decide.guard_reply(render(upd.body, {k: f"[{k}]" for k in PLACEHOLDERS}), {"pickup": settings.pickup_info, "shipping": "none, never", "refunds": "none; all sales final", "codes": "never expire, one use each"})
        if guard is not None:
            if guard.promises_money >= decide.GUARD_THRESHOLD:
                out.append("It may promise a refund, credit or exchange. All sales are final.")
            if guard.invents_logistics >= decide.GUARD_THRESHOLD:
                out.append("It names a pickup date, time or place that differs from the saved pickup info. Check it is exactly right.")
            if guard.commits_org >= decide.GUARD_THRESHOLD:
                out.append(f"It may commit {settings.org_name} to something special (shipping, holding an item, a one-off arrangement).")
    return out


# --------------------------------------------------------------------------- #
# the model's part: one template per revision
# --------------------------------------------------------------------------- #
def _system_prompt() -> str:
    names = "\n".join(f"  {{{k}}}: {v}" for k, v in PLACEHOLDERS.items())
    now = local_now()
    return (
        f"You draft short emails that {settings.org_name} officers send to buyers from the {settings.store_name}. "
        "Write ONE subject and ONE body. The system sends it to each recipient and fills in these placeholders from "
        "that buyer's own order; write them exactly like this, with curly braces:\n"
        f"{names}\n\n"
        "Rules:\n"
        f"- Start the body with 'Hi {{first_name}},' and end it with the sign-off '{settings.org_name} Merch'.\n"
        "- Say what the officer asked, clearly and warmly, in two to four short paragraphs. Plain text only: no "
        "markdown, no bold, no headings. Put {items} on its own lines when you list the order.\n"
        "- Use only facts from the officer's message and the store facts below. Never invent a date, time, room, "
        "price or policy. When the officer says something is still to be decided or will be communicated, say it "
        "will be announced by email, without guessing.\n"
        "- Turn relative dates ('this Saturday', 'this work session', 'tomorrow') into calendar dates using today's "
        "date below. Every time you do, add an entry to assumptions saying how you read it (for example \"'this work "
        "session' = Saturday, September 26\"), so the officer can check it.\n"
        "- Include {pickup_code} only if the officer asks for codes. Never write a code yourself.\n"
        "- Never promise refunds, exchanges, shipping or delivery.\n"
        "- format_note: one or two sentences telling the officer what each person will receive.\n"
        "- desk_notice: if this changes what buyers should be told about pickup, one or two sentences the automated "
        "support desk will tell anyone who asks while it is current (no placeholders, no names); otherwise ''.\n"
        "- desk_notice_until: the last date that notice applies, as YYYY-MM-DD, or '' when open-ended or not needed.\n\n"
        "Store facts:\n"
        f"- Regular pickup: {settings.pickup_info}\n"
        "- Pickup is in person only; merch is never shipped. All sales are final. Codes never expire, and anyone "
        "holding a code may collect that order.\n"
        f"- Buyers with questions can reply to the email or write to {settings.org_email}.\n\n"
        f"Today is {now.strftime('%A, %B %-d, %Y')}, {now.strftime('%-I:%M %p %Z')}."
    )


def draft_with_model(instruction: str, audience: str, current: Optional[Tuple[str, str]] = None, feedback: str = "") -> Optional[dict]:
    user = f"Officer's message:\n{instruction.strip()[:4000]}\n\nRecipients: {audience}"
    if current:
        user += f"\n\nCurrent draft\nSubject: {current[0]}\n\n{current[1]}\n\nThe officer asks for these changes:\n{feedback.strip()[:4000]}\n\nReturn the whole revised draft."
    out = structured_json(label="update draft", system=_system_prompt(), user=user, schema_name="customer_update", schema=DRAFT_SCHEMA)
    if not out or not str(out.get("body", "")).strip():
        return None
    return out


def _say(upd: CustomerUpdate, who: str, text: str) -> None:
    upd.history = list(upd.history or []) + [{"who": who, "text": text.strip()[:4000], "at": utcnow().isoformat()}]


def _apply(upd: CustomerUpdate, draft: Optional[dict]) -> None:
    if draft is None:
        _say(upd, "agent", "I couldn't draft this (no model key, or the model call failed). Write the email in the editor below, or ask again.")
        return
    upd.subject = str(draft["subject"]).strip()[:MAX_SUBJECT]
    upd.body = str(draft["body"]).strip()
    upd.format_note = str(draft["format_note"]).strip() or None
    upd.assumptions = "\n".join(a.strip() for a in draft.get("assumptions") or [] if a.strip()) or None
    upd.desk_notice = str(draft["desk_notice"]).strip() or None
    upd.desk_notice_until = end_of_day(draft["desk_notice_until"]) if upd.desk_notice else None
    _say(upd, "agent", upd.format_note or "Here's a draft.")


def create_update(session: Session, instruction: str, audience: str, order_id: Optional[int] = None) -> CustomerUpdate:
    audience = "order" if audience == "order" else "pending"
    ok, skipped = recipients(session, audience, order_id)
    upd = CustomerUpdate(instruction=instruction.strip()[:4000], audience=audience, order_id=order_id if audience == "order" else None, revision=1)
    _say(upd, "officer", instruction)
    session.add(upd)
    _apply(upd, draft_with_model(instruction, _describe(audience, ok, skipped)))
    session.flush()
    build_previews(session, upd)
    session.commit()
    return upd


def revise_update(session: Session, upd: CustomerUpdate, feedback: str) -> None:
    ok, skipped = recipients(session, upd.audience, upd.order_id)
    _say(upd, "officer", feedback)
    _apply(upd, draft_with_model(upd.instruction, _describe(upd.audience, ok, skipped), (upd.subject, upd.body), feedback))
    upd.revision += 1
    build_previews(session, upd)
    session.commit()


def edit_update(session: Session, upd: CustomerUpdate, subject: str, body: str, desk_notice: str = "", desk_notice_until: str = "") -> None:
    upd.subject = subject.strip()[:MAX_SUBJECT]
    upd.body = body.replace("\r\n", "\n").strip()
    upd.desk_notice = desk_notice.strip() or None
    upd.desk_notice_until = end_of_day(desk_notice_until) if upd.desk_notice else None
    upd.revision += 1
    _say(upd, "officer", "Edited the draft by hand.")
    build_previews(session, upd)
    session.commit()


# --------------------------------------------------------------------------- #
# sending
# --------------------------------------------------------------------------- #
def start_sending(session: Session, upd: CustomerUpdate, revision: int, include: set, notice: bool) -> Tuple[bool, str]:
    """Lock the approved revision for sending. Returns (ok, reason)."""
    if upd.status != "draft":
        return False, "this update was already sent or discarded"
    if revision != upd.revision:
        return False, "the draft changed since you opened it; review the new previews"
    problems = template_problems(upd.subject, upd.body)
    if problems:
        return False, problems[0]
    if not any(m.status == "preview" and m.id in include for m in upd.messages):
        return False, "no recipients selected"
    claimed = session.execute(sql_update(CustomerUpdate).where(CustomerUpdate.id == upd.id, CustomerUpdate.status == "draft").values(status="sending"))
    if claimed.rowcount != 1:
        session.rollback()
        return False, "this update was already sent"
    upd.status = "sending"
    for m in upd.messages:
        if m.status == "preview" and m.id not in include:
            m.status, m.note = "skipped", "left out by the officer"
    if not notice:
        upd.desk_notice, upd.desk_notice_until = None, None
    _say(upd, "officer", f"Approved revision {upd.revision} and sent it.")
    session.commit()
    return True, "sending"


def _still_eligible(upd: CustomerUpdate, order: Optional[Order], to_email: str) -> Optional[str]:
    if order is None:
        return "the order no longer exists"
    if (order.buyer_email or "") != to_email:
        return "the order's email changed after the preview"
    if order.status == "cancelled":
        return "the order was cancelled after the preview"
    if upd.audience == "pending" and (order.status != "pending" or order.quantity_picked >= order.quantity_total):
        return "no longer waiting for pickup"
    return None


def html_body(text: str) -> str:
    paragraphs = [p for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]
    inner = "".join("<p>" + "<br>".join(html.escape(line) for line in p.split("\n")) + "</p>" for p in paragraphs)
    return f"<div style='font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:560px;margin:auto;color:#111;line-height:1.5'>{inner}</div>"


def deliver(update_id: int, client: Optional[AgentMail] = None) -> None:
    """Send every approved message that has not gone out yet. Safe to run twice at once: each message is
    claimed (preview -> sending) before it is sent."""
    with SessionLocal() as session:
        upd = session.get(CustomerUpdate, update_id)
        if not upd or upd.status != "sending":
            return
        try:
            client = client or AgentMail()
        except AgentMailError as exc:
            client = None
            log.error("update %s: cannot send: %s", update_id, exc)
        for m in list(upd.messages):
            if m.status != "preview":
                continue
            claimed = session.execute(sql_update(CustomerUpdateMessage).where(CustomerUpdateMessage.id == m.id, CustomerUpdateMessage.status == "preview").values(status="sending"))
            session.commit()
            if claimed.rowcount != 1:
                continue
            session.refresh(m)
            why = _still_eligible(upd, m.order, m.to_email)
            if why:
                m.status, m.note = "skipped", why
            elif client is None:
                m.status, m.note = "failed", "AgentMail is not configured"
            else:
                try:
                    resp = client.send(to=[m.to_email], subject=m.subject, text=m.body, html=html_body(m.body), labels=["update", f"update-{upd.id}", f"order-{m.order_id}"])
                    m.status, m.note = "sent", None
                    m.message_id, m.thread_id, m.sent_at = resp.get("message_id"), resp.get("thread_id"), utcnow()
                    stamp = local_now().strftime("%b %-d %-I:%M %p")
                    m.order.notes = ((m.order.notes + "; ") if m.order.notes else "") + f"update #{upd.id} emailed {stamp}"
                except AgentMailError as exc:
                    log.error("update %s to order %s failed: %s", upd.id, m.order_id, exc)
                    m.status, m.note = "failed", str(exc)[:500]
            session.commit()
        states = [m.status for m in upd.messages]
        upd.status = "partial" if "failed" in states else "sent"
        upd.sent_at = upd.sent_at or utcnow()
        if upd.desk_notice and not session.scalar(select(DeskNotice).where(DeskNotice.update_id == upd.id)):
            session.add(DeskNotice(text=upd.desk_notice, expires_at=upd.desk_notice_until, update_id=upd.id))
        session.commit()


def retry_failed(session: Session, upd: CustomerUpdate) -> bool:
    """Queue the failed messages of a partly sent update again."""
    if upd.status != "partial":
        return False
    for m in upd.messages:
        if m.status == "failed":
            m.status, m.note = "preview", None
    upd.status = "sending"
    _say(upd, "officer", "Retried the failed emails.")
    session.commit()
    return True


# --------------------------------------------------------------------------- #
# support desk notices
# --------------------------------------------------------------------------- #
def active_notices(session: Session) -> List[DeskNotice]:
    now = utcnow()
    rows = session.scalars(select(DeskNotice).where(DeskNotice.cleared_at.is_(None)).order_by(DeskNotice.created_at)).all()
    return [n for n in rows if n.expires_at is None or _aware(n.expires_at) > now]


def notice_text(session: Session) -> str:
    return " ".join(n.text.strip() for n in active_notices(session))


def add_notice(session: Session, text: str, until: str = "") -> Optional[DeskNotice]:
    if not text.strip():
        return None
    n = DeskNotice(text=text.strip()[:1000], expires_at=end_of_day(until))
    session.add(n)
    session.commit()
    return n


def clear_notice(session: Session, notice_id: int) -> None:
    n = session.get(DeskNotice, notice_id)
    if n and n.cleared_at is None:
        n.cleared_at = utcnow()
        session.commit()
