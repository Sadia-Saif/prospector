"""HeyReach (heyreach.io): the LinkedIn replies in Prospector's inbox.

With HEYREACH_API_KEY set, the Conversations tab reads HeyReach's unified
LinkedIn inbox (every connected LinkedIn sender) and can answer in a
thread from the same LinkedIn account. Reads conversations, lists senders
and campaigns, sends a message the owner wrote; it never starts, stops or
changes a campaign.

Endpoints follow docs.heyreach.io (public API, read 2026-11-21). The key
goes in the X-API-KEY header and nowhere else.
"""

import logging

import httpx

logger = logging.getLogger("harvey.heyreach")

BASE_URL = "https://api.heyreach.io/api/public"
TIMEOUT = 20


class HeyReachClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def _call(self, method: str, path: str, body: dict | None = None):
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.request(method, BASE_URL + path, json=body,
                                     headers={"X-API-KEY": self.api_key, "Accept": "application/json"})
        if r.status_code >= 400:
            raise RuntimeError(f"HeyReach {path} answered {r.status_code}")
        if r.status_code == 204 or not r.content:
            return {}
        return r.json()

    async def check(self) -> bool:
        try:
            await self._call("GET", "/auth/CheckApiKey")
            return True
        except Exception:
            return False

    async def conversations(self, pages: int = 3) -> list[dict]:
        """The latest conversations across every LinkedIn sender, newest first."""
        out, cursor = [], None
        for _ in range(pages):
            data = await self._call("POST", "/inbox/GetConversationsV3",
                                    {"limit": 100, "cursor": cursor, "filters": {}})
            out += [self.conversation(c) for c in data.get("items") or [] if isinstance(c, dict)]
            cursor = data.get("nextCursor")
            if not cursor or not data.get("hasNextPage"):
                break
        return out

    @staticmethod
    def conversation(c: dict) -> dict:
        p = c.get("correspondentProfile") or {}
        acct = c.get("linkedInAccount") or {}
        msgs = []
        for m in c.get("messages") or []:
            if isinstance(m, dict):
                msgs.append({"at": m.get("createdAt") or "", "text": m.get("body") or "",
                             "subject": m.get("subject") or "", "mine": (m.get("sender") or "") == "ME",
                             "inmail": bool(m.get("isInMail"))})
        msgs.sort(key=lambda m: m["at"])
        return {
            "id": str(c.get("id") or ""), "account_id": c.get("linkedInAccountId") or acct.get("id"),
            "account_email": (acct.get("emailAddress") or "").lower(),
            "account_name": " ".join(x for x in (acct.get("firstName"), acct.get("lastName")) if x),
            "campaign_id": c.get("campaignId"),
            "read": bool(c.get("read")), "last_at": c.get("lastMessageAt") or "",
            "preview": c.get("lastMessageText") or "",
            "last_from_me": (c.get("lastMessageSender") or "") == "ME",
            "name": " ".join(x for x in (p.get("firstName"), p.get("lastName")) if x),
            "headline": p.get("headline") or p.get("position") or "",
            "company": p.get("companyName") or "", "profile_url": p.get("profileUrl") or "",
            "image": p.get("imageUrl") or "", "messages": msgs,
        }

    async def campaigns(self) -> dict:
        """{campaign id: name} for every HeyReach campaign."""
        names, offset = {}, 0
        for _ in range(5):
            data = await self._call("POST", "/campaign/GetAll", {"offset": offset, "limit": 100})
            items = data.get("items") or []
            names.update({c.get("id"): c.get("name") or "" for c in items if isinstance(c, dict)})
            offset += len(items)
            if not items or offset >= (data.get("totalCount") or 0):
                break
        return names

    async def send(self, account_id: int, conversation_id: str, message: str) -> None:
        await self._call("POST", "/inbox/SendMessage", {
            "message": message, "subject": "", "conversationId": conversation_id,
            "linkedInAccountId": int(account_id)})
