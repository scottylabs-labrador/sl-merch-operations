"""End-to-end flow against SQLite with a fake AgentMail client."""
import os

os.environ["DATABASE_URL"] = "sqlite:///./test_merch.db"
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["AGENTMAIL_WEBHOOK_SECRET"] = ""
os.environ["VOLUNTEER_PASSCODE"] = "vol"
os.environ["ADMIN_PASSCODE"] = "adm"
os.environ["OPENAI_API_KEY"] = ""
os.environ["AGENTMAIL_API_KEY"] = ""
os.environ["SCOTTYLABS_MERCH_AGENTMAIL_API_TOKEN"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app import webhook as webhook_mod  # noqa: E402
from app import scheduler as scheduler_mod  # noqa: E402
from app import orders as orders_mod  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "new_store_purchase.html")


class FakeAgentMail:
    def __init__(self):
        self.sent, self.replies, self.forwards = [], [], []
        self.inbox_id = "scottylabs-merch@agentmail.to"

    def send(self, to, subject, text, html=None, inline_png=None, labels=None, **kw):
        self.sent.append({"to": to, "subject": subject, "text": text, "png": bool(inline_png)})
        return {"message_id": f"msg-{len(self.sent)}", "thread_id": f"thread-{len(self.sent)}"}

    def reply(self, message_id, text, html=None, labels=None):
        self.replies.append({"message_id": message_id, "text": text})
        return {"message_id": "r1", "thread_id": "t1"}

    def forward(self, message_id, to, text=None):
        self.forwards.append({"message_id": message_id, "to": to})
        return {}

    def get_message(self, message_id):
        return {}


@pytest.fixture()
def client(monkeypatch):
    dbmod.engine.dispose()
    dbmod.Base.metadata.drop_all(dbmod.engine)
    dbmod.Base.metadata.create_all(dbmod.engine)
    fake = FakeAgentMail()
    monkeypatch.setattr(webhook_mod, "AgentMail", lambda: fake)
    monkeypatch.setattr(scheduler_mod, "AgentMail", lambda: fake)
    monkeypatch.setattr(orders_mod, "AgentMail", lambda: fake)
    monkeypatch.setattr(webhook_mod.settings, "agentmail_api_key", "fake")
    c = TestClient(app)
    c.fake = fake
    yield c


def _purchase_event(event_id="evt-1", email_from="tartanconnect@andrew.cmu.edu", buyer="jqtartan@example.invalid"):
    with open(FIXTURE, encoding="utf-8") as fh:
        html = fh.read().replace("jqtartan@example.invalid", buyer)
    return {
        "type": "event",
        "event_type": "message.received",
        "event_id": event_id,
        "message": {
            "inbox_id": "scottylabs-merch@agentmail.to",
            "message_id": f"<{event_id}@tartanconnect>",
            "thread_id": f"thr-{event_id}",
            "from": f"TartanConnect <{email_from}>",
            "to": ["scottylabs-merch@agentmail.to"],
            "subject": "New store purchase for ScottyLabs",
            "html": html,
            "timestamp": "2026-09-08T14:00:00Z",
        },
        "thread": {},
    }


def test_purchase_creates_order_and_emails_code(client):
    r = client.post("/webhooks/agentmail", json=_purchase_event())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["classification"] == "purchase"
    assert len(client.fake.sent) == 1
    sent = client.fake.sent[0]
    assert sent["to"] == ["jqtartan@example.invalid"] and sent["png"]
    assert "SL-" in sent["subject"]

    # Redelivery of the same event is a no-op.
    r2 = client.post("/webhooks/agentmail", json=_purchase_event())
    assert r2.json()["order_id"] == body["order_id"]
    assert len(client.fake.sent) == 1

    # Same purchase forwarded again under a new event id is a duplicate.
    r3 = client.post("/webhooks/agentmail", json=_purchase_event(event_id="evt-2"))
    assert r3.json()["classification"] == "ignored" and "duplicate" in r3.json()["detail"]


def test_untrusted_sender_is_forwarded_not_parsed(client):
    r = client.post("/webhooks/agentmail", json=_purchase_event(event_id="evt-x", email_from="stranger@example.com"))
    assert r.json()["classification"] == "support"
    assert client.fake.forwards and client.fake.forwards[0]["to"] == ["scottylabs@cmu.edu"]
    assert not client.fake.sent


def test_volunteer_lookup_and_single_pickup(client):
    client.post("/webhooks/agentmail", json=_purchase_event())
    code = client.fake.sent[0]["subject"].split(": ")[-1]

    assert client.get("/api/orders/lookup", params={"code": code}).status_code == 401
    client.post("/login", data={"passcode": "vol", "next": "/pickup"})
    r = client.get("/api/orders/lookup", params={"code": code.lower().replace("-", "")})
    assert r.status_code == 200 and r.json()["order"]["status"] == "pending"
    oid = r.json()["order"]["id"]

    r = client.post(f"/api/orders/{oid}/pickup", data={"volunteer": "Helen", "presented_by": "roommate"})
    assert r.status_code == 200 and r.json()["complete"]
    r = client.post(f"/api/orders/{oid}/pickup", data={"volunteer": "Helen"})
    assert r.status_code == 409 and r.json()["already"]
    r = client.get("/api/orders/lookup", params={"q": "jqtartan"})
    assert r.json()["orders"][0]["status"] == "picked_up"


def test_buyer_reply_without_llm_is_forwarded(client):
    client.post("/webhooks/agentmail", json=_purchase_event())
    reply = {
        "type": "event", "event_type": "message.received", "event_id": "evt-reply",
        "message": {"message_id": "<reply@buyer>", "thread_id": "thread-1", "from": "jqtartan@example.invalid", "subject": "Re: code", "text": "can my roommate grab it?", "timestamp": "2026-09-09T10:00:00Z"},
        "thread": {},
    }
    r = client.post("/webhooks/agentmail", json=reply)
    assert r.json()["classification"] == "buyer_reply"
    assert client.fake.forwards and not client.fake.replies


def test_admin_pages_and_csv(client):
    client.post("/webhooks/agentmail", json=_purchase_event())
    client.post("/login", data={"passcode": "adm", "next": "/admin"})
    assert client.get("/admin").status_code == 200
    csv_text = client.get("/admin/orders.csv").text
    assert "jqtartan@example.invalid" in csv_text
    r = client.post("/admin/orders/manual", data={"buyer_name": "Manual Person", "buyer_email": "mp@andrew.cmu.edu", "size": "XL", "quantity": "2", "send_email": "no"}, follow_redirects=False)
    assert r.status_code == 303 and "created-SL-" in r.headers["location"]
    text = client.post("/admin/bring-list").text
    assert "XL" in text and "M" in text


def test_refund_notice_freezes_order(client):
    client.post("/webhooks/agentmail", json=_purchase_event())
    refund = {
        "type": "event", "event_type": "message.received", "event_id": "evt-refund",
        "message": {"message_id": "<refund@tc>", "thread_id": "thr-refund", "from": "TartanConnect <tartanconnect@andrew.cmu.edu>",
                    "subject": "New refund request received for ScottyLabs", "text": "Hi Officer, Jane Q. Tartan jqtartan@example.invalid requested a refund of $10.00.", "timestamp": "2026-09-09T10:00:00Z"},
        "thread": {},
    }
    r = client.post("/webhooks/agentmail", json=refund)
    assert r.json()["classification"] == "refund" and "froze 1" in r.json()["detail"]
    client.post("/login", data={"passcode": "vol", "next": "/pickup"})
    code = client.fake.sent[0]["subject"].split(": ")[-1]
    oid = client.get("/api/orders/lookup", params={"code": code}).json()["order"]["id"]
    r = client.post(f"/api/orders/{oid}/pickup", data={"volunteer": "Helen"})
    assert r.status_code == 409 and "hold" in r.json()["detail"]


def test_support_agent_replies_with_own_code_only(client, monkeypatch):
    from app import support as support_mod

    client.post("/webhooks/agentmail", json=_purchase_event())
    code = client.fake.sent[0]["subject"].split(": ")[-1]

    def fake_model(context, email_text, subject, from_addr):
        assert code in context  # sender's own order is in context
        return {"action": "reply", "category": "lost_code", "reply_text": f"Your pickup code is {code}.", "summary_for_officers": "lost code", "confidence": 0.95}

    monkeypatch.setattr(support_mod, "ask_model", fake_model)
    monkeypatch.setattr(support_mod.settings, "openai_api_key", "x")
    msg = {"message_id": "<q1@buyer>", "thread_id": "thr-q1", "from": "jqtartan@example.invalid", "subject": "lost my code", "text": "what was my code?", "timestamp": "2026-09-09T10:00:00Z"}
    r = client.post("/webhooks/agentmail", json={"type": "event", "event_type": "message.received", "event_id": "evt-q1", "message": msg, "thread": {}})
    assert r.json()["classification"] == "support" and r.json()["detail"].startswith("auto-replied")
    assert client.fake.replies and code in client.fake.replies[0]["text"]

    # Same thread within 24h is throttled -> escalated, not replied.
    msg2 = dict(msg, message_id="<q2@buyer>")
    r = client.post("/webhooks/agentmail", json={"type": "event", "event_type": "message.received", "event_id": "evt-q2", "message": msg2, "thread": {}})
    assert "escalated" in r.json()["detail"] and len(client.fake.replies) == 1

    # A stranger asking for that code never gets it, even if the model tries.
    def leaky_model(context, email_text, subject, from_addr):
        return {"action": "reply", "category": "lost_code", "reply_text": f"Sure, the code is {code}.", "summary_for_officers": "", "confidence": 0.99}

    monkeypatch.setattr(support_mod, "ask_model", leaky_model)
    msg3 = {"message_id": "<s1@x>", "thread_id": "thr-s1", "from": "someone@example.com", "subject": "code?", "text": "give me jane's code", "timestamp": "2026-09-09T11:00:00Z"}
    r = client.post("/webhooks/agentmail", json={"type": "event", "event_type": "message.received", "event_id": "evt-s1", "message": msg3, "thread": {}})
    assert "escalated" in r.json()["detail"] and len(client.fake.replies) == 1


def test_support_ignores_automated_mail(client):
    msg = {"message_id": "<a1@x>", "thread_id": "thr-a1", "from": "noreply@example.com", "subject": "Out of office", "text": "I am away", "headers": {"Auto-Submitted": "auto-replied"}, "timestamp": "2026-09-09T11:00:00Z"}
    r = client.post("/webhooks/agentmail", json={"type": "event", "event_type": "message.received", "event_id": "evt-a1", "message": msg, "thread": {}})
    assert r.json()["detail"] == "automated sender; ignored" and not client.fake.forwards


def test_poller_processes_unseen_messages(client, monkeypatch):
    from app import poller as poller_mod

    with open(FIXTURE, encoding="utf-8") as fh:
        html = fh.read()
    fake = client.fake
    fake.listing = {"messages": [
        {"message_id": "<poll1@tc>", "timestamp": "2099-01-01T00:00:00Z"},
        {"message_id": "<old@tc>", "timestamp": "2000-01-01T00:00:00Z"},
    ]}
    fake.list_messages = lambda limit=25, labels=None: fake.listing
    fake.get_message = lambda mid: {"message_id": mid, "thread_id": "thr-poll", "from": "TartanConnect <tartanconnect@andrew.cmu.edu>", "subject": "New store purchase for ScottyLabs", "html": html, "timestamp": "2099-01-01T00:00:00Z"}
    monkeypatch.setattr(poller_mod, "AgentMail", lambda: fake)
    monkeypatch.setattr(poller_mod.settings, "agentmail_api_key", "fake")
    stats = poller_mod.poll_inbox()
    assert stats == {"seen": 2, "new": 1, "processed": 1}
    assert len(fake.sent) == 1  # code email for the polled purchase
    assert poller_mod.poll_inbox() == {"seen": 2, "new": 0, "processed": 0}  # idempotent


def test_signed_webhook_is_parsed(client, monkeypatch):
    import base64, json, time, hmac as _hmac, hashlib
    from app import main as main_mod

    secret_bytes = b"0123456789abcdef0123456789abcdef"
    secret = "whsec_" + base64.b64encode(secret_bytes).decode()
    monkeypatch.setattr(main_mod.settings, "agentmail_webhook_secret", secret)
    body = json.dumps(_purchase_event(event_id="evt-signed")).encode()
    msg_id, ts = "msg_1", str(int(time.time()))
    sig = base64.b64encode(_hmac.new(secret_bytes, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    headers = {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}", "content-type": "application/json"}
    r = client.post("/webhooks/agentmail", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["classification"] == "purchase"
    bad = dict(headers, **{"svix-signature": "v1,AAAA"})
    assert client.post("/webhooks/agentmail", content=body, headers=bad).status_code == 400


OFFICER = os.path.join(os.path.dirname(__file__), "fixtures", "officer_new_store_purchase.html")
RECEIPT = os.path.join(os.path.dirname(__file__), "fixtures", "buyer_receipt.html")


def _event(event_id, html, subject, from_addr, thread="thr-x"):
    return {"type": "event", "event_type": "message.received", "event_id": event_id,
            "message": {"message_id": f"<{event_id}@x>", "thread_id": thread, "from": from_addr, "subject": subject, "html": html, "timestamp": "2026-09-07T03:10:26Z"}, "thread": {}}


def test_real_officer_notification_then_forwarded_receipt(client):
    officer = open(OFFICER, encoding="utf-8").read()
    receipt = open(RECEIPT, encoding="utf-8").read()
    r = client.post("/webhooks/agentmail", json=_event("evt-off", officer, "Jane Tartan successfully purchased from ScottyLabs Merch Store", "TartanConnect <tartanconnect@andrew.cmu.edu>"))
    assert r.json()["classification"] == "purchase" and "awaiting buyer email" in r.json()["detail"]
    assert not client.fake.sent  # no address to send to yet
    client.post("/login", data={"passcode": "adm", "next": "/admin"})
    csv_text = client.get("/admin/orders.csv").text
    assert "needs_email" in csv_text and "15491205" in csv_text and "Jane Tartan" in csv_text

    # Buyer forwards their receipt from their own address -> email resolved, code sent.
    r = client.post("/webhooks/agentmail", json=_event("evt-rcpt", receipt, "Fwd: You successfully purchased from the ScottyLabs Merch Store", "Jane Tartan <jtartan@example.invalid>"))
    assert r.json()["classification"] == "purchase" and "resolved" in r.json()["detail"]
    assert client.fake.sent and client.fake.sent[0]["to"] == ["jtartan@example.invalid"]
    assert "pending" in client.get("/admin/orders.csv").text

    # The same officer notification again (Svix retry with a new event id) is a duplicate.
    r = client.post("/webhooks/agentmail", json=_event("evt-off2", officer, "Jane Tartan successfully purchased from ScottyLabs Merch Store", "TartanConnect <tartanconnect@andrew.cmu.edu>"))
    assert r.json()["classification"] == "ignored" and "duplicate" in r.json()["detail"]
    assert len(client.fake.sent) == 1


def test_receipt_before_officer_notification(client):
    officer = open(OFFICER, encoding="utf-8").read()
    receipt = open(RECEIPT, encoding="utf-8").read()
    # An officer bought as a test: the receipt is forwarded by the Gmail filter (trusted sender).
    r = client.post("/webhooks/agentmail", json=_event("evt-r1", receipt, "You successfully purchased from the ScottyLabs Merch Store", "TartanConnect <tartanconnect@andrew.cmu.edu>"))
    assert r.json()["classification"] == "purchase" and client.fake.sent[0]["to"] == ["jtartan@example.invalid"]
    r = client.post("/webhooks/agentmail", json=_event("evt-o1", officer, "Jane Tartan successfully purchased from ScottyLabs Merch Store", "TartanConnect <tartanconnect@andrew.cmu.edu>"))
    assert r.json()["classification"] == "ignored" and "duplicate" in r.json()["detail"]


def test_forwarded_receipt_by_stranger_is_escalated(client):
    officer = open(OFFICER, encoding="utf-8").read()
    receipt = open(RECEIPT, encoding="utf-8").read().replace("Hi Jane,", "Hi Mallory,")
    client.post("/webhooks/agentmail", json=_event("evt-o2", officer, "Jane Tartan successfully purchased from ScottyLabs Merch Store", "TartanConnect <tartanconnect@andrew.cmu.edu>"))
    r = client.post("/webhooks/agentmail", json=_event("evt-m", receipt, "Fwd: receipt", "Mallory <mallory@andrew.cmu.edu>"))
    assert r.json()["classification"] == "support" and "escalated" in r.json()["detail"]
    assert not client.fake.sent and client.fake.forwards


def test_reconcile_resolves_awaiting_order(client):
    officer = open(OFFICER, encoding="utf-8").read()
    client.post("/webhooks/agentmail", json=_event("evt-o3", officer, "Jane Tartan successfully purchased from ScottyLabs Merch Store", "TartanConnect <tartanconnect@andrew.cmu.edu>"))
    client.post("/login", data={"passcode": "adm", "next": "/admin"})
    csv_body = "First Name,Last Name,Email,Product,Quantity,Date\nJane,Tartan,jtartan@example.invalid,System Test — $1 donation (officers only, not merch),1,2026-09-06 23:09\n"
    r = client.post("/admin/reconcile", files={"file": ("sales.csv", csv_body.encode("utf-8"), "text/csv")}, data={"send_email": "yes"}, follow_redirects=False)
    assert "resolved-1" in r.headers["location"], r.headers["location"]
    assert client.fake.sent and client.fake.sent[0]["to"] == ["jtartan@example.invalid"]


def test_admin_manual_email_set(client):
    officer = open(OFFICER, encoding="utf-8").read()
    r = client.post("/webhooks/agentmail", json=_event("evt-o4", officer, "Jane Tartan successfully purchased from ScottyLabs Merch Store", "TartanConnect <tartanconnect@andrew.cmu.edu>"))
    oid = r.json()["order_id"]
    client.post("/login", data={"passcode": "adm", "next": "/admin"})
    assert "Awaiting buyer email (1)" in client.get("/admin").text
    r = client.post(f"/admin/orders/{oid}/email", data={"email": "jtartan@example.invalid"}, follow_redirects=False)
    assert "code-sent" in r.headers["location"] and client.fake.sent[0]["to"] == ["jtartan@example.invalid"]


def test_officer_reply_reaches_support_agent(client, monkeypatch):
    from app import support as support_mod

    receipt = open(RECEIPT, encoding="utf-8").read()
    client.post("/webhooks/agentmail", json=_event("evt-rr", receipt, "You successfully purchased from the ScottyLabs Merch Store", "TartanConnect <tartanconnect@andrew.cmu.edu>"))
    code = client.fake.sent[0]["subject"].split(": ")[-1]
    monkeypatch.setattr(support_mod, "ask_model", lambda *a, **k: {"action": "reply", "category": "delegate", "reply_text": f"Yes, a friend can pick it up with code {code}.", "summary_for_officers": "", "confidence": 0.9})
    monkeypatch.setattr(support_mod.settings, "openai_api_key", "x")
    # The officer (a trusted sender because of Gmail forwarding) replies to the code email.
    msg = {"message_id": "<re1@tk>", "thread_id": "thread-1", "from": "Jane Tartan <jtartan@example.invalid>", "subject": "Re: Your ScottyLabs pickup code", "text": "Can my roommate pick it up?", "timestamp": "2026-09-07T03:40:00Z"}
    r = client.post("/webhooks/agentmail", json={"type": "event", "event_type": "message.received", "event_id": "evt-re1", "message": msg, "thread": {}})
    assert r.json()["classification"] == "buyer_reply" and r.json()["detail"].startswith("auto-replied")
    assert client.fake.replies and code in client.fake.replies[0]["text"]


def test_platform_non_purchase_is_ignored(client):
    msg = {"message_id": "<np@tc>", "thread_id": "thr-np", "from": "TartanConnect <tartanconnect@andrew.cmu.edu>", "subject": "New answer submitted for Space Access Request Form", "text": "Hi Officer, thank you for submitting answers.", "timestamp": "2026-09-07T03:40:00Z"}
    r = client.post("/webhooks/agentmail", json={"type": "event", "event_type": "message.received", "event_id": "evt-np", "message": msg, "thread": {}})
    assert r.json()["classification"] == "ignored" and not client.fake.forwards and not client.fake.replies
