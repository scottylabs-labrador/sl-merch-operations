"""Turn a TartanConnect purchase email into a ParsedOrder.

Two real templates (captured 2026-09-06, see tests/fixtures/):

  officer notification  "Hi <officer>, <Buyer Name> successfully purchased from the store:
                         Order summary / Order #15491205 / Sep 6, 2026 11:09pm /
                         Name Quantity Cost / <product> / Ordered / 1 / $1.00 / Total / $1.00"
                         -> buyer name, order number, items, total. NO buyer email.

  buyer receipt         "Hi <First>, You successfully purchased from the store: ..." with the
                         same table, the store's custom message, and a footer
                         "This message is intended for <buyer email>".

Parsing order: the TartanConnect template parser first, then a generic regex
parser for anything else, then (if OPENAI_API_KEY is set) an LLM extraction
whose output is validated against the text. A result is *complete* when it has
items and a buyer email; it is *parsable* when it has items and either a buyer
name or an order number, in which case the order is created and the email is
resolved later (forwarded receipt, Sales report upload, or an officer).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from .config import settings

log = logging.getLogger(__name__)

SIZES = ["XS", "S", "M", "L", "XL", "XXL", "XXXL"]
SIZE_RE = re.compile(r"\bsize\s*[:\-–—]?\s*(XXXL|XXL|XL|XS|S|M|L)\b", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MONEY_RE = re.compile(r"\$\s?(\d{1,6}(?:[.,]\d{2})?)")
ORDER_RE = re.compile(r"Order\s*#\s*(\d{3,})", re.IGNORECASE)
TC_DATE_RE = re.compile(r"\b([A-Z][a-z]{2} \d{1,2}, \d{4} \d{1,2}:\d{2}\s*[ap]m)\b", re.IGNORECASE)
INTENDED_RE = re.compile(r"intended for\s+([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", re.IGNORECASE)
REF_RE = re.compile(
    r"(?:transaction|receipt|order|reference|confirmation|payment)\s*(?:#|no\.?|number|id)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{3,})",
    re.IGNORECASE,
)
PURCHASE_SUBJECT_RE = re.compile(r"purchase|store|order", re.IGNORECASE)


@dataclass
class ParsedItem:
    product_name: str
    quantity: int = 1
    size: Optional[str] = None
    unit_price_cents: int = 0


@dataclass
class ParsedOrder:
    buyer_name: str
    buyer_email: str
    items: List[ParsedItem]
    total_cents: int = 0
    tc_reference: Optional[str] = None
    purchased_at: Optional[dt.datetime] = None
    kind: str = "unknown"  # officer | buyer_receipt | generic
    source: str = "deterministic"
    warnings: List[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return bool(self.buyer_email and EMAIL_RE.fullmatch(self.buyer_email) and self.items)

    @property
    def is_parsable(self) -> bool:
        return bool(self.items and (self.tc_reference or (self.buyer_name and self.buyer_name != "Unknown buyer")))

    def dedup_hash(self, purchased_minute: str) -> str:
        who = self.buyer_email.lower() if self.buyer_email else "name:" + self.buyer_name.strip().lower()
        key = json.dumps(
            {"who": who, "items": sorted((i.product_name, i.quantity) for i in self.items), "minute": purchased_minute},
            sort_keys=True,
        )
        return hashlib.sha256(key.encode()).hexdigest()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for cell in soup.find_all(["td", "th", "p", "li", "div"]):
        cell.append("\n")
    text = soup.get_text("\n")
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _cents(amount: str) -> int:
    return int(round(float(amount.replace(",", "")) * 100))


def is_purchase_notification(subject: Optional[str], text: str) -> bool:
    subj = subject or ""
    if re.search(r"successfully purchased", subj + " " + text, re.IGNORECASE):
        return True
    if PURCHASE_SUBJECT_RE.search(subj):
        return True
    return bool(re.search(r"\b(purchased|new store purchase|store purchase)\b", text, re.IGNORECASE))


def _org_addresses() -> List[str]:
    return [settings.org_email.lower(), settings.agentmail_inbox_id.lower(), *settings.trusted_senders]


def _parse_tc_date(text: str) -> Optional[dt.datetime]:
    m = TC_DATE_RE.search(text)
    if not m:
        return None
    raw = re.sub(r"\s+", " ", m.group(1)).strip()
    for fmt in ("%b %d, %Y %I:%M%p", "%b %d, %Y %I:%M %p"):
        try:
            local = dt.datetime.strptime(raw.upper().replace("AM", "AM").replace("PM", "PM"), fmt)
            return local.replace(tzinfo=ZoneInfo(settings.timezone)).astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return None


# --------------------------------------------------------------------------- #
# TartanConnect template parser
# --------------------------------------------------------------------------- #
def parse_tartanconnect(text: str, subject: str = "") -> Optional[ParsedOrder]:
    """Returns None when the text does not look like the TartanConnect template."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    joined = "\n".join(lines)
    if "successfully purchased" not in joined.lower():
        return None
    warnings: List[str] = []

    # Buyer name + kind
    kind = "generic"
    buyer_name = ""
    m = re.search(r"(?:^|\n)([^\n]{1,80}?)\s*\n?\s*successfully purchased from", joined, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip().rstrip(",")
        if candidate.lower() == "you":
            kind = "buyer_receipt"
            hm = re.search(r"^Hi\s+([^,\n]+),", joined, re.IGNORECASE | re.MULTILINE)
            buyer_name = hm.group(1).strip() if hm else ""
        elif candidate.lower().startswith("hi "):
            # "Hi <officer>, <Buyer Name> successfully purchased" on one line
            parts = candidate.split(",", 1)
            buyer_name = parts[1].strip() if len(parts) > 1 else ""
            kind = "officer"
        else:
            buyer_name = candidate
            kind = "officer"
    if not buyer_name:
        warnings.append("buyer name not found")

    # Buyer email: only the receipt carries one ("This message is intended for ...").
    buyer_email = ""
    im = INTENDED_RE.search(joined)
    if im:
        buyer_email = im.group(1).lower()
    else:
        ours = set(_org_addresses())
        for e in EMAIL_RE.findall(joined):
            if e.lower() not in ours and "tartanconnect" not in e.lower():
                buyer_email = e.lower()
                break
    if not buyer_email:
        warnings.append("buyer email not in notification")

    om = ORDER_RE.search(joined)
    tc_reference = om.group(1) if om else None
    if not tc_reference:
        warnings.append("order number not found")

    # Items: rows between the "Name / Quantity / Cost" header and "Total".
    items: List[ParsedItem] = []
    try:
        start = next(i for i in range(len(lines) - 2) if lines[i].lower() == "name" and lines[i + 1].lower() == "quantity" and lines[i + 2].lower() == "cost") + 3
    except StopIteration:
        start = None
    if start is not None:
        i = start
        name_buf: List[str] = []
        while i < len(lines) and lines[i].lower() != "total":
            ln = lines[i]
            if ln.lower() in ("ordered", "claimed", "purchased") and i + 2 < len(lines):
                qty_s, cost_s = lines[i + 1], lines[i + 2]
                qty = int(qty_s) if re.fullmatch(r"\d{1,4}", qty_s) else 1
                cm = MONEY_RE.search(cost_s)
                line_total = _cents(cm.group(1)) if cm else 0
                name = " ".join(name_buf).strip()
                if name:
                    sm = SIZE_RE.search(name)
                    items.append(ParsedItem(product_name=name, quantity=max(qty, 1), size=sm.group(1).upper() if sm else None, unit_price_cents=int(line_total / max(qty, 1))))
                name_buf = []
                i += 3
                continue
            # A row without the "Ordered" marker: name, qty, cost as three lines.
            if re.fullmatch(r"\d{1,4}", ln) and i + 1 < len(lines) and MONEY_RE.search(lines[i + 1]) and name_buf:
                qty = int(ln)
                line_total = _cents(MONEY_RE.search(lines[i + 1]).group(1))
                name = " ".join(name_buf).strip()
                sm = SIZE_RE.search(name)
                items.append(ParsedItem(product_name=name, quantity=max(qty, 1), size=sm.group(1).upper() if sm else None, unit_price_cents=int(line_total / max(qty, 1))))
                name_buf = []
                i += 2
                continue
            name_buf.append(ln)
            i += 1
    if not items:
        warnings.append("no item rows found")

    total_cents = 0
    tm = re.search(r"\nTotal\n\$?\s?(\d{1,6}(?:[.,]\d{2})?)", joined)
    if tm:
        total_cents = _cents(tm.group(1))
    elif items:
        total_cents = sum(i.unit_price_cents * i.quantity for i in items)

    return ParsedOrder(
        buyer_name=buyer_name or "Unknown buyer",
        buyer_email=buyer_email,
        items=items,
        total_cents=total_cents,
        tc_reference=tc_reference,
        purchased_at=_parse_tc_date(joined),
        kind=kind,
        source="deterministic",
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# generic parser (other platforms / template drift)
# --------------------------------------------------------------------------- #
def parse_deterministic(text: str, subject: str = "") -> ParsedOrder:
    tc = parse_tartanconnect(text, subject)
    if tc and tc.items:
        return tc
    warnings: List[str] = []
    ours = set(_org_addresses())
    emails = [e for e in EMAIL_RE.findall(text) if e.lower() not in ours and "tartanconnect" not in e.lower()]
    buyer_email = emails[0].lower() if emails else ""
    if not buyer_email:
        warnings.append("buyer email not found")

    buyer_name = ""
    m = re.search(r"(?:^|\n)(?:Hi [^,\n]+,\s*)?([A-Z][A-Za-z'.\- ]{1,60}?)\s+(?:has\s+)?(?:just\s+)?(?:purchased|bought|made a purchase)", text)
    if m:
        buyer_name = m.group(1).strip()
    if not buyer_name and buyer_email:
        m = re.search(r"([A-Z][A-Za-z'.\- ]{1,60})\s*\(?\s*" + re.escape(buyer_email), text, re.IGNORECASE)
        if m:
            buyer_name = m.group(1).strip()
    if not buyer_name:
        m = re.search(r"(?:Buyer|Name|Member|Purchaser|Customer)\s*[:\-]\s*([A-Z][A-Za-z'.\- ]{1,60})", text)
        if m:
            buyer_name = m.group(1).strip()
    if not buyer_name:
        buyer_name = buyer_email.split("@")[0] if buyer_email else "Unknown buyer"
        warnings.append("buyer name not found; used email local part")

    items: Dict[str, ParsedItem] = {}
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        sm = SIZE_RE.search(line)
        if not sm:
            continue
        product = line.strip()
        size = sm.group(1).upper()
        qty = 1
        window = " ".join(lines[idx : idx + 4])
        qm = (
            re.search(r"(?:qty|quantity)\s*[:\-]?\s*(\d{1,3})", window, re.IGNORECASE)
            or re.search(r"(\d{1,3})\s*[x×]\s", line)
            or re.search(r"[x×]\s*(\d{1,3})\b", line)
        )
        if qm:
            qty = int(qm.group(1))
        else:
            for nxt in lines[idx + 1 : idx + 4]:
                if re.fullmatch(r"\d{1,3}", nxt.strip()):
                    qty = int(nxt.strip())
                    break
        price = 0
        pm = MONEY_RE.search(window)
        if pm:
            price = _cents(pm.group(1))
        key = product.lower()
        if key in items:
            items[key].quantity += qty
        else:
            items[key] = ParsedItem(product_name=product, quantity=max(qty, 1), size=size, unit_price_cents=price)

    total_cents = 0
    tm = re.search(r"total[^$\n]*\$\s?(\d{1,6}(?:[.,]\d{2})?)", text, re.IGNORECASE)
    if tm:
        total_cents = _cents(tm.group(1))
    else:
        amounts = [_cents(a) for a in MONEY_RE.findall(text)]
        if amounts:
            total_cents = max(amounts)
    if not total_cents and items:
        total_cents = sum(i.unit_price_cents * i.quantity for i in items.values())

    ref = None
    for rm in REF_RE.finditer(text):
        candidate = rm.group(1).strip("#: ")
        if any(ch.isdigit() for ch in candidate):
            ref = candidate
            break
    if not items:
        warnings.append("no product lines with a size were found")

    return ParsedOrder(
        buyer_name=buyer_name,
        buyer_email=buyer_email,
        items=list(items.values()),
        total_cents=total_cents,
        tc_reference=ref,
        purchased_at=_parse_tc_date(text),
        kind=tc.kind if tc else "generic",
        source="deterministic",
        warnings=(tc.warnings if tc else []) + warnings,
    )


# --------------------------------------------------------------------------- #
# LLM fallback (OpenAI Structured Outputs)
# --------------------------------------------------------------------------- #
ORDER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "buyer_name": {"type": "string"},
        "buyer_email": {"type": "string"},
        "tc_reference": {"type": ["string", "null"]},
        "total_dollars": {"type": ["number", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "product_name": {"type": "string"},
                    "size": {"type": ["string", "null"]},
                    "quantity": {"type": "integer"},
                    "unit_price_dollars": {"type": ["number", "null"]},
                },
                "required": ["product_name", "size", "quantity", "unit_price_dollars"],
            },
        },
    },
    "required": ["buyer_name", "buyer_email", "tc_reference", "total_dollars", "items"],
}


def parse_with_llm(text: str, subject: str = "") -> Optional[ParsedOrder]:
    if not settings.openai_api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        response = client.responses.create(
            model=settings.openai_model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You extract structured purchase data from a campus store notification email. "
                        "Return only what is literally present. The buyer is the student who paid, never the "
                        "organization or platform. If no buyer email is present, return an empty string for it. "
                        "Sizes are one of XS,S,M,L,XL,XXL,XXXL or null."
                    ),
                },
                {"role": "user", "content": f"Subject: {subject}\n\n{text[:12000]}"},
            ],
            text={"format": {"type": "json_schema", "name": "purchase", "schema": ORDER_SCHEMA, "strict": True}},
        )
        data = json.loads(response.output_text)
    except Exception as exc:
        log.warning("LLM parse failed: %s", exc)
        return None

    items = []
    for it in data.get("items", []):
        size = (it.get("size") or "").upper() or None
        if size and size not in SIZES:
            size = None
        items.append(ParsedItem(product_name=it["product_name"].strip(), quantity=max(int(it.get("quantity") or 1), 1), size=size, unit_price_cents=int(round((it.get("unit_price_dollars") or 0) * 100))))
    total = int(round((data.get("total_dollars") or 0) * 100))
    parsed = ParsedOrder(
        buyer_name=(data.get("buyer_name") or "").strip() or "Unknown buyer",
        buyer_email=(data.get("buyer_email") or "").strip().lower(),
        items=items,
        total_cents=total or sum(i.unit_price_cents * i.quantity for i in items),
        tc_reference=(data.get("tc_reference") or None),
        purchased_at=_parse_tc_date(text),
        kind="generic",
        source="llm",
    )
    if parsed.buyer_email and parsed.buyer_email not in text.lower():
        parsed.warnings.append("LLM buyer email not present in email text; discarded")
        parsed.buyer_email = ""
    return parsed


def parse_purchase(text: str, subject: str = "") -> ParsedOrder:
    parsed = parse_deterministic(text, subject)
    if parsed.is_complete or (parsed.kind in ("officer", "buyer_receipt") and parsed.is_parsable):
        return parsed
    llm = parse_with_llm(text, subject)
    if llm and (llm.is_complete or llm.is_parsable):
        llm.warnings = parsed.warnings + llm.warnings
        return llm
    return parsed
