"""Inbox poller: a backstop for webhook delivery.

Every minute, list received messages in the AgentMail inbox and run any we have
not seen through the same handler the webhook uses. Because handle_event keys
on event_id (synthetic here: "poll:<message_id>") and the webhook path keys on
AgentMail's event_id, a message processed by one path is skipped by the other
via the message_id check below. So webhooks give speed, polling gives certainty.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, List

from sqlalchemy import select

from .agentmail import AgentMail, AgentMailError
from .config import settings
from .db import InboundEmail, SessionLocal
from .webhook import handle_event

log = logging.getLogger(__name__)


def poll_inbox(limit: int = 25) -> Dict[str, int]:
    """Returns counts of {seen, new, processed}. Safe to call repeatedly."""
    stats = {"seen": 0, "new": 0, "processed": 0}
    if not settings.agentmail_api_key:
        return stats
    client = AgentMail()
    try:
        listing = client.list_messages(limit=limit, labels=["received"])
    except AgentMailError as exc:
        log.warning("poll: list failed: %s", exc)
        return stats
    messages: List[Dict] = listing.get("messages", [])
    stats["seen"] = len(messages)
    with SessionLocal() as session:
        for summary in messages:
            mid = summary.get("message_id")
            if not mid:
                continue
            already = session.scalar(select(InboundEmail).where(InboundEmail.message_id == mid))
            if already:
                continue
            ts = summary.get("timestamp") or ""
            if ts:
                try:
                    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if age > dt.timedelta(hours=settings.poll_max_age_hours):
                        continue  # history from before the service existed is not replayed
                except ValueError:
                    pass
            stats["new"] += 1
            try:
                full = client.get_message(mid)
            except AgentMailError as exc:
                log.warning("poll: get %s failed: %s", mid, exc)
                continue
            payload = {
                "type": "event",
                "event_type": "message.received",
                "event_id": f"poll:{mid}",
                "message": full,
                "thread": {},
            }
            record = handle_event(session, payload, client)
            log.info("poll: %s -> %s (%s)", (full.get("subject") or "")[:60], record.classification, record.detail)
            stats["processed"] += 1
    return stats
