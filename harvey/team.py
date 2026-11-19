"""Team campaigns: one shared Xorora Prospector, a campaign per person.

Anyone on the team builds a campaign from the dashboard: their name, the
Xorora service to offer, the industries, regions and headcount to target,
where to find companies, and the Instantly mailbox it sends from.

A campaign is standing work, not a one-off batch:

1. Finding companies: a discovery run for that campaign's industries and
   regions. Companies it finds are tagged to the campaign.
2. Finding contacts: the heartbeat's Scout finds the owner at each company
   and verifies an email, as for any company.
3. Writing: each heartbeat, the Writer drafts for the campaign's new,
   deliverable contacts, signed by its owner and pitching its service.
4. Sending: the Sender deploys each draft to Instantly on the owner's mailbox.

It keeps going until it has written to ``max_leads`` contacts or its owner
stops it. Once anyone starts a team campaign, Xorora Prospector is in team mode: the
Writer writes nothing that isn't someone's campaign.
"""

import json
import re
import uuid
from datetime import datetime, timezone

TEAM_MODE_KEY = "team_mode"
REQUESTS_KEY = "team_campaign_requests"
SERVICES_KEY = "team_services"
MAX_NAME = 80
MAX_ACCOUNTS = 10
MAX_LIST = 20
DEFAULT_MAX_LEADS = 100
MAX_MAX_LEADS = 2000
BATCH_SIZE = 25
# Emails a day per mailbox, follow-ups included. 50 is the healthy maximum
# for a warmed mailbox; past it a domain's reputation starts to suffer.
DEFAULT_DAILY_LIMIT = 50
MAX_DAILY_LIMIT = 50


def clean_daily_limit(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT
    return max(1, min(n, MAX_DAILY_LIMIT))


_PACIFIC = ("california", ", ca", "los angeles", "san diego", "san francisco", "san jose",
            "sacramento", "seattle", "washington state", "oregon", "portland", "nevada", "las vegas")
_CENTRAL = ("texas", ", tx", "houston", "dallas", "austin", "san antonio", "chicago", "illinois")
_MOUNTAIN = ("arizona", "phoenix", "colorado", "denver", "utah")


def timezone_for(requests: list[dict], campaign_name_: str) -> str | None:
    """The US time zone a team campaign's prospects are in, for its schedule."""
    for r in requests:
        if campaign_name(r.get("owner") or "", r.get("name") or "") == campaign_name_:
            where = " ".join(r.get("regions") or []).lower()
            if any(k in where for k in _PACIFIC):
                return "America/Los_Angeles"
            if any(k in where for k in _MOUNTAIN):
                return "America/Denver"
            if any(k in where for k in _CENTRAL):
                return "America/Chicago"
            return "America/New_York"
    return None


def daily_limit_for(requests: list[dict], campaign_name_: str) -> int | None:
    """The daily limit of the team campaign an Instantly campaign belongs to."""
    for r in requests:
        if campaign_name(r.get("owner") or "", r.get("name") or "") == campaign_name_:
            return clean_daily_limit(r.get("daily_limit"))
    return None

HEADCOUNTS = {
    "": (None, None),
    "1-10": (1, 10),
    "11-50": (11, 50),
    "51-200": (51, 200),
    "201-500": (201, 500),
    "501-1000": (501, 1000),
    "1000+": (1000, None),
}

DEFAULT_SERVICES = [
    {
        "name": "AI Automation & AI Agents",
        "pitch": ("Xorora builds AI agents and workflow automation that take repetitive "
                  "work off a team: lead handling, follow-ups, reporting, data entry and "
                  "support triage, connected to the tools the business already uses."),
        "benefits": ["Hours of manual work removed every week",
                     "Faster response to leads and customers",
                     "Built around the tools you already use"],
    },
    {
        "name": "Custom Software Development",
        "pitch": ("Xorora designs and builds custom web apps, portals and internal tools, "
                  "from first prototype to the system the business runs on."),
        "benefits": ["Software shaped to how you actually work",
                     "One partner from prototype to production",
                     "Senior engineers, clear weekly progress"],
    },
    {
        "name": "Shopify Store Development",
        "pitch": ("Xorora's in-house Shopify team handles stores A to Z: setup, custom "
                  "themes, apps, migrations and speed and conversion fixes."),
        "benefits": ["Faster, higher-converting store",
                     "Custom features without app bloat",
                     "One team for build, fixes and growth"],
    },
    {
        "name": "Digital Marketing (Meta, Google & LinkedIn Ads)",
        "pitch": ("Xorora runs paid acquisition on Meta, Google and LinkedIn: campaign "
                  "setup, creative testing, tracking and weekly optimisation toward leads "
                  "and sales, not clicks."),
        "benefits": ["Lower cost per lead",
                     "Proper tracking so spend is accountable",
                     "Weekly testing and reporting"],
    },
    {
        "name": "Staff Augmentation (Dedicated Developers)",
        "pitch": ("Xorora places dedicated, vetted developers and engineers into a team "
                  "on a monthly basis, so it can ship faster without hiring overhead."),
        "benefits": ["Vetted engineers in days, not months",
                     "Scale up or down monthly",
                     "Works in your process and time zone overlap"],
    },
    {
        "name": "CRM Implementation (Salesforce, HubSpot, Dynamics 365)",
        "pitch": ("Xorora sets up, migrates and customises CRMs (Salesforce, HubSpot, "
                  "SugarCRM, Dynamics 365) so sales and service teams actually use them."),
        "benefits": ["Clean data and pipelines that match how you sell",
                     "Automations that save reps time",
                     "Migration without losing history"],
    },
]

# The best-fit targets for each service: what the campaign builder fills in
# when someone picks it (they can change any of it).
SERVICE_ICP = {
    'AI Automation & AI Agents': {"regions": ['Dallas, TX', 'Houston, TX', 'Miami, FL', 'London, UK', 'Dubai, UAE', 'Toronto, ON'],
        "industries": ['Logistics companies', 'Real estate agencies', 'Insurance agencies', 'Medical clinics', 'Accounting firms', 'Law firms', 'Recruitment agencies'],
        "headcount": '11-50', "titles": ['CEO', 'Founder', 'Owner', 'COO', 'Managing Director']},
    'Custom Software Development': {"regions": ['New York, NY', 'Austin, TX', 'London, UK', 'Berlin, Germany', 'Dubai, UAE', 'Sydney, Australia'],
        "industries": ['Logistics companies', 'Healthcare', 'Financial services', 'Manufacturing', 'Real estate agencies', 'Education and training'],
        "headcount": '51-200', "titles": ['CEO', 'Founder', 'CTO', 'COO', 'Head of Operations']},
    'Shopify Store Development': {"regions": ['Los Angeles, CA', 'Miami, FL', 'New York, NY', 'London, UK', 'Sydney, Australia', 'Dubai, UAE'],
        "industries": ['Retail stores', 'Fashion boutiques', 'Jewelry stores', 'Furniture stores', 'Beauty salons', 'Electronics stores'],
        "headcount": '1-10', "titles": ['Owner', 'Founder', 'CEO', 'Head of Ecommerce']},
    'Digital Marketing (Meta, Google & LinkedIn Ads)': {"regions": ['Miami, FL', 'Houston, TX', 'Phoenix, AZ', 'Dubai, UAE', 'London, UK', 'Melbourne, Australia'],
        "industries": ['Dental clinics', 'Medical clinics', 'Real estate agencies', 'Law firms', 'Fitness centres', 'Restaurants', 'Car dealerships'],
        "headcount": '11-50', "titles": ['Owner', 'Founder', 'CEO', 'Marketing Director', 'Head of Marketing']},
    'Staff Augmentation (Dedicated Developers)': {"regions": ['San Francisco, CA', 'New York, NY', 'Austin, TX', 'London, UK', 'Amsterdam, Netherlands', 'Toronto, ON'],
        "industries": ['Software companies', 'IT services', 'Marketing agencies', 'Consulting firms', 'Fintech'],
        "headcount": '11-50', "titles": ['CTO', 'CEO', 'Founder', 'VP Engineering', 'Head of Engineering']},
    'CRM Implementation (Salesforce, HubSpot, Dynamics 365)': {"regions": ['Chicago, IL', 'Dallas, TX', 'Atlanta, GA', 'London, UK', 'Riyadh, Saudi Arabia', 'Dubai, UAE'],
        "industries": ['Real estate agencies', 'Insurance agencies', 'Financial services', 'Recruitment agencies', 'Logistics companies', 'Wholesale distributors'],
        "headcount": '51-200', "titles": ['CEO', 'COO', 'Head of Sales', 'Sales Director', 'Founder']},
}
DEFAULT_TITLES = ["CEO", "Founder", "Owner", "Managing Director"]
TITLE_SUGGESTIONS = ["CEO", "Founder", "Co-Founder", "Owner", "President", "Managing Director",
    "COO", "CTO", "CFO", "CMO", "VP Engineering", "Head of Engineering", "Head of Operations",
    "Head of Sales", "Sales Director", "Head of Marketing", "Marketing Director",
    "Head of Ecommerce", "General Manager", "Partner", "Principal"]


def icp_for(service_name: str) -> dict:
    rec = SERVICE_ICP.get(service_name) or {}
    return {"industries": list(rec.get("industries") or []),
            "headcount": rec.get("headcount", ""),
            "titles": list(rec.get("titles") or DEFAULT_TITLES),
            "regions": list(rec.get("regions") or [])}


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def clean_name(raw, limit: int = MAX_NAME) -> str:
    return " ".join(str(raw or "").split())[:limit]


def clean_list(raw) -> list[str]:
    if isinstance(raw, str):
        raw = re.split(r"[,\n;]", raw)
    if not isinstance(raw, list):
        return []
    out = [clean_name(x, 80) for x in raw]
    return list(dict.fromkeys(x for x in out if x))[:MAX_LIST]


def headcount_range(label: str) -> tuple[int | None, int | None]:
    return HEADCOUNTS.get(label or "", (None, None))


def size_fits(company_size: str, label: str) -> bool:
    """Unknown size always fits: most small businesses don't publish one."""
    lo, hi = headcount_range(label)
    if lo is None and hi is None:
        return True
    nums = [int(n) for n in re.findall(r"\d+", (company_size or "").replace(",", ""))]
    if not nums:
        return True
    c_lo, c_hi = min(nums), max(nums)
    if "+" in (company_size or ""):
        c_hi = 10 ** 9
    return not ((hi is not None and c_lo > hi) or (lo is not None and c_hi < lo))


async def team_mode(state) -> bool:
    return bool(await state.get_setting(TEAM_MODE_KEY))


async def load_services(state) -> list[dict]:
    raw = await state.get_setting(SERVICES_KEY)
    try:
        data = json.loads(raw) if raw else None
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, list) or not data:
        data = [dict(s) for s in DEFAULT_SERVICES]
    out = [dict(s) for s in data if isinstance(s, dict) and s.get("name")]
    for svc in out:
        svc["icp"] = icp_for(svc["name"])
    return out


async def save_services(state, services: list[dict]):
    clean = []
    for s in services[:30]:
        name = clean_name(s.get("name"))
        if not name:
            continue
        clean.append({
            "name": name,
            "pitch": " ".join(str(s.get("pitch") or "").split())[:800],
            "benefits": clean_list(s.get("benefits"))[:6],
        })
    await state.set_setting(SERVICES_KEY, json.dumps(clean))
    return clean


async def load_requests(state) -> list[dict]:
    raw = await state.get_setting(REQUESTS_KEY)
    try:
        data = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("id")]


async def save_requests(state, requests: list[dict]):
    await state.set_setting(REQUESTS_KEY, json.dumps(requests))


async def update_request(state, request_id: str, **fields) -> dict | None:
    requests = await load_requests(state)
    found = None
    for r in requests:
        if r.get("id") == request_id:
            r.update(fields)
            found = r
    await save_requests(state, requests)
    return found


async def add_company_ids(state, request_id: str, company_ids: list[str]):
    requests = await load_requests(state)
    for r in requests:
        if r.get("id") == request_id:
            have = list(r.get("company_ids") or [])
            r["company_ids"] = list(dict.fromkeys(have + [c for c in company_ids if c]))
    await save_requests(state, requests)


async def add_request(state, *, owner: str, accounts: list[str], name: str,
                      service: dict, industries: list[str], regions: list[str],
                      headcount: str, provider: str, max_spend: float,
                      max_leads: int, company_ids: list[str],
                      daily_limit: int = DEFAULT_DAILY_LIMIT,
                      member_id: str = "", titles: list[str] | None = None) -> dict:
    request = {
        "id": uuid.uuid4().hex[:12],
        "owner": owner,
        "accounts": accounts[:MAX_ACCOUNTS],
        "name": name,
        "service": service,
        "industries": industries,
        "regions": regions,
        "headcount": headcount if headcount in HEADCOUNTS else "",
        "provider": provider,
        "max_spend": max_spend,
        "max_leads": max(1, min(int(max_leads or DEFAULT_MAX_LEADS), MAX_MAX_LEADS)),
        "company_ids": list(dict.fromkeys(company_ids)),
        "daily_limit": clean_daily_limit(daily_limit),
        "member_id": member_id,
        "titles": list(titles or [])[:MAX_LIST],
        "written": 0,
        "status": "active",          # active / stopped / complete
        "discovery": "queued",       # queued / running / done / failed / skipped
        "discovery_note": "",
        "created_at": _now(),
    }
    requests = await load_requests(state)
    requests.append(request)
    await save_requests(state, requests)
    await state.set_setting(TEAM_MODE_KEY, "1")
    return request


async def remove_request(state, request_id: str):
    requests = [r for r in await load_requests(state) if r.get("id") != request_id]
    await save_requests(state, requests)


def campaign_name(owner: str, name: str) -> str:
    """Owner first, so everyone's campaigns sort and filter by person, here
    and in Instantly."""
    return f"{owner} · {clean_name(name) or 'Campaign'}"


async def matching_company_ids(state, industries: list[str], regions: list[str]) -> list[str]:
    """Companies already known that fit: in one of the regions, and in one of
    the industries (by industry, name or description)."""
    if not industries and not regions:
        return []
    where, params = [], []
    if regions:
        where.append("(" + " OR ".join("lower(location) LIKE ?" for _ in regions) + ")")
        params += [f"%{_place_key(r)}%" for r in regions]
    if industries:
        ors = []
        # A map-listed business carries its map category ("clothes",
        # "dentist") as its industry, so search those words too.
        terms = list(industries)
        try:
            from harvey.collectors.discover import OpenStreetMap
            osm = OpenStreetMap.__new__(OpenStreetMap)
            for t in industries:
                terms += [tag.split("=", 1)[1].replace("_", " ") for tag in osm._tags(t)]
        except Exception:
            pass
        industries = list(dict.fromkeys(terms))
        for term in industries:
            for col in ("industry", "name", "description"):
                ors.append(f"lower(COALESCE({col}, '')) LIKE ?")
                params.append(f"%{term.lower()}%")
        where.append("(" + " OR ".join(ors) + ")")
    sql = "SELECT id FROM companies WHERE " + " AND ".join(where) + " LIMIT 5000"
    async with state._connect() as db:
        async with db.execute(sql, tuple(params)) as cursor:
            return [r[0] for r in await cursor.fetchall()]


def _place_key(region: str) -> str:
    """'Miami, FL' matches stored 'Miami, FL' and 'Miami'; compare on the city."""
    return region.split(",")[0].strip().lower()


async def candidate_prospect_ids(state, request: dict) -> list[str]:
    """New, deliverable contacts at this campaign's companies that fit its
    headcount, best first, not already claimed by an earlier campaign."""
    company_ids = list(request.get("company_ids") or [])
    if not company_ids:
        return []
    label = request.get("headcount") or ""
    fitting = company_ids
    if label:
        fitting = []
        async with state._connect() as db:
            for start in range(0, len(company_ids), 500):
                chunk = company_ids[start:start + 500]
                async with db.execute(
                    f"SELECT id, COALESCE(company_size, '') FROM companies "
                    f"WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk),
                ) as cursor:
                    fitting += [r[0] for r in await cursor.fetchall() if size_fits(r[1], label)]
    ids = await state.prospect_ids_for_companies(fitting)
    return await state.writable_prospect_ids(ids)


async def remaining(request: dict) -> int:
    return max(0, int(request.get("max_leads") or DEFAULT_MAX_LEADS) - int(request.get("written") or 0))


async def pending_writable(state) -> int:
    """How many contacts the Writer could draft for right now, across active
    campaigns: what the heartbeat counts as writable in team mode."""
    total, seen = 0, set()
    for r in await load_requests(state):
        if r.get("status") != "active":
            continue
        ids = [i for i in await candidate_prospect_ids(state, r) if i not in seen]
        ids = ids[:await remaining(r)]
        seen.update(ids)
        total += len(ids)
    return total


# ── Suggestions and regions ──

# Business types the free company search (OpenStreetMap) can find; the
# campaign builder suggests these as people type.
INDUSTRY_SUGGESTIONS = [
    "Restaurants", "Cafes", "Hotels", "Real estate agencies", "Property management",
    "Insurance agencies", "Accounting firms", "Tax advisors", "Law firms",
    "Dental clinics", "Medical clinics", "Healthcare", "Hospitals", "Pharmacies",
    "Veterinary clinics", "Opticians", "Fitness centres", "Gyms", "Beauty salons",
    "Spas", "Travel agencies", "Auto repair", "Car dealerships", "Automotive",
    "Retail stores", "Fashion boutiques", "Clothing stores", "Furniture stores",
    "Jewelry stores", "Electronics stores", "Grocery stores", "Supermarkets",
    "Bakeries", "Florists", "Hardware stores", "Wholesale distributors",
    "Construction companies", "Contractors", "Roofing", "Plumbing", "HVAC",
    "Electricians", "Landscaping", "Architecture firms", "Engineering firms",
    "Logistics companies", "Freight and shipping", "Software companies",
    "IT services", "Marketing agencies", "Advertising agencies", "Consulting firms",
    "Financial services", "Fintech", "Recruitment agencies", "Staffing agencies",
    "Telecom", "Energy companies", "Solar companies", "Manufacturing",
    "Printing services", "Photography studios", "Coworking spaces", "Event venues",
    "Nonprofits", "Education and training", "Storage facilities",
]

# A state or country is too big for one map search, so it becomes its main
# cities. Cities typed directly are searched as they are.
REGION_CITIES = {
    # United States
    "california": ["Los Angeles, CA", "San Francisco, CA", "San Diego, CA", "San Jose, CA", "Sacramento, CA"],
    "texas": ["Houston, TX", "Dallas, TX", "Austin, TX", "San Antonio, TX"],
    "florida": ["Miami, FL", "Orlando, FL", "Tampa, FL", "Jacksonville, FL"],
    "new york": ["New York, NY", "Brooklyn, NY", "Buffalo, NY"],
    "illinois": ["Chicago, IL"],
    "georgia": ["Atlanta, GA"],
    "washington": ["Seattle, WA"],
    "massachusetts": ["Boston, MA"],
    "arizona": ["Phoenix, AZ", "Scottsdale, AZ"],
    "colorado": ["Denver, CO"],
    "nevada": ["Las Vegas, NV"],
    "north carolina": ["Charlotte, NC", "Raleigh, NC"],
    "new jersey": ["Newark, NJ", "Jersey City, NJ"],
    "pennsylvania": ["Philadelphia, PA", "Pittsburgh, PA"],
    "ohio": ["Columbus, OH", "Cleveland, OH"],
    "michigan": ["Detroit, MI"],
    "tennessee": ["Nashville, TN"],
    "virginia": ["Virginia Beach, VA", "Richmond, VA"],
    "utah": ["Salt Lake City, UT"],
    "oregon": ["Portland, OR"],
    "minnesota": ["Minneapolis, MN"],
    "usa": ["New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX", "Miami, FL"],
    "united states": ["New York, NY", "Los Angeles, CA", "Chicago, IL", "Houston, TX", "Miami, FL"],
    # Canada
    "canada": ["Toronto, ON", "Vancouver, BC", "Montreal, QC", "Calgary, AB"],
    "ontario": ["Toronto, ON", "Ottawa, ON"],
    "british columbia": ["Vancouver, BC"],
    "alberta": ["Calgary, AB", "Edmonton, AB"],
    # UK and Europe
    "uk": ["London, UK", "Manchester, UK", "Birmingham, UK", "Leeds, UK"],
    "united kingdom": ["London, UK", "Manchester, UK", "Birmingham, UK", "Leeds, UK"],
    "england": ["London, UK", "Manchester, UK", "Birmingham, UK"],
    "scotland": ["Edinburgh, UK", "Glasgow, UK"],
    "ireland": ["Dublin, Ireland", "Cork, Ireland"],
    "germany": ["Berlin, Germany", "Munich, Germany", "Hamburg, Germany", "Frankfurt, Germany"],
    "netherlands": ["Amsterdam, Netherlands", "Rotterdam, Netherlands"],
    # Australia
    "australia": ["Sydney, Australia", "Melbourne, Australia", "Brisbane, Australia", "Perth, Australia"],
    # Middle East
    "uae": ["Dubai, UAE", "Abu Dhabi, UAE", "Sharjah, UAE"],
    "united arab emirates": ["Dubai, UAE", "Abu Dhabi, UAE", "Sharjah, UAE"],
    "saudi arabia": ["Riyadh, Saudi Arabia", "Jeddah, Saudi Arabia", "Dammam, Saudi Arabia"],
    "ksa": ["Riyadh, Saudi Arabia", "Jeddah, Saudi Arabia"],
    "qatar": ["Doha, Qatar"],
}

CITY_SUGGESTIONS = list(dict.fromkeys(
    [c for cities in REGION_CITIES.values() for c in cities]
    + ["Austin, TX", "Fort Lauderdale, FL", "Boca Raton, FL", "West Palm Beach, FL",
       "Irvine, CA", "Oakland, CA", "Fort Worth, TX", "Kansas City, MO", "St. Louis, MO",
       "Indianapolis, IN", "Milwaukee, WI", "Baltimore, MD", "Washington, DC",
       "Mississauga, ON", "Liverpool, UK", "Bristol, UK", "Gold Coast, Australia",
       "Adelaide, Australia", "Auckland, New Zealand", "Muscat, Oman", "Manama, Bahrain",
       "Kuwait City, Kuwait", "Cologne, Germany", "Stuttgart, Germany", "Utrecht, Netherlands"]))

REGION_SUGGESTIONS = [k.title() if len(k) > 3 else k.upper() for k in REGION_CITIES] + CITY_SUGGESTIONS


def expand_regions(regions: list[str]) -> list[str]:
    """States and countries become their main cities; cities stay as typed."""
    out = []
    for r in regions or []:
        out += REGION_CITIES.get(r.strip().lower().rstrip("."), [r])
    return list(dict.fromkeys(out))
