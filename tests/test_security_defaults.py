"""Approval requirements apply to legacy providers; LinkedIn needs opt-in."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import yaml

from harvey.agents.handler import Handler
from harvey.agents.sender import Sender
from harvey.config import BrainConfig, EnvConfig, LinkedInChannelConfig
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect
from harvey.paths import PROJECT_ROOT
from harvey.state import StateManager
from harvey.trainer import Trainer


@pytest_asyncio.fixture
async def state(tmp_path):
    state = StateManager(str(tmp_path / "test.db"))
    await state.init_db()
    return state


def legacy_config(require_approval=True):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            enabled=True, provider="instantly", require_approval=require_approval,
            max_daily_sends=50, send_to_risky=False)),
        persona=SimpleNamespace(name="Xorora Prospector", role="BDR", company="Acme", tone="direct"),
        product=SimpleNamespace(name="Widget", description="A widget", objection_responses={}),
    )


@pytest.mark.asyncio
async def test_legacy_campaign_requires_explicit_autopilot_even_on_activation_retry(state):
    sender = Sender(None, state, legacy_config(), EnvConfig())
    sender.instantly = AsyncMock()
    campaign = Campaign(id="c", name="Draft", instantly_campaign_id="existing-campaign",
                        sequence=[EmailStep(step=1, subject="a note", body="Would this help?")])
    await sender._deploy_campaign(campaign)
    assert sender.instantly.mock_calls == []


@pytest.mark.asyncio
async def test_legacy_campaign_autopilot_is_still_available(state):
    sender = Sender(None, state, legacy_config(False), EnvConfig())
    sender.instantly = AsyncMock()
    sender.instantly.create_campaign.return_value = None
    campaign = Campaign(id="c", name="Draft",
                        sequence=[EmailStep(step=1, subject="a note", body="Would this help?")])
    await sender._deploy_campaign(campaign)
    sender.instantly.create_campaign.assert_awaited_once_with("Draft")


@pytest.mark.asyncio
@pytest.mark.parametrize("require_approval", [True, False])
async def test_legacy_replies_respect_approval_and_record_inbound_messages(state, require_approval):
    pid = await state.add_prospect(Prospect(first_name="Jane", last_name="Doe", company="Acme",
                                          title="Owner", email="jane@acme.test"))
    await state.set_setting("instantly_auto_reply", "1")  # automatic answers on
    handler = Handler(None, state, legacy_config(require_approval), EnvConfig())
    handler.instantly = AsyncMock()
    handler._classify_intent = AsyncMock(return_value="question")
    handler._generate_response = AsyncMock(return_value="Happy to help.")
    await handler._process_reply("jane@acme.test", "What does it cost?", "reply-id")
    if require_approval:
        handler.instantly.send_reply.assert_not_awaited()
        [convo] = await state.get_conversations_by_status("needs_human")
        assert convo.prospect_id == pid and convo.thread[-1].content == "What does it cost?"
    else:
        handler.instantly.send_reply.assert_awaited_once_with("reply-id", "Happy to help.")


@pytest.mark.asyncio
async def test_legacy_opt_out_is_honored_even_when_approval_blocks_sending(state):
    pid = await state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="Owner",
                                          email="jane@acme.test"))
    handler = Handler(None, state, legacy_config(), EnvConfig())
    handler.instantly = AsyncMock()
    await handler._process_reply("jane@acme.test", "Please unsubscribe me", "reply-id")
    assert (await state.get_prospect(pid)).status == "opted_out"
    handler.instantly.send_reply.assert_not_awaited()


def test_linkedin_is_disabled_in_model_and_shipped_template():
    assert LinkedInChannelConfig().enabled is False
    template = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text())
    assert template["channels"]["linkedin"]["enabled"] is False
    assert LinkedInChannelConfig(enabled=True).enabled is True


def test_product_training_does_not_enable_linkedin():
    trainer = Trainer.__new__(Trainer)
    trainer.brain_config = BrainConfig()
    config = trainer._build_config({}, {}, {}, "acme.test")
    assert config["channels"]["linkedin"]["enabled"] is False


@pytest.mark.parametrize("enabled", [False, True])
def test_setup_saves_explicit_linkedin_choice_for_existing_config(tmp_path, monkeypatch, enabled):
    from harvey import setup
    path = tmp_path / "harvey.yaml"
    path.write_text(yaml.safe_dump({"channels": {"linkedin": {"enabled": not enabled}}}))
    monkeypatch.setattr(setup, "CONFIG_FILE", path)
    wizard = setup.SetupWizard()
    wizard.env_vars["LINKEDIN_EMAIL"] = "user@example.test" if enabled else ""
    wizard.config = {"channels": {"linkedin": {"enabled": enabled}}}
    wizard._write_config()
    assert yaml.safe_load(path.read_text())["channels"]["linkedin"]["enabled"] is enabled


# ── a held Instantly setup stays visible and never stalls the pipeline ──

@pytest.mark.asyncio
async def test_held_instantly_reply_spends_no_model_call(state):
    await state.add_prospect(Prospect(first_name="Jane", last_name="Doe", company="Acme",
                                      title="Owner", email="jane@acme.test"))
    handler = Handler(None, state, legacy_config(), EnvConfig())
    handler.instantly = AsyncMock()
    handler._classify_intent = AsyncMock(return_value="interested")
    handler._generate_response = AsyncMock(return_value="Happy to help.")
    await handler._process_reply("jane@acme.test", "Sounds good, tell me more", "reply-id")
    handler._generate_response.assert_not_awaited()
    handler.instantly.send_reply.assert_not_awaited()
    assert len(await state.get_conversations_by_status("needs_human")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("new, expected", [(30, "write_campaign"), (0, "prospect")])
async def test_held_drafts_do_not_block_writing_or_prospecting(new, expected):
    # A held Instantly draft must not win "send_campaign" every cycle and
    # starve the writer and scout forever.
    from harvey.main import decide_next_action
    summary = {"prospects": {"new": new}, "draft_campaigns": 1, "open_conversations": 0}
    assert await decide_next_action(None, None, legacy_config(), summary=summary) == expected


@pytest.mark.asyncio
async def test_drafts_still_deploy_on_instantly_autopilot_and_native_providers():
    from harvey.main import decide_next_action
    summary = {"prospects": {"new": 30}, "draft_campaigns": 1, "open_conversations": 0}
    assert await decide_next_action(None, None, legacy_config(False), summary=summary) == "send_campaign"
    native = legacy_config()
    native.channels.email.provider = "gmail"
    assert await decide_next_action(None, None, native, summary=summary) == "send_campaign"


def test_held_instantly_is_announced_plainly():
    from harvey.config import legacy_sending_held, sending_notice
    assert legacy_sending_held(legacy_config()) is True
    assert legacy_sending_held(legacy_config(False)) is False
    notice = sending_notice(legacy_config())
    assert "Instantly" in notice and "require_approval: false" in notice
    assert sending_notice(legacy_config(False)) == ""
    native = legacy_config()
    native.channels.email.provider = "gmail"
    assert sending_notice(native) == ""


@pytest.mark.asyncio
async def test_training_on_a_local_url_says_why_it_was_refused():
    from harvey.trainer import FallbackCrawler
    crawler = FallbackCrawler()
    assert await crawler.crawl("http://localhost:3000", max_pages=3) == {}
    assert crawler.blocked is True


def test_status_still_works_when_the_config_is_invalid(tmp_path, monkeypatch, capsys):
    # `harvey status` is what people run when something's wrong, so an
    # invalid harvey.yaml must not crash the sending-hold check.
    import harvey.config
    import harvey.state
    from harvey.cli import cmd_status
    from harvey.config import ProspectorConfig
    from pydantic import ValidationError

    def invalid():
        ProspectorConfig(channels={"email": {"max_daily_sends": "lots"}})

    try:
        invalid()
    except ValidationError:
        pass
    real_state = harvey.state.StateManager
    monkeypatch.setattr(harvey.state, "StateManager", lambda: real_state(str(tmp_path / "s.db")))
    monkeypatch.setattr(harvey.config, "load_config", invalid)
    cmd_status(SimpleNamespace(check=False))
    assert "Prospects:" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_team_campaign_adds_every_batch_to_its_one_instantly_campaign(state):
    from harvey import team
    await team.save_requests(state, [{"id": "r1", "owner": "Ann", "name": "Cafes", "daily_limit": 30,
                                      "regions": ["Miami, FL"], "instantly_campaign_id": "inst-1",
                                      "instantly_per_lead": True, "instantly_steps": 1}])
    sender = Sender(None, state, legacy_config(False), EnvConfig())
    sender.instantly = AsyncMock()
    sender._reviewed_leads = lambda ps: ps
    pid = await state.add_prospect(Prospect(first_name="Jo", last_name="Lee", company="Cafe",
                                          title="Owner", email="jo@cafe.example",
                                          email_status="verified"))
    campaign = Campaign(id="", name=team.campaign_name("Ann", "Cafes"), prospect_ids=[pid],
                        sending_accounts=["me@send.example"],
                        sequence=[EmailStep(step=1, subject="a note", body="Would this help?")])
    campaign.id = await state.add_campaign(campaign)
    await sender._deploy_campaign(campaign)
    sender.instantly.create_campaign.assert_not_awaited()      # no second campaign
    sender.instantly.set_campaign_emails.assert_not_awaited()  # its sequence stays
    assert sender.instantly.add_leads.await_args.args[0] == "inst-1"
    lead = sender.instantly.add_leads.await_args.args[1][0]
    assert lead["variables"]["s1_body"].startswith("Would this help?")  # this batch's own copy
    assert lead["variables"]["s1_subject"] == "A note"


@pytest.mark.asyncio
async def test_a_team_campaign_sequence_reads_each_leads_own_copy(state):
    from harvey import team
    await team.save_requests(state, [{"id": "r1", "owner": "Ann", "name": "Cafes", "daily_limit": 30,
                                      "regions": ["Miami, FL"]}])
    sender = Sender(None, state, legacy_config(False), EnvConfig())
    sender.instantly = AsyncMock()
    sender.instantly.create_campaign.return_value = {"id": "new-1"}
    sender._reviewed_leads = lambda ps: ps
    pid = await state.add_prospect(Prospect(first_name="Jo", last_name="Lee", company="Cafe Uno",
                                          title="Owner", email="jo@cafe.example",
                                          email_status="verified"))
    campaign = Campaign(id="", name=team.campaign_name("Ann", "Cafes"), prospect_ids=[pid],
                        sending_accounts=["me@send.example"],
                        sequence=[EmailStep(step=1, subject="hi", body="Hi {{first_name}} at {{company}}")])
    campaign.id = await state.add_campaign(campaign)
    await sender._deploy_campaign(campaign)
    seq = sender.instantly.set_campaign_emails.await_args.args[1]
    assert seq[0]["subject"] == "{{s1_subject}}" and seq[0]["body"].startswith("{{s1_body}}\n\n{{accountSignature}}")
    lead = sender.instantly.add_leads.await_args.args[1][0]
    assert lead["variables"]["s1_body"].startswith("Hi Jo at Cafe Uno")
    [req] = await team.load_requests(state)
    assert req["instantly_campaign_id"] == "new-1" and req["instantly_per_lead"] is True


@pytest.mark.asyncio
async def test_instantly_replies_wait_for_a_person_unless_automatic_answers_are_on(state):
    pid = await state.add_prospect(Prospect(first_name="Jane", last_name="Doe", company="Acme",
                                          title="Owner", email="jane@acme.test"))
    handler = Handler(None, state, legacy_config(False), EnvConfig())
    handler.instantly = AsyncMock()
    handler._classify_intent = AsyncMock(return_value="question")
    handler._generate_response = AsyncMock(return_value="Happy to help.")
    await handler._process_reply("jane@acme.test", "What does it cost?", "reply-id")
    handler.instantly.send_reply.assert_not_awaited()
    [convo] = await state.get_conversations_by_status("needs_human")
    assert convo.prospect_id == pid


@pytest.mark.asyncio
async def test_instantly_v2_replies_are_read_as_received_emails():
    from harvey.integrations.instantly import InstantlyClient
    client = InstantlyClient.__new__(InstantlyClient)
    calls = []

    async def fake_request(method, path, body=None, params=None, **kw):
        calls.append((method, path, params, body))
        if method == "GET":
            return {"items": [{"id": "e1", "from_address_email": "jo@cafe.example",
                               "subject": "Your note", "eaccount": "me@send.example",
                               "body": {"text": "Sounds good"}}]}
        return {"id": "sent"}
    client._request = fake_request
    [r] = await client.get_replies("c1")
    assert (r["lead_email"], r["body"], r["id"]) == ("jo@cafe.example", "Sounds good", "e1")
    assert calls[0][1] == "/emails" and calls[0][2]["email_type"] == "received"
    await client.send_reply("e1", "Thanks")
    _, path, _, body = calls[-1]
    assert path == "/emails/reply" and body["eaccount"] == "me@send.example"
    assert body["subject"] == "Re: Your note" and body["reply_to_uuid"] == "e1"


@pytest.mark.asyncio
async def test_a_campaign_over_two_percent_bounces_is_stopped(state, monkeypatch):
    from harvey import dashboard as D, team
    await team.save_requests(state, [
        {"id": "r1", "owner": "Ann", "name": "A", "status": "active", "instantly_campaign_id": "bad"},
        {"id": "r2", "owner": "Bo", "name": "B", "status": "active", "instantly_campaign_id": "ok"}])
    paused = []

    class FakeInstantly:
        def __init__(self, key): pass
        async def get_campaign_analytics(self, cid):
            return {"bad": {"emails_sent_count": 100, "bounced_count": 3},
                    "ok": {"emails_sent_count": 100, "bounced_count": 2}}[cid]
        async def pause_campaign(self, cid):
            paused.append(cid)
        async def list_leads(self, cid):
            return [{"email": "x@bad.example", "status": -1, "company_name": "Bad Co"},
                    {"email": "y@bad.example", "status": 1}]
    monkeypatch.setattr(D, "_state", lambda: state)
    monkeypatch.setattr(D, "_instantly_key", lambda: "k")
    monkeypatch.setattr("harvey.integrations.instantly.InstantlyClient", FakeInstantly)
    await D._bounce_guard()
    reqs = {r["id"]: r for r in await team.load_requests(state)}
    assert paused == ["bad"]
    assert reqs["r1"]["status"] == "stopped" and "3.0%" in reqs["r1"]["stop_reason"]
    assert reqs["r2"]["status"] == "active"  # exactly 2% is still within the limit
    # The card lists which addresses bounced.
    assert reqs["r1"]["instantly_stats"]["bounced_emails"][0]["email"] == "x@bad.example"


@pytest.mark.asyncio
async def test_one_bounce_alone_never_stops_a_campaign(state, monkeypatch):
    from harvey import dashboard as D, team
    await team.save_requests(state, [{"id": "r1", "owner": "Ann", "name": "A", "status": "active",
                                      "instantly_campaign_id": "c"}])
    class FakeInstantly:
        def __init__(self, key): pass
        async def get_campaign_analytics(self, cid):
            return {"emails_sent_count": 26, "bounced_count": 1}       # 3.8%, but one address
        async def pause_campaign(self, cid):
            raise AssertionError("must not pause")
        async def list_leads(self, cid):
            return []
    monkeypatch.setattr(D, "_state", lambda: state)
    monkeypatch.setattr(D, "_instantly_key", lambda: "k")
    monkeypatch.setattr("harvey.integrations.instantly.InstantlyClient", FakeInstantly)
    await D._bounce_guard()
    assert (await team.load_requests(state))[0]["status"] == "active"


@pytest.mark.asyncio
async def test_instantly_campaigns_track_opens():
    from harvey.integrations.instantly import InstantlyClient
    client = InstantlyClient.__new__(InstantlyClient)
    sent = {}

    async def fake_request(method, path, body=None, **kw):
        sent.update(body or {})
        return {"id": "c1"}
    client._request = fake_request
    await client.create_campaign("x")
    assert sent["open_tracking"] is True


@pytest.mark.asyncio
async def test_outbox_lists_each_instantly_lead_with_their_emails(state, monkeypatch):
    from harvey import dashboard as D, team
    await team.save_requests(state, [{"id": "r1", "owner": "Ann", "name": "Cafes", "regions": ["Miami, FL"],
                                      "instantly_campaign_id": "i1"}])
    pid = await state.add_prospect(Prospect(first_name="Jo", last_name="Lee", company="Cafe Uno",
                                          title="Owner", email="jo@cafe.example", email_status="verified"))
    await state.update_prospect_status(pid, "contacted")
    c = Campaign(id="", name=team.campaign_name("Ann", "Cafes"), prospect_ids=[pid], status="active",
                 sequence=[EmailStep(step=1, subject="hi {{company}}", body="Hi {{first_name}}"),
                           EmailStep(step=2, subject="again", body="Still there?", delay_days=3)])
    cid = await state.add_campaign(c)
    await state.update_campaign(cid, status="active")
    monkeypatch.setattr(D, "_state", lambda: state)
    monkeypatch.setattr(D, "_outbox_client", lambda: None)
    out = await D.outbox_instantly()
    [camp] = out["campaigns"]
    [lead] = camp["leads"]
    assert lead["email"] == "jo@cafe.example"
    assert [(e["subject"], e["day"]) for e in lead["emails"]] == [("Hi Cafe Uno", 0), ("Again", 3)]
    assert lead["emails"][0]["body"].startswith("Hi Jo\n\n[signature of the sending mailbox]")
    assert lead["emails"][0]["sent"] is False

    # Once Instantly has sent it, the Outbox shows the email exactly as it went out.
    class Client:
        async def account_signatures(self):
            return {"me@send.example": {"text": "Ann Lee\nXorora", "html": "<b>Ann Lee</b><br>Xorora"}}
        async def get_sent_emails(self, cid):
            return [{"lead_email": "jo@cafe.example", "subject": "Hi Cafe Uno", "html": "Hi Jo,<br><br>A note.",
                     "body": "Hi Jo,\n\nA note.\n\nAnn Lee\nXorora", "eaccount": "me@send.example",
                     "sent_at": "2026-11-19T15:00:00Z"}]
    D._OUTBOX_CACHE.clear()
    monkeypatch.setattr(D, "_outbox_client", lambda: Client())
    await team.update_request(state, "r1", accounts=["me@send.example"])
    [lead] = (await D.outbox_instantly())["campaigns"][0]["leads"]
    first, second = lead["emails"]
    assert first["sent"] and first["from"] == "me@send.example" and first["body"].endswith("Ann Lee\nXorora")
    assert not second["sent"] and "Ann Lee\nXorora" in second["body"]
    assert "<b>Ann Lee</b><br>Xorora" in second["html"] and first["html"] == "Hi Jo,<br><br>A note."
    D._OUTBOX_CACHE.clear()


@pytest.mark.asyncio
async def test_every_team_prospect_gets_a_sequence_of_their_own(state, monkeypatch):
    from harvey import team
    from harvey.agents.writer import Writer
    await team.save_requests(state, [{"id": "r1", "owner": "Ann", "name": "Cafes", "status": "active",
                                      "max_leads": 10, "accounts": ["me@send.example"]}])
    pids = [await state.add_prospect(Prospect(first_name=n, last_name="Lee", company=n + " Cafe",
                                            title="Owner", email=n.lower() + "@cafe.example",
                                            email_status="verified")) for n in ("Jo", "Al", "Mo")]

    async def candidates(state_, req):
        return pids
    monkeypatch.setattr(team, "candidate_prospect_ids", candidates)
    writer = Writer.__new__(Writer)
    writer.state, writer.config = state, legacy_config(False)
    writer.config.persona.name, writer.config.product.key_benefits = "Ann", []
    writer.brain = SimpleNamespace(paused_reason="")
    seen = []

    async def write(prospects):
        seen.append([p.first_name for p in prospects])
        return [EmailStep(step=1, subject="hi", body="Hi " + prospects[0].first_name)]
    writer._write_sequence = write
    await writer._run_team_requests()
    assert seen == [["Jo"], ["Al"], ["Mo"]]          # one prospect per sequence
    drafts = await state.get_campaigns_by_status("draft")
    assert sorted(len(c.prospect_ids) for c in drafts) == [1, 1, 1]


@pytest.mark.asyncio
async def test_outbox_admins_see_the_team_members_see_their_own(state, monkeypatch):
    from harvey import dashboard as D, team
    await team.save_requests(state, [
        {"id": "r1", "owner": "Ann", "name": "A", "member_id": "m1", "instantly_campaign_id": "i1"},
        {"id": "r2", "owner": "Bo", "name": "B", "member_id": "m2", "instantly_campaign_id": "i2"}])
    monkeypatch.setattr(D, "_state", lambda: state)
    monkeypatch.setattr(D, "_outbox_client", lambda: None)
    ann = {"id": "m1", "name": "Ann", "email": "ann@x.example", "role": "member"}
    bo = {"id": "m2", "name": "Bo", "email": "bo@x.example", "role": "admin"}
    tok = D._SCOPE.set(D.Scope(ann, set(), set(), {"Ann"}))
    try:
        mine = await D.outbox_instantly()
    finally:
        D._SCOPE.reset(tok)
    assert [p["name"] for p in mine["people"]] == ["Ann"] and not mine["admin_view"]
    tok = D._SCOPE.set(D.Scope(bo, set(), set(), {"Bo"}))
    try:
        everyone = await D.outbox_instantly()
    finally:
        D._SCOPE.reset(tok)
    assert [p["name"] for p in everyone["people"]] == ["Bo", "Ann"]  # the admin first, then the team
    assert everyone["admin_view"] is True


def test_email_tidy_greets_by_name_and_keeps_paragraphs():
    from harvey.agents.writer import tidy_body, tidy_subject
    from harvey.integrations.instantly import to_html_breaks
    assert tidy_subject("order status emails") == "Order status emails"
    assert tidy_subject("{{company}} orders") == "{{company}} orders"
    body = tidy_body("Hi there, Noticed the new shop. Orders pile up. Who answers them?")
    assert body.startswith("Hi {{first_name}},\n\n")
    # The wording and line breaks stay exactly as written.
    assert body == "Hi {{first_name}},\n\nNoticed the new shop. Orders pile up. Who answers them?"
    assert tidy_body("Hello,\nA.\nB?", inbox=True) == "Hi {{company}} team,\n\nA.\nB?"
    assert to_html_breaks("Hi,\n\nA") == "<div>Hi,</div><div><br></div><div>A</div>"
    assert to_html_breaks("Hi {{firstName}},<br><br>A & B") == \
        "<div>Hi {{firstName}},</div><div><br></div><div>A &amp; B</div>"


def test_sequences_are_six_emails_signed_by_the_mailbox():
    from harvey.agents import writer, sender
    assert writer.SEQUENCE_LENGTH == 6
    assert "five follow-ups" in writer.SEQUENCE_SHAPE.replace("\n", " ") or "Email 6" in writer.SEQUENCE_SHAPE
    assert sender.ACCOUNT_SIGNATURE == "{{accountSignature}}"
    assert writer.tidy_body("A.\n\nB?\n\nThanks,\nSam") == "Hi {{first_name}},\n\nA.\n\nB?"


def test_a_shared_mailbox_sends_30_a_day_in_total_not_30_per_campaign():
    from harvey import dashboard as D
    caps = D.mailbox_caps([
        {"owner": "Ann", "accounts": ["a@send.example", "b@send.example", "c@send.example"], "daily_limit": 30},
        {"owner": "Bo", "accounts": ["A@send.example"], "daily_limit": 30},
        {"owner": "Cy", "accounts": ["d@send.example"], "daily_limit": 50},          # never above 30
        {"owner": "Di", "accounts": ["e@send.example"], "status": "stopped"}])
    assert caps == {"a@send.example": 30, "b@send.example": 30, "c@send.example": 30, "d@send.example": 30}


def test_outbox_email_html_is_cleaned_and_names_are_not_signed():
    from harvey.mailhtml import sanitize
    from harvey.agents.writer import tidy_body
    dirty = ('<p onclick="x()">Hi<script>alert(1)</script></p><a href="javascript:x">a</a>'
             '<img src="https://evil.example/x.png"><img src="data:image/png;base64,AA" width="80">'
             '<a href="https://xorora.example">site</a>')
    clean = sanitize(dirty)
    assert "script" not in clean and "onclick" not in clean and "javascript" not in clean
    assert "evil.example" not in clean and 'src="data:image/png;base64,AA"' in clean
    assert 'href="https://xorora.example"' in clean
    assert tidy_body("A.\n\nWho handles it?\n\nSadia") == "Hi {{first_name}},\n\nA.\n\nWho handles it?"
    assert tidy_body("A.\n\nWho handles it?\n\nHumayun Khan") == "Hi {{first_name}},\n\nA.\n\nWho handles it?"


@pytest.mark.asyncio
async def test_each_team_campaign_counts_only_its_own_sends_today(state):
    from harvey.agents.sender import Sender
    a = await state.add_prospect(Prospect(first_name="Jo", company="A", email="jo@a.example", email_status="verified"))
    b = await state.add_prospect(Prospect(first_name="Al", company="B", email="al@b.example", email_status="verified"))
    for pid in (a, b):
        await state.update_prospect_status(pid, "contacted")
    for name, pid in (("Ann · Cafes", a), ("Bo · Retail", b)):
        cid = await state.add_campaign(Campaign(id="", name=name, prospect_ids=[pid],
                                               sequence=[EmailStep(step=1, subject="s", body="b")]))
        await state.update_campaign(cid, status="active")
    sender = Sender.__new__(Sender)
    sender.state = state
    assert await sender._count_sends_today("Ann · Cafes") == 1      # not Bo's send too
    assert await sender._count_sends_today() == 2


def test_general_inbox_is_greeted_by_business_name_and_subjects_are_capitalised():
    from harvey.agents.sender import Sender
    from harvey.models.prospect import COMPANY_INBOX_SOURCE
    p = Prospect(first_name="", company="Cafe Uno", email="info@cafe.example", source=COMPANY_INBOX_SOURCE)
    assert Sender._render(None, "Hi {{first_name}},\n\nA note.", p) == "Hi Cafe Uno team,\n\nA note."
    named = Prospect(first_name="Jo", company="Cafe Uno", email="jo@cafe.example")
    assert Sender._render(None, "Hi {{first_name}},", named) == "Hi Jo,"
    assert Sender._cap("boosted posts and tables") == "Boosted posts and tables"


@pytest.mark.asyncio
async def test_outbox_keeps_sent_emails_when_instantly_fails_or_answers_short():
    from harvey import dashboard as D
    D._OUTBOX_CACHE.clear()
    first = [{"lead_email": "a@x.example", "sent_at": "2026-01-01T10:00", "subject": "Hi"}]
    async def ok(): return list(first)
    async def boom(): raise RuntimeError("429")
    async def short(): return []
    assert await D._cached("sent:c1", ok) == first
    D._OUTBOX_CACHE["sent:c1"] = (0, D._OUTBOX_CACHE["sent:c1"][1])      # expired
    assert await D._cached("sent:c1", boom) == first                    # failure keeps them
    D._OUTBOX_CACHE["sent:c1"] = (0, D._OUTBOX_CACHE["sent:c1"][1])
    assert await D._cached("sent:c1", short) == first                   # a short read keeps them
    D._OUTBOX_CACHE.clear()


@pytest.mark.asyncio
async def test_replies_are_read_from_every_instantly_campaign_once(state):
    from harvey.agents.handler import Handler
    for i, (status, cid) in enumerate((("active", "i1"), ("completed", "i2"), ("paused", "i1"))):
        c = await state.add_campaign(Campaign(id="", name=f"c{i}", instantly_campaign_id=cid,
                                             sequence=[EmailStep(step=1, subject="s", body="b")]))
        await state.update_campaign(c, status=status)
    h = Handler.__new__(Handler)
    h.state, h.config = state, legacy_config(False)
    h.brain = SimpleNamespace(load_skills_for_agent=lambda a: "")
    asked = []
    async def get_replies(cid):
        asked.append(cid); return []
    h.instantly = SimpleNamespace(get_replies=get_replies)
    await h.run()
    assert sorted(asked) == ["i1", "i2"]     # a completed batch is still read; i1 only once
