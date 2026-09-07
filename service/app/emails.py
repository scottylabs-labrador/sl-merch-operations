"""Outbound email content. Plain text first (always readable), HTML mirrors it."""
from __future__ import annotations

import html
from typing import Iterable, List, Tuple

from .config import settings
from .db import Order


def _rules_text() -> str:
    return (
        f"How pickup works\n"
        f"- Pickup only, no shipping. Merch is handed out in person at {settings.gbm_info}\n"
        f"- Show this code (or the QR image) to the volunteer at the merch table.\n"
        f"- Can't make it? Send a friend with the code. Whoever presents it gets the order.\n"
        f"- Each code works once. Once the order is handed over, the code is used up.\n"
        f"- Keep your TartanConnect receipt as backup proof of purchase.\n"
    )


def _rules_html() -> str:
    return (
        "<h3 style='margin:24px 0 8px'>How pickup works</h3>"
        "<ul style='line-height:1.5'>"
        f"<li><strong>Pickup only, no shipping.</strong> Merch is handed out in person at {html.escape(settings.gbm_info)}</li>"
        "<li>Show this code (or the QR image) to the volunteer at the merch table.</li>"
        "<li>Can't make it? Send a friend with the code. Whoever presents it gets the order.</li>"
        "<li><strong>Each code works once.</strong> Once the order is handed over, the code is used up.</li>"
        "<li>Keep your TartanConnect receipt as backup proof of purchase.</li>"
        "</ul>"
    )


def code_email(order: Order) -> Tuple[str, str, str]:
    """Return (subject, text, html) for the pickup-code email. QR is referenced as cid:qr."""
    items_text = "\n".join(f"  - {i.quantity} x {i.label}" for i in order.items)
    items_html = "".join(f"<li>{i.quantity} &times; {html.escape(i.label)}</li>" for i in order.items)
    first = order.buyer_name.split(" ")[0] if order.buyer_name else "there"
    subject = f"Your {settings.org_name} pickup code: {order.pickup_code}"
    text = (
        f"Hi {first},\n\n"
        f"Thanks for your order from the {settings.store_name}. Here is your pickup code:\n\n"
        f"    {order.pickup_code}\n\n"
        f"Your order:\n{items_text}\n\n"
        f"{_rules_text()}\n"
        f"Questions? Just reply to this email or write to {settings.org_email}.\n\n"
        f"{settings.org_name}\n"
    )
    html_body = (
        "<div style='font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:560px;margin:auto;color:#111'>"
        f"<p>Hi {html.escape(first)},</p>"
        f"<p>Thanks for your order from the {html.escape(settings.store_name)}. Here is your pickup code:</p>"
        f"<p style='font-size:30px;font-weight:700;letter-spacing:2px;font-family:Menlo,Consolas,monospace;margin:12px 0'>{order.pickup_code}</p>"
        "<p><img src='cid:qr' alt='QR code for your pickup code' width='180' height='180' style='display:block;border:1px solid #ddd;border-radius:8px'></p>"
        f"<p><strong>Your order</strong></p><ul>{items_html}</ul>"
        f"{_rules_html()}"
        f"<p>Questions? Just reply to this email or write to <a href='mailto:{settings.org_email}'>{settings.org_email}</a>.</p>"
        f"<p>{html.escape(settings.org_name)}</p>"
        "</div>"
    )
    return subject, text, html_body


def bring_list_email(rows: Iterable[Tuple[str, int, int]], pending_orders: int, awaiting_email: int = 0) -> Tuple[str, str]:
    """rows: (size, unpicked_quantity, orders) -> (subject, text)."""
    rows = list(rows)
    lines = [f"  {size or 'no size':>8}: {qty:>3} shirts across {orders} orders" for size, qty, orders in rows]
    subject = f"[{settings.org_name} merch] Bring list for this week's GBM: {sum(r[1] for r in rows)} items, {pending_orders} orders"
    text = (
        "Unpicked orders by size (bring at least this many of each):\n\n"
        + ("\n".join(lines) if lines else "  nothing pending")
        + f"\n\nVerification page: {settings.public_base_url}/pickup\n"
        f"Admin view: {settings.public_base_url}/admin\n"
    )
    if awaiting_email:
        text += (
            f"\n{awaiting_email} order(s) have a code but no buyer email yet (TartanConnect's officer notification omits it). "
            f"To send their codes: Store > Sales > Generate Report, then upload the CSV at {settings.public_base_url}/admin. "
            "Buyers can also forward their TartanConnect receipt to the merch inbox to get the code instantly.\n"
        )
    return subject, text


def review_alert_email(subject: str, from_addr: str, reason: str, excerpt: str) -> Tuple[str, str]:
    subj = f"[{settings.org_name} merch] Needs review: {subject or '(no subject)'}"
    text = (
        f"The merch service received an email it could not fully process.\n\n"
        f"From: {from_addr}\nSubject: {subject}\nReason: {reason}\n\n"
        f"First lines:\n{excerpt[:1500]}\n\n"
        f"Open the admin page to create the order by hand: {settings.public_base_url}/admin\n"
    )
    return subj, text


def auto_reply_text(order: Order, intent: str) -> str:
    base = (
        f"Hi {order.buyer_name.split(' ')[0] if order.buyer_name else 'there'},\n\n"
        f"Your pickup code is {order.pickup_code} (order: {order.summary()}).\n\n"
    )
    if intent == "delegate":
        base += (
            "Yes, someone else can pick it up for you. Give them the code above; whoever presents it "
            "receives the order and the code is then used up.\n\n"
        )
    elif intent == "cant_make_it":
        base += (
            "No problem. Codes do not expire, so bring it to any upcoming GBM, or send a friend with it.\n\n"
        )
    base += f"Pickup is in person only at {settings.gbm_info}\n\n{settings.org_name}\n"
    return base
