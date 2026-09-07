import os

from app.parser import html_to_text, is_purchase_notification, parse_deterministic

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "new_store_purchase.html")


def _text():
    with open(FIXTURE, encoding="utf-8") as fh:
        return html_to_text(fh.read())


def test_fixture_is_purchase():
    assert is_purchase_notification("New store purchase for ScottyLabs", _text())
    assert not is_purchase_notification("New officer appointed for ScottyLabs", "Hi Officer, Evan appointed Helen as an officer.")


def test_deterministic_extracts_everything():
    parsed = parse_deterministic(_text(), "New store purchase for ScottyLabs")
    assert parsed.is_complete
    assert parsed.buyer_email == "jqtartan@example.invalid"
    assert parsed.buyer_name == "Jane Q. Tartan"
    assert len(parsed.items) == 1
    item = parsed.items[0]
    assert item.size == "M" and item.quantity == 1 and item.unit_price_cents == 1000
    assert parsed.total_cents == 1000
    assert parsed.tc_reference == "TC-2026-000123"


def test_multi_line_and_quantity():
    text = (
        "Hi Officer,\nSam Buyer sbuyer@andrew.cmu.edu purchased:\n"
        "ScottyLabs Found T-Shirt — Size L\n2\n$10.00\n$20.00\n"
        "ScottyLabs Found T-Shirt — Size XS\n1\n$10.00\n$10.00\n"
        "Total: $30.00\n"
    )
    parsed = parse_deterministic(text)
    assert parsed.buyer_email == "sbuyer@andrew.cmu.edu"
    sizes = {i.size: i.quantity for i in parsed.items}
    assert sizes == {"L": 2, "XS": 1}
    assert parsed.total_cents == 3000


def test_incomplete_when_no_items():
    parsed = parse_deterministic("Hi Officer, someone@andrew.cmu.edu did something unrelated.")
    assert not parsed.is_complete
