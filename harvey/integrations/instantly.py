"""Instantly.ai API client for cold email campaigns."""

import asyncio
import logging
import random
from typing import Any

import httpx

logger = logging.getLogger("harvey.instantly")

BASE_URL = "https://api.instantly.ai/api/v2"

# Retry policy
MAX_RETRIES = 4
BACKOFF_BASE = 1.0  # seconds
BACKOFF_CAP = 30.0
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Explicit timeouts: connect / read / write / pool
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


# Prospector writes {{first_name}} and {{company}}; Instantly names its own
# lead fields firstName and companyName. Anything else is a custom variable.
_INSTANTLY_VARS = {"first_name": "firstName", "last_name": "lastName", "company": "companyName"}


def to_instantly_html(body: str) -> str:
    """An email body the way Instantly's own editor stores it: one <div> a
    line, an empty <div><br></div> between paragraphs.

    Plain newlines collapse in Instantly, and plain text mixed with <br>
    tags is thrown away when the sequence is saved, which left every step
    empty. Wrapped in divs, the text, the greeting and the spacing all keep.
    """
    import html as _html
    import re
    text = (body or "").replace("\r\n", "\n").strip("\n")
    if re.search(r"(?i)<(div|p)\b", text):
        return text  # already in Instantly's own format
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    return "".join(
        "<div>" + (_html.escape(line, quote=False) if line.strip() else "<br>") + "</div>"
        for line in text.split("\n"))


to_html_breaks = to_instantly_html  # older name


def to_instantly_vars(text: str) -> str:
    import re
    return re.sub(r"\{\{\s*(first_name|last_name|company)\s*\}\}",
                  lambda m: "{{" + _INSTANTLY_VARS[m.group(1)] + "}}", text or "")


# Instantly validates time zones against its own list, which leaves out some
# common names. These are other names for the same local time; the fixed
# GMT offset is the last resort (it ignores daylight saving).
_ZONE_ALIASES = {
    # Instantly's own names for each US zone come first (checked against its
    # API: it refuses America/Los_Angeles, America/New_York, America/Denver
    # and America/Phoenix).
    "America/Los_Angeles": ["America/Dawson", "America/Los_Angeles", "America/Tijuana"],
    "America/Denver": ["America/Boise", "America/Denver", "America/Edmonton"],
    "America/Phoenix": ["America/Creston", "America/Phoenix"],
    "America/Chicago": ["America/Chicago", "America/Winnipeg"],
    "America/New_York": ["America/Detroit", "America/New_York", "America/Toronto"],
}


def zone_candidates(timezone: str) -> list[str]:
    return list(dict.fromkeys(_ZONE_ALIASES.get(timezone, [timezone, "America/Detroit"])))


def _html_to_text(html: str) -> str:
    import re
    import html as _html
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html or "")
    text = _html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def campaign_schedule(timezone: str) -> dict:
    """Weekdays, 9am to 5pm in ``timezone``: the schedule Instantly requires."""
    return {"schedules": [{
        "name": "Weekdays",
        "timing": {"from": "09:00", "to": "17:00"},
        "days": {"0": False, "1": True, "2": True, "3": True, "4": True, "5": True, "6": False},
        "timezone": timezone,
    }]}


class InstantlyClient:
    def __init__(self, api_key: str):
        self.api_key = api_key or ""
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _redact(self, text: str) -> str:
        """Strip the API key from anything we log or raise."""
        if self.api_key and text:
            return text.replace(self.api_key, "***REDACTED***")
        return text or ""

    @staticmethod
    def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
        """Exponential backoff with jitter; honors Retry-After on 429."""
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return min(float(retry_after), BACKOFF_CAP) + random.uniform(0, 1)
                except ValueError:
                    pass
        base = min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)
        return base * random.uniform(0.5, 1.5)

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
        params: dict | None = None,
    ) -> dict | list | None:
        """Make an authenticated request with retries, backoff, and validation."""
        if not self.api_key:
            logger.error("Instantly API key is not configured.")
            return None

        url = f"{BASE_URL}/{endpoint.lstrip('/')}"

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await client.request(
                        method, url, headers=self.headers, json=data, params=params
                    )
                except httpx.HTTPError as e:
                    # Network/timeout errors are retryable.
                    if attempt < MAX_RETRIES:
                        delay = self._retry_delay(attempt, None)
                        logger.warning(
                            f"Instantly request error ({method} {endpoint}): "
                            f"{self._redact(str(e))}. "
                            f"Retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s."
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.error(
                        f"Instantly request failed after {MAX_RETRIES} retries "
                        f"({method} {endpoint}): {self._redact(str(e))}"
                    )
                    return None

                if response.status_code in RETRYABLE_STATUS:
                    if attempt < MAX_RETRIES:
                        delay = self._retry_delay(attempt, response)
                        label = (
                            "rate limited (429)"
                            if response.status_code == 429
                            else f"server error ({response.status_code})"
                        )
                        logger.warning(
                            f"Instantly {label} on {method} {endpoint}. "
                            f"Retry {attempt + 1}/{MAX_RETRIES} in {delay:.1f}s."
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.error(
                        f"Instantly API error {response.status_code} on "
                        f"{method} {endpoint} after {MAX_RETRIES} retries: "
                        f"{self._redact(response.text[:500])}"
                    )
                    return None

                if response.status_code >= 400:
                    # Non-retryable client error (400, 401, 403, 404, ...)
                    logger.error(
                        f"Instantly API error {response.status_code} on "
                        f"{method} {endpoint}: {self._redact(response.text[:500])}"
                    )
                    return None

                if response.status_code == 204 or not response.content:
                    return {}

                try:
                    payload = response.json()
                except ValueError:
                    logger.error(
                        f"Instantly returned non-JSON response on {method} "
                        f"{endpoint}: {self._redact(response.text[:200])}"
                    )
                    return None

                if not isinstance(payload, (dict, list)):
                    logger.error(
                        f"Instantly returned unexpected JSON type "
                        f"({type(payload).__name__}) on {method} {endpoint}."
                    )
                    return None
                return payload

        return None

    @staticmethod
    def _extract_items(result: Any) -> list:
        """Normalize list responses: API may return a bare list or {items: [...]}. """
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            items = result.get("items") or result.get("data")
            if isinstance(items, list):
                return items
        return []

    # ── Campaigns ──

    async def create_campaign(self, name: str,
                              email_list: list[str] | None = None,
                              daily_limit: int | None = None,
                              timezone: str | None = None) -> dict | None:
        """Create a new campaign. Returns campaign data with id.

        ``email_list`` limits sending to those Instantly accounts (a team
        member's own mailbox domain); empty means Instantly's default.
        Instantly's v2 API refuses a campaign without a sending schedule, so
        one is always sent: weekdays, 9am to 5pm in the prospects' time zone.
        """
        # stop_for_company: a reply from anyone at a company stops the
        # sequence to everyone there, as on Xorora Prospector's own providers.
        # open_tracking: Instantly counts opens, so each campaign shows its
        # open rate (in Instantly and on the Prospector campaign card).
        payload = {"name": name, "stop_for_company": True, "open_tracking": True}
        if email_list:
            payload["email_list"] = list(email_list)
        if daily_limit:
            payload["daily_limit"] = int(daily_limit)
        # Instantly accepts only time zones from its own list; try the
        # names it may know for the SAME zone, never a different one.
        zones = zone_candidates(timezone or "America/New_York")
        result = None
        for tz in zones:
            payload["campaign_schedule"] = campaign_schedule(tz)
            result = await self._request("POST", "/campaigns", payload)
            if isinstance(result, dict):
                break
        if isinstance(result, dict):
            logger.info(f"Created campaign: {name} (id={result.get('id')})")
            return result
        return None

    async def set_campaign_daily_limit(self, campaign_id: str, limit: int) -> dict | None:
        result = await self._request("PATCH", f"/campaigns/{campaign_id}",
                                     {"daily_limit": int(limit)})
        return result if isinstance(result, dict) else None

    async def set_open_tracking(self, campaign_id: str, on: bool = True) -> dict | None:
        result = await self._request("PATCH", f"/campaigns/{campaign_id}", {"open_tracking": bool(on)})
        return result if isinstance(result, dict) else None

    async def set_campaign_accounts(self, campaign_id: str, emails: list[str]) -> dict | None:
        """The mailboxes a campaign rotates between."""
        result = await self._request("PATCH", f"/campaigns/{campaign_id}",
                                     {"email_list": list(emails)})
        return result if isinstance(result, dict) else None

    async def set_account_daily_limit(self, email: str, limit: int) -> dict | None:
        """Cap what one mailbox sends a day, across every campaign it's in."""
        result = await self._request("PATCH", f"/accounts/{email}",
                                     {"daily_limit": int(limit)})
        return result if isinstance(result, dict) else None

    async def get_campaign(self, campaign_id: str) -> dict | None:
        """Get campaign details."""
        result = await self._request("GET", f"/campaigns/{campaign_id}")
        return result if isinstance(result, dict) else None

    async def list_campaigns(self, limit: int = 100) -> list:
        """List all campaigns."""
        result = await self._request("GET", "/campaigns", params={"limit": limit})
        return self._extract_items(result)

    async def update_campaign_schedule(self, campaign_id: str, timezone: str) -> str | None:
        """Send on weekdays, 9 to 5 in the leads' time zone. Returns the zone
        name Instantly accepted, or None."""
        for tz in zone_candidates(timezone):
            result = await self._request(
                "PATCH", f"/campaigns/{campaign_id}", {"campaign_schedule": campaign_schedule(tz)})
            if isinstance(result, dict):
                return tz
        return None

    async def activate_campaign(self, campaign_id: str) -> dict | None:
        """Start a campaign."""
        result = await self._request("POST", f"/campaigns/{campaign_id}/activate")
        return result if isinstance(result, dict) else None

    async def pause_campaign(self, campaign_id: str) -> dict | None:
        """Pause a campaign."""
        result = await self._request("POST", f"/campaigns/{campaign_id}/pause")
        return result if isinstance(result, dict) else None

    # ── Campaign Emails (Sequences) ──

    async def set_campaign_emails(
        self,
        campaign_id: str,
        sequences: list[dict],
    ) -> dict | None:
        """Set the email sequence for a campaign.

        sequences format:
        [
            {
                "subject": "Subject line",
                "body": "Email body (HTML or plain text)",
                "wait": 0  # days to wait after previous step
            },
            ...
        ]
        """
        # Instantly's v2 API keeps the sequence on the campaign itself, as
        # steps with variants. A step's delay is the days to wait before the
        # NEXT step, so each step carries the following step's wait.
        steps = []
        for i, s in enumerate(sequences):
            nxt = sequences[i + 1].get("wait", 0) if i + 1 < len(sequences) else 0
            steps.append({
                "type": "email",
                "delay": int(nxt or 0),
                "variants": [{"subject": to_instantly_vars(s.get("subject", "")),
                              "body": to_instantly_html(to_instantly_vars(s.get("body", "")))}],
            })
        result = await self._request(
            "PATCH",
            f"/campaigns/{campaign_id}",
            {"sequences": [{"steps": steps}]},
        )
        return result if isinstance(result, dict) else None

    # ── Leads ──

    async def add_leads(
        self,
        campaign_id: str,
        leads: list[dict],
    ) -> dict | None:
        """Add leads to a campaign.

        leads format:
        [
            {
                "email": "john@example.com",
                "first_name": "John",
                "last_name": "Doe",
                "company_name": "Acme Inc",
                "variables": {"custom_var": "value"}
            }
        ]
        """
        if not leads:
            logger.warning("add_leads called with an empty lead list; skipping.")
            return {}
        v2 = []
        for lead in leads:
            lead = dict(lead)
            if "variables" in lead:  # v2 calls them custom variables
                lead["custom_variables"] = {k: v for k, v in (lead.pop("variables") or {}).items()
                                            if v not in (None, "")}
            v2.append(lead)
        result = await self._request(
            "POST",
            "/leads/add",
            {"campaign_id": campaign_id, "leads": v2},
        )
        return result if isinstance(result, dict) else None

    async def get_lead(self, email: str) -> dict | None:
        """Get lead details by email."""
        result = await self._request("GET", "/leads", params={"email": email})
        return result if isinstance(result, dict) else None

    # ── Replies / Emails ──

    async def get_campaign_emails_sent(
        self, campaign_id: str, limit: int = 100
    ) -> list:
        """Get emails sent for a campaign."""
        result = await self._request(
            "GET", "/emails", params={"campaign_id": campaign_id, "limit": limit}
        )
        return self._extract_items(result)

    async def get_replies(self, campaign_id: str | None = None) -> list:
        """Emails leads sent back, optionally for one campaign.

        Instantly's v2 API lists them as received emails; each is reshaped
        to {id, lead_email, body, subject} for the handler, and the mailbox
        it came into is remembered so an answer goes out from the same one.
        """
        params = {"email_type": "received", "limit": 100}
        if campaign_id:
            params["campaign_id"] = campaign_id
        result = await self._request("GET", "/emails", params=params)
        out = []
        for item in self._extract_items(result):
            if not isinstance(item, dict):
                continue
            body = item.get("body")
            if isinstance(body, dict):
                text = body.get("text") or _html_to_text(body.get("html") or "")
            else:
                text = str(body or "")
            uid = str(item.get("id") or "")
            reply = {
                "id": uid,
                # The lead the thread belongs to, even when a colleague answers.
                "lead_email": item.get("lead") or item.get("from_address_email") or "",
                "body": text,
                "subject": item.get("subject") or "",
                "eaccount": item.get("eaccount") or "",
            }
            if uid:
                self.__dict__.setdefault("_reply_meta", {})[uid] = (reply["eaccount"], reply["subject"])
            out.append(reply)
        return out

    async def get_sent_emails(self, campaign_id: str, max_pages: int = 5) -> list:
        """The emails a campaign actually sent, exactly as they went out
        (with the mailbox's signature and footer), oldest first.

        Each is {lead_email, subject, body, eaccount, sent_at}."""
        out, after = [], None
        for _ in range(max_pages):
            params = {"campaign_id": campaign_id, "email_type": "sent", "limit": 100}
            if after:
                params["starting_after"] = after
            result = await self._request("GET", "/emails", params=params)
            if result is None:
                # A failed read (rate limit, timeout) is not "nothing sent":
                # raise so the caller keeps what it already had.
                raise RuntimeError("Instantly didn't answer the sent-emails read")
            for item in self._extract_items(result):
                if not isinstance(item, dict):
                    continue
                body = item.get("body")
                if isinstance(body, dict):
                    raw = body.get("html") or ""
                    text = _html_to_text(raw) or body.get("text") or ""
                else:
                    raw = str(body or "")
                    text = _html_to_text(raw)
                out.append({
                    "html": raw,
                    "lead_email": (item.get("lead") or item.get("to_address_email_list") or "").lower(),
                    "subject": item.get("subject") or "",
                    "body": text,
                    "eaccount": item.get("eaccount") or item.get("from_address_email") or "",
                    "sent_at": item.get("timestamp_email") or item.get("timestamp_created") or "",
                })
            after = result.get("next_starting_after") if isinstance(result, dict) else None
            if not after:
                break
        out.sort(key=lambda e: e["sent_at"] or "")
        return out

    async def account_signatures(self) -> dict:
        """Each sending mailbox's own signature, by address: {"text", "html"}."""
        sigs = {}
        accounts = await self.list_accounts()
        if not accounts:
            raise RuntimeError("Instantly didn't answer the mailbox read")
        for a in accounts:
            if isinstance(a, dict) and a.get("email"):
                sig = a.get("signature") or ""
                sigs[a["email"].lower()] = {"text": _html_to_text(sig) if sig else "", "html": sig}
        return sigs

    async def send_reply(
        self,
        reply_to_uuid: str,
        body: str,
    ) -> dict | None:
        """Answer a lead's email in its thread, from the mailbox it came into."""
        eaccount, subject = self.__dict__.get("_reply_meta", {}).get(reply_to_uuid, ("", ""))
        if subject and not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        result = await self._request(
            "POST",
            "/emails/reply",
            {"reply_to_uuid": reply_to_uuid, "eaccount": eaccount, "subject": subject,
             "body": {"text": body, "html": body.replace("\n", "<br>")}},
        )
        return result if isinstance(result, dict) else None

    # ── Analytics ──

    async def get_campaign_analytics(self, campaign_id: str) -> dict | None:
        """A campaign's totals: emails_sent_count, bounced_count, reply_count..."""
        result = await self._request("GET", "/campaigns/analytics", params={"id": campaign_id})
        items = self._extract_items(result) if not isinstance(result, dict) or "items" in result else [result]
        if isinstance(result, list):
            items = result
        return items[0] if items and isinstance(items[0], dict) else None

    # ── Account / Warmup ──

    async def list_accounts(self) -> list:
        """List connected email accounts."""
        result = await self._request("GET", "/accounts")
        return self._extract_items(result)

    async def check_warmup_status(self, account_id: str) -> dict | None:
        """Check warmup status for an email account."""
        result = await self._request("GET", f"/accounts/{account_id}/warmup")
        return result if isinstance(result, dict) else None
