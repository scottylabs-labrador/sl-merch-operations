"""AgentMail webhook: verify, classify, and act on every inbound email.

Classification rules (in order):
  1. From a trusted sender AND subject mentions a refund            -> refund (freeze orders).
  2. From a trusted sender AND looks like a purchase notification   -> purchase.
  3. Thread matches a code email we sent (buyer replied)            -> buyer_reply -> support agent.
  4. Anything else from anyone                                       -> support agent
     (answers from the knowledge base + sender's orders, or escalates to the org).
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from email.utils import parseaddr
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .agentmail import AgentMail, AgentMailError
from .config import settings
from .db import InboundEmail, Order
from .emails import review_alert_email
from .orders import DuplicateOrder, create_order, find_order_by_reference, resolve_buyer_email, send_code_email
from .parser import ORDER_RE, html_to_text, is_purchase_notification, parse_purchase
from .support import handle_support_email

log = logging.getLogger(__name__)


def verify_signature(secret: str, body: bytes, headers: Dict[str, str]) -> None:
    """Svix-style verification. Raises on bad signature. (Return value of
    Webhook.verify differs across svix versions, so callers parse the body themselves.)"""
    from svix.webhooks import Webhook

    wh = Webhook(secret)
    wh.verify(body, {k.lower(): v for k, v in headers.items()})


def _sender_address(raw_from: str) -> str:
    return (parseaddr(raw_from or "")[1] or raw_from or "").strip().lower()


def _message_text(msg: Dict[str, Any], client: Optional[AgentMail]) -> str:
    text = msg.get("text") or ""
    html = msg.get("html") or ""
    if not text and not html and client:
        # Payloads over 1 MB omit bodies; fetch the message.
        try:
            full = client.get_message(msg["message_id"])
            text, html = full.get("text") or "", full.get("html") or ""
        except AgentMailError as exc:
            log.warning("could not fetch message body: %s", exc)
    return html_to_text(html) if html else text


def _parse_timestamp(value: Optional[str]) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.now(dt.timezone.utc)


def handle_event(session: Session, payload: Dict[str, Any], client: Optional[AgentMail] = None) -> InboundEmail:
    event_type = payload.get("event_type", "")
    msg = payload.get("message") or {}
    event_id = payload.get("event_id")
    message_id = msg.get("message_id", "")
    thread_id = msg.get("thread_id")
    from_addr = _sender_address(msg.get("from") or msg.get("from_") or "")
    subject = msg.get("subject") or ""

    # Exactly-once: Svix retries deliver the same event_id again.
    if event_id:
        seen = session.scalar(select(InboundEmail).where(InboundEmail.event_id == event_id))
        if seen:
            return seen

    record = InboundEmail(
        event_id=event_id,
        message_id=message_id,
        thread_id=thread_id,
        from_addr=from_addr,
        subject=subject[:998],
        raw={"event_type": event_type, "message": {k: v for k, v in msg.items() if k not in ("html",)}},
    )
    session.add(record)
    session.commit()

    if event_type not in ("message.received",):
        # spam / blocked / unauthenticated variants are never acted on.
        record.classification = "ignored"
        record.detail = f"event_type {event_type}"
        record.processed = True
        session.commit()
        return record

    client = client or (AgentMail() if settings.agentmail_api_key else None)
    text = _message_text(msg, client)

    try:
        if from_addr in settings.trusted_senders and re.search(r"refund", subject, re.IGNORECASE):
            _handle_refund(session, record, text, client)
        elif from_addr in settings.trusted_senders and is_purchase_notification(subject, text):
            if settings.email_order_intake:
                _handle_purchase(session, record, msg, text, client)
            else:
                record.classification = "ignored"
                record.detail = "purchase notification; email intake is off, orders come from the store export on /admin"
        elif from_addr in settings.platform_senders:
            record.classification = "ignored"
            record.detail = "platform notification that is not a purchase"
        elif re.search(r"successfully purchased", subject + " " + text[:4000], re.IGNORECASE) and ORDER_RE.search(text):
            if settings.email_order_intake:
                _handle_forwarded_receipt(session, record, msg, text, client)
            else:
                record.classification = "ignored"
                record.detail = "forwarded receipt; email intake is off, orders come from the store export on /admin"
        else:
            matched = session.scalar(select(Order).where(Order.code_email_thread_id == thread_id)) if thread_id else None
            record.classification = "buyer_reply" if matched else "support"
            if matched:
                record.order_id = matched.id
            session.commit()
            handle_support_email(session, record, msg, text, client, matched_order=matched)
    except Exception as exc:  # never let one bad email 500 the webhook forever
        log.exception("webhook handling failed")
        record.classification = "error"
        record.detail = str(exc)[:2000]
    record.processed = True
    session.commit()
    return record


def _handle_purchase(session: Session, record: InboundEmail, msg: Dict[str, Any], text: str, client: Optional[AgentMail]) -> None:
    parsed = parse_purchase(text, msg.get("subject") or "")
    purchased_at = parsed.purchased_at or _parse_timestamp(msg.get("timestamp"))

    if not parsed.is_complete and parsed.is_parsable:
        # Officer notification: everything but the buyer's email. Create the order now
        # (it gets a code) and resolve the email later via forwarded receipt, Sales
        # report upload, or an officer on /admin. If a receipt already created this
        # order, just fill in whatever it lacks.
        existing = find_order_by_reference(session, parsed.tc_reference)
        if existing:
            record.classification = "ignored"
            record.detail = f"duplicate of order {existing.id} ({existing.pickup_code})"
            record.order_id = existing.id
            session.commit()
            return
        try:
            order = create_order(session, parsed, purchased_at=purchased_at, source_message_id=msg.get("message_id"), source_thread_id=msg.get("thread_id"))
        except DuplicateOrder as dup:
            record.classification = "ignored"
            record.detail = f"duplicate of order {dup.existing.id} ({dup.existing.pickup_code})"
            record.order_id = dup.existing.id
            session.commit()
            return
        record.classification = "purchase"
        record.order_id = order.id
        record.detail = f"order {order.id} created via {parsed.source}; awaiting buyer email"
        session.commit()
        return

    if not parsed.is_complete and record.from_addr not in settings.platform_senders:
        # A person (an officer on the trusted list) wrote something that merely
        # mentions the store; treat it as a support conversation.
        from .support import handle_support_email

        matched = session.scalar(select(Order).where(Order.code_email_thread_id == msg.get("thread_id"))) if msg.get("thread_id") else None
        record.classification = "buyer_reply" if matched else "support"
        if matched:
            record.order_id = matched.id
        session.commit()
        handle_support_email(session, record, msg, text, client, matched_order=matched)
        return

    if not parsed.is_complete:
        record.classification = "needs_review"
        record.detail = "; ".join(parsed.warnings) or "could not extract buyer/items"
        session.commit()
        if client:
            subj, body = review_alert_email(msg.get("subject") or "", record.from_addr, record.detail, text)
            try:
                client.send(to=[settings.org_email], subject=subj, text=body, labels=["needs-review"])
            except AgentMailError as exc:
                log.error("review alert failed: %s", exc)
        return

    existing = find_order_by_reference(session, parsed.tc_reference)
    if existing and not existing.buyer_email:
        resolve_buyer_email(session, existing, parsed.buyer_email, client)
        record.classification = "purchase"
        record.order_id = existing.id
        record.detail = f"order {existing.id}: buyer email resolved from {parsed.kind}; code sent"
        session.commit()
        return
    try:
        order = create_order(
            session,
            parsed,
            purchased_at=purchased_at,
            source_message_id=msg.get("message_id"),
            source_thread_id=msg.get("thread_id"),
        )
    except DuplicateOrder as dup:
        record.classification = "ignored"
        record.detail = f"duplicate of order {dup.existing.id} ({dup.existing.pickup_code})"
        record.order_id = dup.existing.id
        session.commit()
        return

    record.classification = "purchase"
    record.order_id = order.id
    record.detail = f"order {order.id} created via {parsed.source}"
    session.commit()

    if client:
        send_code_email(session, order, client)


def _handle_refund(session: Session, record: InboundEmail, text: str, client: Optional[AgentMail]) -> None:
    """A TartanConnect refund-request notification: freeze matching pending orders.

    We never cancel automatically (the officer may deny the refund); the order
    goes to needs_review so the pickup page refuses it until a human decides.
    """
    from .parser import EMAIL_RE, _org_addresses

    ours = set(_org_addresses())
    emails = [e.lower() for e in EMAIL_RE.findall(text) if e.lower() not in ours and "tartanconnect" not in e.lower()]
    frozen = []
    for email in dict.fromkeys(emails):
        for order in session.scalars(select(Order).where(Order.buyer_email == email, Order.status == "pending")).all():
            order.status = "needs_review"
            order.notes = ((order.notes + "; ") if order.notes else "") + "refund request received; verify before handing out"
            frozen.append(order)
    session.commit()
    record.classification = "refund"
    record.detail = f"froze {len(frozen)} order(s): " + ", ".join(o.pickup_code for o in frozen) if frozen else "refund notice; no pending order matched"
    if frozen:
        record.order_id = frozen[0].id
    session.commit()
    if client:
        try:
            client.forward(record.message_id, [settings.org_email], text=f"Refund request. {record.detail}. Decide on /admin, then set the order to cancelled or back to pending.")
        except AgentMailError as exc:
            record.detail += f"; forward failed: {exc}"
            session.commit()


def _handle_forwarded_receipt(session: Session, record: InboundEmail, msg: Dict[str, Any], text: str, client: Optional[AgentMail]) -> None:
    """A buyer forwarded their TartanConnect receipt to the inbox.

    The sender's address is the buyer's address. We attach it to the matching
    order (by order number) and send the code. Guards: the order must not already
    belong to a different address, and the receipt's buyer name must match the
    order's name; otherwise a human decides.
    """
    from .support import handle_support_email

    parsed = parse_purchase(text, msg.get("subject") or "")
    from_addr = record.from_addr
    order = find_order_by_reference(session, parsed.tc_reference)
    receipt_email = parsed.buyer_email or from_addr
    if order is None:
        # Receipt arrived before (or instead of) the officer notification.
        if parsed.items:
            parsed.buyer_email = receipt_email
            parsed.source = "buyer_receipt"
            try:
                order = create_order(session, parsed, source_message_id=msg.get("message_id"), source_thread_id=msg.get("thread_id"))
            except DuplicateOrder as dup:
                order = dup.existing
            record.classification = "purchase"
            record.order_id = order.id
            record.detail = f"order {order.id} created from a forwarded receipt"
            session.commit()
            if order.buyer_email and not order.code_email_sent_at:
                send_code_email(session, order, client)
            return
        record.classification = "support"
        session.commit()
        handle_support_email(session, record, msg, text, client)
        return

    first_name_ok = (not parsed.buyer_name) or order.buyer_name.lower().startswith(parsed.buyer_name.split(" ")[0].lower())
    if not order.buyer_email and first_name_ok:
        resolve_buyer_email(session, order, receipt_email, client)
        record.classification = "purchase"
        record.order_id = order.id
        record.detail = f"order {order.id}: buyer email resolved from forwarded receipt; code sent"
        session.commit()
        return
    if order.buyer_email and order.buyer_email == from_addr:
        send_code_email(session, order, client)
        record.classification = "purchase"
        record.order_id = order.id
        record.detail = f"order {order.id}: receipt forwarded by buyer; code re-sent"
        session.commit()
        return
    record.classification = "support"
    record.order_id = order.id
    record.detail = "forwarded receipt does not match the order's buyer; escalated"
    session.commit()
    if client:
        try:
            client.forward(record.message_id, [settings.org_email], text=f"Receipt for order #{parsed.tc_reference} forwarded by {from_addr}, but order {order.id} belongs to {order.buyer_email or order.buyer_name}. Please check before releasing a code.")
        except AgentMailError as exc:
            record.detail += f"; forward failed: {exc}"
            session.commit()
