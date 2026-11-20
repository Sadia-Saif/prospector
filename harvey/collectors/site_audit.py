"""A website audit made only of checks anyone can repeat.

What a business's site shows the world can be checked from outside: its
speed (Google's own PageSpeed Insights, with real visitors' Core Web Vitals
when Google has them), its on-page SEO, and the things that lose a visitor
before they book or call. Each finding names what was checked, so an email
can quote it and the prospect can see it for themselves.

What can NOT be known from outside: click-through rates and conversion
rates. They live in the business's own Google Ads, Meta and Analytics
accounts, so nothing here estimates them: a guessed rate is a made-up fact.
"""

from __future__ import annotations

import logging
import re
from html import unescape
from urllib.parse import quote, urljoin

import httpx

logger = logging.getLogger("harvey.site_audit")

PSI = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


def _text(html: str) -> str:
    return unescape(re.sub(r"(?s)<[^>]+>", " ", html or "")).strip()


def _tags(html: str, tag: str) -> list[str]:
    return re.findall(rf"(?is)<{tag}\b[^>]*>(.*?)</{tag}>", html or "")


def _meta(html: str, name: str) -> str | None:
    for m in re.finditer(r"(?is)<meta\b[^>]*>", html or ""):
        tag = m.group(0)
        if re.search(rf"""(?i)\b(name|property)\s*=\s*["']{re.escape(name)}["']""", tag):
            c = re.search(r"""(?is)\bcontent\s*=\s*(["'])(.*?)\1""", tag)
            return unescape(c.group(2)).strip() if c else ""
    return None


def seo_problems(html: str, site: str) -> list[str]:
    """On-page SEO problems on the homepage, each phrased as what we saw."""
    out = []
    titles = [_text(t) for t in _tags(html, "title")]
    title = titles[0] if titles else ""
    if not title:
        out.append("the homepage has no page title")
    elif len(title) > 65:
        out.append(f"the page title is {len(title)} characters, so Google cuts it off (keep it under 60)")
    elif len(title) < 15:
        out.append(f"the page title is only '{title}', too short to rank for anything")
    desc = _meta(html, "description")
    if desc is None or not desc:
        out.append("no meta description, so Google writes its own snippet")
    elif len(desc) > 170:
        out.append(f"the meta description is {len(desc)} characters and gets cut off")
    h1s = [t for t in (_text(h) for h in _tags(html, "h1")) if t]
    if not h1s:
        out.append("no H1 heading on the homepage")
    elif len(h1s) > 1:
        out.append(f"{len(h1s)} H1 headings on the homepage (one is best)")
    imgs = re.findall(r"(?is)<img\b[^>]*>", html or "")
    no_alt = [i for i in imgs if not re.search(r"""(?i)\balt\s*=\s*(["'])\s*\S""", i)]
    if len(imgs) >= 3 and len(no_alt) >= max(3, len(imgs) // 3):
        out.append(f"{len(no_alt)} of {len(imgs)} images have no alt text")
    robots = _meta(html, "robots") or ""
    if "noindex" in robots.lower():
        out.append("the homepage tells Google not to index it (noindex)")
    if _meta(html, "viewport") is None:
        out.append("no mobile viewport tag, so phones show the desktop page shrunk down")
    if not re.search(r"""(?i)<link\b[^>]*rel\s*=\s*["']canonical["']""", html or ""):
        out.append("no canonical link")
    if site.startswith("http://"):
        out.append("the site only answered over http, not https (browsers mark it 'Not secure')")
    return out


def conversion_problems(html: str) -> list[str]:
    """Things on the homepage that lose a visitor before they call or book."""
    out = []
    low = (html or "").lower()
    if 'href="tel:' not in low and "href='tel:" not in low:
        out.append("no tap-to-call phone link on the homepage")
    if "<form" not in low and not re.search(r"(?i)(typeform|jotform|hubspot.*forms|wpforms|gravityforms|contact[-_]?form)", low):
        out.append("no contact or enquiry form on the homepage")
    ctas = re.findall(r"(?is)<(?:a|button)\b[^>]*>(.*?)</(?:a|button)>", html or "")
    words = re.compile(r"(?i)\b(book|reserve|order|call|get (a )?quote|schedule|contact us|buy|shop|start|request)\b")
    if not any(words.search(_text(c)) for c in ctas):
        out.append("no clear action button (book, order, call, get a quote) on the homepage")
    return out


async def site_files(client: httpx.AsyncClient, site: str) -> list[str]:
    """robots.txt and sitemap problems."""
    out = []
    try:
        r = await client.get(urljoin(site + "/", "robots.txt"))
        robots = r.text if r.status_code == 200 else ""
    except (httpx.HTTPError, ValueError):
        robots = ""
    if re.search(r"(?im)^\s*user-agent:\s*\*\s*$[\s\S]*?^\s*disallow:\s*/\s*$", robots):
        out.append("robots.txt blocks every search engine from the whole site")
    sitemap = re.search(r"(?im)^\s*sitemap:\s*(\S+)", robots)
    url = sitemap.group(1) if sitemap else urljoin(site + "/", "sitemap.xml")
    try:
        r = await client.get(url)
        head = r.text[:5000].lower() if r.status_code == 200 else ""
        ok = "<urlset" in head or "<sitemapindex" in head
    except (httpx.HTTPError, ValueError):
        ok = False
    if not ok:
        out.append("no XML sitemap found (robots.txt or /sitemap.xml)")
    return out


async def pagespeed(client: httpx.AsyncClient, url: str, key: str = "") -> dict | None:
    """Google PageSpeed Insights for the mobile homepage, or None if it failed.

    {"score": 0-100, "seo": 0-100, "field": "FAST|AVERAGE|SLOW" or "", "lcp_s": float|None}"""
    params = [("url", url), ("strategy", "mobile"), ("category", "performance"), ("category", "seo")]
    if key:
        params.append(("key", key))
    try:
        r = await client.get(PSI, params=params, timeout=90)
        body = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if r.status_code != 200 or "lighthouseResult" not in body:
        logger.info(f"pagespeed: {url} → HTTP {r.status_code}")
        return None
    cats = body["lighthouseResult"].get("categories") or {}
    score = lambda k: round(100 * float((cats.get(k) or {}).get("score") or 0))
    field = (body.get("loadingExperience") or {})
    lcp = ((field.get("metrics") or {}).get("LARGEST_CONTENTFUL_PAINT_MS") or {}).get("percentile")
    return {"score": score("performance"), "seo": score("seo"),
            "field": field.get("overall_category", "") if field.get("metrics") else "",
            "lcp_s": round(lcp / 1000, 1) if isinstance(lcp, (int, float)) else None}


def pagespeed_url(url: str) -> str:
    """The public PageSpeed report for the page, so anyone can re-run the check."""
    return "https://pagespeed.web.dev/analysis?url=" + quote(url, safe="")


def speed_text(ps: dict) -> str:
    parts = [f"Google PageSpeed mobile score {ps['score']}/100"]
    if ps.get("field"):
        word = {"FAST": "good", "AVERAGE": "needs improvement", "SLOW": "poor"}.get(ps["field"], ps["field"].lower())
        parts.append(f"real visitors' speed (Core Web Vitals) is {word}"
                     + (f", main content shows after {ps['lcp_s']}s" if ps.get("lcp_s") else ""))
    return "; ".join(parts)
