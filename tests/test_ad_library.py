"""'Running ads' is written only when an ad library shows the ads.

A tag or pixel on the homepage proves nothing, so the profile stage no
longer turns one into RUNNING_GOOGLE_ADS / RUNNING_META_ADS. The ad-library
check (Google Ads Transparency Center, Meta Ad Library, read through
SerpApi) writes them, with a link to the library as proof."""

import asyncio
import time

import httpx
import pytest

from harvey.integrations import ad_library
from harvey.integrations.ad_library import AdLibrary, check


class FakeState:
    def __init__(self):
        self.settings = {}

    async def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    async def set_setting(self, key, value):
        self.settings[key] = value


def _client(google: dict, meta: dict, seen: list):
    def handler(request: httpx.Request):
        params = dict(request.url.params)
        seen.append(params)
        body = google if params["engine"] == "google_ads_transparency_center" else meta
        return httpx.Response(200, json=body)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(lib, wants=("RUNNING_GOOGLE_ADS", "RUNNING_META_ADS"), name="Example Grill House"):
    return asyncio.run(check(lib, "c-1", "examplegrill.example.com", name, lambda c: c in wants))


NOW = time.time()


def test_live_ads_in_both_libraries_are_recorded_with_proof():
    seen = []
    google = {"ad_creatives": [
        {"advertiser": "Example Grill LLC", "target_domain": "examplegrill.example.com", "last_shown": NOW - 86400},
        {"advertiser": "Example Grill LLC", "target_domain": "www.examplegrill.example.com", "last_shown": NOW - 3 * 86400},
    ]}
    meta = {"ads": [{"is_active": True, "page_id": "123", "page_name": "Example Grill House",
                     "snapshot": {"link_url": "https://examplegrill.example.com/menu"}}]}
    lib = AdLibrary("k", state=FakeState(), client=_client(google, meta, seen))
    obs = {o["signal_code"]: o for o in _run(lib)}
    g, m = obs["RUNNING_GOOGLE_ADS"], obs["RUNNING_META_ADS"]
    assert g["value_num"] == 1 and "2 ad(s)" in g["value_text"] and g["collector"] == "ad_library"
    assert g["evidence_url"].startswith("https://adstransparency.google.com/")
    assert m["value_num"] == 1 and "view_all_page_id=123" in m["evidence_url"]
    assert seen[0]["text"] == "examplegrill.example.com" and seen[1]["q"] == "Example Grill House"
    assert seen[1]["active_status"] == "active"


def test_other_advertisers_and_old_ads_do_not_count():
    google = {"ad_creatives": [
        {"advertiser": "Someone Else", "target_domain": "other.example.org", "last_shown": NOW},
        {"advertiser": "Example Grill LLC", "target_domain": "examplegrill.example.com",
         "last_shown": NOW - 90 * 86400},                     # shown three months ago: not running now
    ]}
    meta = {"ads": [
        {"is_active": True, "page_name": "Example Grill House Of Another Town",
         "snapshot": {"link_url": "https://another.example.net"}},
        {"is_active": False, "page_name": "Example Grill House",
         "snapshot": {"link_url": "https://examplegrill.example.com"}},
    ]}
    lib = AdLibrary("k", state=FakeState(), client=_client(google, meta, []))
    obs = {o["signal_code"]: o for o in _run(lib)}
    assert obs["RUNNING_GOOGLE_ADS"]["value_num"] == 0
    assert obs["RUNNING_META_ADS"]["value_num"] == 0


def test_a_one_word_name_match_alone_is_not_proof():
    meta = {"ads": [{"is_active": True, "page_name": "Grill", "snapshot": {"link_url": ""}}]}
    lib = AdLibrary("k", state=FakeState(), client=_client({}, meta, []))
    obs = {o["signal_code"]: o for o in _run(lib, wants=("RUNNING_META_ADS",), name="Grill")}
    assert obs["RUNNING_META_ADS"]["value_num"] == 0


def test_no_key_or_failed_check_writes_nothing():
    assert _run(AdLibrary("", state=FakeState())) == []

    def broken(request):
        return httpx.Response(500, json={"error": "Something broke"})
    lib = AdLibrary("k", state=FakeState(), client=httpx.AsyncClient(transport=httpx.MockTransport(broken)))
    assert _run(lib) == []


def test_monthly_cap_stops_the_searches():
    state = FakeState()
    seen = []
    lib = AdLibrary("k", state=state, monthly=1, client=_client({}, {}, seen))
    obs = _run(lib)
    assert len(seen) == 1 and [o["signal_code"] for o in obs] == ["RUNNING_GOOGLE_ADS"]
    assert not lib.ready


def test_serpapi_out_of_searches_stops_further_checks():
    def out(request):
        return httpx.Response(429, json={"error": "Your account has run out of searches."})
    lib = AdLibrary("k", state=FakeState(), client=httpx.AsyncClient(transport=httpx.MockTransport(out)))
    assert _run(lib) == [] and lib.exhausted


@pytest.mark.parametrize("html", [
    '<script async src="https://www.googletagmanager.com/gtag/js?id=AW-123"></script>',
    "<script>fbq('init', '1');</script>",
])
def test_a_tag_on_the_site_no_longer_counts_as_running_ads(tmp_path, html):
    from harvey.collectors.profile import ProfileCollector
    from harvey.state import StateManager

    async def go():
        state = StateManager(db_path=str(tmp_path / "t.db"))
        await state.init_db()
        pc = ProfileCollector(state, confirmed={"RUNNING_GOOGLE_ADS", "RUNNING_META_ADS"})

        async def fake_resolve(domain):
            return "https://examplegrill.example.com", "<html>" + html + "</html>"
        pc._resolve_site = fake_resolve
        return await pc.profile_company("c-1", "examplegrill.example.com", "Example Grill")
    obs = asyncio.run(go())
    assert not [o for o in obs if o["signal_code"].startswith("RUNNING_")]


def test_old_tag_based_ads_rows_are_removed_on_upgrade(tmp_path):
    import aiosqlite
    from harvey import state as state_mod
    from harvey.state import StateManager

    async def go():
        path = str(tmp_path / "old.db")
        old = StateManager(db_path=path)
        # Build the database as it was before this upgrade.
        saved = state_mod.MIGRATIONS
        state_mod.MIGRATIONS = saved[:-1]
        try:
            await old.init_db()
        finally:
            state_mod.MIGRATIONS = saved
        from harvey.signals import seed_signal_catalog
        await seed_signal_catalog(old)
        await old.add_observation("RUNNING_GOOGLE_ADS", company_id="c-1", collector="profile", value_num=1)
        await old.add_observation("RUNNING_META_ADS", company_id="c-1", collector="ad_library", value_num=1)
        await StateManager(db_path=path).init_db()
        async with aiosqlite.connect(path) as db:
            async with db.execute("SELECT signal_code, collector FROM observations") as cur:
                return await cur.fetchall()
    assert asyncio.run(go()) == [("RUNNING_META_ADS", "ad_library")]


def test_library_links():
    assert ad_library.google_library_url("a.example.com").endswith("domain=a.example.com")
    assert "q=Example%20Grill" in ad_library.meta_library_url("Example Grill")


def test_writer_gets_only_verified_ads_as_facts(tmp_path):
    from harvey.agents.writer import Writer
    from harvey.signals import seed_signal_catalog
    from harvey.state import StateManager

    async def go():
        state = StateManager(db_path=str(tmp_path / "w.db"))
        await state.init_db()
        await seed_signal_catalog(state)
        await state.add_observation("RUNNING_META_ADS", company_id="c-1", collector="ad_library",
                                    value_num=1, value_text="2 active ad(s) in Meta Ad Library")
        await state.add_observation("RUNNING_GOOGLE_ADS", company_id="c-1", collector="ad_library",
                                    value_num=0)
        w = Writer.__new__(Writer)
        w.state = state
        return await w._verified_ads("c-1")
    assert asyncio.run(go()) == ["- Verified in the ad library: 2 active ad(s) in Meta Ad Library"]


@pytest.mark.parametrize("html", ['<a href="https://www.opentable.com/r/example-grill">Book</a>',
                                  '<a href="https://resy.com/cities/mia/example-grill">Reserve</a>',
                                  '<a href="/reservations">Reservations</a>'])
def test_restaurant_reservations_count_as_online_booking(tmp_path, html):
    from harvey.collectors.profile import ProfileCollector
    from harvey.state import StateManager

    async def go():
        state = StateManager(db_path=str(tmp_path / "b.db"))
        await state.init_db()
        pc = ProfileCollector(state, confirmed={"NO_ONLINE_BOOKING"})

        async def fake_resolve(domain):
            return "https://examplegrill.example.com", "<html>" + html + "</html>"
        pc._resolve_site = fake_resolve
        return await pc.profile_company("c-1", "examplegrill.example.com", "Example Grill")
    obs = asyncio.run(go())
    assert [o["value_num"] for o in obs if o["signal_code"] == "NO_ONLINE_BOOKING"] == [0]


def test_site_audit_finds_checkable_seo_and_conversion_problems():
    from harvey.collectors import site_audit
    html = ("<html><head><title>Home</title></head><body>"
            + "".join(f'<img src="/{i}.jpg">' for i in range(4))
            + "<h1>A</h1><h1>B</h1><a href='/menu'>Menu</a></body></html>")
    seo = site_audit.seo_problems(html, "http://examplegrill.example.com")
    assert any("too short" in p for p in seo) and any("meta description" in p for p in seo)
    assert any("2 H1" in p for p in seo) and any("4 of 4 images" in p for p in seo)
    assert any("viewport" in p for p in seo) and any("https" in p for p in seo)
    conv = site_audit.conversion_problems(html)
    assert len(conv) == 3
    good = ('<html><head><title>Example Grill, Cuban food in Example City</title>'
            '<meta name="description" content="Family restaurant.">'
            '<meta name="viewport" content="width=device-width"><link rel="canonical" href="/">'
            '</head><body><h1>Example Grill</h1><a href="tel:+15550000000">Call</a>'
            '<form></form><a href="/book">Book a table</a></body></html>')
    assert site_audit.seo_problems(good, "https://examplegrill.example.com") == []
    assert site_audit.conversion_problems(good) == []


def test_pagespeed_reads_lab_and_field_data():
    from harvey.collectors import site_audit
    body = {"lighthouseResult": {"categories": {"performance": {"score": 0.41}, "seo": {"score": 0.83}}},
            "loadingExperience": {"overall_category": "SLOW",
                                  "metrics": {"LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 4120}}}}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    ps = asyncio.run(site_audit.pagespeed(client, "https://examplegrill.example.com"))
    assert ps == {"score": 41, "seo": 83, "field": "SLOW", "lcp_s": 4.1}
    text = site_audit.speed_text(ps)
    assert "41/100" in text and "poor" in text and "4.1s" in text
    assert site_audit.pagespeed_url("https://a.example.com/").endswith("https%3A%2F%2Fa.example.com%2F")
    bad = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, json={})))
    assert asyncio.run(site_audit.pagespeed(bad, "https://a.example.com")) is None
