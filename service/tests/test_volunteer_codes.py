"""Volunteer access codes: generation, login, and immediate revocation."""
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
from app.volunteer_codes import BODY_LENGTH, PREFIX, generate_volunteer_code, normalize_volunteer_code  # noqa: E402

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
    for _ in range(200):
        code = generate_volunteer_code()
        assert code.startswith(PREFIX + "-")
        body = code.replace("-", "")[len(PREFIX):]
        assert len(body) == BODY_LENGTH


def test_normalize_variants():
    code = generate_volunteer_code()
    compact = code.replace("-", "")
    assert normalize_volunteer_code(code) == code
    assert normalize_volunteer_code(compact.lower()) == code
    assert normalize_volunteer_code(f"  {code}  ") == code
    assert normalize_volunteer_code(compact[len(PREFIX):]) == code  # body only


def test_normalize_rejects_garbage():
    assert normalize_volunteer_code("") is None
    assert normalize_volunteer_code("hello") is None
    assert normalize_volunteer_code("VOL-123") is None


# --- admin minting --------------------------------------------------------- #
def test_admin_generates_a_batch_shown_on_page(client):
    _as_admin(client)
    r = client.post("/admin/volunteers", data={"count": "5", "label": "Sat AM"}, follow_redirects=True)
    assert r.status_code == 200
    with dbmod.SessionLocal() as s:
        codes = vcodes.list_codes(s)
    assert len(codes) == 5
    assert all(c.label == "Sat AM" and c.active for c in codes)
    page = client.get("/admin/volunteers").text
    for c in codes:
        assert c.code in page


def test_count_is_clamped(client):
    _as_admin(client)
    client.post("/admin/volunteers", data={"count": "9999"})
    with dbmod.SessionLocal() as s:
        assert len(vcodes.list_codes(s)) == vcodes.MAX_BATCH


# --- volunteer login + revocation ----------------------------------------- #
def test_code_signs_in_then_revoke_cuts_off_immediately(client):
    _as_admin(client)
    client.post("/admin/volunteers", data={"count": "1", "label": "Helen"})
    with dbmod.SessionLocal() as s:
        vc = vcodes.list_codes(s)[0]
        code_str, code_id = vc.code, vc.id

    vol = TestClient(app)
    # a wrong code is refused
    bad = vol.post("/login", data={"passcode": "VOL-AAAAA-BBBBB", "next": "/pickup"}, follow_redirects=False)
    assert "error=1" in bad.headers["location"]

    # the real code grants the volunteer role
    vol.post("/login", data={"passcode": code_str, "next": "/pickup"})
    assert vol.get("/pickup").status_code == 200
    # but not admin
    assert vol.get("/admin/volunteers", headers=HTML, follow_redirects=False).status_code == 303

    with dbmod.SessionLocal() as s:
        row = s.get(dbmod.VolunteerCode, code_id)
        assert row.last_used_at is not None and row.use_count == 1

    # admin revokes -> the already-open session loses access on the next request
    client.post(f"/admin/volunteers/{code_id}/revoke")
    gone = vol.get("/pickup", headers=HTML, follow_redirects=False)
    assert gone.status_code == 303 and "/login" in gone.headers["location"]
    # and the code can no longer be used to sign in
    again = vol.post("/login", data={"passcode": code_str, "next": "/pickup"}, follow_redirects=False)
    assert "error=1" in again.headers["location"]

    # restore brings it back
    client.post(f"/admin/volunteers/{code_id}/restore")
    vol.post("/login", data={"passcode": code_str, "next": "/pickup"})
    assert vol.get("/pickup").status_code == 200


def test_shared_passcode_still_works(client):
    vol = TestClient(app)
    vol.post("/login", data={"passcode": "vol", "next": "/pickup"})
    assert vol.get("/pickup").status_code == 200


def test_volunteer_cannot_manage_codes(client):
    _as_admin(client)
    client.post("/admin/volunteers", data={"count": "2"})
    vol = TestClient(app)
    vol.post("/login", data={"passcode": "vol", "next": "/pickup"})
    # cannot view or mint
    assert vol.get("/admin/volunteers", headers=HTML, follow_redirects=False).status_code == 303
    assert vol.post("/admin/volunteers", data={"count": "3"}, follow_redirects=False).status_code == 401
    with dbmod.SessionLocal() as s:
        assert len(vcodes.list_codes(s)) == 2  # the volunteer's POST created nothing
