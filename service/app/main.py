"""FastAPI application: webhook intake, volunteer pickup page, admin page."""
from __future__ import annotations

import csv
import datetime as dt
import hmac
import io
import json
import logging
import os
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .agentmail import AgentMail, AgentMailError
from .codes import normalize_code
from .config import settings
from .db import InboundEmail, Order, OrderItem, find_order_by_code, get_session, init_db
from .orders import DuplicateOrder, create_order, find_order_by_reference, record_pickup, resolve_buyer_email, send_code_email, unpicked_by_size
from .parser import ParsedItem, ParsedOrder
from .scheduler import send_bring_list, start_scheduler
from .webhook import handle_event, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    scheduler = start_scheduler() if os.environ.get("DISABLE_SCHEDULER") != "1" else None
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


app = FastAPI(title="ScottyLabs Merch Pickup Service", docs_url=None, redoc_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
signer = URLSafeSerializer(settings.session_secret, salt="merch-session")

ROLE_COOKIE = "merch_role"


# --------------------------------------------------------------------------- #
# auth helpers (two shared passcodes, stored as a signed cookie)
# --------------------------------------------------------------------------- #
def _role_from_request(request: Request) -> Optional[str]:
    raw = request.cookies.get(ROLE_COOKIE)
    if not raw:
        return None
    try:
        data = signer.loads(raw)
    except BadSignature:
        return None
    return data.get("role")


def require_role(*roles: str):
    def dependency(request: Request) -> str:
        role = _role_from_request(request)
        if role == "admin" or role in roles:
            return role
        raise HTTPException(status_code=401, detail="passcode required")

    return dependency


def _login_response(role: str, next_url: str) -> Response:
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(ROLE_COOKIE, signer.dumps({"role": role}), httponly=True, samesite="lax", max_age=60 * 60 * 12, secure=settings.public_base_url.startswith("https"))
    return resp


@app.get("/", include_in_schema=False)
def root() -> Response:
    return RedirectResponse("/pickup")


@app.get("/health")
def health(session: Session = Depends(get_session)) -> JSONResponse:
    pending = session.scalar(select(func.count()).select_from(Order).where(Order.status == "pending")) or 0
    return JSONResponse({"ok": True, "pending_orders": pending, "time": dt.datetime.now(dt.timezone.utc).isoformat()})


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/pickup", error: str = "") -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"next": next, "error": error})


@app.post("/login")
def login(passcode: str = Form(...), next: str = Form("/pickup")) -> Response:
    code = passcode.strip()
    if hmac.compare_digest(code, settings.admin_passcode):
        return _login_response("admin", next)
    if hmac.compare_digest(code, settings.volunteer_passcode):
        return _login_response("volunteer", "/pickup" if next.startswith("/admin") else next)
    return RedirectResponse(f"/login?next={next}&error=1", status_code=303)


@app.get("/logout")
def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(ROLE_COOKIE)
    return resp


@app.exception_handler(HTTPException)
async def _auth_redirect(request: Request, exc: HTTPException):
    if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# --------------------------------------------------------------------------- #
# AgentMail webhook
# --------------------------------------------------------------------------- #
@app.post("/webhooks/agentmail")
async def agentmail_webhook(request: Request, session: Session = Depends(get_session)) -> JSONResponse:
    body = await request.body()
    if settings.agentmail_webhook_secret:
        try:
            verify_signature(settings.agentmail_webhook_secret, body, dict(request.headers))
        except Exception as exc:
            log.warning("webhook signature rejected: %s", exc)
            raise HTTPException(status_code=400, detail="bad signature")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(status_code=400, detail="body is not JSON")
    else:
        # Allowed only for local testing; production must set the secret.
        if settings.is_production:
            raise HTTPException(status_code=500, detail="AGENTMAIL_WEBHOOK_SECRET not configured")
        payload = await request.json()
    record = handle_event(session, payload)
    return JSONResponse({"ok": True, "classification": record.classification, "detail": record.detail, "order_id": record.order_id})


# --------------------------------------------------------------------------- #
# Volunteer pickup page + API
# --------------------------------------------------------------------------- #
def _order_payload(order: Order) -> dict:
    return {
        "id": order.id,
        "code": order.pickup_code,
        "status": order.status,
        "buyer_name": order.buyer_name,
        "buyer_email": order.buyer_email,
        "items": [{"label": i.label, "size": i.size, "quantity": i.quantity} for i in order.items],
        "quantity_total": order.quantity_total,
        "quantity_picked": order.quantity_picked,
        "purchased_at": order.purchased_at.isoformat() if order.purchased_at else None,
        "pickups": [
            {"at": p.picked_up_at.isoformat(), "volunteer": p.volunteer, "presented_by": p.presented_by, "quantity": p.quantity, "note": p.note}
            for p in order.pickups
        ],
    }


@app.get("/pickup", response_class=HTMLResponse)
def pickup_page(request: Request, role: str = Depends(require_role("volunteer"))) -> HTMLResponse:
    return templates.TemplateResponse(request, "pickup.html", {"role": role, "org": settings.org_name})


@app.get("/api/orders/lookup")
def lookup(code: str = "", q: str = "", role: str = Depends(require_role("volunteer")), session: Session = Depends(get_session)) -> JSONResponse:
    if code:
        order = find_order_by_code(session, code)
        if not order:
            return JSONResponse({"found": False, "reason": "No order with that code. Check for typos, or search by name/email."}, status_code=404)
        return JSONResponse({"found": True, "order": _order_payload(order)})
    q = q.strip().lower()
    if len(q) < 3:
        raise HTTPException(status_code=400, detail="search needs at least 3 characters")
    rows = session.scalars(
        select(Order).where((func.lower(Order.buyer_email).contains(q)) | (func.lower(Order.buyer_name).contains(q))).order_by(Order.purchased_at.desc()).limit(10)
    ).all()
    return JSONResponse({"found": bool(rows), "orders": [_order_payload(o) for o in rows]})


@app.post("/api/orders/{order_id}/pickup")
def confirm_pickup(
    order_id: int,
    volunteer: str = Form(...),
    presented_by: str = Form(""),
    quantity: Optional[int] = Form(None),
    note: str = Form(""),
    role: str = Depends(require_role("volunteer")),
    session: Session = Depends(get_session),
) -> JSONResponse:
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    if order.status == "cancelled":
        raise HTTPException(status_code=409, detail="order is cancelled")
    if order.status == "needs_review":
        raise HTTPException(status_code=409, detail="order is on hold (refund request or parse problem). An officer must clear it on /admin first.")
    if order.quantity_picked >= order.quantity_total:
        last = order.pickups[-1] if order.pickups else None
        return JSONResponse(
            {"ok": False, "already": True, "message": f"Already picked up{(' on ' + last.picked_up_at.strftime('%b %d %H:%M') + ' by ' + last.volunteer) if last else ''}.", "order": _order_payload(order)},
            status_code=409,
        )
    if not volunteer.strip():
        raise HTTPException(status_code=400, detail="volunteer name required")
    pickup, complete = record_pickup(session, order, volunteer.strip(), presented_by.strip() or None, quantity, note.strip() or None)
    return JSONResponse({"ok": True, "complete": complete, "order": _order_payload(order)})


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, role: str = Depends(require_role("admin")), session: Session = Depends(get_session)) -> HTMLResponse:
    orders = session.scalars(select(Order).order_by(Order.purchased_at.desc()).limit(500)).all()
    rows, pending = unpicked_by_size(session)
    awaiting = [o for o in orders if o.status == "needs_email"]
    recent = session.scalars(select(InboundEmail).order_by(InboundEmail.received_at.desc()).limit(50)).all()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"orders": orders, "bring": rows, "pending": pending, "awaiting": awaiting, "recent": recent, "org": settings.org_name, "inbox": settings.agentmail_inbox_id},
    )


@app.post("/admin/orders/{order_id}/email")
def set_buyer_email(order_id: int, email: str = Form(...), role: str = Depends(require_role("admin")), session: Session = Depends(get_session)) -> Response:
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    email = email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="bad email")
    ok = resolve_buyer_email(session, order, email)
    return RedirectResponse(f"/admin?flash={'email-set-code-sent' if ok else 'email-set-send-failed'}", status_code=303)


@app.get("/admin/orders.csv")
def orders_csv(role: str = Depends(require_role("admin")), session: Session = Depends(get_session)) -> Response:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["order_id", "pickup_code", "status", "buyer_name", "buyer_email", "items", "qty_total", "qty_picked", "total_usd", "purchased_at", "tc_reference", "code_email_sent_at", "last_pickup_at", "last_volunteer", "notes"])
    for o in session.scalars(select(Order).order_by(Order.purchased_at)).all():
        last = o.pickups[-1] if o.pickups else None
        w.writerow([o.id, o.pickup_code, o.status, o.buyer_name, o.buyer_email, o.summary(), o.quantity_total, o.quantity_picked, f"{o.total_cents/100:.2f}", o.purchased_at.isoformat() if o.purchased_at else "", o.tc_reference or "", o.code_email_sent_at.isoformat() if o.code_email_sent_at else "", last.picked_up_at.isoformat() if last else "", last.volunteer if last else "", o.notes or ""])
    return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=merch-orders.csv"})


@app.post("/admin/orders/{order_id}/resend")
def resend_code(order_id: int, role: str = Depends(require_role("admin")), session: Session = Depends(get_session)) -> Response:
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    ok = send_code_email(session, order)
    return RedirectResponse(f"/admin?flash={'sent' if ok else 'send-failed'}", status_code=303)


@app.post("/admin/orders/{order_id}/status")
def set_status(order_id: int, status: str = Form(...), role: str = Depends(require_role("admin")), session: Session = Depends(get_session)) -> Response:
    if status not in ("pending", "needs_email", "picked_up", "needs_review", "cancelled"):
        raise HTTPException(status_code=400, detail="bad status")
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="order not found")
    order.status = status
    session.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/orders/manual")
def manual_order(
    buyer_name: str = Form(...),
    buyer_email: str = Form(...),
    size: str = Form(...),
    quantity: int = Form(1),
    send_email: str = Form("yes"),
    role: str = Depends(require_role("admin")),
    session: Session = Depends(get_session),
) -> Response:
    size = size.strip().upper()
    parsed = ParsedOrder(
        buyer_name=buyer_name.strip(),
        buyer_email=buyer_email.strip().lower(),
        items=[ParsedItem(product_name=f"ScottyLabs Found T-Shirt — Size {size}", quantity=max(quantity, 1), size=size, unit_price_cents=1000)],
        total_cents=1000 * max(quantity, 1),
        source="manual",
    )
    try:
        order = create_order(session, parsed)
    except DuplicateOrder as dup:
        return RedirectResponse(f"/admin?flash=duplicate-of-{dup.existing.pickup_code}", status_code=303)
    if send_email == "yes":
        send_code_email(session, order)
    return RedirectResponse(f"/admin?flash=created-{order.pickup_code}", status_code=303)


@app.post("/admin/reconcile")
async def reconcile(file: UploadFile = File(...), send_email: str = Form("yes"), role: str = Depends(require_role("admin")), session: Session = Depends(get_session)) -> Response:
    """Upload TartanConnect's Store Sales report (CSV). Creates orders for any purchase we never saw.

    Column names differ between CampusGroups exports, so matching is fuzzy: any
    column containing 'email', 'first'/'last'/'name', 'product'/'item', 'quantity'/'qty', 'date'.
    """
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))
    created, resolved, dupes, skipped = 0, 0, 0, 0
    for row in reader:
        cols = {k.lower(): (v or "").strip() for k, v in row.items() if k}
        email = next((v for k, v in cols.items() if "email" in k), "")
        first = next((v for k, v in cols.items() if "first" in k), "")
        last = next((v for k, v in cols.items() if "last" in k), "")
        name = (first + " " + last).strip() or next((v for k, v in cols.items() if k == "name" or "buyer" in k), "")
        product = next((v for k, v in cols.items() if "product" in k or "item" in k), "")
        qty_raw = next((v for k, v in cols.items() if "quantity" in k or k == "qty"), "1")
        date_raw = next((v for k, v in cols.items() if "date" in k), "")
        ref = next((v for k, v in cols.items() if "transaction" in k or "receipt" in k or "reference" in k or k.endswith(" id")), "")
        if not email or not product:
            skipped += 1
            continue
        from .parser import SIZE_RE

        sm = SIZE_RE.search(product)
        try:
            qty = int(float(qty_raw or "1"))
        except ValueError:
            qty = 1
        purchased_at = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                purchased_at = dt.datetime.strptime(date_raw, fmt).replace(tzinfo=dt.timezone.utc)
                break
            except ValueError:
                continue
        # 1) An order we already know (officer notification) that lacks an email: fill it in.
        match = find_order_by_reference(session, ref.lstrip("#") if ref else None)
        if match is None and name:
            candidates = session.scalars(select(Order).where(Order.status == "needs_email", func.lower(Order.buyer_name) == name.lower())).all()
            candidates = [o for o in candidates if any(product.strip().lower()[:40] == i.product_name.lower()[:40] for i in o.items)] or candidates
            if len(candidates) == 1:
                match = candidates[0]
        if match is not None:
            if not match.buyer_email:
                resolve_buyer_email(session, match, email, send=(send_email == "yes"))
                resolved += 1
            else:
                dupes += 1
            continue
        # 2) Never seen: create it.
        parsed = ParsedOrder(
            buyer_name=name or email.split("@")[0],
            buyer_email=email.lower(),
            items=[ParsedItem(product_name=product, quantity=max(qty, 1), size=sm.group(1).upper() if sm else None, unit_price_cents=1000)],
            total_cents=1000 * max(qty, 1),
            tc_reference=(ref.lstrip("#") or None) if ref else None,
            source="reconcile",
        )
        try:
            order = create_order(session, parsed, purchased_at=purchased_at)
        except DuplicateOrder:
            dupes += 1
            continue
        created += 1
        if send_email == "yes":
            send_code_email(session, order)
    return RedirectResponse(f"/admin?flash=reconciled-created-{created}-resolved-{resolved}-dupes-{dupes}-skipped-{skipped}", status_code=303)


@app.post("/admin/bring-list")
def bring_list_now(role: str = Depends(require_role("admin"))) -> PlainTextResponse:
    return PlainTextResponse(send_bring_list())


@app.post("/admin/inbound/{inbound_id}/reprocess")
def reprocess_inbound(inbound_id: int, role: str = Depends(require_role("admin")), session: Session = Depends(get_session)) -> JSONResponse:
    """Re-run classification for one inbound email (after a routing fix)."""
    from .webhook import handle_event

    rec = session.get(InboundEmail, inbound_id)
    if not rec:
        raise HTTPException(status_code=404, detail="inbound email not found")
    client = AgentMail()
    try:
        full = client.get_message(rec.message_id)
    except AgentMailError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    session.delete(rec)
    session.commit()
    payload = {"type": "event", "event_type": "message.received", "event_id": f"reprocess:{inbound_id}:{rec.message_id}", "message": full, "thread": {}}
    new = handle_event(session, payload, client)
    return JSONResponse({"ok": True, "classification": new.classification, "detail": new.detail, "order_id": new.order_id})


@app.post("/admin/poll")
def poll_now(role: str = Depends(require_role("admin"))) -> JSONResponse:
    from .poller import poll_inbox

    return JSONResponse(poll_inbox())


@app.get("/admin/agentmail")
def agentmail_status(role: str = Depends(require_role("admin"))) -> JSONResponse:
    try:
        client = AgentMail()
        return JSONResponse({"whoami": client.whoami(), "webhooks": client.list_webhooks()})
    except AgentMailError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
