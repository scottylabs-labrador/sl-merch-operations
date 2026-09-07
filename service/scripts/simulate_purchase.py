"""Send a synthetic TartanConnect-style purchase notification INTO the inbox.

This is the end-to-end smoke test once the webhook is registered: the service
should create an order and email the pickup code to BUYER_EMAIL.

Usage:
    python scripts/simulate_purchase.py buyer@andrew.cmu.edu "Buyer Name" M 1

The email is sent from the AgentMail inbox to itself, so for the service to
accept it you must temporarily include the inbox address in TRUSTED_SENDERS
(e.g. TRUSTED_SENDERS=tartanconnect@andrew.cmu.edu,scottylabs-merch@agentmail.to).
Remove it again afterwards.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agentmail import AgentMail  # noqa: E402
from app.config import settings  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "new_store_purchase.html")


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    email, name = sys.argv[1], sys.argv[2]
    size = sys.argv[3] if len(sys.argv) > 3 else "M"
    qty = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    with open(FIXTURE, encoding="utf-8") as fh:
        html = fh.read()
    html = (
        html.replace("Jane Q. Tartan", name)
        .replace("jqtartan@example.invalid", email)
        .replace("Size M", f"Size {size}")
        .replace(">1<", f">{qty}<")
        .replace("$10.00", f"${10 * qty:.2f}")
    )
    client = AgentMail()
    resp = client.send(
        to=[settings.agentmail_inbox_id],
        subject="[TEST] New store purchase for ScottyLabs",
        text=f"[TEST] {name} purchased {qty} x ScottyLabs Found T-Shirt — Size {size} ({email})",
        html=html,
        labels=["test-purchase"],
    )
    print("sent:", resp)


if __name__ == "__main__":
    main()
