"""Support agent for the merch inbox.

Every inbound email that is not a purchase or refund notification lands here:
replies to code emails, questions from people who have not bought yet, and
anything else addressed to the inbox. The agent answers from a fixed knowledge
base plus the sender's own orders, and escalates to a human for everything it
cannot settle from those facts.

Guardrails (all enforced in code, not just in the prompt):
  * Orders are looked up ONLY by the sender's address. A code is never sent to
    an address that did not place the order.
  * Automated mail (Auto-Submitted, Precedence bulk/list, our own address) is
    never answered.
  * At most one auto-reply per thread per 24 hours and three per thread total;
    after that the thread goes to a human.
  * Money, refunds, exchanges, complaints, and low-confidence answers escalate.
  * Without OPENROUTER_API_KEY, everything is forwarded to the org inbox.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .agentmail import AgentMail, AgentMailError
from .config import settings
from . import decide
from .emails import auto_reply_text
from .llm import structured_json
from .db import InboundEmail, Order

log = logging.getLogger(__name__)

AUTO_REPLY_PREFIX = "auto-replied"
MAX_AUTO_REPLIES_PER_THREAD = 3

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["reply", "escalate", "ignore"]},
        "category": {
            "type": "string",
            "enum": [
                "lost_code", "delegate", "cant_make_it", "pickup_logistics", "order_status",
                "sizing_or_product", "how_to_buy", "about_org", "refund_or_money", "complaint", "spam_or_unrelated", "other",
            ],
        },
        "reply_text": {"type": "string"},
        "summary_for_officers": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["action", "category", "reply_text", "summary_for_officers", "confidence"],
}

ESCALATE_ALWAYS = {"refund_or_money", "complaint", "other"}


def knowledge_base() -> str:
    return f"""
ORGANIZATION: {settings.org_name} is Carnegie Mellon's student-run software and tech organization (projects, events,
and hackathons such as TartanHacks). Learn more at https://scottylabs.org. Events, GBMs, and meeting times are posted
at https://luma.com/scottylabs. Human contact: {settings.org_email}. For questions about joining, projects, or events,
point people to those links and the org email; you only handle the merch store.
STORE: {settings.store_name} on TartanConnect (CMU's CampusGroups site). Products: the ScottyLabs "Found" T-Shirt,
a black Gildan Softstyle unisex jersey tee with a double-sided print (ScottyLabs logo on the front chest, the
"scottylabs found!" globe with Pittsburgh coordinates on the back). Sold at cost, $10, one listing per size
(XS, S, M, L, XL, XXL). Sizes are unisex/men's cut; suggest sizing down one size for a fitted look.
Inventory is tracked per size on TartanConnect; when a size sells out its listing shows "sold out".
PICKUP: In person only at {settings.gbm_info} No shipping, no exceptions. After buying, the buyer gets a unique
pickup code (format SL-XXXX-XXXX) by email from this inbox, with a QR image. Codes never expire. Anyone holding
the code may collect the order (delegate/friend pickup is fine). Each code works exactly once. If the buyer
cannot attend, they bring the code to any later GBM or send a friend.
LOST CODE: If the sender's address has an order in the context below, restate the code. If it does not, say no
order was found under this address, ask them to reply from the email used at checkout, and escalate.
PAYMENT: Payments run through TartanConnect/CMU CashNet; this inbox never sees card details. The receipt from
TartanConnect is proof of purchase. Refunds and size exchanges are decided by officers: explain that a person
will follow up, and escalate. Do not promise a refund or exchange.
TONE: Friendly, brief, plain. Two short paragraphs at most. Sign off as "{settings.org_name} Merch Desk (automated)".
Never invent dates, rooms, prices, stock counts, or policies that are not in this document or the order context.
"""


def _orders_for_sender(session: Session, email: str) -> List[Order]:
    if not email:
        return []
    return session.scalars(select(Order).where(Order.buyer_email == email.lower()).order_by(Order.purchased_at.desc())).all()


def _order_context(orders: List[Order]) -> str:
    if not orders:
        return "No orders found for the sender's email address."
    parts = []
    for o in orders:
        last = o.pickups[-1] if o.pickups else None
        parts.append(
            f"- Order {o.id}: code {o.pickup_code}; status {o.status}; items {o.summary()}; "
            f"ordered {o.purchased_at.strftime('%b %d, %Y') if o.purchased_at else 'unknown'}; "
            f"picked up {o.quantity_picked}/{o.quantity_total}"
            + (f" (last handoff {last.picked_up_at.strftime('%b %d')} by {last.volunteer}" + (f" to {last.presented_by}" if last.presented_by else "") + ")" if last else "")
        )
    return "\n".join(parts)


def _is_automated(msg: Dict[str, Any], from_addr: str) -> bool:
    headers = {str(k).lower(): str(v).lower() for k, v in (msg.get("headers") or {}).items()}
    if from_addr == settings.agentmail_inbox_id.lower():
        return True
    local = from_addr.split("@")[0]
    if local in ("mailer-daemon", "postmaster", "noreply", "no-reply", "donotreply", "do-not-reply") or local.endswith(("-noreply", "-no-reply", "noreply")):
        return True
    if headers.get("auto-submitted", "no") not in ("", "no"):
        return True
    if any(tok in headers.get("precedence", "") for tok in ("bulk", "list", "junk")):
        return True
    if "x-autoreply" in headers or "x-autorespond" in headers:
        return True
    return False


def _prior_auto_replies(session: Session, thread_id: Optional[str]) -> List[InboundEmail]:
    if not thread_id:
        return []
    rows = session.scalars(select(InboundEmail).where(InboundEmail.thread_id == thread_id)).all()
    return [r for r in rows if (r.detail or "").startswith(AUTO_REPLY_PREFIX)]


def ask_model(context: str, email_text: str, subject: str, from_addr: str) -> Optional[Dict[str, Any]]:
    """Ask the model for a structured support decision. None when no key is set or on any failure."""
    return structured_json(
        label="support model call",
        system=(
            "You are the automated support desk for a student club's merchandise store. Decide whether to "
            "reply, escalate to a human officer, or ignore, and if replying write the reply. Use ONLY the "
            "knowledge base and the sender's order context. If the answer needs anything else, escalate. "
            "Set confidence between 0 and 1 for how sure you are the reply is correct and complete.\n\n"
            + context
        ),
        user=f"From: {from_addr}\nSubject: {subject}\n\n{email_text[:8000]}",
        schema_name="support_decision",
        schema=DECISION_SCHEMA,
    )


def handle_support_email(
    session: Session,
    record: InboundEmail,
    msg: Dict[str, Any],
    text: str,
    client: Optional[AgentMail],
    matched_order: Optional[Order] = None,
) -> None:
    from_addr = record.from_addr
    subject = record.subject or ""

    if _is_automated(msg, from_addr):
        record.detail = "automated sender; ignored"
        session.commit()
        return

    # One calibrated read of the email before anything else happens.
    triage = decide.triage_email(text, subject, from_addr)
    if triage is not None and triage.automated >= decide.AUTOMATED_THRESHOLD:
        record.detail = f"jev: automated message ignored (p={triage.automated:.2f})"
        session.commit()
        return

    orders = _orders_for_sender(session, from_addr)
    if matched_order and matched_order not in orders:
        # Reply came on a code thread but from a different address: never reveal the code.
        orders_ctx = "The sender replied on an order thread, but from an address that did not place the order. Do not share the code; ask them to write from the purchasing address."
    else:
        orders_ctx = _order_context(orders)

    prior = _prior_auto_replies(session, record.thread_id)
    recent = [r for r in prior if r.received_at and (dt.datetime.now(dt.timezone.utc) - r.received_at.replace(tzinfo=dt.timezone.utc)) < dt.timedelta(hours=24)]
    throttled = len(prior) >= MAX_AUTO_REPLIES_PER_THREAD or bool(recent)

    # Jev decides the easy cases without the LLM: escalate money, complaints and
    # questions about other people's orders; answer the three fixed intents from
    # templates when the sender has exactly one live order.
    jev_escalate = triage is not None and triage.escalate
    if triage is not None and not throttled and not jev_escalate and client is not None and triage.category in decide.TEMPLATE_INTENTS and triage.category_confidence >= decide.TEMPLATE_THRESHOLD:
        pending = [o for o in orders if o.status == "pending"]
        target = matched_order if (matched_order in orders and matched_order.status == "pending") else (pending[0] if len(pending) == 1 else None)
        if target is not None:
            intent = decide.TEMPLATE_INTENTS[triage.category]
            try:
                client.reply(record.message_id, text=auto_reply_text(target, intent), labels=["auto-reply", intent])
                record.detail = f"{AUTO_REPLY_PREFIX} ({intent}, jev conf {triage.category_confidence:.2f})"
                session.commit()
                return
            except AgentMailError as exc:
                log.error("templated reply failed: %s", exc)
                record.detail = f"templated reply failed: {exc}"

    context = knowledge_base() + "\nSENDER'S ORDERS:\n" + orders_ctx
    if triage is not None:
        context += "\nPRE-CLASSIFICATION (calibrated, from a separate model): " + triage.summary()
    decision = None if (throttled or jev_escalate) else ask_model(context, text, subject, from_addr)

    if decision and decision.get("action") == "ignore" and decision.get("category") == "spam_or_unrelated" and decision.get("confidence", 0) >= 0.8:
        record.detail = "support: ignored as unrelated (" + decision.get("summary_for_officers", "")[:200] + ")"
        session.commit()
        return

    can_reply = (
        decision is not None
        and decision.get("action") == "reply"
        and decision.get("category") not in ESCALATE_ALWAYS
        and decision.get("confidence", 0) >= 0.7
        and decision.get("reply_text", "").strip()
        and client is not None
    )

    if can_reply:
        reply_text = decision["reply_text"].strip()
        # Belt and braces: a reply may only contain codes that belong to the sender.
        for token in _codes_in_text(reply_text):
            if token not in {o.pickup_code for o in orders}:
                can_reply = False
                decision["summary_for_officers"] = "model tried to include a code not owned by sender; escalated. " + decision.get("summary_for_officers", "")
                break

    if can_reply:
        # Second opinion on the draft: no promises of money, no invented logistics, no commitments.
        guard = decide.guard_reply(reply_text, {"pickup": settings.gbm_info, "shipping": "none", "refunds_and_exchanges": "decided by officers only, never promised", "codes": "never expire, one use each"})
        if guard is not None and guard.flags:
            can_reply = False
            decision["summary_for_officers"] = f"guardrail flagged: {', '.join(guard.flags)}; escalated. " + decision.get("summary_for_officers", "")

    if can_reply:
        footer = f"\n\n(You are talking to the {settings.org_name} Merch Desk, an automated assistant. Reply again and an officer will see it.)"
        try:
            client.reply(record.message_id, text=reply_text + footer, labels=["auto-reply", decision.get("category", "other")])
            record.detail = f"{AUTO_REPLY_PREFIX} ({decision.get('category')}, conf {decision.get('confidence'):.2f})"
            session.commit()
            return
        except AgentMailError as exc:
            log.error("support reply failed: %s", exc)
            record.detail = f"support reply failed: {exc}"

    # Escalate: forward to the org with the model's summary (or a plain note).
    if throttled:
        why = "throttled: too many auto-replies on this thread"
    elif jev_escalate:
        why = f"jev: {triage.escalate_reason} ({triage.summary()})"
    elif decision:
        why = f"{decision.get('category')}: {decision.get('summary_for_officers')}"
    else:
        why = "no model decision (no key or call failed)"
    record.detail = (record.detail + "; " if record.detail else "") + f"escalated to {settings.org_email} ({why[:300]})"
    if client:
        try:
            client.forward(
                record.message_id,
                [settings.org_email],
                text=(
                    f"Merch Desk could not answer this automatically. Reason: {why}\n"
                    f"Sender: {from_addr}\nOrders for this address:\n{orders_ctx}\n\nReply to the sender directly."
                ),
            )
        except AgentMailError as exc:
            record.detail += f"; forward failed: {exc}"
    session.commit()


def _codes_in_text(text: str) -> List[str]:
    import re

    from .codes import normalize_code

    found = []
    for m in re.finditer(r"\bSL[-\s]?[A-Z0-9]{4}[-\s]?[A-Z0-9]{4}\b", text.upper()):
        code = normalize_code(m.group(0))
        if code:
            found.append(code)
    return found
