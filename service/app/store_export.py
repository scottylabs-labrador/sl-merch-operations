"""TartanConnect store export: parse, plan, commit.

The store has no API, so an officer downloads the order export
(Store → Sales → download CSV) and uploads it on /admin. This module turns
that file into orders.

Shape of the export: one row per item line, with the buyer's name and email,
the item, price, quantity, status and a timestamp. There is no order number.
A checkout is every row that shares the same buyer email and the same
timestamp, so rows are grouped back into orders on that key, and an order's
identity is (email, timestamp, items). That identity is stable across
re-uploads of the full history, which is how officers are expected to use the
page: upload the latest export, known orders are skipped, new ones get codes,
refunded ones get cancelled.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from . import decide
from .checkout_answer import CheckoutAnswer, merge_answers
from .consent import latest_revocation
from .config import settings
from .db import Order
from .orders import DuplicateOrder, create_order, resolve_buyer_email, send_code_email
from .parser import SIZE_RE, ParsedItem, ParsedOrder

log = logging.getLogger(__name__)


class ExportFormatError(ValueError):
    """The file is not a TartanConnect store export we can read."""


# Column aliases: exact header (normalised) first, then a substring fallback.
COLUMNS: Dict[str, Tuple[Tuple[str, ...], str]] = {
    "email": (("buyer email", "email"), "email"),
    "first": (("buyer first name", "first name"), "first"),
    "last": (("buyer last name", "last name"), "last"),
    "item": (("item name", "item", "product name", "product"), "item"),
    "price": (("item price", "unit price", "price"), "price"),
    "qty": (("quantity", "qty"), "quantity"),
    "total": (("total paid", "total"), "total"),
    "status": (("status",), "status"),
    "date": (("date", "order date", "purchase date"), "date"),
    "comments": (("comments", "comment"), "comment"),
    "notes": (("notes", "note"), "note"),
}
REQUIRED = ("email", "item", "date")

DATE_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)

# "… — Size M" is handled by SIZE_RE; these cover "(M)", "- XL", "/ Small", "Medium".
TRAILING_SIZE_RE = re.compile(r"(?:^|[\s\-–—(/:])(XXXL|XXL|XL|XS|S|M|L)\)?\s*$", re.IGNORECASE)
WORD_SIZES = (
    ("extra small", "XS"), ("x-small", "XS"), ("xsmall", "XS"),
    ("3xl", "XXXL"), ("xxx-large", "XXXL"), ("xxxl", "XXXL"),
    ("2xl", "XXL"), ("xx-large", "XXL"), ("xxl", "XXL"),
    ("extra large", "XL"), ("x-large", "XL"), ("xlarge", "XL"),
    ("small", "S"), ("medium", "M"), ("large", "L"),
)


def size_from_item(name: str) -> Optional[str]:
    m = SIZE_RE.search(name) or TRAILING_SIZE_RE.search(name)
    if m:
        return m.group(1).upper()
    lowered = name.lower()
    for word, size in WORD_SIZES:
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", lowered):
            return size
    return None


def _cents(raw: str) -> int:
    cleaned = re.sub(r"[^0-9.\-]", "", raw or "")
    try:
        return int(round(float(cleaned) * 100)) if cleaned else 0
    except ValueError:
        return 0


def _tz() -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001 - missing tzdata; fall back rather than fail the upload
        log.warning("timezone %s unavailable, treating export timestamps as UTC", settings.timezone)
        return dt.timezone.utc


def _parse_date(raw: str) -> Optional[dt.datetime]:
    raw = re.sub(r"\s+", " ", (raw or "").strip())
    for fmt in DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt).replace(tzinfo=_tz()).astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return None


def _ignored_item(name: str) -> bool:
    lowered = name.lower()
    return any(p and p in lowered for p in settings.export_ignore_items)


def _resolve_columns(fieldnames: List[str]) -> Dict[str, str]:
    norm = {re.sub(r"\s+", " ", (f or "").strip().lower().strip('"')): f for f in fieldnames if f}
    out: Dict[str, str] = {}
    for logical, (exact, fuzzy) in COLUMNS.items():
        for name in exact:
            if name in norm:
                out[logical] = norm[name]
                break
        else:
            for key, original in norm.items():
                if fuzzy in key and original not in out.values():
                    out[logical] = original
                    break
    missing = [c for c in REQUIRED if c not in out]
    if missing:
        raise ExportFormatError(
            f"missing column(s) {', '.join(missing)}; found: {', '.join(f for f in fieldnames if f)}"
        )
    return out


@dataclass
class ExportOrder:
    buyer_name: str
    buyer_email: str
    purchased_at: dt.datetime
    items: List[ParsedItem] = field(default_factory=list)
    total_cents: int = 0
    statuses: List[str] = field(default_factory=list)
    lines: List[int] = field(default_factory=list)
    comments: List[str] = field(default_factory=list)

    @property
    def ordered(self) -> bool:
        return bool(self.statuses) and all(s == "ordered" for s in self.statuses)

    @property
    def status_text(self) -> str:
        return ", ".join(sorted(set(self.statuses)))

    @property
    def key(self) -> str:
        # Items are identified by size (falling back to the name), so renaming a listing
        # never makes past checkouts look new on the next upload.
        payload = {
            "email": self.buyer_email,
            "at": self.purchased_at.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
            "items": sorted((_item_identity(i.size, i.product_name), i.quantity) for i in self.items),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def summary(self) -> str:
        return ", ".join(f"{i.quantity}× {i.product_name}" + (f" [{i.size}]" if i.size else "") for i in self.items)

    @property
    def answer(self) -> CheckoutAnswer:
        return merge_answers(self.comments)


def parse_store_export(text: str) -> Tuple[List[ExportOrder], List[Tuple[int, str]]]:
    """Return (orders, skipped) where skipped is [(line_number, reason)]."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ExportFormatError("empty file")
    col = _resolve_columns(list(reader.fieldnames))
    get = lambda row, logical: (row.get(col[logical]) or "").strip() if logical in col else ""  # noqa: E731

    grouped: Dict[Tuple[str, dt.datetime], ExportOrder] = {}
    skipped: List[Tuple[int, str]] = []
    for line_no, row in enumerate(reader, start=2):
        if not any((v or "").strip() for v in row.values()):
            continue
        email = get(row, "email").lower()
        item = get(row, "item")
        if not email or "@" not in email:
            skipped.append((line_no, "no buyer email"))
            continue
        if not item:
            skipped.append((line_no, "no item name"))
            continue
        if _ignored_item(item):
            skipped.append((line_no, f"ignored item: {item[:60]}"))
            continue
        purchased_at = _parse_date(get(row, "date"))
        if purchased_at is None:
            skipped.append((line_no, f"unreadable date: {get(row, 'date')!r}"))
            continue
        try:
            quantity = max(int(float(get(row, "qty") or "1")), 1)
        except ValueError:
            quantity = 1
        unit_cents = _cents(get(row, "price"))
        total_cents = _cents(get(row, "total")) or unit_cents * quantity
        name = (get(row, "first") + " " + get(row, "last")).strip() or email.split("@")[0]

        order = grouped.setdefault((email, purchased_at), ExportOrder(buyer_name=name, buyer_email=email, purchased_at=purchased_at))
        order.items.append(ParsedItem(product_name=item, quantity=quantity, size=size_from_item(item), unit_price_cents=unit_cents))
        order.total_cents += total_cents
        order.statuses.append((get(row, "status") or "ordered").lower())
        order.lines.append(line_no)
        if get(row, "comments"):
            order.comments.append(get(row, "comments"))
    return sorted(grouped.values(), key=lambda o: (o.purchased_at, o.buyer_email)), skipped


# --------------------------------------------------------------------------- #
# plan (no writes) and commit
# --------------------------------------------------------------------------- #

ACTIONS = ("create", "duplicate", "cancel", "not_ordered", "resolve_email", "not_merch")


@dataclass
class Planned:
    order: ExportOrder
    action: str
    existing: Optional[Order] = None
    note: str = ""


def _item_identity(size: Optional[str], name: str) -> str:
    return ("size:" + size.upper()) if size else ("name:" + re.sub(r"\s+", " ", name.strip().lower()))


def _same_items(a: ExportOrder, b: Order) -> bool:
    left = sorted((_item_identity(i.size, i.product_name), i.quantity) for i in a.items)
    right = sorted((_item_identity(i.size, i.product_name), i.quantity) for i in b.items)
    return left == right


def _fuzzy_match(session: Session, eo: ExportOrder) -> Optional[Order]:
    """An order created another way (manual, email) for the same buyer, items and moment."""
    candidates = session.scalars(select(Order).where(func.lower(Order.buyer_email) == eo.buyer_email)).all()
    for cand in candidates:
        at = cand.purchased_at
        if at is None:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=dt.timezone.utc)
        if abs((at - eo.purchased_at).total_seconds()) <= 15 * 60 and _same_items(eo, cand):
            return cand
    return None


def _awaiting_email_match(session: Session, eo: ExportOrder) -> Optional[Order]:
    """An order from an officer notification (no email in that template) for the same name, items and moment."""
    candidates = session.scalars(select(Order).where(Order.status == "needs_email", func.lower(Order.buyer_name) == eo.buyer_name.lower())).all()
    for cand in candidates:
        at = cand.purchased_at
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=dt.timezone.utc)
        if at is not None and abs((at - eo.purchased_at).total_seconds()) <= 24 * 3600 and _same_items(eo, cand):
            return cand
    return None


def plan_store_export(session: Session, orders: List[ExportOrder]) -> List[Planned]:
    plan: List[Planned] = []
    # One Jev call judges every distinct listing name; rows that are not pickup items get no code.
    not_merch = decide.classify_items(sorted({i.product_name for eo in orders for i in eo.items}))
    for eo in orders:
        if eo.items and not_merch and all(not_merch.get(i.product_name, 0.0) >= decide.NOT_MERCH_THRESHOLD for i in eo.items):
            worst = max(not_merch.get(i.product_name, 0.0) for i in eo.items)
            plan.append(Planned(eo, "not_merch", None, f"Jev: not a pickup item (p={worst:.2f}); no order created"))
            continue
        existing = session.scalar(select(Order).where(Order.dedup_hash == eo.key))
        note = ""
        if existing is None:
            existing = _fuzzy_match(session, eo)
            if existing is not None:
                note = "matched an existing order by buyer, time and items"
        if existing is None and eo.ordered:
            awaiting = _awaiting_email_match(session, eo)
            if awaiting is not None:
                plan.append(Planned(eo, "resolve_email", awaiting, f"fills in the email for code {awaiting.pickup_code}"))
                continue
        if existing is not None:
            if not eo.ordered and existing.status in ("pending", "needs_email", "needs_review"):
                plan.append(Planned(eo, "cancel", existing, f"export says {eo.status_text}; code {existing.pickup_code} will be refused at pickup"))
            else:
                plan.append(Planned(eo, "duplicate", existing, note or f"already has code {existing.pickup_code} ({existing.status})"))
        elif not eo.ordered:
            plan.append(Planned(eo, "not_ordered", None, f"status {eo.status_text}; no order created"))
        else:
            plan.append(Planned(eo, "create"))
    return plan


@dataclass
class UploadResult:
    created: List[Order] = field(default_factory=list)
    emailed: int = 0
    email_failed: int = 0
    cancelled: int = 0
    duplicates: int = 0
    skipped: int = 0
    resolved: int = 0


def _utc(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(dt.timezone.utc)


def _consent_effective_from() -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(settings.consent_prompt_effective_from.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        return dt.datetime.max.replace(tzinfo=dt.timezone.utc)  # misconfigured: never grant consent


def apply_answer(session: Session, order: Order, eo: ExportOrder, overwrite: bool = False) -> None:
    """Store what the buyer typed at checkout, and what we read from it, on the order.

    Call/text consent is granted only when the parser's allowlist says yes, the purchase
    happened after the disclosure went live, and the buyer has not opted out since.
    """
    if not eo.comments or (order.checkout_answer and not overwrite):
        return
    ans = eo.answer
    notes = list(ans.notes)
    order.checkout_answer = ans.raw[:2000]
    order.contact_email = ans.cmu_email  # only a CMU address counts; a non-CMU one stays in the raw answer
    order.contact_phone = ans.phone
    order.terms_agreed = ans.agreed
    consent = ans.calls_opt_in
    if consent and _utc(eo.purchased_at) < _consent_effective_from():
        consent = False
        notes.append("purchased before the call/text disclosure went live: no consent")
    revoked = latest_revocation(session, [eo.buyer_email, ans.cmu_email], ans.phone)
    if revoked is not None and revoked >= _utc(eo.purchased_at):
        consent = False
        order.calls_opt_out_at = order.calls_opt_out_at or revoked
        notes.append("buyer opted out of calls/texts after this purchase: no consent")
    if order.calls_opt_out_at is not None:
        consent = False
    order.calls_opt_in = consent
    if notes:
        order.notes = ((order.notes + "; ") if order.notes else "") + "; ".join(notes)[:1500]


def commit_store_export(session: Session, plan: List[Planned], send_email: bool) -> UploadResult:
    result = UploadResult()
    for p in plan:
        try:
            _commit_one(session, p, send_email, result)
        except SQLAlchemyError as exc:  # one bad row (e.g. a value too long for its column) never stops the rest
            session.rollback()
            log.warning("store export row for %s skipped: %s", p.order.buyer_email, exc)
            result.skipped += 1
    return result


def _commit_one(session: Session, p: "Planned", send_email: bool, result: "UploadResult") -> None:
    if True:
        eo = p.order
        if p.action == "create":
            parsed = ParsedOrder(
                buyer_name=eo.buyer_name,
                buyer_email=eo.buyer_email,
                items=eo.items,
                total_cents=eo.total_cents,
                purchased_at=eo.purchased_at,
                kind="store_export",
                source="store_export",
                dedup_override=eo.key,
            )
            try:
                order = create_order(session, parsed, purchased_at=eo.purchased_at)
            except DuplicateOrder:
                result.duplicates += 1
                return
            if eo.comments:
                apply_answer(session, order, eo)
            else:
                order.checkout_answer = ""  # blank box: record that nothing was typed
            session.commit()
            result.created.append(order)
            if send_email:
                if send_code_email(session, order):
                    result.emailed += 1
                else:
                    result.email_failed += 1
        elif p.action == "cancel" and p.existing is not None:
            p.existing.status = "cancelled"
            p.existing.notes = ((p.existing.notes + "; ") if p.existing.notes else "") + f"cancelled via store export ({eo.status_text})"
            session.commit()
            result.cancelled += 1
        elif p.action == "resolve_email" and p.existing is not None:
            if resolve_buyer_email(session, p.existing, eo.buyer_email, send=send_email):
                result.emailed += 1
            p.existing.dedup_hash = eo.key
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
            result.resolved += 1
        elif p.action == "duplicate":
            result.duplicates += 1
            if p.existing is not None:
                apply_answer(session, p.existing, eo)  # fills in orders that predate the answer; never overwrites
                # An order created another way: adopt the export identity so later uploads match exactly.
                if p.existing.dedup_hash != eo.key:
                    p.existing.dedup_hash = eo.key
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
        else:
            result.skipped += 1
