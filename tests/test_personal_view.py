"""Each signed-in person sees only their own campaigns' data; the Team
campaigns panel is the one shared view."""

import asyncio
import json

from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import members, team
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.test_dashboard_auth import ORIGIN, env  # noqa: F401  (fixture)


def _seed():
    st = StateManager(db_path=str(dash.DB_PATH))

    async def go():
        await st.init_db()
        a, a_tok = await members.signup(st, name="Ana Admin", email="ana@example.com",
                                        password="ana-pass-1", role="admin")
        b, b_tok = await members.signup(st, name="Ben Member", email="ben@example.com", password="ben-pass-1")
        mine = await st.add_company(Company(name="Ana Cafe", domain="ana-example.com"))
        his = await st.add_company(Company(name="Ben Bakery", domain="ben-example.com"))
        loose = await st.add_company(Company(name="Nobody Diner", domain="nobody-example.com"))
        await st.add_prospect(Prospect(first_name="A", last_name="One", company_id=mine, email="a@ana-example.com"))
        await st.add_prospect(Prospect(first_name="B", last_name="Two", company_id=his, email="b@ben-example.com"))
        await team.save_requests(st, [
            {"id": "r1", "member_id": a["id"], "owner": "Ana Admin", "company_ids": [mine], "status": "live"},
            {"id": "r2", "member_id": b["id"], "owner": "Ben Member", "company_ids": [his], "status": "live"}])
        return a_tok, b_tok, loose
    return asyncio.run(go())


def _client(token, session=None):
    h = {"Authorization": f"Bearer {token}"}
    if session:
        h["X-Member-Session"] = session
    return TestClient(dash.app, base_url=ORIGIN, headers=h)


def test_each_person_sees_only_their_own(env):
    a_tok, b_tok, _ = _seed()
    with _client(env["token"], a_tok) as ana, _client(env["token"], b_tok) as ben:
        assert [c["name"] for c in ana.get("/api/companies").json()] == ["Ana Cafe"]   # admins too
        assert [c["name"] for c in ben.get("/api/companies").json()] == ["Ben Bakery"]
        assert [p["email"] for p in ben.get("/api/prospects").json()] == ["b@ben-example.com"]
        assert ben.get("/api/today").json()["stats"]["companies"] == 1
        assert ben.get("/api/today").json()["stats"]["prospects"] == 1
        his = ben.get("/api/companies").json()[0]["id"]
        assert ana.get(f"/api/companies/{his}/contacts").json() == []
        assert ana.get(f"/api/companies/{his}/profile").status_code == 404
        steps = {s["label"]: s for s in ben.get("/api/live-status").json()["steps"]}
        assert steps["Find businesses"]["done"] == 1
        assert "b@ben-example.com" not in ana.get("/api/export/prospects.csv?all=true").text
        # The shared view: everyone's campaigns, with progress.
        assert {c["id"] for c in ben.get("/api/team").json()["campaigns"]} == {"r1", "r2"}


def test_no_session_no_data_once_the_team_has_accounts(env):
    _seed()
    with _client(env["token"]) as raw:
        r = raw.get("/api/companies")
        assert r.status_code == 401 and r.json()["error"] == "signed_out"


def test_single_user_install_sees_everything(env):
    st = StateManager(db_path=str(dash.DB_PATH))

    async def go():
        await st.init_db()
        await st.add_company(Company(name="Solo Shop", domain="solo-example.com"))
    asyncio.run(go())
    with _client(env["token"]) as raw:
        assert [c["name"] for c in raw.get("/api/companies").json()] == ["Solo Shop"]
