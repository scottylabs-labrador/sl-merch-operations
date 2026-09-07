"""Register (or show) the AgentMail webhook that points at this service.

Usage:
    python scripts/register_webhook.py https://<railway-domain>/webhooks/agentmail
    python scripts/register_webhook.py --list
    python scripts/register_webhook.py --delete <webhook_id>

Prints the webhook secret once. Put it in Railway as AGENTMAIL_WEBHOOK_SECRET.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agentmail import AgentMail  # noqa: E402


def main() -> None:
    client = AgentMail()
    args = sys.argv[1:]
    if not args or args[0] == "--list":
        print(json.dumps(client.list_webhooks(), indent=2))
        return
    if args[0] == "--delete":
        print(json.dumps(client.delete_webhook(args[1]), indent=2))
        return
    url = args[0]
    if not url.startswith("https://"):
        sys.exit("webhook URL must be https")
    existing = client.list_webhooks().get("webhooks", [])
    for wh in existing:
        if wh.get("url") == url:
            print("already registered:", wh.get("webhook_id"))
            print("secret is only shown at creation; delete and re-create to rotate it")
            return
    created = client.create_webhook(url)
    print("webhook_id:", created.get("webhook_id"))
    print("AGENTMAIL_WEBHOOK_SECRET=" + str(created.get("secret")))
    print("events:", created.get("event_types"))


if __name__ == "__main__":
    main()
