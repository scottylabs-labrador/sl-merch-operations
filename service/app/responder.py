"""Buyer replies to the code email.

With OPENROUTER_API_KEY set, the reply is classified into a small fixed set of
intents and answered from templates (the model never writes free text to the
buyer). Anything outside those intents, or any classification failure, is
forwarded to the org inbox. Without a key, every reply is forwarded.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from .agentmail import AgentMail, AgentMailError
from .config import settings
from .llm import structured_json
from .db import InboundEmail, Order
from .emails import auto_reply_text

log = logging.getLogger(__name__)

INTENTS = ["code_request", "delegate", "cant_make_it", "other"]

INTENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intent": {"type": "string", "enum": INTENTS},
        "confidence": {"type": "number"},
    },
    "required": ["intent", "confidence"],
}


def classify_intent(text: str) -> Optional[str]:
    data = structured_json(
        label="intent classification",
        system=(
            "Classify a buyer's reply about picking up club merchandise. Intents: "
            "code_request (they lost or want their pickup code), "
            "delegate (they ask whether someone else can pick up), "
            "cant_make_it (they cannot attend the meeting and ask what to do), "
            "other (anything else: refunds, size changes, complaints, questions about the product). "
            "Use 'other' whenever unsure."
        ),
        user=text[:6000],
        schema_name="intent",
        schema=INTENT_SCHEMA,
    )
    if data is None:
        return None
    if data.get("confidence", 0) < 0.7:
        return "other"
    return data.get("intent") if data.get("intent") in INTENTS else "other"


def handle_buyer_reply(session: Session, record: InboundEmail, order: Order, text: str, client: Optional[AgentMail]) -> None:
    intent = classify_intent(text)
    if intent in ("code_request", "delegate", "cant_make_it") and order.status == "pending" and client:
        try:
            client.reply(record.message_id, text=auto_reply_text(order, intent), labels=["auto-reply"])
            record.detail = f"auto-replied ({intent})"
            session.commit()
            return
        except AgentMailError as exc:
            log.error("auto-reply failed: %s", exc)
            record.detail = f"auto-reply failed: {exc}"
    else:
        record.detail = f"intent={intent or 'unclassified'}; forwarded to org"
    if client:
        try:
            client.forward(
                record.message_id,
                [settings.org_email],
                text=(
                    f"Buyer reply on order {order.id} / code {order.pickup_code} ({order.buyer_name}, {order.buyer_email}). "
                    f"Status: {order.status}. Reply to the buyer directly."
                ),
            )
        except AgentMailError as exc:
            record.detail += f"; forward failed: {exc}"
    session.commit()
