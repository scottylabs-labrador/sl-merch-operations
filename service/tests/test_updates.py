"""Officer updates to buyers: drafting as a template, previews, approval, scoped sending, desk notices."""
import os

os.environ["DATABASE_URL"] = "sqlite:///./test_merch.db"
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["AGENTMAIL_WEBHOOK_SECRET"] = ""
os.environ["VOLUNTEER_PASSCODE"] = "vol"
os.environ["ADMIN_PASSCODE"] = "adm"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["AGENTMAIL_API_KEY"] = ""
os.environ["SCOTTYLABS_MERCH_AGENTMAIL_API_TOKEN"] = ""

import datetime as dt  # noqa: E402
from urllib.parse import unquote  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import orders as orders_mod  # noqa: E402
from app import support as support_mod  # noqa: E402
from app import updates as updates_mod  # noqa: E402
from app.emails import auto_reply_text  # noqa: E402
from app.main import app  # noqa: E402

DRAFT = {
    "subject": "No merch pickup this Saturday",
    "body": "Hi {first_name},\n\nThere is no pickup at the work session on Saturday, September 26. Your order is safe:\n{items}\n\nWe'll email the makeup pickup time soon.\n\nScottyLabs Merch",
    "format_note": "Each buyer gets a short note with their first name and the items still waiting for them.",
    "assumptions": ["'This work session' means Saturday, September 26."],
    "desk_notice": "No pickup on Saturday, September 26; a makeup pickup will be announced by email.",
    "desk_notice_until": "2099-09-27",
}


class FakeMail:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def send(self, to, subject, text, html=None, labels=None, **kw):
        from app.agentmail import AgentMailError

        if to[0] in self.fail_for:
            raise AgentMailError("simulated outage")
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html, "labels": labels})
        return {"message_id": f"m{len(self.sent)}", "thread_id": f"t{len(self.sent)}"}


@pytest.fixture()
def client(monkeypatch):
    dbmod.engine.dispose()
    dbmod.Base.metadata.drop_all(dbmod.engine)
    dbmod.Base.metadata.create_all(dbmod.engine)
    fake = FakeMail()
    calls = []

    def fake_draft(instruction, audience, current=None, feedback=""):
        calls.append({"instruction": instruction, "audience": audience, "current": current, "feedback": feedback})
        return dict(DRAFT) if not feedback else {**DRAFT, "subject": "Update: no pickup Saturday, Sept 26"}

    monkeypatch.setattr(updates_mod, "AgentMail", lambda: fake)
    monkeypatch.setattr(orders_mod, "AgentMail", lambda: fake)
    monkeypatch.setattr(updates_mod, "draft_with_model", fake_draft)
    c = TestClient(app)
    c.fake, c.calls = fake, calls
    c.post("/login", data={"passcode": "adm", "next": "/admin"})
    yield c


def _order(c, name, email, size="M", qty=1):
    r = c.post("/admin/orders/manual", data={"buyer_name": name, "buyer_email": email, "size": size, "quantity": str(qty), "send_email": "no"}, follow_redirects=False)
    assert r.status_code == 303
    with dbmod.SessionLocal() as s:
        return s.scalar(select(dbmod.Order).where(dbmod.Order.buyer_email == email)).id


def _update(uid):
    with dbmod.SessionLocal() as s:
        u = s.get(dbmod.CustomerUpdate, uid)
        return u, [(m.order_id, m.to_email, m.status, m.note, m.subject, m.body) for m in u.messages]


def _draft(c, instruction="No pickup this worksession; later ones yes. Makeup pickup Sunday or early next week, to be communicated.", **extra):
    r = c.post("/admin/updates", data={"instruction": instruction, **extra}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/updates/")
    return int(r.headers["location"].rsplit("/", 1)[1])


def _send(c, uid, include=None, notice="yes", confirm="yes", revision=None):
    u, msgs = _update(uid)
    ids = include if include is not None else [m.id for m in _messages(uid) if m.status == "preview"]
    data = {"revision": str(revision if revision is not None else u.revision), "include": [str(i) for i in ids], "notice": notice, "confirm": confirm}
    return c.post(f"/admin/updates/{uid}/send", data=data, follow_redirects=False)


def _messages(uid):
    with dbmod.SessionLocal() as s:
        return list(s.get(dbmod.CustomerUpdate, uid).messages)


def test_everyone_waiting_gets_their_own_copy_and_only_after_approval(client):
    a = _order(client, "Ada Lovelace", "ada@andrew.cmu.edu", "S", 1)
    b = _order(client, "Grace Hopper", "grace@andrew.cmu.edu", "XXL", 2)
    done = _order(client, "Alan Turing", "alan@andrew.cmu.edu", "L", 1)
    client.post("/login", data={"passcode": "vol", "next": "/pickup"})
    client.post(f"/api/orders/{done}/pickup", data={"volunteer": "Helen"})
    client.post("/login", data={"passcode": "adm", "next": "/admin"})

    uid = _draft(client)
    # The model is told what to say and how many people get it, never who they are.
    call = client.calls[-1]
    assert "2 buyer" in call["audience"]
    for secret in ("Ada", "Grace", "ada@", "grace@", "SL-"):
        assert secret not in call["audience"] and secret not in call["instruction"]

    page = client.get(f"/admin/updates/{uid}").text
    assert "Hi Ada," in page and "Hi Grace," in page and "alan@andrew.cmu.edu" not in page
    assert "1 x ScottyLabs Found T-Shirt, size S" in page and "2 x ScottyLabs Found T-Shirt, size XXL" in page
    assert "Saturday, September 26" in page and "Also tell the support desk" in page
    assert client.fake.sent == []                                  # nothing leaves before approval

    r = _send(client, uid)
    assert r.status_code == 303
    assert sorted(m["to"][0] for m in client.fake.sent) == ["ada@andrew.cmu.edu", "grace@andrew.cmu.edu"]
    ada = next(m for m in client.fake.sent if m["to"] == ["ada@andrew.cmu.edu"])
    assert ada["text"].startswith("Hi Ada,") and "size S" in ada["text"] and "XXL" not in ada["text"] and "Grace" not in ada["text"]
    assert "<p>Hi Ada,</p>" in ada["html"] and f"order-{a}" in ada["labels"]
    u, msgs = _update(uid)
    assert u.status == "sent" and all(m[2] == "sent" for m in msgs)
    with dbmod.SessionLocal() as s:
        assert f"update #{uid} emailed" in s.get(dbmod.Order, b).notes
        notices = updates_mod.active_notices(s)
        assert len(notices) == 1 and "September 26" in notices[0].text
        assert "September 26" in support_mod.knowledge_base(updates_mod.notice_text(s))

    # A second click, another tab, or a replayed form sends nothing more.
    r = _send(client, uid)
    assert "already" in r.headers["location"] and len(client.fake.sent) == 2


def test_one_order_scope(client):
    _order(client, "Ada Lovelace", "ada@andrew.cmu.edu")
    target = _order(client, "Grace Hopper", "grace@andrew.cmu.edu", "L")
    uid = _draft(client, "Tell her the L she ordered is in and she can pick it up Sunday", audience="order", order_id=str(target))
    assert "one buyer" in client.calls[-1]["audience"]
    _send(client, uid)
    assert [m["to"] for m in client.fake.sent] == [["grace@andrew.cmu.edu"]]


def test_order_audience_needs_an_order(client):
    r = client.post("/admin/updates", data={"instruction": "hi", "audience": "order", "order_id": ""}, follow_redirects=False)
    assert "Choose" in r.headers["location"]


def test_left_out_recipient_and_confirmation(client):
    _order(client, "Ada Lovelace", "ada@andrew.cmu.edu")
    _order(client, "Grace Hopper", "grace@andrew.cmu.edu")
    uid = _draft(client)
    first = [m.id for m in _messages(uid) if m.status == "preview"][0]
    r = _send(client, uid, confirm="")
    assert "confirm" in r.headers["location"] and client.fake.sent == []
    _send(client, uid, include=[first])
    assert len(client.fake.sent) == 1
    statuses = sorted(m[2] for m in _update(uid)[1])
    assert statuses == ["sent", "skipped"]
    assert any(m[3] == "left out by the officer" for m in _update(uid)[1])


def test_changed_draft_must_be_reviewed_again(client):
    _order(client, "Ada Lovelace", "ada@andrew.cmu.edu")
    uid = _draft(client)
    client.post(f"/admin/updates/{uid}/revise", data={"feedback": "put the date in the subject"})
    u, _ = _update(uid)
    assert u.revision == 2 and u.subject == "Update: no pickup Saturday, Sept 26"
    assert client.calls[-1]["current"][0] == "No merch pickup this Saturday"      # the model revises its own draft
    r = _send(client, uid, revision=1)
    assert "changed" in r.headers["location"] and client.fake.sent == []
    _send(client, uid)
    assert client.fake.sent[0]["subject"] == "Update: no pickup Saturday, Sept 26"


def test_template_problems_block_sending(client):
    _order(client, "Ada Lovelace", "ada@andrew.cmu.edu")
    uid = _draft(client)
    client.post(f"/admin/updates/{uid}/edit", data={"subject": "Hi", "body": "Hi {first_name}, bring {code}. Also SL-AB12-CD34."})
    page = client.get(f"/admin/updates/{uid}").text
    assert "Unknown placeholder" in page and "literal pickup code" in page
    r = _send(client, uid)
    assert "Not sent" in unquote(r.headers["location"]) and client.fake.sent == []


def test_codes_only_through_the_placeholder(client):
    _order(client, "Ada Lovelace", "ada@andrew.cmu.edu")
    _order(client, "Grace Hopper", "grace@andrew.cmu.edu")
    uid = _draft(client)
    client.post(f"/admin/updates/{uid}/edit", data={"subject": "Your code", "body": "Hi {first_name}, your code is {pickup_code}.\n\nScottyLabs Merch"})
    _send(client, uid)
    with dbmod.SessionLocal() as s:
        codes = {o.buyer_email: o.pickup_code for o in s.scalars(select(dbmod.Order)).all()}
    for m in client.fake.sent:
        mine = codes[m["to"][0]]
        assert mine in m["text"] and all(c not in m["text"] for e, c in codes.items() if e != m["to"][0])


def test_model_unavailable_then_written_by_hand(client, monkeypatch):
    _order(client, "Ada Lovelace", "ada@andrew.cmu.edu")
    monkeypatch.setattr(updates_mod, "draft_with_model", lambda *a, **k: None)
    uid = _draft(client)
    page = client.get(f"/admin/updates/{uid}").text
    assert "couldn&#39;t draft" in page or "couldn't draft" in page
    assert "The subject is empty" in page
    client.post(f"/admin/updates/{uid}/edit", data={"subject": "Pickup moved", "body": "Hi {first_name},\n\nPickup moves to Sunday.\n\nScottyLabs Merch", "desk_notice": "", "desk_notice_until": ""})
    _send(client, uid, notice="")
    assert client.fake.sent[0]["text"].startswith("Hi Ada,")
    with dbmod.SessionLocal() as s:
        assert updates_mod.active_notices(s) == []


def test_picked_up_after_preview_is_skipped(client):
    a = _order(client, "Ada Lovelace", "ada@andrew.cmu.edu")
    _order(client, "Grace Hopper", "grace@andrew.cmu.edu")
    uid = _draft(client)
    client.post("/login", data={"passcode": "vol", "next": "/pickup"})
    client.post(f"/api/orders/{a}/pickup", data={"volunteer": "Helen"})
    client.post("/login", data={"passcode": "adm", "next": "/admin"})
    _send(client, uid)
    assert [m["to"] for m in client.fake.sent] == [["grace@andrew.cmu.edu"]]
    assert any(m[0] == a and m[2] == "skipped" and "no longer waiting" in m[3] for m in _update(uid)[1])


def test_failed_sends_can_be_retried(client, monkeypatch):
    _order(client, "Ada Lovelace", "ada@andrew.cmu.edu")
    _order(client, "Grace Hopper", "grace@andrew.cmu.edu")
    flaky = FakeMail(fail_for={"grace@andrew.cmu.edu"})
    monkeypatch.setattr(updates_mod, "AgentMail", lambda: flaky)
    uid = _draft(client)
    _send(client, uid)
    u, msgs = _update(uid)
    assert u.status == "partial" and sorted(m[2] for m in msgs) == ["failed", "sent"]
    flaky.fail_for.clear()
    client.post(f"/admin/updates/{uid}/retry")
    u, msgs = _update(uid)
    assert u.status == "sent" and [m["to"][0] for m in flaky.sent] == ["ada@andrew.cmu.edu", "grace@andrew.cmu.edu"]
    with dbmod.SessionLocal() as s:
        assert len(updates_mod.active_notices(s)) == 1                   # posted once, not once per attempt


def test_discard_sends_nothing(client):
    _order(client, "Ada Lovelace", "ada@andrew.cmu.edu")
    uid = _draft(client)
    client.post(f"/admin/updates/{uid}/discard")
    r = _send(client, uid)
    assert "Not sent" in unquote(r.headers["location"]) and client.fake.sent == []


def test_notices_expire_and_clear(client):
    with dbmod.SessionLocal() as s:
        old = updates_mod.add_notice(s, "Old news", "2000-01-01")
        live = updates_mod.add_notice(s, "Pickup moved to Sunday", "")
        assert [n.id for n in updates_mod.active_notices(s)] == [live.id]
        updates_mod.clear_notice(s, live.id)
        assert updates_mod.active_notices(s) == [] and old.id


def test_desk_knows_the_notice():
    assert "CURRENT NOTICE" not in support_mod.knowledge_base()
    assert "No pickup Saturday" in support_mod.knowledge_base("No pickup Saturday")
    order = dbmod.Order(pickup_code="SL-AAAA-BBBB", buyer_name="Ada Lovelace", buyer_email="ada@andrew.cmu.edu", dedup_hash="x")
    assert "Please note: No pickup Saturday" in auto_reply_text(order, "cant_make_it", "No pickup Saturday")
    assert "Please note" not in auto_reply_text(order, "cant_make_it")


def test_item_labels_and_dates():
    assert updates_mod._item_name("ScottyLabs Found T-Shirt, Size S (Pittsburgh Pickup Only, No Refunds)") == "ScottyLabs Found T-Shirt"
    assert updates_mod._item_name("ScottyLabs Found T-Shirt — Size M") == "ScottyLabs Found T-Shirt"
    end = updates_mod.end_of_day("2026-09-27")
    assert end.tzinfo is not None and end.astimezone(updates_mod._tz()).strftime("%Y-%m-%d %H:%M") == "2026-09-27 23:59"
    assert updates_mod.end_of_day("") is None and updates_mod.end_of_day("soon") is None
    assert updates_mod.render("Hi {first_name}, {nope}", {"first_name": "Ada"}) == "Hi Ada, {nope}"


def test_system_prompt_has_today_and_the_rules():
    p = updates_mod._system_prompt()
    today = dt.datetime.now(updates_mod._tz()).strftime("%B %-d, %Y")
    assert today in p and "{first_name}" in p and "Never invent" in p and "desk_notice" in p
