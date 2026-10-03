"""Access codes: generation, volunteer + admin login, and immediate revocation."""
import os

os.environ["DATABASE_URL"] = "sqlite:///./test_merch.db"
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["AGENTMAIL_WEBHOOK_SECRET"] = ""
os.environ["VOLUNTEER_PASSCODE"] = "vol"
os.environ["ADMIN_PASSCODE"] = "adm"
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["AGENTMAIL_API_KEY"] = ""
os.environ["SCOTTYLABS_MERCH_AGENTMAIL_API_TOKEN"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod  # noqa: E402
from app.main import app  # noqa: E402
from app import volunteer_codes as vcodes  # noqa: E402
from app.volunteer_codes import BODY_LENGTH, PREFIXES, generate_code, normalize_code  # noqa: E402

HTML = {"accept": "text/html"}  # make the 401 -> /login redirect fire, like a browser


@pytest.fixture()
def client():
    dbmod.engine.dispose()
    dbmod.Base.metadata.drop_all(dbmod.engine)
    dbmod.Base.metadata.create_all(dbmod.engine)
    return TestClient(app)


def _as_admin(c: TestClient) -> None:
    c.post("/login", data={"passcode": "adm", "next": "/admin"})


# --- pure helpers ---------------------------------------------------------- #
def test_generate_shape():
    for role, prefix in PREFIXES.items():
        for _ in range(100):
            code = generate_code(role)
            assert code.startswith(prefix + "-")
            body = code.replace("-", "")[len(prefix):]
            assert len(body) == BODY_LENGTH


def test_normalize_variants():
    for role in PREFIXES:
        code = generate_code(role)
        compact = code.replace("-", "")
        assert normalize_code(code) == code
        assert normalize_code(compact.lower()) == code
        assert normalize_code(f"  {code}  ") == code


def test_normalize_rejects_garbage_and_bare_body():
    assert normalize_code("") is None
    assert normalize_code("hello") is None
    assert normalize_code("VOL-123") is None
    # a bare 10-char body (no role prefix) is ambiguous between roles -> rejected
    body = generate_code("volunteer").replace("-", "")[3:]
    assert normalize_code(body) is None


# --- admin minting --------------------------------------------------------- #
def test_admin_generates_volunteer_and_admin_batches(client):
    _as_admin(client)
    client.post("/admin/volunteers", data={"count": "5", "label": "Sat AM"})  # defaults to volunteer
    client.post("/admin/volunteers", data={"count": "1", "label": "Parsmi", "code_role": "admin"})
    with dbmod.SessionLocal() as s:
        codes = vcodes.list_codes(s)
    roles = sorted(c.role for c in codes)
    assert roles == ["admin"] + ["volunteer"] * 5
    admin_code = next(c for c in codes if c.role == "admin")
    assert admin_code.code.startswith("ADM-") and admin_code.label == "Parsmi"
    page = client.get("/admin/volunteers").text
    for c in codes:
        assert c.code in page


# --- volunteer login + revocation ----------------------------------------- #
def test_volunteer_code_signs_in_then_revoke_cuts_off(client):
    _as_admin(client)
    client.post("/admin/volunteers", data={"count": "1", "label": "Helen"})
    with dbmod.SessionLocal() as s:
        vc = vcodes.list_codes(s)[0]
        code_str, code_id = vc.code, vc.id

    vol = TestClient(app)
    vol.post("/login", data={"passcode": code_str, "next": "/pickup"})
    assert vol.get("/pickup").status_code == 200
    # a volunteer code does NOT reach admin
    assert vol.get("/admin/volunteers", headers=HTML, follow_redirects=False).status_code == 303

    client.post(f"/admin/volunteers/{code_id}/revoke")
    gone = vol.get("/pickup", headers=HTML, follow_redirects=False)
    assert gone.status_code == 303 and "/login" in gone.headers["location"]
    again = vol.post("/login", data={"passcode": code_str, "next": "/pickup"}, follow_redirects=False)
    assert "error=1" in again.headers["location"]


# --- admin login + revocation --------------------------------------------- #
def test_admin_code_grants_admin_and_revoke_cuts_off(client):
    _as_admin(client)
    client.post("/admin/volunteers", data={"count": "1", "label": "Parsmi Rajput (prajput@andrew.cmu.edu)", "code_role": "admin"})
    with dbmod.SessionLocal() as s:
        vc = next(c for c in vcodes.list_codes(s) if c.role == "admin")
        code_str, code_id = vc.code, vc.id

    parsmi = TestClient(app)
    parsmi.post("/login", data={"passcode": code_str, "next": "/admin"})
    # reaches the full admin area, including the codes manager and order list
    assert parsmi.get("/admin").status_code == 200
    assert parsmi.get("/admin/volunteers").status_code == 200
    with dbmod.SessionLocal() as s:
        assert s.get(dbmod.VolunteerCode, code_id).use_count == 1

    # revoking the admin code ends the open admin session immediately
    client.post(f"/admin/volunteers/{code_id}/revoke")
    gone = parsmi.get("/admin", headers=HTML, follow_redirects=False)
    assert gone.status_code == 303 and "/login" in gone.headers["location"]
    again = parsmi.post("/login", data={"passcode": code_str, "next": "/admin"}, follow_redirects=False)
    assert "error=1" in again.headers["location"]

    # the master ADMIN_PASSCODE is unaffected
    client.post(f"/admin/volunteers/{code_id}/restore")
    parsmi.post("/login", data={"passcode": code_str, "next": "/admin"})
    assert parsmi.get("/admin").status_code == 200


def test_shared_passcodes_still_work(client):
    v = TestClient(app)
    v.post("/login", data={"passcode": "vol", "next": "/pickup"})
    assert v.get("/pickup").status_code == 200
    a = TestClient(app)
    a.post("/login", data={"passcode": "adm", "next": "/admin"})
    assert a.get("/admin").status_code == 200


def test_volunteer_cannot_manage_or_mint_codes(client):
    vol = TestClient(app)
    vol.post("/login", data={"passcode": "vol", "next": "/pickup"})
    assert vol.get("/admin/volunteers", headers=HTML, follow_redirects=False).status_code == 303
    assert vol.post("/admin/volunteers", data={"count": "3", "code_role": "admin"}, follow_redirects=False).status_code == 401
    with dbmod.SessionLocal() as s:
        assert len(vcodes.list_codes(s)) == 0  # nothing minted by the volunteer
