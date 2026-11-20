"""Proof that a business is running ads, from the public ad libraries.

An ad tag or pixel on a website only says someone once installed it; it
doesn't say a single ad is running. So "running Google Ads" and "running
Meta ads" are recorded only after the ad libraries themselves show live
ads for this business:

- Google Ads Transparency Center, searched by the business's domain. An ad
  counts only when its target domain is the business's own domain and it
  was shown in the last 30 days.
- Meta Ad Library, searched by the business's name. An ad counts only when
  it is active and either links to the business's own domain or runs from a
  Page whose name is exactly the business's name.

Both are read through SerpApi (one key, 250 free searches a month, two
searches per business). Without a key nothing is checked and no ads signal
is written at all: an unverified "running ads" is worse than none.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import httpx

logger = logging.getLogger("harvey.ad_library")

SERPAPI = "https://serpapi.com/search.json"
RECENT_DAYS = 30
USAGE_KEY = "serpapi_searches:"          # + YYYY-MM, a monthly counter in settings
DEFAULT_MONTHLY = 240                    # SerpApi's free plan is 250; keep a margin


class OutOfSearches(Exception):
    """The month's searches are used up (our cap, or SerpApi said so)."""


def _host(url: str) -> str:
    try:
        host = urlparse(url if "//" in url else "https://" + url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def _same_site(url: str, domain: str) -> bool:
    host = _host(url)
    domain = domain.lower().removeprefix("www.")
    return bool(host) and (host == domain or host.endswith("." + domain))


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def google_library_url(domain: str) -> str:
    return "https://adstransparency.google.com/?region=anywhere&domain=" + quote(domain)


def meta_library_url(name: str, page_id: str = "") -> str:
    if page_id:
        return ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
                f"&country=ALL&view_all_page_id={quote(page_id)}&search_type=page")
    return ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
            f"&country=ALL&q={quote(name)}&search_type=keyword_unordered")


class AdLibrary:
    """Checks one business at a time against both ad libraries."""

    def __init__(self, api_key: str, state=None, monthly: int = DEFAULT_MONTHLY,
                 client: httpx.AsyncClient | None = None):
        self.api_key = (api_key or "").strip()
        self.state = state
        self.monthly = max(0, int(monthly or 0))
        self._client = client
        self.exhausted = False

    @property
    def ready(self) -> bool:
        return bool(self.api_key) and not self.exhausted

    async def _spend(self):
        """Count one search against the month's cap, or refuse."""
        if not self.state:
            return
        key = USAGE_KEY + datetime.now(timezone.utc).strftime("%Y-%m")
        used = int(await self.state.get_setting(key, "0") or 0)
        if used >= self.monthly:
            self.exhausted = True
            raise OutOfSearches(f"monthly cap of {self.monthly} ad-library searches reached")
        await self.state.set_setting(key, str(used + 1))

    async def _search(self, params: dict) -> dict:
        if not self.ready:
            raise OutOfSearches("no SerpApi key or searches left")
        await self._spend()
        client = self._client or httpx.AsyncClient(timeout=30)
        try:
            r = await client.get(SERPAPI, params={**params, "api_key": self.api_key})
            body = r.json() if r.content else {}
        finally:
            if client is not self._client:
                await client.aclose()
        err = str((body or {}).get("error") or "")
        if err:
            low = err.lower()
            if "run out" in low or ("searches" in low and "limit" in low) or r.status_code == 429:
                self.exhausted = True
                raise OutOfSearches(err)
            if "hasn't returned any results" in low or "no results" in low:
                return {}
            raise RuntimeError(err)
        if r.status_code >= 400:
            raise RuntimeError(f"SerpApi HTTP {r.status_code}")
        return body or {}

    async def google(self, domain: str) -> dict:
        """{"count": live ads for the domain, "url": the library page}."""
        body = await self._search({"engine": "google_ads_transparency_center", "text": domain})
        cutoff = time.time() - RECENT_DAYS * 86400
        live = []
        for ad in body.get("ad_creatives") or []:
            if not _same_site(str(ad.get("target_domain") or ""), domain):
                continue
            last = ad.get("last_shown")
            if isinstance(last, (int, float)) and last >= cutoff:
                live.append(ad)
        advertiser = next((a.get("advertiser") for a in live if a.get("advertiser")), "")
        return {"count": len(live), "advertiser": advertiser, "url": google_library_url(domain)}

    async def meta(self, domain: str, name: str) -> dict:
        """{"count": active ads that are this business's own, "page": ..., "url": ...}."""
        if not _norm_name(name):
            return {"count": 0, "page": "", "url": meta_library_url(domain)}
        body = await self._search({"engine": "meta_ad_library", "q": name,
                                   "active_status": "active", "ad_type": "all", "country": "ALL"})
        want = _norm_name(name)
        mine, page, page_id = [], "", ""
        for ad in body.get("ads") or []:
            if ad.get("is_active") is False:
                continue
            snap = ad.get("snapshot") or {}
            links = [snap.get("link_url") or ""] + [c.get("link_url") or "" for c in snap.get("cards") or []]
            pname = ad.get("page_name") or snap.get("page_name") or ""
            by_link = any(_same_site(u, domain) for u in links if u)
            by_name = len(want.split()) >= 2 and _norm_name(pname) == want
            if by_link or by_name:
                mine.append(ad)
                page = page or pname
                page_id = page_id or str(ad.get("page_id") or "")
        return {"count": len(mine), "page": page, "url": meta_library_url(name, page_id)}


async def check(library: AdLibrary, company_id: str, domain: str, name: str,
                wants) -> list[dict]:
    """The ads observations for one business: only what the libraries show.

    Live ads give value 1 with the count and the library link; a library that
    shows none gives 0 (checked, nothing running). A failed or skipped check
    writes nothing, so it can never read as a fact."""
    obs = []

    def add(code, num, text, url):
        obs.append({"signal_code": code, "company_id": company_id, "collector": "ad_library",
                    "value_num": num, "value_text": text, "confidence": 1.0, "evidence_url": url})

    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    if wants("RUNNING_GOOGLE_ADS") and library.ready:
        try:
            g = await library.google(domain)
            add("RUNNING_GOOGLE_ADS", 1 if g["count"] else 0,
                (f"{g['count']} ad(s) shown in the last {RECENT_DAYS} days in Google Ads "
                 f"Transparency Center (checked {today})") if g["count"] else
                (f"no ads for {domain} in the last {RECENT_DAYS} days in Google Ads "
                 f"Transparency Center (checked {today})"), g["url"])
        except OutOfSearches as e:
            logger.info(f"ad library: {e}")
        except Exception as e:
            logger.info(f"ad library: Google check failed for {domain}: {e}")
    if wants("RUNNING_META_ADS") and library.ready:
        try:
            m = await library.meta(domain, name)
            add("RUNNING_META_ADS", 1 if m["count"] else 0,
                (f"{m['count']} active ad(s) in Meta Ad Library"
                 + (f" from the Page '{m['page']}'" if m["page"] else "")
                 + f" (checked {today})") if m["count"] else
                (f"no active ads for '{name}' or {domain} in Meta Ad Library (checked {today})"),
                m["url"])
        except OutOfSearches as e:
            logger.info(f"ad library: {e}")
        except Exception as e:
            logger.info(f"ad library: Meta check failed for {domain}: {e}")
    return obs
