"""The single checkout answer: parsing real-world typing, storing it, consent, migration, buyer-facing text."""
import base64
import os
import sqlite3

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_flow import client  # noqa: F401
from app import db as dbmod
from app import emails as emails_mod
from app.checkout_answer import merge_answers, parse_checkout_answer
from app.db import InboundEmail, Order


@pytest.mark.parametrize(
    "typed, agreed, cmu, phone, calls",
    [
        ("YES, jdoe@andrew.cmu.edu, 412-555-2671, CALLS OK", True, "jdoe@andrew.cmu.edu", "+14125552671", True),
        ("yes jdoe@andrew.cmu.edu +1 412 555 2671 calls ok", True, "jdoe@andrew.cmu.edu", "+14125552671", True),
        ("jdoe@andrew.cmu.edu, yes, 412.555.2671, CALLS OK", True, "jdoe@andrew.cmu.edu", "+14125552671", True),
        ("YES, jdoe@andrew.cmu.edu, (412) 555-2671, CALLS/TEXTS OK", True, "jdoe@andrew.cmu.edu", "+14125552671", True),
        ("YES, jdoe@andrew.cmu.edu, 412-555-2671, CALLS & TEXTS OK, thanks!", True, "jdoe@andrew.cmu.edu", "+14125552671", True),
        ("yep jdoe@andrew.cmu.edu 4125552671", True, "jdoe@andrew.cmu.edu", "+14125552671", False),
        ("yes", True, None, None, False),
        ("y", True, None, None, False),
        ("", False, None, None, False),
        ("YES, jdoe@andrew.cmu.edu, (412) 555-0123", True, "jdoe@andrew.cmu.edu", None, False),       # the example number
        ("YES, andrewid@andrew.cmu.edu, 412-555-2671", True, None, "+14125552671", False),         # the example email
        ("Yes, jsmith@gmail.com, 412-555-2671, calls ok", True, None, "+14125552671", True),        # consent ok, CMU email missing
        ("YES, jdoe@andrew.cmu.edu, +44 20 7946 0958, CALLS OK", True, "jdoe@andrew.cmu.edu", "+442079460958", False),
        ("13812345678 calls ok", False, None, "13812345678", False),
        ("yes, jdoe@andrew.cmu.edu, 4125552671, no", True, "jdoe@andrew.cmu.edu", "+14125552671", False),
        ("no", False, None, None, False),
        ("YES jdoe @ andrew.cmu. edu 412-555-2671", True, "jdoe@andrew.cmu.edu", "+14125552671", False),
    ],
)
def test_parse_real_answers(typed, agreed, cmu, phone, calls):
    a = parse_checkout_answer(typed)
    assert (a.agreed, a.cmu_email, a.phone, a.calls_opt_in) == (agreed, cmu, phone, calls), (typed, a)


@pytest.mark.parametrize("typed", [
    "YES jdoe@andrew.cmu.edu 412 555 2671 calls ok: no",
    "yes, jdoe@andrew.cmu.edu, 4125552671, i don’t want calls ok",
    "yes, jdoe@andrew.cmu.edu, 4125552671, what is calls ok?",
    "yes, jdoe@andrew.cmu.edu, 4125552671, calls ok? nah",
    "yes, jdoe@andrew.cmu.edu, 4125552671, calls ok (n)",
    "yes, jdoe@andrew.cmu.edu, 4125552671, calls ok — no",
    "yes, jdoe@andrew.cmu.edu, 4125552671, calls ok but not texts",
    "yes, jdoe@andrew.cmu.edu, 4125552671, calls ok, texts no",
    "yes, jdoe@andrew.cmu.edu, 4125552671, calls ok, don’t text",
    "yes, jdoe@andrew.cmu.edu, 4125552671, pls call ok if its delayed",
    "yes, jdoe@andrew.cmu.edu, 4125552671, TEXTS OK",
    "yes, jdoe@andrew.cmu.edu, 4125552671, CALLS OK, NO TEXTS",
    "yes, jdoe@andrew.cmu.edu, 4125552671, cals ok",
    "yes, jdoe@andrew.cmu.edu, 4125552671, opt in",
    "yes, jdoe@andrew.cmu.edu, 4125552671, 4125559999, calls ok",
    "YES jdoe@andrew.cmu.edu 4125552671 ok",
    "yes, sure, whatever",
])
def test_consent_is_never_inferred_from_ambiguous_text(typed):
    a = parse_checkout_answer(typed)
    assert a.calls_opt_in is False, (typed, a)


def test_notes_explain_what_was_unclear():
    assert any("typo" in n for n in parse_checkout_answer("yes, jdoe@andrew.cmu.edu, 4125552671, cals ok").notes)
    assert any("extra text" in n for n in parse_checkout_answer("yes jdoe@andrew.cmu.edu 412 555 2671 2 shirts for my son").notes)
    assert any("possible non-US" in n for n in parse_checkout_answer("13812345678 calls ok").notes)
    assert any("placeholder" in n for n in parse_checkout_answer("YES, andrewid@andrew.cmu.edu, 412-555-0123").notes)
    assert any("trailing NO" in n for n in parse_checkout_answer("yes, jdoe@andrew.cmu.edu, 4125552671, no").notes)


def test_missing_parts_and_merge_across_sizes():
    a = merge_answers(["YES, jdoe@andrew.cmu.edu", "412-555-2671, CALLS OK"])
    assert a.complete and a.calls_opt_in
    same_twice = merge_answers(["YES, jdoe@andrew.cmu.edu, 412-555-2671", "YES, jdoe@andrew.cmu.edu, 412-555-2671"])
    assert same_twice.complete and not any("more than one" in n for n in same_twice.notes)
    one_blank = merge_answers(["YES, jdoe@andrew.cmu.edu, 412-555-2671, CALLS OK", ""])
    assert one_blank.calls_opt_in
    assert parse_checkout_answer("YES").missing == ["CMU email", "phone number"]


EXPORT_HEADER = "Buyer First Name,Buyer Last Name,Buyer Email,Item Name,Item Price,Quantity,Total Paid,Comments,Status,Date\n"


def _commit(client, body, send_email=True):
    client.post("/login", data={"passcode": "adm", "next": "/admin"}, follow_redirects=False)
    return client.post("/admin/upload/commit", data={"csv_b64": base64.b64encode(body.encode()).decode(), "send_email": "yes" if send_email else "no"}, follow_redirects=False)


def test_export_upload_stores_answer_and_flags_incomplete(client):
    body = EXPORT_HEADER + (
        '"Jane","Doe","jdoe@andrew.cmu.edu","ScottyLabs Found T-Shirt — Size M","10.4","1","10.4","YES, jdoe@andrew.cmu.edu, 412-555-2671, CALLS OK","Ordered","9/26/2026 1:00:00 PM"\n'
        '"Sam","Lee","slee@andrew.cmu.edu","ScottyLabs Found T-Shirt — Size S","10.4","1","10.4","","Ordered","9/26/2026 1:05:00 PM"\n'
    )
    r = _commit(client, body)
    assert "upload-created-2" in r.headers["location"]
    with Session(dbmod.engine) as s:
        jane = s.scalar(select(Order).where(Order.buyer_email == "jdoe@andrew.cmu.edu"))
        sam = s.scalar(select(Order).where(Order.buyer_email == "slee@andrew.cmu.edu"))
        assert (jane.terms_agreed, jane.contact_email, jane.contact_phone, jane.calls_opt_in) == (True, "jdoe@andrew.cmu.edu", "+14125552671", True)
        assert jane.checkout_answer.startswith("YES") and jane.answer_missing == []
        assert sam.checkout_answer == "" and sam.answer_missing == ["YES agreement", "CMU email", "phone number"]
    page = client.get("/admin").text
    assert "Incomplete checkout answers (1)" in page and "calls/texts OK" in page
    csv_text = client.get("/admin/orders.csv").text
    assert "contact_phone" in csv_text.splitlines()[0] and "+14125552671" in csv_text


def test_preview_shows_what_was_read(client):
    client.post("/login", data={"passcode": "adm", "next": "/admin"}, follow_redirects=False)
    body = EXPORT_HEADER + '"Jane","Doe","jdoe@andrew.cmu.edu","ScottyLabs Found T-Shirt — Size M","10.4","1","10.4","yes 4125552671","Ordered","9/26/2026 1:00:00 PM"\n'
    r = client.post("/admin/upload", files={"file": ("export.csv", body.encode(), "text/csv")})
    assert r.status_code == 200 and "missing: CMU email" in r.text and "+14125552671" in r.text


def test_email_opt_out_revokes_consent(client, monkeypatch):
    from app import support as support_mod

    body = EXPORT_HEADER + '"Jane","Doe","jdoe@andrew.cmu.edu","ScottyLabs Found T-Shirt — Size M","10.4","1","10.4","YES, jdoe@andrew.cmu.edu, 412-555-2671, CALLS OK","Ordered","9/26/2026 1:00:00 PM"\n'
    _commit(client, body, send_email=False)
    monkeypatch.setattr(support_mod, "ask_model", lambda *a, **k: None)
    ev = {"type": "event", "event_type": "message.received", "event_id": "evt-stop",
          "message": {"message_id": "<stop@x>", "thread_id": "thr-stop", "from": "jdoe@andrew.cmu.edu", "subject": "stop", "text": "Please stop calling and texting me, thanks.", "timestamp": "2026-09-27T10:00:00Z"}, "thread": {}}
    client.post("/webhooks/agentmail", json=ev)
    with Session(dbmod.engine) as s:
        jane = s.scalar(select(Order).where(Order.buyer_email == "jdoe@andrew.cmu.edu"))
        rec = s.scalar(select(InboundEmail).where(InboundEmail.event_id == "evt-stop"))
        assert jane.calls_opt_in is False and jane.calls_opt_out_at is not None
        assert "opt-out recorded" in rec.detail
    # a later re-upload of the same export never turns consent back on
    _commit(client, body, send_email=False)
    with Session(dbmod.engine) as s:
        assert s.scalar(select(Order).where(Order.buyer_email == "jdoe@andrew.cmu.edu")).calls_opt_in is False


def test_migration_adds_columns_to_an_old_database(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, pickup_code VARCHAR(32), tc_reference VARCHAR(128), dedup_hash VARCHAR(64), buyer_name VARCHAR(200), buyer_email VARCHAR(320), total_cents INTEGER, purchased_at DATETIME, status VARCHAR(32), parse_source VARCHAR(32), source_message_id VARCHAR(512), source_thread_id VARCHAR(128), code_email_message_id VARCHAR(512), code_email_thread_id VARCHAR(128), code_email_sent_at DATETIME, code_email_error TEXT, notes TEXT, created_at DATETIME, updated_at DATETIME)")
    con.execute("INSERT INTO orders (id, pickup_code, dedup_hash, buyer_name, buyer_email, status, parse_source) VALUES (1, 'SL-AAAA-BBBB', 'h', 'Old', 'old@andrew.cmu.edu', 'pending', 'store_export')")
    con.commit(); con.close()
    from sqlalchemy import create_engine, inspect
    eng = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(dbmod, "engine", eng)
    dbmod.init_db()
    cols = {c["name"] for c in inspect(eng).get_columns("orders")}
    assert {"checkout_answer", "contact_email", "contact_phone", "terms_agreed", "calls_opt_in", "calls_opt_out_at"} <= cols
    dbmod.init_db()  # idempotent
    with Session(eng) as s:
        old = s.get(Order, 1)
        assert old.calls_opt_in is False and old.answer_missing == []  # predates the answer: not flagged


def test_buyer_facing_text_has_new_pickup_and_policy():
    from app.config import settings
    from app.support import knowledge_base

    assert "Tepper 3808" in settings.pickup_info and "Saturdays from 4:00 to 5:00 PM" in settings.pickup_info
    rules = emails_mod._rules_text() + emails_mod._rules_html()
    assert "GBM" not in rules and "no refunds" in rules and "never ship" in rules
    kb = knowledge_base()
    assert "$10.40" in kb and "ALL SALES ARE FINAL" in kb and "not handed out at GBMs" in kb


def test_consent_requires_purchase_after_disclosure_went_live(client):
    body = EXPORT_HEADER + '"Old","Buyer","old@andrew.cmu.edu","ScottyLabs Found T-Shirt, Size L (Pittsburgh Pickup Only, No Refunds)","10.4","1","10.4","YES, old@andrew.cmu.edu, 412-555-2671, CALLS OK","Ordered","9/20/2026 1:00:00 PM"\n'
    _commit(client, body, send_email=False)
    with Session(dbmod.engine) as s:
        o = s.scalar(select(Order).where(Order.buyer_email == "old@andrew.cmu.edu"))
        assert o.calls_opt_in is False and "before the call/text disclosure" in (o.notes or "")


def test_opt_out_blocks_consent_on_an_older_order_uploaded_later(client, monkeypatch):
    from app import support as support_mod

    first = EXPORT_HEADER + '"Jane","Doe","jdoe@andrew.cmu.edu","ScottyLabs Found T-Shirt, Size M (Pittsburgh Pickup Only, No Refunds)","10.4","1","10.4","YES, jdoe@andrew.cmu.edu, 412-555-2671, CALLS OK","Ordered","9/28/2026 1:00:00 PM"\n'
    _commit(client, first, send_email=False)
    monkeypatch.setattr(support_mod, "ask_model", lambda *a, **k: None)
    client.post("/webhooks/agentmail", json={"type": "event", "event_type": "message.received", "event_id": "evt-stop2",
        "message": {"message_id": "<stop2@x>", "thread_id": "thr-stop2", "from": "jdoe@andrew.cmu.edu", "subject": "calls", "text": "please don’t call or text me", "timestamp": "2026-09-29T10:00:00Z"}, "thread": {}})
    older = first + '"Jane","Doe","jdoe@andrew.cmu.edu","ScottyLabs Found T-Shirt, Size S (Pittsburgh Pickup Only, No Refunds)","10.4","1","10.4","YES, jdoe@andrew.cmu.edu, 412-555-2671, CALLS OK","Ordered","9/27/2026 1:00:00 PM"\n'
    _commit(client, older, send_email=False)
    with Session(dbmod.engine) as s:
        orders = s.scalars(select(Order).where(Order.buyer_email == "jdoe@andrew.cmu.edu")).all()
        assert len(orders) == 2 and all(o.calls_opt_in is False for o in orders)


def test_renaming_a_listing_does_not_duplicate_past_orders(client):
    old_name = EXPORT_HEADER + '"Jane","Doe","jdoe@andrew.cmu.edu","ScottyLabs Found T-Shirt — Size M","10","1","10","YES","Ordered","9/10/2026 1:00:00 PM"\n'
    new_name = old_name.replace("ScottyLabs Found T-Shirt — Size M", "ScottyLabs Found T-Shirt, Size M (Pittsburgh Pickup Only, No Refunds)")
    r1 = _commit(client, old_name, send_email=True)
    r2 = _commit(client, new_name, send_email=True)
    assert "upload-created-1" in r1.headers["location"] and "upload-created-0" in r2.headers["location"]
    assert len(client.fake.sent) == 1


def test_every_committed_export_is_archived(client):
    from app.db import ExportUpload

    body = EXPORT_HEADER + '"Jane","Doe","jdoe@andrew.cmu.edu","ScottyLabs Found T-Shirt, Size M (Pittsburgh Pickup Only, No Refunds)","10.4","1","10.4","YES, jdoe@andrew.cmu.edu, 412-555-2671","Ordered","9/26/2026 1:00:00 PM"\n'
    _commit(client, body, send_email=False)
    import hashlib

    with Session(dbmod.engine) as s:
        up = s.scalar(select(ExportUpload))
        assert up is not None and base64.b64decode(up.content_b64) == body.encode() and up.sha256 == hashlib.sha256(body.encode()).hexdigest()


def test_non_cmu_email_is_not_the_cmu_contact(client):
    body = EXPORT_HEADER + '"Pat","Parent","pat@gmail.com","ScottyLabs Found T-Shirt, Size M (Pittsburgh Pickup Only, No Refunds)","10.4","1","10.4","Yes, pat@gmail.com, 412-555-2671","Ordered","9/26/2026 1:00:00 PM"\n'
    _commit(client, body, send_email=False)
    with Session(dbmod.engine) as s:
        o = s.scalar(select(Order).where(Order.buyer_email == "pat@gmail.com"))
        assert o.contact_email is None and o.answer_missing == ["CMU email"]



# ---------------- regressions from the adversarial review ----------------

@pytest.mark.parametrize("typed", [
    "YES, jdoe@andrew.cmu.edu, +91 9172345678, CALLS OK",
    "YES, jdoe@andrew.cmu.edu, +91-9876543210, CALLS OK",
    "YES, jdoe@andrew.cmu.edu, +92 301 2345678, CALLS OK",
    "YES, jdoe@andrew.cmu.edu, +234 803 234 5678, CALLS OK",
    "YES, jdoe@andrew.cmu.edu, +44 207 946 0958, CALLS OK",
    "YES, jdoe@andrew.cmu.edu, 0091 9876543210, CALLS OK",
])
def test_international_numbers_are_never_read_as_us(typed):
    a = parse_checkout_answer(typed)
    assert a.phone and not a.phone.startswith("+1") and a.calls_opt_in is False, (typed, a)
    assert any("international" in n for n in a.notes)


@pytest.mark.parametrize("second_box", [
    "YES, jdoe@andrew.cmu.edu, 412-867-5309, remove CALLS OK",
    "skip the calls",
    "texts only",
    "same as the other box except CALLS OK",
])
def test_any_box_retracting_blocks_consent(second_box):
    assert merge_answers(["YES, jdoe@andrew.cmu.edu, 412-867-5309, CALLS OK", second_box]).calls_opt_in is False


@pytest.mark.parametrize("suffix", ["CALLS OK 不要", "CALLS OK: não", "CALLS OK нет", "不要 CALLS OK", "CALLS OK ❌", "CALLS OK 👎", "CALLS OK [ ]"])
def test_non_ascii_or_symbol_declines_block_consent(suffix):
    assert parse_checkout_answer("YES, jdoe@andrew.cmu.edu, 412-867-5309, " + suffix).calls_opt_in is False


def test_curly_quoted_opt_in_still_counts():
    assert parse_checkout_answer("YES, jdoe@andrew.cmu.edu, 412-867-5309, “CALLS OK”").calls_opt_in is True


def _email(client, event_id, sender, subject, text, ts="2026-09-29T10:00:00Z"):
    return client.post("/webhooks/agentmail", json={"type": "event", "event_type": "message.received", "event_id": event_id,
        "message": {"message_id": f"<{event_id}@x>", "thread_id": f"thr-{event_id}", "from": sender, "subject": subject, "text": text, "timestamp": ts}, "thread": {}})


CLEAN = EXPORT_HEADER + '"Jane","Doe","jdoe@andrew.cmu.edu","ScottyLabs Found T-Shirt, Size M (Pittsburgh Pickup Only, No Refunds)","10.4","1","10.4","YES, jdoe@andrew.cmu.edu, 412-555-2671, CALLS OK","Ordered","9/26/2026 1:00:00 PM"\n'


@pytest.mark.parametrize("text", [
    "Please unsubscribe me from the AI voice reminders.",
    "I no longer wish to be called or texted.",
    "No more robocalls please",
    "Please opt me out of the reminders.",
    "I revoke my consent to automated calls and texts.",
    "Take me off the call list please",
    "No calls please.",
    "No texts or calls, thanks",
    "Remove my number from your call list",
    "STOP",
])
def test_opt_out_emails_are_recorded(client, monkeypatch, text):
    from app import support as support_mod

    _commit(client, CLEAN, send_email=False)
    monkeypatch.setattr(support_mod, "ask_model", lambda *a, **k: None)
    _email(client, "evt-oo", "jdoe@andrew.cmu.edu", "re: order", text)
    with Session(dbmod.engine) as s:
        assert s.scalar(select(Order)).calls_opt_in is False, text


@pytest.mark.parametrize("text", [
    "What time is pickup on Saturday? Please don't forget my order!\n\nSent from my phone",
    "No more questions, thanks!\n\nSent from my phone",
    "I don't have my phone with me, can you resend my code?",
    "Thanks!\n\nOn Tue, Sep 29, 2026 ScottyLabs Merch Ops wrote:\n> reply STOP to a text to stop calls",
])
def test_ordinary_emails_do_not_record_opt_outs(client, monkeypatch, text):
    from app import support as support_mod

    _commit(client, CLEAN, send_email=False)
    monkeypatch.setattr(support_mod, "ask_model", lambda *a, **k: None)
    _email(client, "evt-ok", "jdoe@andrew.cmu.edu", "question", text)
    with Session(dbmod.engine) as s:
        assert s.scalar(select(Order)).calls_opt_in is True, text


def test_opt_out_before_the_order_is_uploaded_still_blocks_consent(client, monkeypatch):
    from app import support as support_mod

    monkeypatch.setattr(support_mod, "ask_model", lambda *a, **k: None)
    _email(client, "evt-early", "jdoe@andrew.cmu.edu", "changed my mind", "I changed my mind, please do not call or text me.", ts="2026-09-26T18:00:00Z")
    _commit(client, CLEAN, send_email=False)
    with Session(dbmod.engine) as s:
        assert s.scalar(select(Order)).calls_opt_in is False


def test_officer_records_opt_out_by_phone_and_it_sticks(client):
    _commit(client, CLEAN, send_email=False)
    r = client.post("/admin/calls-opt-out", data={"email": "", "phone": "(412) 555-2671", "note": "texted STOP"}, follow_redirects=False)
    assert "opt-out-recorded-1" in r.headers["location"]
    _commit(client, CLEAN, send_email=False)  # re-upload never brings consent back
    with Session(dbmod.engine) as s:
        o = s.scalar(select(Order))
        assert o.calls_opt_in is False and o.calls_opt_out_at is not None


def test_officer_records_missing_details_without_granting_consent(client):
    body = EXPORT_HEADER + '"Sam","Lee","slee@andrew.cmu.edu","ScottyLabs Found T-Shirt, Size S (Pittsburgh Pickup Only, No Refunds)","10.4","1","10.4","","Ordered","9/26/2026 1:05:00 PM"\n'
    _commit(client, body, send_email=False)
    with Session(dbmod.engine) as s:
        oid = s.scalar(select(Order)).id
    r = client.post(f"/admin/orders/{oid}/answer", data={"details": "YES, slee@andrew.cmu.edu, 412-555-2671, CALLS OK"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(dbmod.engine) as s:
        o = s.get(Order, oid)
        assert o.answer_missing == [] and o.calls_opt_in is False and "officer recorded" in o.notes


def test_bad_rows_never_stop_an_upload(client):
    import hashlib

    long_email = "a" * 330 + "@andrew.cmu.edu"
    body = ("﻿" + EXPORT_HEADER
            + f'"Long","Email","long@andrew.cmu.edu","ScottyLabs Found T-Shirt, Size M (Pittsburgh Pickup Only, No Refunds)","10.4","1","10.4","YES, {long_email}, 412-555-2671","Ordered","9/26/2026 1:00:00 PM"\n'
            + '"Nul","Byte","nul@andrew.cmu.edu","ScottyLabs Found T-Shirt, Size L (Pittsburgh Pickup Only, No Refunds)","10.4","1","10.4","YES,\x00 nul@andrew.cmu.edu, 412-555-2672","Ordered","9/26/2026 1:10:00 PM"\n')
    raw = body.encode("utf-8")
    client.post("/login", data={"passcode": "adm", "next": "/admin"}, follow_redirects=False)
    r = client.post("/admin/upload/commit", data={"csv_b64": base64.b64encode(raw).decode(), "send_email": "no", "filename": "export.csv"}, follow_redirects=False)
    assert r.status_code == 303 and "upload-created-2" in r.headers["location"], r.headers.get("location")
    from app.db import ExportUpload

    with Session(dbmod.engine) as s:
        assert s.scalar(select(ExportUpload)).sha256 == hashlib.sha256(raw).hexdigest()
        long_o = s.scalar(select(Order).where(Order.buyer_email == "long@andrew.cmu.edu"))
        assert long_o.contact_email is None
        assert "\x00" not in (s.scalar(select(Order).where(Order.buyer_email == "nul@andrew.cmu.edu")).checkout_answer or "")
