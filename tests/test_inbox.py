"""The Conversations inbox: every email that came back to a campaign
(replies, auto-replies, bounces), threads, replying and lead status."""

import pytest
import pytest_asyncio

from harvey.models import Prospect
from harvey.state import StateManager


def _msg(i, lead, subject, text, at, received=True, auto=False, frm=None):
    return {"id": f"e{i}", "thread_id": "t1", "campaign_id": "i1", "lead_email": lead,
            "from": frm or (lead if received else "me@send.example"), "to": "me@send.example",
            "subject": subject, "html": "", "text": text, "preview": text, "eaccount": "me@send.example",
            "at": at, "received": received, "auto": auto, "unread": True, "i_status": None}


class Client:
    def __init__(self):
        self.replies, self.interest, self.read = [], [], []

    async def list_emails(self, cid, email_type="received", max_pages=3):
        if email_type == "manual":
            return []
        return [
            _msg(1, "jo@cafe.example", "Re: Quick question", "Thanks, not for us.", "2026-11-19T10:00:00Z"),
            _msg(2, "al@diner.example", "Automatic reply: Quick question", "I'm out of the office.",
                 "2026-11-19T11:00:00Z", auto=True),
            _msg(3, "mo@grill.example", "Undelivered Mail Returned to Sender", "Address not found",
                 "2026-11-19T12:00:00Z", frm="mailer-daemon@mx.example"),
        ]

    async def get_sent_emails(self, cid):
        return [{"lead_email": "jo@cafe.example", "subject": "Quick question", "html": "Hi Jo,<br>A note.",
                 "body": "Hi Jo,\nA note.", "eaccount": "me@send.example", "sent_at": "2026-11-18T15:00:00Z"}]

    async def account_signatures(self):
        return {"me@send.example": {"text": "Ann Lee", "html": "<b>Ann Lee</b>"}}

    async def reply_to(self, email_id, eaccount, subject, text, html):
        self.replies.append((email_id, eaccount, subject, text, html))
        return {"id": "r1"}

    async def set_interest(self, lead, value, cid=""):
        self.interest.append((lead, value, cid))
        return True

    async def list_campaigns(self, limit=100):
        return [{"id": "i1", "name": "Cafes"}, {"id": "i9", "name": "Older campaign"}]

    async def mark_thread_read(self, tid):
        self.read.append(tid)
        return True


@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "test.db"))
    await s.init_db()
    return s


@pytest_asyncio.fixture
async def inbox(state, monkeypatch):
    from harvey import dashboard as D, team
    await team.save_requests(state, [{"id": "r1", "owner": "Ann", "name": "Cafes", "instantly_campaign_id": "i1"}])
    await state.add_prospect(Prospect(first_name="Jo", last_name="Lee", company="Cafe Uno",
                                      title="Owner", email="jo@cafe.example", email_status="verified"))
    client = Client()
    D._OUTBOX_CACHE.clear()
    monkeypatch.setattr(D, "_state", lambda: state)
    monkeypatch.setattr(D, "_outbox_client", lambda: client)
    yield D, client
    D._OUTBOX_CACHE.clear()


@pytest.mark.asyncio
async def test_inbox_shows_replies_auto_replies_and_bounces(inbox):
    D, _ = inbox
    out = await D.get_inbox()
    by = {t["lead_email"]: t for t in out["threads"]}
    assert by["jo@cafe.example"]["kind"] == "reply" and by["jo@cafe.example"]["name"] == "Jo Lee"
    assert by["al@diner.example"]["kind"] == "auto" and by["al@diner.example"]["status"] == "out_of_office"
    assert by["mo@grill.example"]["kind"] == "bounce"
    assert [t["lead_email"] for t in out["threads"]][0] == "mo@grill.example"   # newest first


@pytest.mark.asyncio
async def test_thread_holds_the_cold_email_and_the_reply_in_order(inbox):
    D, client = inbox
    out = await D.get_inbox_thread("i1", "jo@cafe.example")
    assert [m["received"] for m in out["messages"]] == [False, True]
    assert out["messages"][0]["campaign_email"] and "A note." in out["messages"][0]["html"]
    assert "not for us" in out["messages"][1]["html"]


@pytest.mark.asyncio
async def test_team_members_only_reach_their_own_campaigns(inbox):
    from types import SimpleNamespace
    D, _ = inbox
    token = D._SCOPE.set(SimpleNamespace(member={"id": "m2", "name": "Bo", "email": "bo@send.example",
                                                 "role": "member"}))
    try:
        assert (await D.get_inbox_thread("i1", "jo@cafe.example")).status_code == 404
        assert (await D.get_inbox())["threads"] == []
    finally:
        D._SCOPE.reset(token)


class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


@pytest.mark.asyncio
async def test_reply_goes_out_in_thread_from_the_mailbox_with_its_signature(inbox):
    D, client = inbox
    out = await D.inbox_reply(_Req({"campaign": "i1", "lead": "jo@cafe.example", "text": "Understood, thanks Jo."}))
    assert out["success"]
    [(eid, acct, subject, text, html)] = client.replies
    assert eid == "e1" and acct == "me@send.example" and subject == "Re: Quick question"
    assert "<b>Ann Lee</b>" in html and text.startswith("Understood, thanks Jo.\n\nAnn Lee")


@pytest.mark.asyncio
async def test_status_is_kept_and_sent_to_instantly(inbox):
    D, client = inbox
    out = await D.inbox_status(_Req({"campaign": "i1", "lead": "jo@cafe.example", "status": "not_interested"}))
    assert out["success"] and client.interest == [("jo@cafe.example", -1, "i1")]
    by = {t["lead_email"]: t for t in (await D.get_inbox())["threads"]}
    assert by["jo@cafe.example"]["status"] == "not_interested"
    bad = await D.inbox_status(_Req({"campaign": "i1", "lead": "jo@cafe.example", "status": "nope"}))
    assert bad.status_code == 400


def test_instantly_item_reads_lead_and_kind():
    from harvey.integrations.instantly import InstantlyClient
    m = InstantlyClient._inbox_item({"id": "x", "ue_type": 2, "from_address_email": "Jo@Cafe.example",
                                     "lead": "jo@cafe.example", "is_auto_reply": True,
                                     "body": {"html": "<div>Away</div>"}, "subject": "Away"}, "i1")
    assert m["received"] and m["auto"] and m["lead_email"] == "jo@cafe.example" and m["text"] == "Away"


@pytest.mark.asyncio
async def test_admins_see_replies_from_every_instantly_campaign(inbox, monkeypatch):
    D, client = inbox
    orig = client.list_emails

    async def more(cid, email_type="received", max_pages=3):
        got = await orig(cid, email_type, max_pages)
        if email_type == "received" and cid is None:
            got.append(dict(_msg(9, "kim@shop.example", "Re: hello", "Sounds good", "2026-11-19T13:00:00Z"),
                            campaign_id="i9"))
        return got
    client.list_emails = more
    by = {t["lead_email"]: t for t in (await D.get_inbox())["threads"]}
    assert by["kim@shop.example"]["campaign"] == "Older campaign" and not by["kim@shop.example"]["prospector"]


class HR:
    sent = []

    async def conversations(self, pages=3):
        return [
            {"id": "c1", "account_id": 7, "account_email": "ann@send.example", "account_name": "Ann Lee",
             "campaign_id": 3, "read": False, "last_at": "2026-11-20T10:00:00Z", "preview": "Sure, let's talk",
             "last_from_me": False, "name": "Jo Lee", "headline": "Owner", "company": "Cafe Uno",
             "profile_url": "https://www.linkedin.com/in/example", "image": "",
             "messages": [{"at": "2026-11-19T10:00:00Z", "text": "Hi Jo", "subject": "", "mine": True, "inmail": False},
                          {"at": "2026-11-20T10:00:00Z", "text": "Sure, let's talk", "subject": "", "mine": False,
                           "inmail": False}]},
            {"id": "c2", "account_id": 7, "account_email": "ann@send.example", "account_name": "Ann Lee",
             "campaign_id": 3, "read": True, "last_at": "2026-11-18T10:00:00Z", "preview": "Hi Al",
             "last_from_me": True, "name": "Al Roe", "headline": "", "company": "", "profile_url": "", "image": "",
             "messages": [{"at": "2026-11-18T10:00:00Z", "text": "Hi Al", "subject": "", "mine": True, "inmail": False}]},
        ]

    async def campaigns(self):
        return {3: "Cafes LinkedIn"}

    async def send(self, account_id, conversation_id, message):
        self.sent.append((account_id, conversation_id, message))


@pytest.mark.asyncio
async def test_linkedin_tab_shows_replies_and_answers_in_thread(inbox, monkeypatch):
    D, _ = inbox
    hr = HR()
    monkeypatch.setattr(D, "_heyreach_client", lambda: hr)
    out = await D.get_linkedin_inbox()
    assert [t["name"] for t in out["threads"]] == ["Jo Lee"]          # only where they wrote back
    t = out["threads"][0]
    assert t["campaign"] == "Cafes LinkedIn" and t["unread"] and t["key"] == "7|c1"
    thread = await D.get_linkedin_thread("7", "c1")
    assert [m["mine"] for m in thread["messages"]] == [True, False]
    assert (await D.get_linkedin_thread("7", "nope")).status_code == 404
    r = await D.linkedin_reply(_Req({"account": "7", "id": "c1", "text": "Great, Tuesday?"}))
    assert r["success"] and hr.sent == [(7, "c1", "Great, Tuesday?")]


@pytest.mark.asyncio
async def test_linkedin_tab_asks_for_a_key_without_one(inbox, monkeypatch):
    D, _ = inbox
    monkeypatch.setattr(D, "_heyreach_client", lambda: None)
    assert (await D.get_linkedin_inbox())["connected"] is False


@pytest.mark.asyncio
async def test_calls_are_logged_and_listed(inbox):
    D, _ = inbox
    bad = await D.log_call(_Req({"name": "Jo", "outcome": "nope"}))
    assert bad.status_code == 400
    ok = await D.log_call(_Req({"name": "Jo Lee", "company": "Cafe Uno", "outcome": "interested",
                                "notes": "Wants a call Tuesday"}))
    assert ok["success"]
    calls = (await D.get_calls())["calls"]
    assert calls[0]["name"] == "Jo Lee" and calls[0]["outcome"] == "interested"


def test_heyreach_conversation_is_read_into_a_thread():
    from harvey.integrations.heyreach import HeyReachClient
    c = HeyReachClient.conversation({
        "id": "c1", "linkedInAccountId": 7, "read": False, "lastMessageAt": "2026-11-20T10:00:00Z",
        "lastMessageText": "Yes", "lastMessageSender": "CORRESPONDENT", "campaignId": 3,
        "correspondentProfile": {"firstName": "Jo", "lastName": "Lee", "companyName": "Cafe Uno"},
        "linkedInAccount": {"emailAddress": "Ann@Send.example", "firstName": "Ann"},
        "messages": [{"createdAt": "2026-11-20T10:00:00Z", "body": "Yes", "sender": "CORRESPONDENT"},
                     {"createdAt": "2026-11-19T10:00:00Z", "body": "Hi Jo", "sender": "ME"}]})
    assert c["name"] == "Jo Lee" and c["account_email"] == "ann@send.example" and not c["last_from_me"]
    assert [m["mine"] for m in c["messages"]] == [True, False]
