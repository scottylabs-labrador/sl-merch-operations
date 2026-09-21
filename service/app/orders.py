"""Order lifecycle: create from a parsed purchase, send the code, record pickups."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .agentmail import AgentMail, AgentMailError
from .codes import generate_code
from .config import settings
from .db import Order, OrderItem, Pickup, utcnow
from .emails import code_email
from .parser import ParsedOrder
from .qr import qr_png

log = logging.getLogger(__name__)


class DuplicateOrder(Exception):
    def __init__(self, existing: Order):
        super().__init__(f"duplicate of order {existing.id}")
        self.existing = existing


def create_order(
    session: Session,
    parsed: ParsedOrder,
    purchased_at: Optional[dt.datetime] = None,
    source_message_id: Optional[str] = None,
    source_thread_id: Optional[str] = None,
    status: str = "pending",
) -> Order:
    """Persist a parsed purchase with a fresh unique pickup code.

    Raises DuplicateOrder if the TartanConnect reference or the dedup hash has
    been seen before (e.g. the notification was forwarded twice).
    """
    purchased_at = purchased_at or parsed.purchased_at or utcnow()
    minute = purchased_at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")
    dedup = parsed.dedup_override or parsed.dedup_hash(minute)
    if not parsed.buyer_email and status == "pending":
        status = "needs_email"

    existing = session.scalar(select(Order).where(Order.dedup_hash == dedup))
    if existing:
        raise DuplicateOrder(existing)
    if parsed.tc_reference:
        existing = session.scalar(select(Order).where(Order.tc_reference == parsed.tc_reference))
        if existing:
            raise DuplicateOrder(existing)

    for _attempt in range(5):
        order = Order(
            pickup_code=generate_code(),
            tc_reference=parsed.tc_reference,
            dedup_hash=dedup,
            buyer_name=parsed.buyer_name[:200],
            buyer_email=parsed.buyer_email.lower()[:320],
            total_cents=parsed.total_cents,
            purchased_at=purchased_at,
            status=status,
            parse_source=parsed.source,
            source_message_id=source_message_id,
            source_thread_id=source_thread_id,
            notes="; ".join(parsed.warnings) or None,
        )
        for item in parsed.items:
            order.items.append(
                OrderItem(
                    product_name=item.product_name[:300],
                    size=item.size,
                    quantity=item.quantity,
                    unit_price_cents=item.unit_price_cents,
                )
            )
        session.add(order)
        try:
            session.commit()
            session.refresh(order)
            return order
        except IntegrityError as exc:
            session.rollback()
            # Unique-code collision is the only retryable case; anything else re-raises.
            if "pickup_code" in str(exc.orig).lower():
                continue
            existing = session.scalar(select(Order).where(Order.dedup_hash == dedup))
            if existing:
                raise DuplicateOrder(existing) from exc
            raise
    raise RuntimeError("could not allocate a unique pickup code after 5 attempts")


def resolve_buyer_email(session: Session, order: Order, email: str, client: Optional[AgentMail] = None, send: bool = True) -> bool:
    """Attach a buyer email to an order that was created without one, then send the code."""
    order.buyer_email = email.strip().lower()[:320]
    if order.status == "needs_email":
        order.status = "pending"
    order.notes = ((order.notes + "; ") if order.notes else "") + "buyer email resolved"
    session.commit()
    if send and client is not False:
        return send_code_email(session, order, client)
    return True


def find_order_by_reference(session: Session, tc_reference: Optional[str]) -> Optional[Order]:
    if not tc_reference:
        return None
    return session.scalar(select(Order).where(Order.tc_reference == tc_reference))


def send_code_email(session: Session, order: Order, client: Optional[AgentMail] = None) -> bool:
    """Email the buyer their code with an inline QR. Records success/failure on the order."""
    if not order.buyer_email:
        order.code_email_error = "no buyer email yet"
        session.commit()
        return False
    client = client or AgentMail()
    subject, text, html = code_email(order)
    try:
        resp = client.send(
            to=[order.buyer_email],
            subject=subject,
            text=text,
            html=html,
            inline_png=qr_png(order.pickup_code),
            labels=["pickup-code", f"order-{order.id}"],
        )
        order.code_email_message_id = resp.get("message_id")
        order.code_email_thread_id = resp.get("thread_id")
        order.code_email_sent_at = utcnow()
        order.code_email_error = None
        session.commit()
        return True
    except AgentMailError as exc:
        log.error("code email failed for order %s: %s", order.id, exc)
        order.code_email_error = str(exc)[:2000]
        session.commit()
        return False


def record_pickup(
    session: Session,
    order: Order,
    volunteer: str,
    presented_by: Optional[str] = None,
    quantity: Optional[int] = None,
    note: Optional[str] = None,
) -> Tuple[Pickup, bool]:
    """Mark items handed over. Returns (pickup, order_now_complete).

    Idempotent guard: if the order is already fully picked up, no new row is
    written and the existing last pickup is returned.
    """
    remaining = order.quantity_total - order.quantity_picked
    if remaining <= 0:
        return order.pickups[-1], True
    qty = min(quantity or remaining, remaining)
    pickup = Pickup(order_id=order.id, volunteer=volunteer[:200], presented_by=(presented_by or "")[:200] or None, quantity=qty, note=note)
    session.add(pickup)
    order.pickups.append(pickup)
    complete = order.quantity_picked >= order.quantity_total
    if complete:
        order.status = "picked_up"
    session.commit()
    session.refresh(order)
    return pickup, complete


def unpicked_by_size(session: Session):
    """[(size, unpicked_quantity, order_count)] for pending orders."""
    orders = session.scalars(select(Order).where(Order.status.in_(["pending", "needs_email"]))).all()
    totals = {}
    counts = {}
    for order in orders:
        picked = order.quantity_picked
        for item in order.items:
            # Attribute already-picked quantity to items in order; good enough for a bring list.
            take = min(picked, item.quantity)
            picked -= take
            left = item.quantity - take
            if left <= 0:
                continue
            totals[item.size] = totals.get(item.size, 0) + left
            counts[item.size] = counts.get(item.size, 0) + 1
    size_rank = {s: i for i, s in enumerate(["XS", "S", "M", "L", "XL", "XXL", "XXXL"])}
    return sorted(((s, totals[s], counts[s]) for s in totals), key=lambda r: size_rank.get(r[0], 99)), len(orders)
