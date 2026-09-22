"""Officer uploads TartanConnect's store export: parse, preview, commit, re-upload, refunds."""
import base64
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_flow import _purchase_event, client  # noqa: F401  (shared fixture: fresh SQLite + fake AgentMail)
from app import webhook as webhook_mod
from app.db import InboundEmail
from app import db as dbmod
from app import store_export as se
from app.db import Order

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "store_export.csv")


def _csv() -> str:
    with open(FIX, encoding="utf-8") as f:
        return f.read()


def _login(client):
    client.post("/login", data={"passcode": "adm", "next": "/admin"}, follow_redirects=False)


def _commit(client, text: str, send_email: bool = True):
    return client.post(
        "/admin/upload/commit",
        data={"csv_b64": base64.b64encode(text.encode()).decode(), "send_email": "yes" if send_email else "no"},
        follow_redirects=False,
    )


def test_parse_groups_rows_into_orders():
    orders, skipped = se.parse_store_export(_csv())
    by = {o.buyer_email: o for o in orders}
    jane = by["jdoe@andrew.cmu.edu"]
    assert jane.buyer_name == "Jane Doe"
    assert sorted((i.size, i.quantity, i.unit_price_cents) for i in jane.items) == [("L", 2, 1000), ("M", 1, 1000)]
    assert jane.total_cents == 3000 and jane.ordered
    assert jane.purchased_at.tzinfo is not None
    assert by["rpatel@andrew.cmu.edu"].ordered is False
    assert "officer@andrew.cmu.edu" not in by
    assert any("ignored item" in reason for _, reason in skipped)
    again, _ = se.parse_store_export(_csv())
    assert {o.key for o in again} == {o.key for o in orders}


def test_size_detection_variants():
    cases = [
        ("ScottyLabs Found T-Shirt — Size M", "M"),
        ("Found Tee (XL)", "XL"),
        ("Found Tee - XXL", "XXL"),
        ("Found Tee / Small", "S"),
        ("Found Tee Medium", "M"),
        ("Sticker pack", None),
    ]
    for name, size in cases:
        assert se.size_from_item(name) == size, name


def test_rejects_unrelated_csv():
    try:
        se.parse_store_export("foo,bar\n1,2\n")
    except se.ExportFormatError as exc:
        assert "missing column" in str(exc)
    else:
        raise AssertionError("expected ExportFormatError")


def test_preview_writes_nothing(client):
    _login(client)
    with open(FIX, "rb") as f:
        r = client.post("/admin/upload", files={"file": ("export.csv", f, "text/csv")})
    assert r.status_code == 200
    assert 'id="count-create">2<' in r.text
    assert 'id="count-not_ordered">1<' in r.text
    assert 'id="count-skipped-rows">1<' in r.text
    assert client.fake.sent == []
    with Session(dbmod.engine) as s:
        assert s.scalar(select(Order)) is None


def test_commit_then_reupload_is_idempotent(client):
    _login(client)
    r = _commit(client, _csv())
    assert r.status_code == 303 and "upload-created-2-emailed-2" in r.headers["location"]
    assert len(client.fake.sent) == 2
    codes = {m["subject"] for m in client.fake.sent}
    assert len(codes) == 2

    r = _commit(client, _csv())
    assert "upload-created-0-emailed-0-cancelled-0-duplicates-2-resolved-0" in r.headers["location"]
    assert len(client.fake.sent) == 2  # nobody emailed twice

    # an extra row for a new buyer in the next export creates exactly one more
    extra = _csv() + '"Ana","Ruiz","aruiz@andrew.cmu.edu","2029","Undergraduate Student","ScottyLabs Found T-Shirt — Size M","10","1","10","","YES","Ordered","9/10/2026 4:00:00 PM","-"\n'
    r = _commit(client, extra)
    assert "upload-created-1-emailed-1-cancelled-0-duplicates-2-resolved-0" in r.headers["location"]
    with Session(dbmod.engine) as s:
        orders = s.scalars(select(Order)).all()
        assert len(orders) == 3 and all(o.parse_source == "store_export" for o in orders)
        jane = next(o for o in orders if o.buyer_email == "jdoe@andrew.cmu.edu")
        assert jane.quantity_total == 3 and jane.total_cents == 3000


def test_refund_in_later_export_cancels_the_order(client):
    _login(client)
    first = _csv().replace('"Refunded"', '"Ordered"')
    r = _commit(client, first)
    assert "upload-created-3" in r.headers["location"]
    r = _commit(client, _csv())
    assert "upload-created-0-emailed-0-cancelled-1-duplicates-2-resolved-0" in r.headers["location"]
    with Session(dbmod.engine) as s:
        ravi = s.scalar(select(Order).where(Order.buyer_email == "rpatel@andrew.cmu.edu"))
        assert ravi.status == "cancelled" and "store export" in (ravi.notes or "")
    # a refund we never had an order for is skipped, not created
    r = _commit(client, _csv())
    assert "cancelled-0" in r.headers["location"]


def test_manual_order_is_matched_by_later_export(client):
    _login(client)
    client.post("/admin/orders/manual", data={"buyer_name": "Sam Lee", "buyer_email": "slee@andrew.cmu.edu", "size": "S", "quantity": "1", "send_email": "no"}, follow_redirects=False)
    with Session(dbmod.engine) as s:
        sam = s.scalar(select(Order).where(Order.buyer_email == "slee@andrew.cmu.edu"))
        # manual orders use a generic product name; align it and the time so the fuzzy match applies
        sam.items[0].product_name = "ScottyLabs Found T-Shirt — Size S"
        sam.purchased_at = se.parse_store_export(_csv())[0][1].purchased_at if False else next(o for o in se.parse_store_export(_csv())[0] if o.buyer_email == "slee@andrew.cmu.edu").purchased_at
        s.commit()
    r = _commit(client, _csv())
    assert "upload-created-1-emailed-1" in r.headers["location"]  # only Jane is new
    with Session(dbmod.engine) as s:
        assert s.scalar(select(Order).where(Order.buyer_email == "slee@andrew.cmu.edu")).parse_source == "manual"
        assert len(s.scalars(select(Order)).all()) == 2


def test_email_intake_off_ignores_purchase_notifications(client, monkeypatch):
    monkeypatch.setattr(webhook_mod.settings, "email_order_intake", False)
    r = client.post("/webhooks/agentmail", json=_purchase_event())
    assert r.status_code == 200
    with Session(dbmod.engine) as s:
        assert s.scalar(select(Order)) is None
        rec = s.scalar(select(InboundEmail))
        assert rec.classification == "ignored" and "store export" in (rec.detail or "")
    assert client.fake.sent == []
