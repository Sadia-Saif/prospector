"""Team member accounts for the shared Xorora Prospector.

Each person signs up once with their name, work email and a password, then
signs in with email and password. A member sees and runs only their own
campaigns, and a read-only list of teammates' campaigns by name. The full
dashboard (Settings, keys, everyone's contacts) stays behind the admin link.

Stored in the settings table:
- ``team_members``: [{id, name, email, salt, hash, created_at}]
- ``team_sessions``: {sha256(session token): {member_id, created_at}}

Passwords are PBKDF2-SHA256 with a per-member salt; session tokens are random
and only their hash is stored. Signing up needs the team invite code, which
rides in the team link's #fragment (never in a URL the server logs).
"""

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

MEMBERS_KEY = "team_members"
SESSIONS_KEY = "team_sessions"
PBKDF2_ROUNDS = 200_000
MIN_PASSWORD = 8
MAX_SESSIONS = 500
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Failed sign-ins per email: 5 within 10 minutes locks that email for 10 minutes.
_failures: dict[str, list[float]] = {}
LOCK_AFTER = 5
LOCK_WINDOW = 600


class MemberError(ValueError):
    """Shown to the person as-is."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def clean_email(raw) -> str:
    email = str(raw or "").strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        raise MemberError("Enter a valid email address.")
    return email


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt),
                               PBKDF2_ROUNDS).hex()


def _token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_or_create_invite(path: Path) -> str:
    path = Path(path)
    if path.is_file():
        code = path.read_text(encoding="utf-8").strip()
        if code:
            return code
    path.parent.mkdir(parents=True, exist_ok=True)
    code = secrets.token_urlsafe(24)
    path.write_text(code + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return code


def invite_ok(presented, invite: str) -> bool:
    return bool(presented) and hmac.compare_digest(
        str(presented).encode("utf-8", "replace"), invite.encode("utf-8"))


async def _load(state, key, default):
    raw = await state.get_setting(key)
    try:
        data = json.loads(raw) if raw else default
    except (json.JSONDecodeError, TypeError):
        data = default
    return data if isinstance(data, type(default)) else default


async def load_members(state) -> list[dict]:
    return [m for m in await _load(state, MEMBERS_KEY, []) if isinstance(m, dict) and m.get("id")]


def public(member: dict) -> dict:
    return {"id": member["id"], "name": member["name"], "email": member["email"]}


async def signup(state, *, name: str, email: str, password: str) -> tuple[dict, str]:
    from harvey import team
    name = team.clean_name(name, 60)
    if not name:
        raise MemberError("Enter your name.")
    email = clean_email(email)
    if len(password or "") < MIN_PASSWORD:
        raise MemberError(f"Choose a password of at least {MIN_PASSWORD} characters.")
    members = await load_members(state)
    if any(m["email"] == email for m in members):
        raise MemberError("There's already an account for this email. Sign in instead.")
    if any(m["name"].lower() == name.lower() for m in members):
        raise MemberError("Someone on the team already uses this name. Add your surname.")
    salt = secrets.token_hex(16)
    member = {"id": uuid.uuid4().hex[:12], "name": name, "email": email,
              "salt": salt, "hash": _hash(password, salt), "created_at": _now()}
    members.append(member)
    await state.set_setting(MEMBERS_KEY, json.dumps(members))
    return member, await _new_session(state, member["id"])


def _locked(email: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failures.get(email, []) if now - t < LOCK_WINDOW]
    _failures[email] = recent
    return len(recent) >= LOCK_AFTER


async def login(state, *, email: str, password: str) -> tuple[dict, str]:
    email = clean_email(email)
    if _locked(email):
        raise MemberError("Too many wrong passwords. Try again in 10 minutes.")
    member = next((m for m in await load_members(state) if m["email"] == email), None)
    if not member or not hmac.compare_digest(_hash(password or "", member["salt"]), member["hash"]):
        _failures.setdefault(email, []).append(time.monotonic())
        raise MemberError("Email or password is wrong.")
    _failures.pop(email, None)
    return member, await _new_session(state, member["id"])


async def _new_session(state, member_id: str) -> str:
    token = secrets.token_urlsafe(32)
    sessions = await _load(state, SESSIONS_KEY, {})
    sessions[_token_key(token)] = {"member_id": member_id, "created_at": _now()}
    if len(sessions) > MAX_SESSIONS:  # drop the oldest
        for k in sorted(sessions, key=lambda k: sessions[k].get("created_at", ""))[:len(sessions) - MAX_SESSIONS]:
            sessions.pop(k, None)
    await state.set_setting(SESSIONS_KEY, json.dumps(sessions))
    return token


async def member_for_token(state, token: str) -> dict | None:
    if not token or len(token) > 200:
        return None
    sessions = await _load(state, SESSIONS_KEY, {})
    s = sessions.get(_token_key(token))
    if not s:
        return None
    return next((m for m in await load_members(state) if m["id"] == s.get("member_id")), None)


async def logout(state, token: str):
    sessions = await _load(state, SESSIONS_KEY, {})
    if sessions.pop(_token_key(token or ""), None) is not None:
        await state.set_setting(SESSIONS_KEY, json.dumps(sessions))


async def remove_member(state, member_id: str):
    """Admin: delete an account and sign it out everywhere."""
    members = [m for m in await load_members(state) if m["id"] != member_id]
    await state.set_setting(MEMBERS_KEY, json.dumps(members))
    sessions = {k: v for k, v in (await _load(state, SESSIONS_KEY, {})).items()
                if v.get("member_id") != member_id}
    await state.set_setting(SESSIONS_KEY, json.dumps(sessions))
