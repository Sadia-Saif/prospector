"""Xorora Prospector Dashboard, local web UI to set up, control, and monitor Xorora Prospector."""

import asyncio
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import aiosqlite
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, Response,
)

logger = logging.getLogger("harvey.dashboard")

from harvey import dashboard_auth as auth  # noqa: E402
from harvey.paths import PROJECT_ROOT  # noqa: E402
from harvey.pidfile import LOCK_FILE, loop_lock_held, pid_alive  # noqa: E402
from harvey.textio import read_user_text  # noqa: E402
DB_PATH = PROJECT_ROOT / "data" / "harvey.db"
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "harvey.yaml"
PID_FILE = PROJECT_ROOT / "data" / "harvey.pid"
LOG_FILE = PROJECT_ROOT / "data" / "harvey.log"
TOKEN_FILE = PROJECT_ROOT / "data" / "dashboard-token"


def _resolve_config_file() -> Path:
    """harvey.local.yaml wins when present, same resolution as config.py's
    loader, so the dashboard doesn't judge setup complete against the
    tracked harvey.yaml template while the real trained config sits in the
    gitignored local override."""
    from harvey.config import _find_config_file, ConfigFileNotFoundError
    try:
        return Path(_find_config_file())
    except ConfigFileNotFoundError:
        return CONFIG_FILE

# No /docs, /redoc or /openapi.json: the API is for this UI only, and every
# route except the static shell needs the token (see dashboard_auth).
app = FastAPI(title="Xorora Prospector Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
token_store = auth.TokenStore(TOKEN_FILE)


# ── Personal data: each signed-in person sees only their own campaigns ──
#
# Once the team has accounts, every dashboard call must come from a signed-in
# member, and the data pages (Today, Outbox, Conversations, Companies,
# Contacts, Sequences, exports) show only the companies that person's own
# campaigns found, and those companies' contacts, emails and replies. Admins
# too. The Team campaigns panel on Campaigns is the one place people see each
# other's progress. A single-user install with no accounts sees everything.

import contextvars  # noqa: E402

_SCOPE: contextvars.ContextVar = contextvars.ContextVar("xp_scope", default=None)
# Paths any signed-in caller reaches without a personal scope check (they hold
# no one's prospect data): the shell, member routes and server status.
_UNSCOPED_PREFIXES = ("/api/member/",)


class Scope:
    """The companies and people one member may see."""

    def __init__(self, member: dict, companies: set[str], prospects: set[str], names: set[str]):
        self.member = member
        self.companies = companies
        self.prospects = prospects
        self.names = names            # campaign owner names that are theirs


def _scope() -> "Scope | None":
    return _SCOPE.get()


def _cw(col: str, joiner: str = "AND", kind: str = "companies") -> tuple[str, tuple]:
    """(" AND col IN (...)", ids) for the caller's scope, or ("", ()) unscoped."""
    sc = _scope()
    if sc is None:
        return "", ()
    ids = sorted(getattr(sc, kind))
    if not ids:
        return f" {joiner} 0", ()
    return f" {joiner} {col} IN ({','.join('?' * len(ids))})", tuple(ids)


def _mine_company(company_id: str) -> bool:
    sc = _scope()
    return sc is None or company_id in sc.companies


def _mine_prospect(prospect_id: str) -> bool:
    sc = _scope()
    return sc is None or prospect_id in sc.prospects


def _owns(req: dict, m: dict) -> bool:
    """A team campaign is this member's: theirs by account, or (for ones
    started before accounts) by the name on it. Admins get no exception."""
    if req.get("member_id"):
        return req.get("member_id") == m["id"]
    owner = str(req.get("owner") or "").strip().lower()
    return bool(owner) and owner in (m["name"].lower(), m["email"].lower(),
                                     m["email"].split("@")[0].replace(".", " ").lower())


async def build_scope(state, m: dict) -> Scope:
    from harvey import team
    mine = [r for r in await team.load_requests(state) if _owns(r, m)]
    companies = {c for r in mine for c in (r.get("company_ids") or []) if c}
    prospects: set[str] = set()
    ids = sorted(companies)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        rows = await query_db(f"SELECT id FROM prospects WHERE company_id IN ({','.join('?' * len(chunk))})",
                              tuple(chunk))
        prospects.update(r["id"] for r in rows)
    names = {str(r.get("owner") or "").strip() for r in mine if r.get("owner")}
    return Scope(m, companies, prospects, names)


class _PersonalScope:
    """Sets the caller's scope for every dashboard API call (inside the guard)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or not path.startswith("/api/") or path.startswith(_UNSCOPED_PREFIXES):
            return await self.app(scope, receive, send)
        from harvey import members
        state = _state()
        try:
            await state.init_db()
            accounts = await members.load_members(state)
        except Exception:
            accounts = []
        if not accounts:
            return await self.app(scope, receive, send)        # single user: everything
        headers = dict((k.decode().lower(), v.decode()) for k, v in scope.get("headers", []))
        m = await members.member_for_token(state, headers.get("x-member-session", ""))
        if not m:
            body = json.dumps({"success": False, "error": "signed_out",
                               "message": "Sign in with your own account to see your campaigns."}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        token = _SCOPE.set(await build_scope(state, m))
        try:
            return await self.app(scope, receive, send)
        finally:
            _SCOPE.reset(token)


app.add_middleware(_PersonalScope)      # inside the guard: runs after the token check
app.add_middleware(auth.DashboardGuard, store=token_store)

# Xorora Prospector process tracking
_harvey_process: subprocess.Popen | None = None
_harvey_started_at: datetime | None = None
_env_lock = asyncio.Lock()


# ── Helpers ──


async def query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return results as list of dicts.

    Never raises: a missing DB file, missing table, or malformed schema
    returns [] so no dashboard route can 500 on an empty install.
    """
    if not DB_PATH.exists():
        return []
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("query_db failed (%s): %s", sql.split(None, 4)[:4], e)
        return []


def _mask_key(key: str) -> str:
    """Mask an API key for display: show first 4 and last 4 chars."""
    if not key or len(key) < 10:
        return "****" if key else ""
    return key[:4] + "****" + key[-4:]


def _read_env_file() -> dict[str, str]:
    """Read .env file and return as dict."""
    env_vars = {}
    if ENV_FILE.exists():
        for line in read_user_text(ENV_FILE).splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


def _env_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    key = stripped.partition("=")[0].strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    return key


def _write_env_file(updates: dict[str, str]):
    """Set keys in .env, keeping every other line (comments, order, other
    keys) as it was.

    Written to a private temporary file in the same directory and swapped
    in atomically, so a crash never leaves a half-written .env and the
    credentials are never readable by other users, even for a moment: the
    result is owner-only (0600 on POSIX) whether .env existed or not. A
    symlinked .env is refused rather than replaced or followed.
    """
    if ENV_FILE.is_symlink():
        raise OSError(f"{ENV_FILE} is a symbolic link; edit it by hand.")
    if ENV_FILE.exists() and not ENV_FILE.is_file():
        raise OSError(f"{ENV_FILE} is not a regular file.")
    lines = (read_user_text(ENV_FILE).splitlines()
             if ENV_FILE.exists() else [])

    out, written = [], set()
    for line in lines:
        key = _env_key(line)
        if key in updates:
            if key in written:
                continue  # a later duplicate would override the new value
            out.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in updates.items() if k not in written]

    fd, tmp = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=str(ENV_FILE.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ENV_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    load_dotenv(str(ENV_FILE), override=True)


def _check_harvey_pid() -> int | None:
    """Check if there's a running Xorora Prospector process from a PID file."""
    global _harvey_process, _harvey_started_at
    if _harvey_process and _harvey_process.poll() is None:
        return _harvey_process.pid
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        # pid_alive, never os.kill(pid, 0): on Windows that terminates the loop.
        # And only while a loop holds the lock: after a crash or a reboot the
        # PID can belong to an unrelated program, which Stop must not signal.
        if pid is not None and pid_alive(pid) and loop_lock_held(LOCK_FILE):
            return pid
        try:
            PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return None


# ── Setup Status ──


@app.get("/api/setup-status")
async def get_setup_status():
    """Check what's configured and what still needs setup."""
    checks = []

    # 1. Venv
    checks.append({
        "id": "venv", "label": "Python virtual environment",
        "done": (PROJECT_ROOT / ".venv").is_dir(),
        "required": True,
        "help": "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e .",
    })

    # 2. Env file
    env_vars = _read_env_file()
    env_exists = ENV_FILE.exists() and bool(env_vars)
    checks.append({
        "id": "env_file", "label": "Environment file (.env)",
        "done": env_exists,
        "required": True,
        "help": "Go to the Settings tab to enter your API keys.",
    })

    # 3. Email provider configured (matches channels.email.provider)
    provider = _current_provider()

    def _has(*keys):
        return all((env_vars.get(k, "") or os.getenv(k, "")).strip() for k in keys)

    if provider == "gmail":
        provider_done = _has("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET") and \
            (PROJECT_ROOT / "data" / "gmail_token.json").is_file()
        provider_help = ("Set GMAIL_CLIENT_ID/SECRET in Settings, then run "
                         "'xorora-prospector gmail auth' in your terminal.")
    elif provider == "microsoft":
        provider_done = _has("M365_CLIENT_ID") and \
            (PROJECT_ROOT / "data" / "m365_token.json").is_file()
        provider_help = ("Set the Microsoft 365 client and tenant IDs in Settings, then run "
                         "'xorora-prospector microsoft auth' in your terminal.")
    elif provider == "smtp":
        provider_done = _has("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD")
        provider_help = "Enter your SMTP host, username, and password in Settings."
    else:
        instantly_key = env_vars.get("INSTANTLY_API_KEY", "") or os.getenv("INSTANTLY_API_KEY", "")
        provider_done = bool(instantly_key) and instantly_key != "your_instantly_api_key_here"
        provider_help = "Enter your Instantly API key in Settings."
    checks.append({
        "id": "email_provider", "label": f"Email provider configured ({provider})",
        "done": provider_done,
        "required": True,
        "help": provider_help,
    })

    # 4. Email verification (needed for addresses to be sendable, not 'guess')
    verifier_set = (_has("REOON_API_KEY") or _has("ZEROBOUNCE_API_KEY") or _has("HUNTER_API_KEY")
                    or _has("PROSPEO_API_KEY") or _has("APOLLO_API_KEY"))
    checks.append({
        "id": "verifier", "label": "Email verification key",
        "done": verifier_set,
        "required": True,
        "help": "Add a Reoon (free 600/mo), ZeroBounce, or Hunter key in Settings, "
                "without one, found emails stay 'guess' and are never sent.",
    })

    # 5. Config valid
    config_valid = False
    config_path = _resolve_config_file()
    if config_path.exists():
        try:
            cfg = yaml.safe_load(read_user_text(config_path))
            company = cfg.get("persona", {}).get("company", "")
            product = cfg.get("product", {}).get("name", "")
            config_valid = company not in ("Your Company", "") and product not in ("Your Product", "")
        except Exception:
            pass
    checks.append({
        "id": "config", "label": f"Xorora Prospector configured ({config_path.name})",
        "done": config_valid,
        "required": True,
        "help": "Train Xorora Prospector on your product. Use the trainer or set up manually through Claude.",
    })

    # 6. Product trained
    product_trained = (PROJECT_ROOT / "skills" / "product_knowledge.md").exists()
    checks.append({
        "id": "product_trained", "label": "Product knowledge trained",
        "done": product_trained,
        "required": True,
        "help": "Run: xorora-prospector train https://yourwebsite.com (or set up through Claude).",
    })

    # 7. LinkedIn (optional)
    linkedin_email = env_vars.get("LINKEDIN_EMAIL", "") or os.getenv("LINKEDIN_EMAIL", "")
    linkedin_pass = env_vars.get("LINKEDIN_PASSWORD", "") or os.getenv("LINKEDIN_PASSWORD", "")
    checks.append({
        "id": "linkedin", "label": "LinkedIn credentials",
        "done": bool(linkedin_email) and bool(linkedin_pass),
        "required": False,
        "help": "Optional. Enter your LinkedIn credentials in Settings to enable LinkedIn prospecting.",
    })

    # 8. Cloudflare (optional)
    cf_id = env_vars.get("CLOUDFLARE_ACCOUNT_ID", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    cf_token = env_vars.get("CLOUDFLARE_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")
    checks.append({
        "id": "cloudflare", "label": "Cloudflare deep crawling",
        "done": bool(cf_id) and bool(cf_token),
        "required": False,
        "help": "Optional. For JavaScript-rendered website crawling during training.",
    })

    # 9. Warmup service: TrulyInbox connected, or another service the user named
    from harvey.integrations import trulyinbox as ti
    try:
        state = _state()
        await state.init_db()
        choice = await ti.get_choice(state)
    except Exception:
        choice = ""
    if _has("TRULYINBOX_API_KEY"):
        choice = "connected"
    checks.append({
        "id": "warmup_service",
        "label": {"connected": "Warmup: TrulyInbox connected",
                  "other": "Warmup: your own warmup service"}.get(choice, "Warm up your mailbox"),
        "done": bool(choice),
        # Instantly warms its own accounts, so there it's a suggestion, not a step.
        "required": provider != "instantly",
        "help": ("A new mailbox needs 2-4 weeks of warmup before cold email, and warmup "
                 "keeps it out of spam after that. We recommend TrulyInbox: connect it and "
                 "Xorora Prospector shows whether your mailbox is warmed."),
        "link": ti.AFFILIATE_URL,
    })

    required_checks = [c for c in checks if c["required"]]
    completed_required = sum(1 for c in required_checks if c["done"])

    return {
        "checks": checks,
        "completed": completed_required,
        "total_required": len(required_checks),
        "percent": int(completed_required / len(required_checks) * 100) if required_checks else 0,
    }


# ── Settings ──


def _current_provider() -> str:
    """Read channels.email.provider from the active config (best-effort)."""
    try:
        cfg = yaml.safe_load(read_user_text(_resolve_config_file())) or {}
        return ((cfg.get("channels") or {}).get("email") or {}).get("provider", "instantly")
    except Exception:
        return "instantly"


@app.get("/api/settings")
async def get_settings():
    """Get current settings, presence flags only for secrets, never raw values."""
    env_vars = _read_env_file()
    all_keys = [
        "INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
        "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
        "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
        "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
        "M365_CLIENT_ID", "M365_TENANT_ID",
        "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY",
        "PROSPEO_API_KEY", "APOLLO_API_KEY", "APOLLO_MONTHLY_CREDITS", "SERPAPI_API_KEY",
        "HEYREACH_API_KEY",
    ]
    for key in all_keys:
        if key not in env_vars:
            env_vars[key] = os.getenv(key, "")

    def is_set(k):
        return bool((env_vars.get(k) or "").strip())

    # Gmail is authorized once xorora-prospector gmail auth has stored a token file.
    gmail_token = (PROJECT_ROOT / "data" / "gmail_token.json").is_file()

    return {
        "provider": _current_provider(),
        # Non-secret values echo back so fields repopulate; secrets are
        # presence-only so keys never leave the box.
        "instantly_api_key_set": is_set("INSTANTLY_API_KEY"),
        "heyreach_api_key_set": is_set("HEYREACH_API_KEY"),
        "linkedin_email": env_vars.get("LINKEDIN_EMAIL", ""),
        "linkedin_password_set": is_set("LINKEDIN_PASSWORD"),
        "cloudflare_account_id": env_vars.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "cloudflare_api_token_set": is_set("CLOUDFLARE_API_TOKEN"),
        "gmail_client_id": env_vars.get("GMAIL_CLIENT_ID", ""),
        "gmail_client_secret_set": is_set("GMAIL_CLIENT_SECRET"),
        "gmail_authorized": gmail_token,
        "smtp_host": env_vars.get("SMTP_HOST", ""),
        "smtp_port": env_vars.get("SMTP_PORT", ""),
        "smtp_username": env_vars.get("SMTP_USERNAME", ""),
        "smtp_password_set": is_set("SMTP_PASSWORD"),
        "imap_host": env_vars.get("IMAP_HOST", ""),
        "imap_port": env_vars.get("IMAP_PORT", ""),
        "imap_username": env_vars.get("IMAP_USERNAME", ""),
        "imap_password_set": is_set("IMAP_PASSWORD"),
        # Microsoft 365: ids aren't secret (no client secret: Xorora Prospector signs
        # in as a public client with PKCE); the token file means signed in.
        "m365_client_id": env_vars.get("M365_CLIENT_ID", ""),
        "m365_tenant_id": env_vars.get("M365_TENANT_ID", ""),
        "m365_authorized": (PROJECT_ROOT / "data" / "m365_token.json").is_file(),
        "reoon_api_key_set": is_set("REOON_API_KEY"),
        "zerobounce_api_key_set": is_set("ZEROBOUNCE_API_KEY"),
        "hunter_api_key_set": is_set("HUNTER_API_KEY"),
        "prospeo_api_key_set": is_set("PROSPEO_API_KEY"),
        "apollo_api_key_set": is_set("APOLLO_API_KEY"),
        "serpapi_api_key_set": is_set("SERPAPI_API_KEY"),
        "apollo_monthly_credits": env_vars.get("APOLLO_MONTHLY_CREDITS", "") or "50",
        "apollo_used": await _apollo_used(),
        "send_published_risky": await _published_risky(),
    }


async def _published_risky() -> bool:
    try:
        from harvey.agents.sender import PUBLISHED_RISKY_KEY
        state = _state()
        await state.init_db()
        return (await state.get_setting(PUBLISHED_RISKY_KEY)) == "1"
    except Exception:
        return False


@app.post("/api/settings/published-risky")
async def set_published_risky(request: Request):
    """Admins: send to a business's own published inbox even when the
    verifier can't confirm it. {"on": true|false}"""
    if (denied := await _admins_only(request)) is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("on"), bool):
        return JSONResponse({"success": False, "message": 'expected {"on": true|false}'}, status_code=400)
    from harvey.agents.sender import PUBLISHED_RISKY_KEY
    state = _state()
    await state.init_db()
    await state.set_setting(PUBLISHED_RISKY_KEY, "1" if body["on"] else "")
    return {"success": True, "on": body["on"]}


async def _apollo_used() -> int:
    try:
        from harvey.integrations.email_finder import apollo_usage
        state = _state()
        await state.init_db()
        return int((await apollo_usage(state))["used"])
    except Exception:
        return 0


async def _admins_only(request: Request):
    """Keys and accounts are shared by the whole team, so only admins change them."""
    from harvey import members
    state = _state()
    await state.init_db()
    m = await members.member_for_token(state, request.headers.get("x-member-session", ""))
    if members.is_admin(m):
        return None
    # A single-user install with no team accounts: the token holder is the admin.
    if not await members.load_members(state):
        return None
    return JSONResponse({"success": False, "message": "Only admins can change keys and accounts.",
                         "error": "Only admins can change keys and accounts."}, status_code=403)


@app.post("/api/settings/env")
async def save_env_settings(request: Request):
    if (denied := await _admins_only(request)) is not None:
        return denied
    """Save environment variables to .env file."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    async with _env_lock:
        updates = {}
        for key in ["INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
                     "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
                     "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                     "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                     "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
                     "M365_CLIENT_ID", "M365_TENANT_ID",
                     "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY",
                     "PROSPEO_API_KEY", "APOLLO_API_KEY", "APOLLO_MONTHLY_CREDITS", "SERPAPI_API_KEY",
                     "HEYREACH_API_KEY"]:
            if key in data and data[key] is not None:
                value = data[key]
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    return JSONResponse({"success": False, "message": f"{key} must be text."},
                                        status_code=400)
                value = str(value)
                # A port Xorora Prospector can't parse would stop it starting at all.
                if key.endswith("_PORT") and value.strip() and not value.strip().isdigit():
                    return JSONResponse({"success": False, "message": f"{key} must be a number."},
                                        status_code=400)
                if len(value) > 4096 or "\x00" in value:
                    return JSONResponse({"success": False, "message": f"{key} is invalid."},
                                        status_code=400)
                # Strip newlines so a crafted value can't inject extra .env entries
                updates[key] = value.replace("\n", " ").replace("\r", " ").strip()
        if updates:
            try:
                _write_env_file(updates)
            except Exception as e:
                logger.warning("Failed to write .env: %s", e)
                return {"success": False, "message": "Could not write .env file."}
    return {"success": True}


@app.post("/api/trulyinbox/connect")
async def trulyinbox_connect(request: Request):
    if (denied := await _admins_only(request)) is not None:
        return denied
    """Check a TrulyInbox key against the sending mailbox, then save it to .env."""
    from harvey import deliverability as d
    from harvey.integrations import trulyinbox as ti
    try:
        data = await request.json()
    except Exception:
        data = None
    key = data.get("key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key.strip() or len(key) > 512:
        return JSONResponse({"success": False, "message": "Paste your TrulyInbox API key."},
                            status_code=400)
    key = key.strip()
    try:
        from harvey.config import load_config
        config = load_config()
        address, warmup = ti.sending_address(config), config.channels.email.warmup
    except Exception:
        address, warmup = "", None
    if not address:
        return JSONResponse({"success": False, "message": ti.NO_ADDRESS}, status_code=400)
    workspace = (_read_env_file().get("TRULYINBOX_WORKSPACE_ID", "")
                 or os.getenv("TRULYINBOX_WORKSPACE_ID", "")).strip()
    ti.forget_rejected(key)
    try:
        client = ti.TrulyInbox(key, workspace)
        health = await client.mailbox_health(address)
    except ti.TrulyInboxError as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=400)
    updates = {"TRULYINBOX_API_KEY": key}
    if not workspace:
        updates["TRULYINBOX_WORKSPACE_ID"] = client.workspace_id
    async with _env_lock:
        try:
            _write_env_file(updates)
        except Exception as e:
            logger.warning("Failed to write .env: %s", e)
            return {"success": False, "message": "Could not write .env file."}
    state = _state()
    await state.init_db()
    await ti.set_choice(state, "connected")
    return {"success": True, "message": d.check_warmup_service(health, warmup).detail}


@app.post("/api/trulyinbox/other")
async def trulyinbox_other(request: Request):
    if (denied := await _admins_only(request)) is not None:
        return denied
    """The user warms their mailbox with another service: stop recommending one."""
    from harvey.integrations import trulyinbox as ti
    state = _state()
    await state.init_db()
    await ti.set_choice(state, "other")
    return {"success": True}


@app.post("/api/settings/test-instantly")
async def test_instantly(request: Request):
    """Test an Instantly API key."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    api_key = str(data.get("api_key", "") or "")
    if not api_key:
        return {"success": False, "message": "No API key provided."}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.instantly.ai/api/v2/accounts",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                return {"success": True, "message": "Connected to Instantly."}
            else:
                return {"success": False, "message": f"API returned {resp.status_code}. Check your key."}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


# ── Companies ──


# The Companies tab shows the best-fit companies, up to this many; the CSV has all.
COMPANIES_TAB_LIMIT = 2000


@app.get("/api/companies")
async def get_companies():
    """Companies with contact counts, fit score and register status, best fit first."""
    where, args = _cw("c.id", "WHERE")
    rows = await query_db(f"""
        SELECT c.*,
            (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id) as contact_count
        FROM companies c {where} ORDER BY c.created_at DESC
    """, args)
    try:
        from harvey.config import load_config
        from harvey.register import build_register
        register = {r["id"]: r for r in await build_register(query_db, load_config())}
        for c in rows:
            r = register.get(c["id"])
            if r:
                c["fit"], c["fit_reasons"] = r["fit"], r["fit_reasons"]
                c["register_status"], c["register_why"] = r["status"], r["why_not_contacted"]
        rows.sort(key=lambda c: c.get("fit", 0), reverse=True)
    except Exception as e:
        # A config that won't load must not take the Companies tab down with it.
        logger.warning(f"Company fit unavailable: {e}")
    return rows[:COMPANIES_TAB_LIMIT]


@app.get("/api/export/companies.csv")
async def export_companies():
    """The company register: every company, why it fits, why it wasn't contacted."""
    from harvey.config import load_config
    from harvey.register import build_register, to_csv
    try:
        text = to_csv([r for r in await build_register(query_db, load_config()) if _mine_company(r.get("id"))])
    except Exception as e:
        logger.warning("Company register export failed: %s", e)
        return PlainTextResponse(f"Company register export failed: {e}", status_code=500)
    return PlainTextResponse(
        "﻿" + text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="company-register.csv"'},
    )


@app.get("/api/companies/{company_id}/profile")
async def get_company_profile(company_id: str):
    """One company's facts for its detail card: the register row plus what
    Xorora Prospector observed on its site (latest value per signal, with the page)."""
    rows = await query_db("SELECT * FROM companies WHERE id = ?", (company_id,))
    if not rows or not _mine_company(company_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    observations = await query_db(
        "SELECT o.signal_code, o.value_text, o.value_num, o.evidence_url, o.observed_at, o.collector, "
        "COALESCE(s.label, o.signal_code) AS label, COALESCE(s.value_type, '') AS value_type "
        "FROM observations o "
        "LEFT JOIN signal_codes s ON s.code = o.signal_code "
        "WHERE o.company_id = ? AND o.prospect_id = '' ORDER BY o.observed_at DESC LIMIT 200",
        (company_id,),
    )
    register = {}
    try:
        from harvey.config import load_config
        from harvey.register import build_register
        register = next(iter(await build_register(query_db, load_config(), company_id)), {})
    except Exception as e:
        logger.warning(f"Company profile register unavailable: {e}")
    confirmed = await query_db(
        "SELECT code, label, value_type, category FROM signal_codes "
        "WHERE status = 'confirmed' ORDER BY category, code")
    return {"company": rows[0], "register": register, "observations": observations,
            "confirmed_signals": confirmed}


@app.get("/api/companies/{company_id}/contacts")
async def get_company_contacts(company_id: str):
    """Get all contacts for a specific company."""
    if not _mine_company(company_id):
        return []
    rows = await query_db(
        "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
        (company_id,),
    )
    return rows


# ── Feedback ──


@app.post("/api/feedback")
async def add_feedback(request: Request):
    """Add a comment/feedback on any entity."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    entity_type = str(data.get("entity_type", "") or "")[:50]
    entity_id = str(data.get("entity_id", "") or "")[:100]
    comment = str(data.get("comment", "") or "").strip()[:4000]
    if not comment:
        return {"success": False, "message": "Comment is required."}
    feedback_id = uuid.uuid4().hex[:12]
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(DB_PATH)) as db:
            # Ensure the table exists so feedback works even on a fresh install
            await db.execute(
                """CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT,
                    entity_id TEXT,
                    comment TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )"""
            )
            await db.execute(
                "INSERT INTO feedback (id, entity_type, entity_id, comment) VALUES (?, ?, ?, ?)",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to save feedback: %s", e)
        return {"success": False, "message": "Could not save feedback."}
    return {"success": True, "id": feedback_id}


@app.get("/api/feedback/{entity_type}/{entity_id}")
async def get_feedback(entity_type: str, entity_id: str):
    """Get feedback for an entity."""
    rows = await query_db(
        "SELECT * FROM feedback WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC",
        (entity_type, entity_id),
    )
    return rows


# ── Xorora Prospector Controls ──


@app.get("/api/harvey/status")
async def get_harvey_status():
    """Check if Xorora Prospector is currently running."""
    pid = _check_harvey_pid()
    started = _harvey_started_at.isoformat() if _harvey_started_at else None
    return {"running": pid is not None, "pid": pid, "started_at": started}


AUTOSTART_FILE = PROJECT_ROOT / "data" / "autostart"


BOUNCE_LIMIT = 0.02      # stop a campaign the moment bounces pass 2%
BOUNCE_MIN_SENT = 25     # below this, one bounce says nothing about the list
BOUNCE_MIN_COUNT = 2     # one bounce alone never stops a campaign


async def _bounce_guard_loop():
    """Every 10 minutes: any campaign whose Instantly bounce rate passes 2%
    is stopped at once, in Instantly and here, and the people running it
    are told on the Today page."""
    while True:
        await asyncio.sleep(600)
        try:
            await _bounce_guard()
        except Exception as e:
            logger.warning(f"Bounce guard: {e}")


async def _campaign_instantly_ids(state, req: dict) -> list[str]:
    from harvey import team
    name = team.campaign_name(req.get("owner") or "", req.get("name") or "")
    async with state._connect() as db:
        async with db.execute(
            "SELECT instantly_campaign_id FROM campaigns WHERE name = ? "
            "AND COALESCE(instantly_campaign_id, '') != ''", (name,)) as cur:
            ids = [r[0] for r in await cur.fetchall()]
    return list(dict.fromkeys(ids + ([req["instantly_campaign_id"]] if req.get("instantly_campaign_id") else [])))


async def _instantly_totals(client, ids: list[str]) -> dict:
    """Sent, opened, replied and bounced across a campaign's Instantly campaigns."""
    t = {"sent": 0, "opened": 0, "replied": 0, "bounced": 0}
    for cid in ids:
        a = await client.get_campaign_analytics(cid) or {}
        t["sent"] += int(a.get("emails_sent_count") or 0)
        t["opened"] += int(a.get("open_count_unique") or a.get("open_count") or 0)
        t["replied"] += int(a.get("reply_count_unique") or a.get("reply_count") or 0)
        t["bounced"] += int(a.get("bounced_count") or 0)
    return t


async def _bounced_leads(client, state, ids: list[str]) -> list[dict]:
    """The addresses Instantly says bounced, with how Xorora Prospector had
    rated each one (verified / risky / guess), so the card shows the cause.
    Each is marked invalid so it is never emailed again."""
    out = []
    for cid in ids:
        try:
            leads = await client.list_leads(cid)
        except Exception as e:
            logger.warning(f"Bounce list: couldn't read leads of {cid}: {e}")
            continue
        for l in leads:
            if l.get("status") != -1:      # Instantly's "bounced" lead status
                continue
            email = (l.get("email") or "").strip().lower()
            if not email:
                continue
            rated = ""
            async with state._connect() as db:
                async with db.execute("SELECT email_status FROM prospects WHERE lower(email) = ?",
                                      (email,)) as cur:
                    row = await cur.fetchone()
                    rated = (row[0] if row else "") or ""
                if row and rated != "invalid":
                    await db.execute("UPDATE prospects SET email_status = 'invalid' WHERE lower(email) = ?",
                                     (email,))
                    await db.commit()
            out.append({"email": email, "company": l.get("company_name")
                        or (l.get("payload") or {}).get("companyName") or "",
                        "was": "invalid" if rated == "invalid" else rated})
    return out


async def _bounce_totals(client, ids: list[str]) -> tuple[int, int]:
    t = await _instantly_totals(client, ids)
    return t["sent"], t["bounced"]


async def _bounce_guard():
    from harvey import team
    from harvey.integrations.instantly import InstantlyClient
    key = _instantly_key()
    if not key:
        return
    client = InstantlyClient(key)
    state = _state()
    await state.init_db()
    for r in await team.load_requests(state):
        if r.get("status") != "active":
            continue
        ids = await _campaign_instantly_ids(state, r)
        if not ids:
            continue
        totals = await _instantly_totals(client, ids)
        bounced_list = await _bounced_leads(client, state, ids) if totals.get("bounced") else []
        # The campaign card shows these: sent, open rate, replies, bounces
        # and which addresses bounced.
        await team.update_request(state, r["id"], instantly_stats={
            **totals, "bounced_emails": bounced_list,
            "at": datetime.now().isoformat(timespec="minutes")})
        sent, bounced = totals["sent"], totals["bounced"]
        base = r.get("bounce_baseline") or {}
        sent -= int(base.get("sent") or 0)
        bounced -= int(base.get("bounced") or 0)
        if sent < BOUNCE_MIN_SENT or bounced < BOUNCE_MIN_COUNT or bounced / max(sent, 1) <= BOUNCE_LIMIT:
            continue
        rate = round(100 * bounced / sent, 1)
        for cid in ids:
            try:
                await client.pause_campaign(cid)
            except Exception as e:
                logger.warning(f"Bounce guard: couldn't pause {cid}: {e}")
        note = (f"Stopped automatically: {rate}% of emails bounced ({bounced} of {sent}), "
                "over the 2% safety limit. Check the list before resuming.")
        await team.update_request(state, r["id"], status="stopped", stop_reason=note)
        # Published-but-unconfirmed inboxes are the likeliest cause: off again.
        from harvey.agents.sender import PUBLISHED_RISKY_KEY
        await state.set_setting(PUBLISHED_RISKY_KEY, "")
        await state.log_action(action_type="escalation", agent="sender", details={
            "campaign_name": team.campaign_name(r.get("owner") or "", r.get("name") or ""),
            "reason": note})
        logger.warning(f"Bounce guard: {r.get('owner')} · {r.get('name')}: {note}")


def mailbox_caps(requests: list[dict]) -> dict[str, int]:
    """Each mailbox's daily cap across EVERY campaign that uses it.

    The cap is per mailbox, not per campaign: two people (or two campaigns)
    who tick the same mailbox share its 30 a day, never 30 each, because
    what protects a domain is how much one mailbox sends in a day."""
    from harvey import team
    caps: dict[str, int] = {}
    for r in requests:
        if r.get("status") == "stopped":
            continue
        limit = min(int(r.get("daily_limit") or team.DEFAULT_DAILY_LIMIT), team.MAX_DAILY_LIMIT)
        for a in r.get("accounts") or []:
            a = a.lower()
            caps[a] = min(caps.get(a, limit), limit)
    return caps


async def _cap_shared_mailboxes(requests: list[dict]):
    """Hold every mailbox to its cap in Instantly itself (the account's own
    daily limit, which Instantly enforces across all campaigns)."""
    key = _instantly_key()
    if not key:
        return
    from harvey.integrations.instantly import InstantlyClient
    client = InstantlyClient(key)
    for email, cap in mailbox_caps(requests).items():
        try:
            await client.set_account_daily_limit(email, cap)
        except Exception as e:
            logger.warning(f"Team: couldn't cap {email} at {cap} a day: {e}")


async def _keep_campaigns_supplied():
    """Every 30 minutes: a live campaign with nobody left to write to, and
    still short of its target, searches again wider, so sending keeps going
    up to each mailbox's daily limit."""
    from harvey import team
    while True:
        await asyncio.sleep(1800)
        try:
            state = _state()
            await state.init_db()
            await _cap_shared_mailboxes(await team.load_requests(state))
            for r in await team.load_requests(state):
                if r.get("status") == "active":
                    # Keep every live Instantly campaign on its leads' local
                    # hours, including batches deployed since the last pass.
                    await _apply_campaign_timezone(r)
                if r.get("status") != "active" or r.get("discovery") in ("queued", "running"):
                    continue
                if int(r.get("written") or 0) >= int(r.get("max_leads") or 0):
                    continue
                last = r.get("discovered_at") or r.get("created_at") or ""
                try:
                    age = (datetime.now() - datetime.fromisoformat(last)).total_seconds()
                except ValueError:
                    age = 1e9
                if age < 6 * 3600 or int(r.get("rounds") or 0) >= 10:
                    continue
                if (await _team_stats(state, r))["ready"] == 0:
                    await team.update_request(state, r["id"], discovery="queued",
                                              discovery_note="Looking further for more companies…")
            _kick_team_discovery()
        except Exception as e:
            logger.warning("Campaign top-up failed: %s", e)


@app.on_event("startup")
async def _autostart_agent():
    """Bring the agent back after the dashboard restarts (a deploy, a reboot),
    unless someone stopped it on purpose."""
    asyncio.get_running_loop().create_task(_keep_campaigns_supplied())
    asyncio.get_running_loop().create_task(_bounce_guard_loop())
    asyncio.get_running_loop().create_task(_outbox_warm_loop())
    # A restart stops any company search mid-way: run those again.
    try:
        from harvey import team
        state = _state()
        await state.init_db()
        for r in await team.load_requests(state):
            if r.get("discovery") == "running":
                await team.update_request(state, r["id"], discovery="queued",
                                          discovery_note="Finding companies…")
        _kick_team_discovery()
    except Exception as e:
        logger.warning("Couldn't resume campaign searches: %s", e)
    if AUTOSTART_FILE.exists():
        try:
            await start_harvey()
        except Exception as e:
            logger.warning("Autostart failed: %s", e)


@app.post("/api/harvey/start")
async def start_harvey():
    """Start Xorora Prospector's heartbeat loop as a subprocess."""
    global _harvey_process, _harvey_started_at
    try:
        AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART_FILE.write_text("1\n", encoding="utf-8")
    except OSError:
        pass

    if _check_harvey_pid():
        return {"success": False, "message": "Xorora Prospector is already running."}
    from harvey.main import _needs_setup
    if _needs_setup():
        return {"success": False,
                "message": "Xorora Prospector isn't set up yet. Run `xorora-prospector setup` in a terminal first."}

    # Ensure data dir exists
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

    # The native Claude CLI installer puts claude in ~/.local/bin without
    # always adding it to PATH; the heartbeat can't think without it.
    env = os.environ.copy()
    local_bin = Path.home() / ".local" / "bin"
    if not shutil.which("claude") and local_bin.is_dir():
        env["PATH"] = str(local_bin) + os.pathsep + env.get("PATH", "")

    try:
        log_handle = open(LOG_FILE, "a", encoding="utf-8")
        try:
            _harvey_process = subprocess.Popen(
                [sys.executable, "-m", "harvey", "run"],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
                env=env,
            )
        finally:
            # Child holds its own copies of the fds; don't leak ours.
            log_handle.close()
    except Exception as e:
        logger.warning("Failed to start Xorora Prospector: %s", e)
        return {"success": False, "message": f"Failed to start Xorora Prospector: {e}"}
    _harvey_started_at = datetime.now()
    # The loop writes its own PID file once it holds the loop lock. Writing
    # the child's PID here would overwrite a running loop's record if this
    # child then finds the lock taken and exits.
    return {"success": True, "pid": _harvey_process.pid}


@app.post("/api/harvey/stop")
async def stop_harvey():
    """Stop the Xorora Prospector subprocess."""
    global _harvey_process, _harvey_started_at
    AUTOSTART_FILE.unlink(missing_ok=True)

    pid = _check_harvey_pid()
    if not pid:
        return {"success": False, "message": "Xorora Prospector is not running."}

    own = _harvey_process if _harvey_process and _harvey_process.pid == pid else None

    def alive() -> bool:
        # Our own child is reaped by poll(); left unreaped it is a zombie,
        # which still answers signal 0.
        if own is not None:
            return own.poll() is None
        return pid_alive(pid)

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for graceful shutdown
        for _ in range(10):
            if not alive():
                break
            await asyncio.sleep(0.5)
        else:
            # Force kill if still running (Windows has no SIGKILL; SIGTERM
            # there is already TerminateProcess)
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
            await asyncio.sleep(0.5)
    except OSError as e:
        logger.warning("Couldn't signal Xorora Prospector (pid %s): %s", pid, e)

    if alive():
        # Started by another user, or from an elevated terminal on Windows.
        return {"success": False,
                "message": f"Couldn't stop Xorora Prospector (pid {pid}). Stop it where it was started."}

    _harvey_process = None
    _harvey_started_at = None
    PID_FILE.unlink(missing_ok=True)

    return {"success": True}


@app.get("/api/harvey/logs")
async def get_harvey_logs():
    """Get recent log lines."""
    if not LOG_FILE.exists():
        return {"lines": []}
    try:
        # Tail only the last 64KB so a huge log file never blocks the UI
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", errors="replace")
        lines = text.strip().splitlines()[-100:]
        return {"lines": lines}
    except Exception:
        return {"lines": []}


# ── Pipeline Data (existing endpoints) ──


def _pipeline_bucket(status: str | None, stage: str | None, intent: str | None,
                     email_status: str | None = None) -> str:
    """One funnel stage per contact, from its status and latest conversation."""
    status = status or "new"
    if status in ("new", "queued"):
        return "to_write"
    if status == "contacted":
        # A bounce leaves the status alone and marks the address invalid.
        return "closed_out" if email_status == "invalid" else "contacted"
    if status == "meeting":
        return "ready_to_meet"
    if status == "replied" and stage != "closed_lost":
        if stage == "closing":
            return "ready_to_meet"
        if intent == "interested" or stage in ("presenting", "negotiating"):
            return "interested"
        return "replied"
    return "closed_out"   # lost, opted out, bounced, unwritable


@app.get("/api/stats")
async def get_stats():
    """Pipeline overview stats."""
    try:
        pw, pa = _cw("company_id", "WHERE")
        prospects = await query_db(
            f"SELECT status, COUNT(*) as count FROM prospects {pw} GROUP BY status", pa
        )
        prospect_total = sum(r["count"] for r in prospects)
        prospect_map = {r["status"]: r["count"] for r in prospects}

        campaigns = await query_db(
            "SELECT status, COUNT(*) as count FROM campaigns GROUP BY status"
        )
        campaign_map = {r["status"]: r["count"] for r in campaigns}

        cw, ca = _cw("prospect_id", "WHERE", "prospects")
        conversations = await query_db(
            f"SELECT status, COUNT(*) as count FROM conversations {cw} GROUP BY status", ca
        )
        convo_map = {r["status"]: r["count"] for r in conversations}

        # Where each contact stands. Prospect status alone stops at
        # "replied"; how a reply went lives on its latest conversation.
        sw, sa = _cw("p.company_id", "WHERE")
        rows = await query_db(f"""
            SELECT p.status, p.email_status, l.stage, l.intent, COUNT(*) AS count FROM prospects p
            LEFT JOIN (SELECT prospect_id, stage, intent, ROW_NUMBER() OVER (
                         PARTITION BY prospect_id ORDER BY updated_at DESC, created_at DESC) AS rn
                       FROM conversations) l ON l.prospect_id = p.id AND l.rn = 1
            {sw}
            GROUP BY p.status, p.email_status, l.stage, l.intent
        """, sa)
        pipeline = dict.fromkeys(
            ("to_write", "contacted", "replied", "interested", "ready_to_meet", "closed_out"), 0)
        for r in rows:
            pipeline[_pipeline_bucket(r["status"], r["stage"], r["intent"],
                                      r["email_status"])] += r["count"]

        actions = await query_db("SELECT COUNT(*) as count FROM actions")
        action_count = actions[0]["count"] if actions else 0

        # usage_log rows are keyed by the LOCAL date (increment_usage /
        # get_usage_today use date.today()); SQLite's date('now') is UTC and
        # can select a different day's counts on either side of UTC midnight.
        usage = await query_db(
            "SELECT claude_calls, codex_calls FROM usage_log WHERE date = ?",
            (date.today().isoformat(),),
        )
        usage_today = (usage[0]["claude_calls"] or 0) if usage else 0
        codex_today = (usage[0]["codex_calls"] or 0) if usage else 0

        return {
            "prospects": {"total": prospect_total, "by_status": prospect_map},
            "campaigns": {"total": sum(campaign_map.values()), "by_status": campaign_map},
            "conversations": {"total": sum(convo_map.values()), "by_status": convo_map},
            "pipeline": pipeline,
            "actions_total": action_count,
            "claude_calls_today": usage_today,
            "codex_calls_today": codex_today,
            "brain_provider": _active_provider(),
            "brain_status": await _brain_status(),
        }
    except Exception as e:
        return {"error": str(e)}


_USAGE_SUM = (
    "COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS calls, "
    "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
    "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
    "ROUND(COALESCE(SUM(cost_usd), 0), 4) AS cost_usd"
)

_quota_clients: dict = {}


def _active_brain():
    """The brain config harvey.yaml selects (None if unreadable)."""
    try:
        from harvey.config import load_brain_config
        return load_brain_config()
    except Exception:
        return None


async def _brain_status() -> dict:
    """Last recorded brain health transition ({} if none): lets the
    dashboard say "Codex paused: <why>" instead of looking healthy."""
    from harvey.brain import BRAIN_STATUS_KEY, active_brain_status
    rows = await query_db("SELECT value FROM settings WHERE key = ?", (BRAIN_STATUS_KEY,))
    # Only the configured provider's health (a stale Codex pause isn't
    # Claude's).
    return active_brain_status(rows[0]["value"], _active_provider()) if rows else {}


def _active_provider() -> str:
    brain = _active_brain()
    return brain.provider if brain else ""


def _quota_client_for(provider: str, model: str = ""):
    """One cached quota reader per provider (and Codex model). Each reads
    only its own subscription, so a Claude gauge is never shown as Codex or
    vice versa, and Codex shows the bucket metering the configured model."""
    key = (provider, model if provider == "codex" else "")
    if key not in _quota_clients:
        if provider == "codex":
            from harvey.codex import CodexClient, CodexQuotaClient
            _quota_clients[key] = CodexQuotaClient(CodexClient(model=model))
        elif provider == "claude":
            from harvey.integrations.quota import QuotaClient
            _quota_clients[key] = QuotaClient()
        else:
            return None
    return _quota_clients[key]


@app.get("/api/usage")
async def get_usage():
    """Token/cost accounting + live subscription quota for the Usage tab."""

    totals = {}
    for label, where in (
        ("today", "date(created_at) = date('now')"),
        ("week", "created_at >= datetime('now', '-7 days')"),
        ("month", "created_at >= datetime('now', '-30 days')"),
    ):
        rows = await query_db(f"SELECT {_USAGE_SUM} FROM usage_events WHERE {where}")
        totals[label] = rows[0] if rows else {}

    def grouped(expr, alias):
        return (
            f"SELECT {expr} AS {alias}, {_USAGE_SUM} FROM usage_events "
            f"WHERE created_at >= datetime('now', '-30 days') "
            f"GROUP BY {alias} ORDER BY output_tokens DESC LIMIT 25"
        )

    by_agent = await query_db(grouped("CASE WHEN agent = '' THEN 'other' ELSE agent END", "agent"))
    by_task = await query_db(grouped("CASE WHEN task = '' THEN 'other' ELSE task END", "task"))
    by_model = await query_db(grouped("CASE WHEN model = '' THEN 'unknown' ELSE model END", "model"))
    by_provider = await query_db(grouped("provider", "provider"))
    by_day = await query_db(
        f"SELECT date(created_at) AS day, {_USAGE_SUM} FROM usage_events "
        f"WHERE created_at >= datetime('now', '-30 days') "
        f"GROUP BY day ORDER BY day ASC"
    )

    brain = _active_brain()
    provider = brain.provider if brain else ""
    quota = None
    try:
        client = _quota_client_for(provider, brain.model if brain else "")
        if client is not None:
            quota = await client.get_utilization()
    except Exception as e:
        logger.debug("Quota lookup failed: %s", e)

    return {
        "provider": provider,
        "quota": quota,
        # Whose quota this is, even when it couldn't be read, so the UI shows
        # "unavailable" for the active provider instead of hiding the gauge.
        "quota_provider": provider,
        "quota_status": ("available" if quota else "unavailable") if provider else "",
        "by_provider": by_provider,
        "totals": totals,
        "by_day": by_day,
        "by_agent": by_agent,
        "by_task": by_task,
        "by_model": by_model,
    }


@app.get("/api/prospects")
async def get_prospects():
    where, args = _cw("company_id", "WHERE")
    rows = await query_db(f"SELECT * FROM prospects {where} ORDER BY created_at DESC LIMIT 200", args)
    # Someone whose opt-out was undone keeps a way to see what happened.
    undone = {r["pid"] for r in await query_db(
        "SELECT DISTINCT json_extract(details_json, '$.prospect_id') AS pid FROM actions "
        "WHERE action_type = 'opt_out_undone' AND json_valid(details_json)")}
    for r in rows:
        r["opt_out_undone"] = r.get("id") in undone
    return rows


def _state():
    from harvey.state import StateManager
    return StateManager(db_path=str(DB_PATH))


# What a reviewer needs next to a draft to check its claims against the facts.
OUTBOX_CONTEXT_SIGNALS = (
    "TECH_STACK", "INCUMBENT_AGENCY", "HIRING_ROLE", "RUNNING_GOOGLE_ADS", "NO_ONLINE_BOOKING",
)


async def _mine_outbox(state, item_id: str) -> bool:
    if _scope() is None:
        return True
    item = await state.get_outbox_item(item_id)
    return bool(item) and _mine_prospect(item.get("prospect_id"))


def _in(ids) -> str:
    return ",".join("?" for _ in ids)


async def _with_context(items: list[dict]) -> list[dict]:
    """Attach the campaign, company, contact and signals behind each draft."""
    if not items:
        return items
    pids = sorted({i["prospect_id"] for i in items if i.get("prospect_id")})
    cids = sorted({i["campaign_id"] for i in items if i.get("campaign_id")})

    people = {r["id"]: r for r in (await query_db(
        "SELECT p.id, p.first_name, p.last_name, p.title, p.source, p.email_status, "
        "p.score, p.personalization_notes, p.company_id, c.name AS company_name, "
        "c.domain, c.website, c.description, c.industry AS found_by "
        "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id "
        f"WHERE p.id IN ({_in(pids)})", tuple(pids)) if pids else [])}
    campaigns = {r["id"]: r["name"] for r in (await query_db(
        f"SELECT id, name FROM campaigns WHERE id IN ({_in(cids)})",
        tuple(cids)) if cids else [])}

    company_ids = sorted({p["company_id"] for p in people.values() if p.get("company_id")})
    signals: dict[str, dict[str, str]] = {}
    if company_ids:
        rows = await query_db(
            "SELECT o.company_id, o.signal_code, o.value_num, o.value_text, "
            "o.observed_at, o.run_id, "
            "COALESCE(NULLIF(sc.label, ''), o.signal_code) AS label FROM observations o "
            "LEFT JOIN signal_codes sc ON sc.code = o.signal_code "
            f"WHERE o.company_id IN ({_in(company_ids)}) "
            f"AND o.signal_code IN ({_in(OUTBOX_CONTEXT_SIGNALS)}) "
            "ORDER BY o.observed_at, o.id",
            (*company_ids, *OUTBOX_CONTEXT_SIGNALS),
        )
        # Oldest first, so the latest observation wins. A signal can be seen
        # several times at once (one HIRING_ROLE row per open role): every
        # value from the latest sighting shows, not just one of them.
        latest: dict[tuple, tuple] = {}
        for r in rows:
            if r["value_text"]:
                value = r["value_text"]
            elif r["value_num"] is None:
                value = ""
            else:
                value = "yes" if r["value_num"] else "no"
            key = (r["company_id"], r["label"])
            seen = (r["run_id"] or r["observed_at"])
            prev_seen, values = latest.get(key, (None, []))
            if seen != prev_seen:
                values = []
            if value not in values:
                values = values + [value]
            latest[key] = (seen, values)
        for (company_id, label), (_, values) in latest.items():
            signals.setdefault(company_id, {})[label] = ", ".join(v for v in values if v)

    for item in items:
        p = people.get(item.get("prospect_id")) or {}
        item["context"] = {
            "campaign": campaigns.get(item.get("campaign_id"), ""),
            "company": p.get("company_name", ""),
            "website": p.get("website") or (f"https://{p['domain']}" if p.get("domain") else ""),
            "description": p.get("description", ""),
            "found_by": p.get("found_by", ""),
            "contact": " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x),
            "contact_title": p.get("title", ""),
            "contact_source": p.get("source", ""),
            "email_status": p.get("email_status", ""),
            "score": p.get("score"),
            "notes": p.get("personalization_notes", ""),
            "signals": signals.get(p.get("company_id"), {}),
        }
    return items


_OUTBOX_CACHE: dict = {}


def _outbox_client():
    from harvey.integrations.instantly import InstantlyClient
    key = _instantly_key()
    return InstantlyClient(key) if key else None


_REFRESHING: set = set()


async def _cached(key: str, fetch, seconds: int = 300, wait: bool = False):
    """Instantly reads for the Outbox, kept a few minutes so the page stays fast.

    Past its age, a read already held is returned at once and refreshed in
    the background, so the page never waits on Instantly once it has data."""
    import time
    hit = _OUTBOX_CACHE.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    if hit and not wait:
        if key not in _REFRESHING:
            _REFRESHING.add(key)

            async def refresh():
                try:
                    await _cached(key, fetch, seconds, wait=True)
                finally:
                    _REFRESHING.discard(key)
            asyncio.get_running_loop().create_task(refresh())
        return hit[1]
    try:
        value = await fetch()
    except Exception as e:
        logger.warning(f"Outbox: reading Instantly failed ({key}): {e}")
        if hit:
            # Keep showing what was there; try again sooner than a full wait.
            _OUTBOX_CACHE[key] = (time.time() - seconds + 60, hit[1])
            return hit[1]
        empty = {} if key == "sigs" else "" if key.startswith("img:") else []
        # Remember the miss for a minute: retrying at once against a busy
        # Instantly only makes every page load wait on its back-off.
        _OUTBOX_CACHE[key] = (time.time() - seconds + 60, empty)
        return empty
    if key.startswith("sent:") and hit:
        # An email that went out never "un-sends": keep every one seen before,
        # even if a later read comes back short.
        seen = {(e.get("lead_email"), e.get("sent_at"), e.get("subject")) for e in value}
        value = value + [e for e in hit[1]
                         if (e.get("lead_email"), e.get("sent_at"), e.get("subject")) not in seen]
        value.sort(key=lambda e: e.get("sent_at") or "")
    _OUTBOX_CACHE[key] = (time.time(), value)
    return value


async def _outbox_warm_loop():
    """Keep the Outbox's Instantly reads warm: right after start, then every
    4 minutes, so opening the Outbox never waits on Instantly."""
    await asyncio.sleep(20)
    while True:
        try:
            await outbox_instantly()
            await get_inbox()
            if _heyreach_client():
                await get_linkedin_inbox()
        except Exception as e:
            logger.warning(f"Outbox: warming failed: {e}")
        await asyncio.sleep(240)


@app.get("/api/outbox/instantly")
async def outbox_instantly():
    """Everything sent through Instantly, by person, then campaign, then
    lead and the emails that lead gets. Admins see the whole team's; everyone
    else only their own."""
    from harvey import team, members
    from harvey.agents.sender import Sender
    state = _state()
    await state.init_db()
    sc = _scope()
    see_all = sc is None or members.is_admin(sc.member)
    from harvey.config import load_config, compliance_footer_lines
    from harvey.mailhtml import fetch_image, safe_email_html, text_to_html

    async def _image(url):
        return await _cached("img:" + url, lambda: fetch_image(url), seconds=86400)
    out = []
    client = _outbox_client()
    sigs = await _cached("sigs", lambda: client.account_signatures()) if client else {}
    footer = "\n".join(compliance_footer_lines(load_config()))
    for r in await team.load_requests(state):
        if not see_all and not _owns(r, sc.member):
            continue
        name = team.campaign_name(r.get("owner") or "", r.get("name") or "")
        # What Instantly really sent, by lead, so the Outbox shows each email
        # exactly as it went out: the mailbox's own signature and footer too.
        sent: dict[str, list] = {}
        if client:
            for cid in await _campaign_instantly_ids(state, r):
                for e in await _cached("sent:" + cid, lambda cid=cid: client.get_sent_emails(cid)):
                    sent.setdefault(e["lead_email"], []).append(e)
        accounts = [a.lower() for a in (r.get("accounts") or [])]
        leads = []
        batches = [c for st in ("active", "paused", "completed")
                   for c in await state.get_campaigns_by_status(st) if c.name == name]
        for c in sorted(batches, key=lambda c: c.created_at, reverse=True):
            for pid in c.prospect_ids:
                p = await state.get_prospect(pid)
                if not p or p.status == "new":
                    continue  # held back (unverified) or not sent yet
                day, emails = 0, []
                went = sorted(sent.get((p.email or "").lower(), []), key=lambda e: e["sent_at"] or "")
                for i, step in enumerate(c.sequence):
                    day += int(step.delay_days or 0)
                    if i < len(went):
                        e = went[i]
                        emails.append({"step": step.step, "day": day, "sent": True,
                                       "subject": e["subject"], "body": e["body"],
                                       "html": await safe_email_html(e.get("html") or "", _image),
                                       "from": e["eaccount"], "sent_at": e["sent_at"]})
                        continue
                    # Not sent yet: as it will go out, signed by the mailbox.
                    sig = next((sigs.get(a) for a in accounts if (sigs.get(a) or {}).get("html")), None) or {}
                    text = Sender._render(None, step.body, p).rstrip()
                    body = text + "\n\n" + (sig.get("text") or "[signature of the sending mailbox]")
                    markup = text_to_html(text) + "<br><br>" + (
                        await safe_email_html(sig["html"], _image) if sig.get("html")
                        else "<i>[signature of the sending mailbox]</i>")
                    if footer:
                        body += "\n\n" + footer
                        markup += "<br><br>" + text_to_html(footer)
                    emails.append({"step": step.step, "day": day, "sent": False, "html": markup,
                                   "subject": Sender._cap(Sender._render(None, step.subject, p)), "body": body})
                leads.append({"id": p.id, "name": p.full_name(), "email": p.email,
                              "company": p.company, "title": p.title, "status": p.status,
                              "sent_at": c.created_at.isoformat() if c.created_at else "",
                              "emails": emails})
        if leads or r.get("instantly_campaign_id"):
            out.append({"id": r.get("id"), "name": name, "owner": r.get("owner"),
                        "campaign": r.get("name"), "status": r.get("status"),
                        "timezone": team.ZONE_LABELS.get(team.request_timezone(r), ""),
                        "stats": r.get("instantly_stats") or {}, "leads": leads})
    people: dict[str, list] = {}
    for c in out:
        people.setdefault(c.get("owner") or "Unknown", []).append(c)
    me = (sc.member.get("name") if sc is not None else "") or ""
    order = sorted(people, key=lambda n: (n != me, n.lower()))   # you first
    return {"campaigns": out, "admin_view": see_all and sc is not None,
            "people": [{"name": n, "you": n == me, "campaigns": people[n]} for n in order]}


# ── Inbox: every email that came back to a Prospector campaign ──
# Replies, auto-replies, out-of-office notes and bounce notices, read from
# Instantly's Unibox for the campaigns Prospector runs, shown as threads
# with the cold emails they answer. Replying sends from the mailbox the
# email came into, in the same thread, with that mailbox's signature.

INBOX_STATUSES = [  # key, label, Instantly interest value
    ("lead", "Lead", None),
    ("interested", "Interested", 1),
    ("meeting_booked", "Meeting booked", 2),
    ("meeting_completed", "Meeting completed", 3),
    ("won", "Won", 4),
    ("out_of_office", "Out of office", 0),
    ("wrong_person", "Wrong person", -2),
    ("not_interested", "Not interested", -1),
    ("lost", "Lost", -3),
]
_STATUS_BY_VALUE = {v: k for k, _, v in INBOX_STATUSES if v not in (None, 0)}
_BOUNCE = re.compile(r"(?i)undeliver|delivery status|delivery has failed|failure notice|returned mail|"
                     r"mail delivery (failed|subsystem)|could not be delivered|address not found")
_AUTO = re.compile(r"(?i)automatic reply|auto[- ]?reply|autoreply|out of (the )?office|"
                   r"away from (the )?office|on leave|on vacation|currently (unavailable|travelling|traveling|away)")


def _inbox_kind(m: dict) -> str:
    who = m.get("from") or ""
    if who.startswith(("mailer-daemon@", "postmaster@")) or _BOUNCE.search(m.get("subject") or ""):
        return "bounce"
    if m.get("auto") or _AUTO.search((m.get("subject") or "") + " " + (m.get("text") or "")[:300]):
        return "auto"
    return "reply"


async def _inbox_scope(state) -> tuple[bool, dict, set]:
    """(sees everything, {Instantly campaign id: Prospector campaign name},
    {ids this caller may read}). Admins read the whole Instantly inbox, like
    Instantly's own Unibox; everyone else only their own campaigns."""
    from harvey import team, members
    sc = _scope()
    see_all = sc is None or members.is_admin(sc.member)
    names, mine = {}, set()
    for r in await team.load_requests(state):
        name = team.campaign_name(r.get("owner") or "", r.get("name") or "")
        ids = await _campaign_instantly_ids(state, r)
        for cid in ids:
            names[cid] = name
        if see_all or _owns(r, sc.member):
            mine.update(ids)
    return see_all, names, mine


async def _inbox_raw(client, cid: str) -> list[dict]:
    """Everything that came back to one Instantly campaign ("*": the whole
    workspace), plus the answers sent by hand from the Unibox or from here."""
    whole = cid == "*"
    camp = None if whole else cid
    got = await _cached("recv:" + cid, lambda: client.list_emails(camp, "received", max_pages=5 if whole else 3),
                        seconds=120)
    mine = await _cached("manual:" + cid, lambda: client.list_emails(camp, "manual", max_pages=2 if whole else 1),
                         seconds=120)
    return list(got) + list(mine)


async def _inbox_messages(client, state) -> tuple[bool, dict, list[dict]]:
    see_all, names, mine = await _inbox_scope(state)
    msgs = []
    if see_all:
        msgs = await _inbox_raw(client, "*")
    else:
        for cid in sorted(mine):
            msgs += await _inbox_raw(client, cid)
    return see_all, names, msgs


async def _instantly_campaign_names(client) -> dict:
    async def fetch():
        return {str(c.get("id")): c.get("name") or "" for c in await client.list_campaigns(limit=100)
                if isinstance(c, dict)}
    return await _cached("campaign_names", fetch, seconds=1800)


async def _inbox_statuses(state) -> dict:
    try:
        return json.loads(await state.get_setting("inbox_status", "{}") or "{}")
    except (ValueError, TypeError):
        return {}


@app.get("/api/inbox")
async def get_inbox():
    client = _outbox_client()
    if not client:
        return {"connected": False, "threads": [], "statuses": []}
    state = _state()
    await state.init_db()
    marks = await _inbox_statuses(state)
    see_all, names, msgs = await _inbox_messages(client, state)
    other = await _instantly_campaign_names(client) if see_all else {}
    threads: dict[str, dict] = {}
    for m in msgs:
        lead = m.get("lead_email") or ""
        if not lead:
            continue
        cid = m.get("campaign_id") or ""
        key = cid + "|" + lead
        t = threads.setdefault(key, {
            "key": key, "lead_email": lead, "campaign_id": cid,
            "campaign": names.get(cid) or other.get(cid) or ("No campaign" if not cid else "Other campaign"),
            "prospector": cid in names, "messages": 0, "unread": False,
            "last_at": "", "kinds": set(), "i_status": None, "has_received": False})
        t["messages"] += 1
        if m.get("received"):
            t["has_received"] = True
            t["kinds"].add(_inbox_kind(m))
            t["unread"] = t["unread"] or m.get("unread")
            if m.get("i_status") not in (None, 0):
                t["i_status"] = m.get("i_status")
        if (m.get("at") or "") >= t["last_at"]:
            t["last_at"] = m.get("at") or ""
            t["subject"] = m.get("subject") or ""
            t["preview"] = (m.get("preview") or "")[:180]
    shown = [t for t in threads.values() if t.pop("has_received")]
    emails = sorted({t["lead_email"] for t in shown})
    people = {}
    if emails:
        async with state._connect() as db:
            async with db.execute(
                    "SELECT email, first_name, last_name, company FROM prospects "
                    f"WHERE LOWER(email) IN ({_in(emails)})", tuple(emails)) as cur:
                for e, f, l, co in await cur.fetchall():
                    people[(e or "").lower()] = {"first_name": f, "last_name": l, "company": co}
    for t in shown:
        kinds = t.pop("kinds")
        t["kind"] = "bounce" if "bounce" in kinds else "reply" if "reply" in kinds else "auto"
        p = people.get(t["lead_email"], {})
        t["name"] = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or ""
        t["company"] = p.get("company") or ""
        status = marks.get(t["key"]) or _STATUS_BY_VALUE.get(t.pop("i_status"))
        if not status:
            status = "out_of_office" if t["kind"] == "auto" else "lead"
        t["status"] = status
    shown.sort(key=lambda t: t["last_at"], reverse=True)
    return {"connected": True, "threads": shown, "admin_view": see_all,
            "statuses": [{"key": k, "label": l} for k, l, _ in INBOX_STATUSES]}


async def _inbox_thread_messages(client, state, cid: str, lead: str) -> list[dict]:
    lead = lead.lower()
    _, _, raw = await _inbox_messages(client, state)
    msgs = [dict(m) for m in raw if m.get("lead_email") == lead and (m.get("campaign_id") or "") == cid]
    if cid:
        for e in await _cached("sent:" + cid, lambda: client.get_sent_emails(cid)):
            if (e.get("lead_email") or "").lower() == lead:
                msgs.append({"id": "", "received": False, "from": e.get("eaccount") or "", "to": lead,
                             "subject": e.get("subject") or "", "html": e.get("html") or "",
                             "text": e.get("body") or "", "at": e.get("sent_at") or "",
                             "eaccount": (e.get("eaccount") or "").lower(), "campaign_email": True})
    msgs.sort(key=lambda m: m.get("at") or "")
    return msgs


async def _inbox_allowed(state, cid: str) -> bool:
    see_all, _, mine = await _inbox_scope(state)
    return see_all or (bool(cid) and cid in mine)


@app.get("/api/inbox/thread")
async def get_inbox_thread(campaign: str, lead: str):
    from harvey.mailhtml import fetch_image, safe_email_html, text_to_html
    client = _outbox_client()
    state = _state()
    await state.init_db()
    if not client or not await _inbox_allowed(state, campaign):
        return _refuse("unknown conversation", 404)

    async def _image(url):
        return await _cached("img:" + url, lambda: fetch_image(url), seconds=86400)
    out = []
    thread_ids = set()
    for m in await _inbox_thread_messages(client, state, campaign, lead):
        markup = m.get("html") or ""
        html_out = (await safe_email_html(markup, _image)) if markup else text_to_html(m.get("text") or "")
        if m.get("thread_id"):
            thread_ids.add(m["thread_id"])
        out.append({"id": m.get("id") or "", "received": bool(m.get("received")),
                    "kind": _inbox_kind(m) if m.get("received") else "sent",
                    "from": m.get("from") or "", "to": m.get("to") or "",
                    "subject": m.get("subject") or "", "at": m.get("at") or "",
                    "eaccount": m.get("eaccount") or "", "html": html_out,
                    "campaign_email": bool(m.get("campaign_email"))})
    # Opening a thread reads it, here and in Instantly's Unibox.
    if any(m.get("received") for m in out):
        for tid in thread_ids:
            asyncio.get_running_loop().create_task(client.mark_thread_read(tid))
    return {"messages": out}


@app.post("/api/inbox/reply")
async def inbox_reply(request: Request):
    """Send the owner's answer in the thread, from the mailbox it came into,
    signed with that mailbox's Instantly signature."""
    import html as _html
    from harvey.integrations.instantly import to_instantly_html
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _refuse("expected JSON", 400)
    cid, lead = str(body.get("campaign") or ""), str(body.get("lead") or "").lower()
    text = str(body.get("text") or "").strip()
    if not text:
        return _refuse("Write a reply first.", 400)
    if len(text) > 20000:
        return _refuse("That reply is too long.", 400)
    client = _outbox_client()
    state = _state()
    await state.init_db()
    if not client or not await _inbox_allowed(state, cid):
        return _refuse("unknown conversation", 404)
    msgs = await _inbox_thread_messages(client, state, cid, lead)
    last = next((m for m in reversed(msgs) if m.get("received") and m.get("id")), None)
    if not last:
        return _refuse("There's no email from them to answer.")
    eaccount = last.get("eaccount") or ""
    if not eaccount:
        return _refuse("Couldn't tell which mailbox this came into.")
    subject = last.get("subject") or ""
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject
    sigs = await _cached("sigs", lambda: client.account_signatures())
    sig = (sigs or {}).get(eaccount) or {}
    when = (last.get("at") or "")[:16].replace("T", " ")
    quote_head = f"On {when} UTC, {last.get('from') or lead} wrote:"
    html_body = to_instantly_html(text)
    if sig.get("html"):
        html_body += "<div><br></div>" + sig["html"]
    html_body += ("<div><br></div><div>" + _html.escape(quote_head) + "</div>"
                  "<blockquote style=\"margin:0 0 0 .8ex;border-left:1px solid #ccc;padding-left:1ex\">"
                  + (last.get("html") or to_instantly_html(last.get("text") or "")) + "</blockquote>")
    plain = text + ("\n\n" + sig["text"] if sig.get("text") else "") + "\n\n" + quote_head + "\n" + "\n".join(
        "> " + line for line in (last.get("text") or "").splitlines())
    sent = await client.reply_to(last["id"], eaccount, subject, plain, html_body)
    if not sent:
        return _refuse("Instantly didn't take the reply. Try again in a minute.", 502)
    for k in [k for k in _OUTBOX_CACHE if k.startswith("manual:")]:
        _OUTBOX_CACHE.pop(k, None)
    await state.log_action("inbox_reply", "dashboard", {"campaign": cid, "from": eaccount})
    return {"success": True, "from": eaccount}


@app.post("/api/inbox/status")
async def inbox_status(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _refuse("expected JSON", 400)
    cid, lead = str(body.get("campaign") or ""), str(body.get("lead") or "").lower()
    status = str(body.get("status") or "")
    value = next((v for k, _, v in INBOX_STATUSES if k == status), "missing")
    if value == "missing":
        return _refuse("unknown status", 400)
    client = _outbox_client()
    state = _state()
    await state.init_db()
    if not client or not await _inbox_allowed(state, cid):
        return _refuse("unknown conversation", 404)
    marks = await _inbox_statuses(state)
    marks[cid + "|" + lead] = status
    await state.set_setting("inbox_status", json.dumps(marks))
    synced = await client.set_interest(lead, value, cid)
    return {"success": True, "synced": synced}


# ── LinkedIn replies (HeyReach) ──

def _heyreach_client():
    from harvey.integrations.heyreach import HeyReachClient
    key = (_read_env_file().get("HEYREACH_API_KEY", "") or os.getenv("HEYREACH_API_KEY", "")).strip()
    return HeyReachClient(key) if key else None


async def _linkedin_visible(client) -> list[dict]:
    """HeyReach conversations this caller may read: admins every sender's,
    a team member those of the LinkedIn account signed up with their email."""
    from harvey import members
    convos = await _cached("li:convos", lambda: client.conversations(), seconds=120)
    sc = _scope()
    if sc is None or members.is_admin(sc.member):
        return convos
    me = (sc.member.get("email") or "").lower()
    return [c for c in convos if c.get("account_email") == me]


@app.get("/api/inbox/linkedin")
async def get_linkedin_inbox():
    client = _heyreach_client()
    if not client:
        return {"connected": False, "threads": []}
    convos = await _linkedin_visible(client)
    names = await _cached("li:campaigns", lambda: client.campaigns(), seconds=1800)
    threads = []
    for c in convos:
        if not any(not m["mine"] for m in c["messages"]) and c["last_from_me"]:
            continue  # only conversations where they wrote back
        threads.append({k: v for k, v in c.items() if k != "messages"} | {
            "key": f"{c['account_id']}|{c['id']}",
            "campaign": (names or {}).get(c.get("campaign_id")) or "",
            "unread": not c.get("read")})
    threads.sort(key=lambda t: t.get("last_at") or "", reverse=True)
    return {"connected": True, "threads": threads}


async def _linkedin_find(client, account: str, cid: str) -> dict | None:
    for c in await _linkedin_visible(client):
        if str(c.get("account_id")) == str(account) and c.get("id") == cid:
            return c
    return None


@app.get("/api/inbox/linkedin/thread")
async def get_linkedin_thread(account: str, id: str):
    client = _heyreach_client()
    c = await _linkedin_find(client, account, id) if client else None
    if not c:
        return _refuse("unknown conversation", 404)
    return {"messages": c["messages"], "account_name": c.get("account_name") or ""}


@app.post("/api/inbox/linkedin/reply")
async def linkedin_reply(request: Request):
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _refuse("expected JSON", 400)
    text = str(body.get("text") or "").strip()
    if not text:
        return _refuse("Write a message first.", 400)
    if len(text) > 8000:
        return _refuse("That message is too long for LinkedIn.", 400)
    client = _heyreach_client()
    c = await _linkedin_find(client, str(body.get("account") or ""), str(body.get("id") or "")) if client else None
    if not c:
        return _refuse("unknown conversation", 404)
    try:
        await client.send(c["account_id"], c["id"], text)
    except Exception as e:
        logger.warning(f"LinkedIn reply failed: {e}")
        return _refuse("HeyReach didn't take the message. Try again in a minute.", 502)
    _OUTBOX_CACHE.pop("li:convos", None)
    state = _state()
    await state.init_db()
    await state.log_action("linkedin_reply", "dashboard", {"account": str(c["account_id"])})
    return {"success": True, "from": c.get("account_name") or "your LinkedIn account"}


@app.post("/api/settings/test-heyreach")
async def test_heyreach(request: Request):
    if (denied := await _admins_only(request)) is not None:
        return denied
    client = _heyreach_client()
    if not client:
        return {"success": False, "message": "Save a HeyReach API key first."}
    ok = await client.check()
    return {"success": ok, "message": "Connected to HeyReach." if ok else "HeyReach refused that key."}


# ── Calls: outcomes logged by hand ──

CALL_OUTCOMES = [
    ("interested", "Interested"), ("meeting_booked", "Meeting booked"),
    ("call_back", "Call back later"), ("not_interested", "Not interested"),
    ("voicemail", "Left voicemail"), ("no_answer", "No answer"),
    ("wrong_number", "Wrong number"), ("gatekeeper", "Reached gatekeeper"),
]
_CALL_FIELDS = {"name": 120, "company": 160, "phone": 40, "email": 200, "notes": 4000}


async def _calls(state) -> list[dict]:
    try:
        rows = json.loads(await state.get_setting("call_log", "[]") or "[]")
    except (ValueError, TypeError):
        rows = []
    return [r for r in rows if isinstance(r, dict)]


@app.get("/api/inbox/calls")
async def get_calls():
    from harvey import members
    state = _state()
    await state.init_db()
    rows = await _calls(state)
    sc = _scope()
    if sc is not None and not members.is_admin(sc.member):
        rows = [r for r in rows if r.get("member_id") == sc.member.get("id")]
    rows.sort(key=lambda r: r.get("at") or "", reverse=True)
    return {"calls": rows, "outcomes": [{"key": k, "label": l} for k, l in CALL_OUTCOMES]}


@app.post("/api/inbox/calls")
async def log_call(request: Request):
    import uuid as _uuid
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _refuse("expected JSON", 400)
    outcome = str(body.get("outcome") or "")
    if outcome not in dict(CALL_OUTCOMES):
        return _refuse("Pick how the call went.", 400)
    row = {k: str(body.get(k) or "").strip()[:n] for k, n in _CALL_FIELDS.items()}
    if not (row["name"] or row["company"] or row["phone"]):
        return _refuse("Add who you called.", 400)
    sc = _scope()
    row.update(id=_uuid.uuid4().hex[:12], outcome=outcome,
               at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
               by=(sc.member.get("name") if sc is not None else "") or "",
               member_id=(sc.member.get("id") if sc is not None else "") or "")
    state = _state()
    await state.init_db()
    rows = (await _calls(state))[-1999:] + [row]
    await state.set_setting("call_log", json.dumps(rows))
    await state.log_action("call_logged", "dashboard", {"outcome": outcome})
    return {"success": True, "call": row}


@app.get("/api/outbox")
async def get_outbox_api():
    """Outbox queue + kill-switch state for the Outbox tab."""
    try:
        state = _state()
        await state.init_db()
        # A send cut off mid-way shows up under Didn't send even when Xorora Prospector
        # isn't running.
        await state.fail_stale_sending_outbox()
        mine = lambda items: [i for i in items if _mine_prospect(i.get("prospect_id"))]  # noqa: E731
        ow, oa = _cw("prospect_id", "AND", "prospects")
        big = 2000 if _scope() is not None else None
        return {
            **_compliance_view(),
            "paused": await state.get_setting("sending_paused"),
            "pending": await _with_context(
                mine(await state.get_outbox(status="pending_review", limit=big or 100))[:100]),
            # Mail going out right now leads the list.
            "approved": (mine(await state.get_outbox(status="sending", limit=big or 10))[:10]
                         + mine(await state.get_outbox(status="approved", limit=big or 50))[:50]),
            "sent": (await query_db(
                f"SELECT * FROM outbox WHERE status = 'sent'{ow} "
                "ORDER BY sent_at DESC LIMIT 25", oa)),
            "failed": (await query_db(
                f"SELECT * FROM outbox WHERE status IN ('failed','rejected','cancelled'){ow} "
                "ORDER BY updated_at DESC LIMIT 25", oa)),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/sending-mode")
async def get_sending_mode():
    """Approval (every email waits for you) or autopilot, and what that means here."""
    from harvey.config import load_config
    from harvey.gate import _booking_prefix, allowed_link_domains
    path = _resolve_config_file()
    try:
        config = load_config(str(path))
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read {path.name}: {e}"}, status_code=500)
    state = _state()
    await state.init_db()
    return {
        "mode": "approval" if config.channels.email.require_approval else "autopilot",
        "provider": config.channels.email.provider,
        "file": path.name,
        "your_domains": sorted(allowed_link_domains(config)),
        "booking_page": _booking_prefix(config),
        "held": await state.count_held_for_review(),
    }


@app.post("/api/sending-mode")
async def set_sending_mode(request: Request):
    """Switch it. Autopilot needs {"mode": "autopilot", "confirm": "autopilot"};
    {"mode": "approval"} works at once and sends back to the Outbox whatever
    autopilot approved that hasn't gone out."""
    from harvey.config import ConfigError, set_require_approval
    try:
        body = await request.json()
    except ValueError:
        body = None
    mode = body.get("mode") if isinstance(body, dict) else None
    if mode not in ("approval", "autopilot"):
        return _refuse('expected {"mode": "approval"} or {"mode": "autopilot", "confirm": "autopilot"}', 400)
    if mode == "autopilot" and body.get("confirm") != "autopilot":
        return _refuse('Type "autopilot" to confirm: Xorora Prospector will send email without asking you.', 400)
    path = _resolve_config_file()
    try:
        set_require_approval(mode == "approval", path)
    except ConfigError as e:
        return _refuse(str(e), 409)
    state = _state()
    await state.init_db()
    requeued = await state.requeue_autopilot_approved() if mode == "approval" else 0
    await state.log_action("sending_mode", "dashboard", {"mode": mode, "file": path.name,
                                                         "requeued": requeued})
    out = {"success": True, "mode": mode}
    if mode == "approval":
        out["requeued"] = requeued
    return out


@app.post("/api/outbox/approve-all")
async def outbox_approve_all():
    try:
        state = _state()
        await state.init_db()
        if _scope() is not None:
            # Only the caller's own drafts: one by one, through the same check.
            n = 0
            for item in await state.get_outbox(status="pending_review", limit=2000):
                if _mine_prospect(item.get("prospect_id")):
                    n += await state.approve_outbox(item["id"]) or 0
            return {"success": True, "approved": n, "held": await state.count_held_for_review()}
        n = await state.approve_outbox()
        # Held ones (possible planted link or leak) are approved one by one.
        return {"success": True, "approved": n, "held": await state.count_held_for_review()}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/approve")
async def outbox_approve(item_id: str):
    try:
        state = _state()
        await state.init_db()
        if not await _mine_outbox(state, item_id):
            return _refuse("That email isn't in your campaigns.", 403)
        n = await state.approve_outbox(item_id)
        return {"success": bool(n)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/edit")
async def outbox_edit(item_id: str, request: Request):
    """Rewrite a draft awaiting review. Never approves it."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    subject = str(body.get("subject") or "").strip()
    text = str(body.get("body") or "").strip()
    try:
        state = _state()
        await state.init_db()
        item = await state.get_outbox_item(item_id)
        if item and not _mine_prospect(item.get("prospect_id")):
            return _refuse("That email isn't in your campaigns.", 403)
        if not item or item.get("status") != "pending_review":
            return JSONResponse({"success": False,
                                 "message": "Only drafts awaiting review can be edited."},
                                status_code=409)
        if item.get("kind") == "reply":
            # Gmail threads a reply by its subject: a new one would land as a
            # fresh, unthreaded email to someone who wrote in. Only the body
            # of a reply is editable; its stored subject is kept exactly.
            subject = item.get("subject") or ""
        if not subject.strip() or not text:
            return JSONResponse({"success": False,
                                 "message": "Subject and body are both required."},
                                status_code=400)
        # The sender re-checks every email before it goes out; an edit it
        # would block is refused now, while the reviewer can still fix it,
        # instead of failing quietly at send time. Content rules only: the
        # recipient isn't something an edit can change.
        from harvey.gate import pre_send_check
        checked = text
        footer = _compliance_view()["footer"]
        if item.get("kind") == "sequence" and footer and not text.rstrip().endswith(footer):
            checked = text.rstrip() + "\n\n" + footer  # as the Sender will send it
        gate = pre_send_check("reviewer@example.com", subject, checked)
        if not gate:
            return JSONResponse({"success": False,
                                 "message": "The sender would have blocked this: "
                                            + "; ".join(gate.reasons)},
                                status_code=400)
        if not await state.edit_outbox_draft(item_id, subject, text):
            return JSONResponse({"success": False,
                                 "message": "Only drafts awaiting review can be edited."},
                                status_code=409)
        await state.log_action("outbox_edited", "dashboard", {
            "outbox_id": item_id, "to_email": item.get("to_email", ""),
            "old_subject": item.get("subject", ""), "old_body": item.get("body", ""),
        })
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


# A hand send is only offered to someone Xorora Prospector could still email: never to
# someone who replied, opted out, bounced or was already contacted.
HAND_SENDABLE_STATUSES = ("new", "queued")
HAND_SENDABLE_EMAIL = ("verified", "risky")


def _harvey_can_send() -> bool:
    """Can Xorora Prospector send email itself (a connected mailbox, or Instantly)?

    Sending by hand from the dashboard is only offered when it can't: with
    two senders, any timing gap between them emails someone twice. If the
    config can't be read, the answer is yes (the safe side: no hand sends).
    """
    try:
        from harvey.config import load_config, load_env
        from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
        config, env = load_config(), load_env()
        from harvey.config import legacy_sending_held
        email = config.channels.email
        if not email.enabled or legacy_sending_held(config):
            return False
        if (email.provider or "").strip().lower() in NATIVE_PROVIDERS:
            provider = get_mail_provider(config, env)
            return bool(provider and provider.is_configured())
        return bool(env.instantly_api_key)
    except Exception:
        return True


def _send_as() -> str:
    """The address Xorora Prospector sends as, so Gmail opens the right account."""
    try:
        from harvey.config import load_config
        return load_config().persona.email or ""
    except Exception:
        return ""


def _compliance_view() -> dict:
    """What the Sender adds under each cold email, so a reviewer sees the
    email as it will leave, and the warning when the address is missing."""
    try:
        from harvey.config import compliance_footer_lines, compliance_notice, load_config
        config = load_config()
        return {"footer": "\n".join(compliance_footer_lines(config)),
                "compliance_notice": compliance_notice(config)}
    except Exception:
        return {"footer": "", "compliance_notice": ""}


async def _harvey_committed_ids() -> set[str]:
    """Everyone Xorora Prospector has approved or sent mail for (Outbox or Instantly).

    Approved mail can be going out at this moment, so these people are past
    the point where a hand send can be recorded safely. So is anyone whose
    send was cut off mid-way: it may have reached them. A draft that only
    awaits review isn't: marking the person sent cancels it.
    """
    from harvey.state import MAYBE_SENT_PREFIX
    ids = {r["prospect_id"] for r in await query_db(
        "SELECT DISTINCT prospect_id FROM outbox WHERE status IN ('approved', 'sending', 'sent') "
        "OR (status = 'failed' AND substr(error, 1, ?) = ?)",
        (len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX))}
    for row in await query_db(
            "SELECT prospect_ids_json FROM campaigns WHERE COALESCE(instantly_campaign_id, '') != ''"):
        try:
            found = json.loads(row.get("prospect_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(found, list):
            ids.update(i for i in found if isinstance(i, str))
    return ids


async def _hand_sent_ids() -> set[str]:
    from harvey.state import MANUAL_SEND_KEY
    rows = await query_db("SELECT key FROM settings WHERE key LIKE ?", (MANUAL_SEND_KEY + "%",))
    return {r["key"][len(MANUAL_SEND_KEY):] for r in rows}


async def _harvey_sent_to(prospect_id: str) -> bool:
    """Did Xorora Prospector itself email this person (native send, or an Instantly campaign)?

    A send in flight, or cut off mid-way, counts: it may have reached them.
    """
    from harvey.state import MAYBE_SENT_PREFIX
    if await query_db(
            "SELECT 1 FROM outbox WHERE prospect_id = ? AND (status IN ('sending', 'sent') "
            "OR (status = 'failed' AND substr(error, 1, ?) = ?)) LIMIT 1",
            (prospect_id, len(MAYBE_SENT_PREFIX), MAYBE_SENT_PREFIX)):
        return True
    for row in await query_db(
            "SELECT prospect_ids_json FROM campaigns WHERE COALESCE(instantly_campaign_id, '') != ''"):
        try:
            if prospect_id in json.loads(row.get("prospect_ids_json") or "[]"):
                return True
        except (json.JSONDecodeError, TypeError):
            continue
    return False


def _refuse(message: str, code: int = 409):
    return JSONResponse({"success": False, "message": message}, status_code=code)


@app.post("/api/prospects/{prospect_id}/sent")
async def mark_prospect_sent(prospect_id: str, request: Request):
    """The user sent this email by hand (e.g. from Gmail): record it, or undo it."""
    from harvey.state import HAND_SENT_REASON, MANUAL_SEND_KEY
    try:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict) or not isinstance(body.get("sent", True), bool):
            return _refuse('expected {"sent": true|false}', 400)
        sent = body.get("sent", True)
        rows = await query_db("SELECT status FROM prospects WHERE id = ?", (prospect_id,))
        if not rows or not _mine_prospect(prospect_id):
            return _refuse("unknown prospect", 404)
        current = rows[0].get("status") or "new"
        state = _state()
        await state.init_db()
        key = MANUAL_SEND_KEY + prospect_id
        marked = bool(await state.get_setting(key))

        if sent:
            if marked:
                return {"success": True, "sent": True, "already": True}
            if current not in HAND_SENDABLE_STATUSES:
                # A reply, opt-out or close is never touched; neither is
                # someone Xorora Prospector already emailed.
                return {"success": True, "sent": True, "status": current}
            # Allowed even when Xorora Prospector can send: recording "I already emailed
            # them" can only ever stop Xorora Prospector sending. Not once Xorora Prospector has
            # approved or sent mail to them, though: that may be going out now.
            # The marker goes down FIRST: a drain that checks after this sees
            # it and cancels; one that checked before only holds an approved
            # row, which the check below then sees.
            await state.set_setting(key, current)
            if prospect_id in await _harvey_committed_ids():
                await state.delete_setting(key)
                return _refuse("Xorora Prospector has already approved or sent an email to this person. "
                               "Check the Outbox.")
            await state.update_prospect_status(prospect_id, "contacted")
            # Anything Xorora Prospector had queued for them must not go out as well.
            changed = await state.cancel_pending_outbox_for_prospect(
                prospect_id, reason=HAND_SENT_REASON)
        else:
            if not marked:
                if current in HAND_SENDABLE_STATUSES:
                    return {"success": True, "sent": False}
                if current == "contacted":
                    # Setting them back would make Xorora Prospector write and send again.
                    return _refuse("This wasn't marked as sent by hand here, so it stays sent.")
                return {"success": True, "sent": True, "status": current}
            if current != "contacted":
                # They replied, opted out or bounced since: that stands.
                return {"success": True, "sent": True, "status": current}
            previous = await state.get_setting(key) or "queued"
            await state.update_prospect_status(
                prospect_id, previous if previous in HAND_SENDABLE_STATUSES else "queued")
            await state.delete_setting(key)
            # What marking sent cancelled comes back for review, never straight out.
            changed = await state.restore_hand_cancelled_outbox(
                prospect_id, reason=HAND_SENT_REASON)
        if sent:
            # Open in Gmail adds today's footer; remember it as Xorora Prospector's text.
            footer = _compliance_view()["footer"]
            await state.remember_footer_lines(footer.split("\n"))
        await state.log_action(
            action_type="manual_send" if sent else "manual_send_undone",
            agent="user",
            details={"prospect_id": prospect_id, "outbox_items": changed},
        )
        return {"success": True, "sent": sent}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/reject")
async def outbox_reject(item_id: str):
    try:
        state = _state()
        await state.init_db()
        if not await _mine_outbox(state, item_id):
            return _refuse("That email isn't in your campaigns.", 403)
        if not await state.reject_outbox_item(item_id):
            return _refuse("That email is already sending or sent, so it can't be rejected.")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/sending/{action}")
async def sending_toggle(action: str):
    if action not in ("pause", "resume"):
        return JSONResponse({"success": False, "message": "unknown action"}, status_code=400)
    try:
        state = _state()
        await state.init_db()
        if action == "pause":
            await state.set_setting("sending_paused", "paused from dashboard")
        else:
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/export/prospects.csv")
async def export_prospects(all: bool = False, min_score: int = 0, email_status: str = ""):
    """Sequencer-ready CSV download of the prospect list."""
    from harvey.state import StateManager
    from harvey.export import export_prospects_csv

    state = StateManager(db_path=str(DB_PATH))
    try:
        await state.init_db()
        statuses = [s.strip() for s in email_status.split(",") if s.strip()] or None
        _, text = await export_prospects_csv(
            state, email_statuses=statuses, min_score=min_score, include_all=all,
            only_ids=_scope().prospects if _scope() is not None else None,
        )
    except Exception as e:
        logger.warning("Prospect export failed: %s", e)
        text = ""
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="prospects.csv"'},
    )


@app.get("/api/campaigns")
async def get_campaigns():
    sc = _scope()
    rows = await query_db("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT "
                          + ("1000" if sc is not None else "100"))
    if sc is not None:
        def _theirs(row):
            try:
                ids = json.loads(row.get("prospect_ids_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                ids = []
            return (row.get("owner") or "") in sc.names or any(i in sc.prospects for i in ids or [])
        rows = [r for r in rows if _theirs(r)][:100]
    harvey_sends = _harvey_can_send()
    send_as = "" if harvey_sends else _send_as()
    footer = _compliance_view()["footer"]
    committed = await _harvey_committed_ids()
    hand_sent = await _hand_sent_ids()
    for row in rows:
        try:
            row["sequence"] = json.loads(row.get("sequence_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["sequence"] = []
        try:
            row["prospect_ids"] = json.loads(row.get("prospect_ids_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["prospect_ids"] = []
        try:
            row["sending_accounts"] = json.loads(row.get("sending_accounts_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            row["sending_accounts"] = []
        row["owner"] = row.get("owner") or ""
        # Who each draft goes to, so the UI can open it in the user's own mail client.
        ids = row["prospect_ids"]
        row["recipients"] = await query_db(
            f"SELECT id, email, first_name, last_name, company, title, status, email_status, "
            f"source, personalization_notes FROM prospects "
            f"WHERE id IN ({','.join('?' * len(ids))}) AND email != ''",
            tuple(ids),
        ) if ids else []
        row["send_as"] = send_as
        # A cold email sent by hand needs the same footer Xorora Prospector adds.
        row["footer"] = footer
        for r in row["recipients"]:
            r["sent_by_hand"] = r["id"] in hand_sent
            # "I already emailed them": always recordable before Xorora Prospector commits.
            r["can_mark_sent"] = (
                r["id"] not in hand_sent and r["id"] not in committed
                and (r.get("status") or "new") in HAND_SENDABLE_STATUSES
            )
            # The Gmail button: only when Xorora Prospector can't send itself.
            r["can_send_by_hand"] = (
                r["can_mark_sent"] and not harvey_sends
                and (r.get("email_status") or "guess") in HAND_SENDABLE_EMAIL
            )
    return rows


# ── Team campaigns: one shared Xorora Prospector, a campaign per person ──


def _instantly_key() -> str:
    key = (_read_env_file().get("INSTANTLY_API_KEY", "")
           or os.getenv("INSTANTLY_API_KEY", "")).strip()
    return "" if key == "your_instantly_api_key_here" else key


@app.get("/api/team/accounts")
async def team_accounts():
    """The Instantly sending accounts a person can pick for their campaign."""
    key = _instantly_key()
    if not key:
        return {"connected": False, "accounts": [],
                "message": "Add your Instantly API key in Settings first."}
    from harvey.integrations.instantly import InstantlyClient
    items = await InstantlyClient(key).list_accounts()
    accounts = []
    for a in items:
        email = str(a.get("email") or "").strip()
        if not email:
            continue
        accounts.append({
            "email": email,
            "domain": email.split("@")[-1].lower(),
            "status": a.get("status"),
            "warmup_status": a.get("warmup_status"),
        })
    accounts.sort(key=lambda a: (a["domain"], a["email"]))
    return {"connected": True, "accounts": accounts}


@app.get("/api/team/options")
async def team_options():
    """Everything the campaign builder's dropdowns need."""
    from harvey import team
    from harvey.collectors.discover import DEFAULT_PROVIDER, provider_menu
    from harvey.config import load_env
    state = _state()
    await state.init_db()
    try:
        providers = [
            {"key": p["key"], "label": p["label"], "cost_note": p["cost_note"],
             "configured": p["configured"]}
            for p in provider_menu(load_env().model_dump())
        ]
    except Exception:
        providers = []
    return {
        "services": await team.load_services(state),
        "headcounts": [h for h in team.HEADCOUNTS if h],
        "providers": providers,
        "default_provider": DEFAULT_PROVIDER,
        "suggest": {"industries": team.INDUSTRY_SUGGESTIONS, "regions": team.REGION_SUGGESTIONS,
                    "titles": team.TITLE_SUGGESTIONS},
    }


@app.post("/api/team/services")
async def team_save_services(request: Request):
    from harvey import team
    try:
        body = await request.json()
        services = body.get("services") if isinstance(body, dict) else None
        if not isinstance(services, list):
            raise ValueError("services must be a list")
    except (ValueError, TypeError) as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    state = _state()
    await state.init_db()
    return {"success": True, "services": await team.save_services(state, services)}


async def _team_stats(state, req: dict) -> dict:
    """Where a campaign stands: companies, contacts, and who's been written to."""
    from harvey import team
    company_ids = list(req.get("company_ids") or [])
    stats = {"companies": len(company_ids), "contacts": 0, "ready": 0}
    if company_ids:
        ids = await state.prospect_ids_for_companies(company_ids)
        stats["contacts"] = len(ids)
        stats["ready"] = len(await team.candidate_prospect_ids(state, req))
    return stats


@app.get("/api/team")
async def team_overview(request: Request):
    """Every team campaign with its progress, and who has campaigns."""
    from harvey import team, members
    state = _state()
    await state.init_db()
    caller = await _caller(state, request)
    out = []
    for r in await team.load_requests(state):
        view = {k: r.get(k) for k in (
            "id", "owner", "name", "accounts", "industries", "regions", "headcount",
            "provider", "max_leads", "daily_limit", "titles", "written", "status", "discovery",
            "discovery_note", "created_at", "stop_reason", "instantly_stats")}
        view["service"] = (r.get("service") or {}).get("name", "")
        view["timezone"] = team.ZONE_LABELS.get(team.request_timezone(r), "")
        view["can_edit"] = _may_change(r, caller)
        view["mine"] = bool(caller) and r.get("member_id") == caller["id"]
        if view["can_edit"]:
            view.update(await _team_stats(state, r))
        else:
            # Someone else's campaign: who runs it and what it targets, never
            # its progress, contacts, mailbox or numbers.
            for k in ("accounts", "max_leads", "daily_limit", "written", "discovery",
                      "discovery_note", "provider", "stop_reason", "instantly_stats"):
                view.pop(k, None)
            view["private"] = True
        out.append(view)
    owners = sorted({r["owner"] for r in out if r.get("owner")})
    _kick_team_discovery()
    return {"team_mode": await team.team_mode(state), "owners": owners,
            "campaigns": out, "me": members.public(caller) if caller else None}


async def _caller(state, request: Request):
    """The signed-in team member making a dashboard call, or None."""
    from harvey import members
    return await members.member_for_token(state, request.headers.get("x-member-session", ""))


def _may_change(req: dict, m: dict | None) -> bool:
    """Admins change any campaign; everyone else only their own."""
    from harvey import members
    if not m:
        return False
    if members.is_admin(m) or req.get("member_id") == m["id"]:
        return True
    # Campaigns started before accounts existed belong to whoever's name is on them.
    if not req.get("member_id"):
        owner = str(req.get("owner") or "").strip().lower()
        return owner in (m["name"].lower(), m["email"].lower(),
                         m["email"].split("@")[0].replace(".", " ").lower())
    return False


@app.post("/api/team/campaign")
async def team_start_campaign(request: Request):
    """Start a personal campaign: who, what service, which companies, which mailbox."""
    state = _state()
    await state.init_db()
    m = await _caller(state, request)
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
    except (ValueError, TypeError) as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    if not m:
        return JSONResponse({"success": False, "error": "Sign in at the team page to start a campaign."},
                            status_code=401)
    return await _create_team_campaign(state, body, member=m)


async def _create_team_campaign(state, body: dict, member: dict | None = None):
    from harvey import team
    from harvey.collectors.discover import PROVIDERS
    try:
        owner = team.clean_name(member["name"] if member else body.get("owner"), 60)
        if not owner:
            raise ValueError("Enter your name.")
        accounts = body.get("accounts") or []
        if isinstance(accounts, str):
            accounts = [accounts]
        accounts = [str(a).strip() for a in accounts if "@" in str(a)]
        if not accounts:
            raise ValueError("Pick the Instantly mailbox to send from.")
        industries = team.clean_list(body.get("industries"))
        if not industries:
            raise ValueError("Add at least one industry.")
        regions = team.clean_list(body.get("regions"))
        if not regions:
            raise ValueError("Add at least one region or city.")
        services = {s["name"]: s for s in await team.load_services(state)}
        service = services.get(str(body.get("service") or ""))
        if not service:
            raise ValueError("Pick the service this campaign offers.")
        headcount = str(body.get("headcount") or "")
        # Always the free source: no keys, no spend, nothing to choose.
        provider = "osm" if "osm" in PROVIDERS else next(iter(PROVIDERS))
        max_spend = 0.0
        max_leads = int(body.get("max_leads") or team.DEFAULT_MAX_LEADS)
        daily_limit = team.clean_daily_limit(body.get("daily_limit"))
        name = team.clean_name(body.get("name")) or (
            f"{service['name'].split(' (')[0]} · {industries[0]} · {regions[0]}")
        titles = team.clean_list(body.get("titles"))
        if member and not titles:
            raise ValueError("Add who to email, e.g. CEO or Founder.")
        titles = titles or team.icp_for(service["name"])["titles"]
    except (ValueError, TypeError) as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)

    existing = await team.matching_company_ids(state, industries, regions)
    req = await team.add_request(
        state, owner=owner, accounts=accounts, name=name, service=service,
        industries=industries, regions=regions, headcount=headcount,
        provider=provider, max_spend=max_spend, max_leads=max_leads,
        company_ids=existing, daily_limit=daily_limit,
        member_id=member["id"] if member else "", titles=titles,
    )
    await _apply_mailbox_limits(accounts, daily_limit)
    _kick_team_discovery()
    return {"success": True, "id": req["id"], "campaign": team.campaign_name(owner, name),
            "existing_companies": len(existing)}


async def _edit_team_campaign(state, request_id: str, body: dict):
    """Change a live campaign. New targeting searches again; contacts already
    emailed stay emailed and are never written to twice."""
    from harvey import team
    req = next((r for r in await team.load_requests(state) if r.get("id") == request_id), None)
    if not req:
        return JSONResponse({"success": False, "error": "No such campaign."}, status_code=404)
    changes: dict = {}
    try:
        if "service" in body:
            services = {s["name"]: s for s in await team.load_services(state)}
            svc = services.get(str(body.get("service") or ""))
            if not svc:
                raise ValueError("Pick the service this campaign offers.")
            changes["service"] = {k: v for k, v in svc.items() if k != "icp"}
        for key, label in (("industries", "industry"), ("regions", "city or region"), ("titles", "person to email")):
            if key in body:
                vals = team.clean_list(body.get(key))
                if not vals:
                    raise ValueError(f"Add at least one {label}.")
                changes[key] = vals
        if "headcount" in body:
            hc = str(body.get("headcount") or "")
            changes["headcount"] = hc if hc in team.HEADCOUNTS else ""
        if "accounts" in body:
            acc = [str(a).strip() for a in (body.get("accounts") or []) if "@" in str(a)]
            if not acc:
                raise ValueError("Pick the Instantly mailbox to send from.")
            changes["accounts"] = acc[:team.MAX_ACCOUNTS]
        if "max_leads" in body:
            changes["max_leads"] = max(1, min(int(body.get("max_leads") or team.DEFAULT_MAX_LEADS),
                                              team.MAX_MAX_LEADS))
        if "daily_limit" in body:
            changes["daily_limit"] = team.clean_daily_limit(body.get("daily_limit"))
        if body.get("name"):
            changes["name"] = team.clean_name(body.get("name"))
    except (ValueError, TypeError) as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    retarget = any(changes.get(k) is not None and changes.get(k) != req.get(k)
                   for k in ("industries", "regions", "headcount"))
    if retarget:
        changes["company_ids"] = await team.matching_company_ids(
            state, changes.get("industries", req.get("industries") or []),
            team.expand_regions(changes.get("regions", req.get("regions") or [])))
        changes.update(discovery="queued", discovery_note="Searching for your new targets…", rounds=0)
    if req.get("status") == "complete" and int(changes.get("max_leads", 0)) > int(req.get("written") or 0):
        changes["status"] = "active"
    await team.update_request(state, request_id, **changes)
    if "daily_limit" in changes or "accounts" in changes:
        accounts = changes.get("accounts", req.get("accounts") or [])
        name = team.campaign_name(req.get("owner") or "", changes.get("name", req.get("name") or ""))
        async with state._connect() as db:
            async with db.execute(
                "SELECT instantly_campaign_id FROM campaigns WHERE name = ? "
                "AND COALESCE(instantly_campaign_id, '') != ''", (name,)) as cur:
                ids = [r[0] for r in await cur.fetchall()]
        if "accounts" in changes and ids and _instantly_key():
            from harvey.integrations.instantly import InstantlyClient
            client = InstantlyClient(_instantly_key())
            for cid in ids:  # a live campaign starts rotating the new mailboxes too
                try:
                    await client.set_campaign_accounts(cid, accounts)
                except Exception as e:
                    logger.warning(f"Team: couldn't update the mailboxes on campaign {cid}: {e}")
        await _apply_mailbox_limits(accounts,
                                    changes.get("daily_limit", team.clean_daily_limit(req.get("daily_limit"))),
                                    ids)
    if "regions" in changes and changes["regions"] != req.get("regions"):
        # New regions can mean a new time zone: live Instantly campaigns
        # switch their sending hours to the leads' new local time.
        await _apply_campaign_timezone({**req, **changes})
    if retarget:
        _kick_team_discovery()
    return {"success": True, "retargeted": retarget}


async def _apply_campaign_timezone(req: dict):
    """Send this campaign's live Instantly campaigns 9 to 5 in its leads' zone."""
    from harvey import team
    key = _instantly_key()
    if not key:
        return
    from harvey.integrations.instantly import InstantlyClient
    client = InstantlyClient(key)
    state = _state()
    name = team.campaign_name(req.get("owner") or "", req.get("name") or "")
    async with state._connect() as db:
        async with db.execute(
            "SELECT instantly_campaign_id FROM campaigns WHERE name = ? "
            "AND COALESCE(instantly_campaign_id, '') != ''", (name,)) as cur:
            ids = [r[0] for r in await cur.fetchall()]
    for cid in ids:
        try:
            await client.update_campaign_schedule(cid, team.request_timezone(req))
        except Exception as e:
            logger.warning(f"Team: couldn't set the time zone on campaign {cid}: {e}")


async def _apply_mailbox_limits(accounts: list[str], limit: int, campaign_ids=()):
    """Set the daily cap on the mailboxes (and any live Instantly campaigns)
    in Instantly itself, so it holds whatever else uses those mailboxes."""
    key = _instantly_key()
    if not key:
        return
    from harvey.integrations.instantly import InstantlyClient
    client = InstantlyClient(key)
    from harvey import team
    caps = mailbox_caps(await team.load_requests(_state()))
    for email in accounts:
        try:
            await client.set_account_daily_limit(email, min(limit, caps.get(email.lower(), limit)))
        except Exception as e:  # best effort: the campaign's own cap still applies
            logger.warning(f"Team: couldn't set the daily limit on {email}: {e}")
    # The limit is per mailbox: a campaign rotating between several sends
    # that many from each.
    for cid in campaign_ids:
        try:
            await client.set_campaign_daily_limit(cid, limit * max(1, len(accounts)))
        except Exception as e:
            logger.warning(f"Team: couldn't set the daily limit on campaign {cid}: {e}")


@app.post("/api/team/campaign/{request_id}/{action}")
async def team_campaign_action(request_id: str, action: str, request: Request,
                               _checked: bool = False):
    from harvey import team
    state = _state()
    await state.init_db()
    if not _checked:
        m = await _caller(state, request)
        req = next((r for r in await team.load_requests(state) if r.get("id") == request_id), None)
        if req and not _may_change(req, m):
            return JSONResponse({"success": False,
                                 "error": "Only the person who started this campaign (or an admin) can change it."},
                                status_code=403)
    if action == "edit":
        return await _edit_team_campaign(state, request_id, await _json_body(request))
    if action == "limit":
        try:
            body = await request.json()
        except Exception:
            body = {}
        limit = team.clean_daily_limit((body or {}).get("daily_limit"))
        req = await team.update_request(state, request_id, daily_limit=limit)
        if not req:
            return JSONResponse({"success": False, "error": "no such campaign"}, status_code=404)
        name = team.campaign_name(req.get("owner") or "", req.get("name") or "")
        async with state._connect() as db:
            async with db.execute(
                "SELECT instantly_campaign_id FROM campaigns WHERE name = ? "
                "AND COALESCE(instantly_campaign_id, '') != ''", (name,)) as cur:
                ids = [r[0] for r in await cur.fetchall()]
        await _apply_mailbox_limits(list(req.get("accounts") or []), limit, ids)
        return {"success": True, "daily_limit": limit}
    if action == "stop":
        await team.update_request(state, request_id, status="stopped")
    elif action == "resume":
        # Count bounces afresh from here, and start its Instantly campaigns again.
        req = next((r for r in await team.load_requests(state) if r.get("id") == request_id), None) or {}
        ids = await _campaign_instantly_ids(state, req) if req else []
        baseline = {}
        if ids and _instantly_key():
            from harvey.integrations.instantly import InstantlyClient
            client = InstantlyClient(_instantly_key())
            sent, bounced = await _bounce_totals(client, ids)
            baseline = {"sent": sent, "bounced": bounced}
            for cid in ids:
                try:
                    await client.activate_campaign(cid)
                except Exception as e:
                    logger.warning(f"Team: couldn't restart Instantly campaign {cid}: {e}")
        await team.update_request(state, request_id, status="active", stop_reason="",
                                  bounce_baseline=baseline)
    elif action == "delete":
        await team.remove_request(state, request_id)
    elif action == "find-more":
        await team.update_request(state, request_id, discovery="queued", discovery_note="")
        _kick_team_discovery()
    else:
        return JSONResponse({"success": False, "error": "unknown action"}, status_code=400)
    return {"success": True}


# ── Team member portal: each person signs in and sees only their own ──

TEAM_INVITE_FILE = PROJECT_ROOT / "data" / "team-invite"
ADMIN_INVITE_FILE = PROJECT_ROOT / "data" / "admin-invite"


def _invite_role(code) -> str:
    """'admin' for the admin link's code, 'member' for the team link's, else ''."""
    from harvey import members
    if members.invite_ok(code, members.load_or_create_invite(ADMIN_INVITE_FILE)):
        return "admin"
    if members.invite_ok(code, members.load_or_create_invite(TEAM_INVITE_FILE)):
        return "member"
    return ""


@app.get("/member", response_class=HTMLResponse)
async def member_page():
    return (WEB_DIR / "member.html").read_text(encoding="utf-8")


async def _member(request: Request):
    from harvey import members
    state = _state()
    await state.init_db()
    token = request.headers.get("x-member-session", "")
    return state, await members.member_for_token(state, token)


def _signin_needed():
    return JSONResponse({"success": False, "error": "signin",
                         "message": "Please sign in again."}, status_code=401)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


@app.post("/api/member/signup")
async def member_signup(request: Request):
    from harvey import members
    body = await _json_body(request)
    role = _invite_role(body.get("invite"))
    if not role:
        return JSONResponse({"success": False, "error": "Open the team link you were sent to create an account."},
                            status_code=403)
    state = _state()
    await state.init_db()
    try:
        member, token = await members.signup(state, name=body.get("name"), email=body.get("email"),
                                             password=str(body.get("password") or ""), role=role)
    except members.MemberError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    return {"success": True, "session": token, "member": members.public(member)}


@app.post("/api/member/login")
async def member_login(request: Request):
    from harvey import members
    body = await _json_body(request)
    state = _state()
    await state.init_db()
    try:
        member, token = await members.login(state, email=body.get("email"),
                                            password=str(body.get("password") or ""))
    except members.MemberError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)
    # Signing in through the admin link makes an existing member an admin.
    if _invite_role(body.get("invite")) == "admin" and not members.is_admin(member):
        member = await members.set_role(state, member["id"], "admin") or member
    return {"success": True, "session": token, "member": members.public(member)}


@app.get("/api/member/admin-token")
async def member_admin_token(request: Request):
    """Admins open the full dashboard with the dashboard's own token."""
    from harvey import members
    state, m = await _member(request)
    if not m:
        return _signin_needed()
    return {"success": True, "token": token_store.get()}


@app.get("/api/member/people")
async def member_people(request: Request):
    from harvey import members
    state, m = await _member(request)
    if not m:
        return _signin_needed()
    if not members.is_admin(m):
        return JSONResponse({"success": False, "error": "Admins only."}, status_code=403)
    return {"success": True, "people": [members.public(x) for x in await members.load_members(state)]}


@app.post("/api/member/people/{member_id}/{action}")
async def member_people_action(member_id: str, action: str, request: Request):
    from harvey import members
    state, m = await _member(request)
    if not m:
        return _signin_needed()
    if not members.is_admin(m):
        return JSONResponse({"success": False, "error": "Admins only."}, status_code=403)
    if member_id == m["id"]:
        return JSONResponse({"success": False, "error": "You can't change your own account here."}, status_code=400)
    if action in ("admin", "member"):
        await members.set_role(state, member_id, action)
    elif action == "remove":
        await members.remove_member(state, member_id)
    else:
        return JSONResponse({"success": False, "error": "unknown action"}, status_code=400)
    return {"success": True}


@app.post("/api/member/logout")
async def member_logout(request: Request):
    from harvey import members
    state = _state()
    await state.init_db()
    await members.logout(state, request.headers.get("x-member-session", ""))
    return {"success": True}


@app.get("/api/member/me")
async def member_me(request: Request):
    from harvey import members
    state, m = await _member(request)
    if not m:
        return _signin_needed()
    return {"success": True, "member": members.public(m)}


@app.get("/api/member/options")
async def member_options(request: Request):
    from harvey import team
    state, m = await _member(request)
    if not m:
        return _signin_needed()
    accounts = await team_accounts()
    used_ind, used_reg = [], []
    for r in await team.load_requests(state):
        used_ind += r.get("industries") or []
        used_reg += r.get("regions") or []
    return {
        "services": await team.load_services(state),
        "headcounts": [h for h in team.HEADCOUNTS if h],
        "accounts": accounts,
        "suggest": {
            "industries": list(dict.fromkeys(team.INDUSTRY_SUGGESTIONS + used_ind)),
            "regions": list(dict.fromkeys(team.REGION_SUGGESTIONS + used_reg)),
            "titles": team.TITLE_SUGGESTIONS,
        },
    }


def _is_mine(req: dict, m: dict) -> bool:
    return bool(m) and req.get("member_id") == m["id"]


@app.get("/api/member/campaigns")
async def member_campaigns(request: Request):
    from harvey import team
    state, m = await _member(request)
    if not m:
        return _signin_needed()
    mine, others = [], []
    for r in await team.load_requests(state):
        stats = await _team_stats(state, r)
        base = {"id": r.get("id"), "owner": r.get("owner"), "name": r.get("name"),
                "service": (r.get("service") or {}).get("name", ""),
                "status": r.get("status"), "discovery": r.get("discovery"),
                "written": r.get("written") or 0, "max_leads": r.get("max_leads") or 0,
                "companies": stats["companies"], "created_at": r.get("created_at"),
                "industries": r.get("industries") or [], "regions": r.get("regions") or [],
                "headcount": r.get("headcount") or ""}
        if _is_mine(r, m):
            base.update({k: r.get(k) for k in ("industries", "regions", "headcount", "accounts",
                                                "titles", "daily_limit", "discovery_note")})
            base.update(stats)
            mine.append(base)
        else:
            others.append({k: base[k] for k in ("id", "owner", "name", "service", "status",
                                                 "industries", "regions", "headcount", "created_at")})
    _kick_team_discovery()
    return {"success": True, "mine": mine, "team": others,
            "agent_running": _check_harvey_pid() is not None}


@app.post("/api/member/campaign")
async def member_start_campaign(request: Request):
    state, m = await _member(request)
    if not m:
        return _signin_needed()
    return await _create_team_campaign(state, await _json_body(request), member=m)


async def _own_request(state, m, request_id):
    from harvey import team
    for r in await team.load_requests(state):
        if r.get("id") == request_id:
            return r if _is_mine(r, m) else None
    return None


@app.post("/api/member/campaign/{request_id}/{action}")
async def member_campaign_action(request_id: str, action: str, request: Request):
    state, m = await _member(request)
    if not m:
        return _signin_needed()
    if not await _own_request(state, m, request_id):
        return JSONResponse({"success": False, "error": "Only the person who started a campaign can change it."},
                            status_code=403)
    return await team_campaign_action(request_id, action, request, _checked=True)


@app.get("/api/member/campaign/{request_id}/contacts")
async def member_campaign_contacts(request_id: str, request: Request):
    state, m = await _member(request)
    if not m:
        return _signin_needed()
    req = await _own_request(state, m, request_id)
    if not req:
        return JSONResponse({"success": False, "error": "Not your campaign."}, status_code=403)
    ids = list(req.get("company_ids") or [])[:2000]
    rows = []
    if ids:
        marks = ",".join("?" * len(ids))
        rows = await query_db(
            f"SELECT first_name, last_name, title, company, email, status FROM prospects "
            f"WHERE company_id IN ({marks}) ORDER BY score DESC LIMIT 300", tuple(ids))
    return {"success": True, "contacts": rows}


_team_worker: "asyncio.Task | None" = None


def _kick_team_discovery():
    """Run queued campaign discoveries one at a time, in the background."""
    global _team_worker
    if _team_worker and not _team_worker.done():
        return
    try:
        _team_worker = asyncio.get_running_loop().create_task(_team_discovery_loop())
    except RuntimeError:
        pass


async def _team_discovery_loop():
    global _discovery_task, _discovery_report, _discovery_kind
    from harvey import team
    from harvey.collectors.discover import build_queries
    from harvey.config import load_config
    from harvey.pipeline import run_prospecting
    state = _state()
    await state.init_db()
    while True:
        queued = [r for r in await team.load_requests(state)
                  if r.get("discovery") == "queued" and r.get("status") == "active"]
        if not queued:
            return
        if _discovery_task and not _discovery_task.done():
            await asyncio.sleep(10)  # a manual run is going; wait our turn
            continue
        req = queued[0]
        await team.update_request(state, req["id"], discovery="running",
                                  discovery_note="Finding companies…")
        try:
            config = load_config().model_copy(deep=True)
            config.icp.industries = list(req.get("industries") or [])
            cities = team.expand_regions(list(req.get("regions") or []))
            config.icp.geography = cities
            if req.get("headcount"):
                config.icp.company_size = req["headcount"] + " employees"
            if req.get("titles"):
                config.icp.titles = list(req["titles"])
            queries = build_queries(config, cities=cities,
                                    depth=30, limit=min(1000, 100 * (int(req.get("rounds") or 0) + 1)))
            async with state._connect() as db:
                async with db.execute("SELECT id FROM companies") as cursor:
                    before = {r[0] for r in await cursor.fetchall()}
            _discovery_kind = "discover"

            async def _run():
                return await run_prospecting(state, config, req.get("provider") or None,
                                             queries, max_spend=0.0)
            # A Stop pressed on an earlier manual run must not block every
            # campaign forever: a campaign starting its round is a fresh start.
            await state.set_setting("discovery_paused", "")
            _discovery_task = asyncio.create_task(_run())
            result = await _discovery_task
            _discovery_report = {**(result.discover or {}),
                                 "profiled_companies": result.profiled_companies,
                                 "errors": result.errors}
            async with state._connect() as db:
                async with db.execute("SELECT id FROM companies") as cursor:
                    after = [r[0] for r in await cursor.fetchall()]
            found = [c for c in after if c not in before]
            found += await team.matching_company_ids(state, req.get("industries") or [], cities)
            await team.add_company_ids(state, req["id"], found)
            note = f"Found {len(set(found))} companies."
            if result.errors:
                note += " Some searches failed: " + "; ".join(map(str, result.errors[:2]))[:300]
            await team.update_request(state, req["id"], discovery="done", discovery_note=note,
                                      rounds=int(req.get("rounds") or 0) + 1,
                                      discovered_at=datetime.now().isoformat())
        except Exception as exc:
            logger.exception("team discovery failed")
            await team.update_request(state, req["id"], discovery="failed",
                                      discovery_note=str(exc)[:300])


@app.get("/api/prospects/{prospect_id}/opt-out")
async def get_opt_out(prospect_id: str):
    """Why this person is opted out: the rule, the words and their message."""
    rows = await query_db("SELECT status FROM prospects WHERE id = ?", (prospect_id,))
    if not rows or not _mine_prospect(prospect_id):
        return _refuse("unknown prospect", 404)
    status = rows[0].get("status") or "new"
    state = _state()
    await state.init_db()
    history = await state.opt_out_history(prospect_id)
    last = next((h for h in reversed(history) if h["action"] == "opt_out"), None)
    out = {
        "opted_out": status == "opted_out", "status": status, "recorded": bool(last),
        "at": None, "rule": None, "matched": "", "why": "", "message": "",
        "conversation_id": "", "history": history,
    }
    if last:
        out.update({k: last.get(k) or out[k] for k in
                    ("rule", "matched", "why", "message", "conversation_id")})
        out["at"] = last["at"]
    elif status == "opted_out" or history:
        # Opted out before Xorora Prospector kept reasons (and maybe undone since):
        # show the last thing they wrote.
        for c in await query_db(
                "SELECT id, thread_json, updated_at FROM conversations "
                "WHERE prospect_id = ? ORDER BY updated_at DESC", (prospect_id,)):
            try:
                thread = json.loads(c.get("thread_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            theirs = [m for m in thread if isinstance(m, dict) and m.get("sender") == "prospect"]
            if theirs:
                out.update(message=str(theirs[-1].get("content") or ""),
                           conversation_id=c["id"], at=theirs[-1].get("timestamp") or c.get("updated_at"))
                break
    return out


@app.post("/api/prospects/{prospect_id}/opt-out/undo")
async def undo_opt_out(prospect_id: str, request: Request):
    """The owner says this opt-out was a mistake. Needs {"confirm": true}."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return _refuse('expected {"confirm": true}', 400)
    rows = await query_db("SELECT email FROM prospects WHERE id = ?", (prospect_id,))
    if not rows or not _mine_prospect(prospect_id):
        return _refuse("unknown prospect", 404)
    state = _state()
    await state.init_db()
    history = await state.opt_out_history(prospect_id)
    last = next((h for h in reversed(history) if h["action"] == "opt_out"), {})
    if not await state.undo_opt_out(prospect_id, details={
            "prospect_email": rows[0].get("email") or "",
            "previous_why": last.get("why", ""), "previous_at": last.get("at", "")}):
        return _refuse("This person isn't opted out.")
    return {"success": True, "status": "replied"}


@app.get("/api/conversations")
async def get_conversations(awaiting: bool = False):
    where, args = _cw("c.prospect_id", "WHERE", "prospects")
    rows = await query_db(f"""
        SELECT c.*, p.first_name, p.last_name, p.email as prospect_email, p.company,
               p.status as prospect_status
        FROM conversations c
        LEFT JOIN prospects p ON c.prospect_id = p.id
        {where}
        ORDER BY c.updated_at DESC LIMIT 100
    """, args)
    # The stored thread holds replies and Xorora Prospector's answers; the cold emails
    # a reply answers live in the Outbox. Attach those (sent ones only) and
    # any answer still waiting for approval, so the page shows the exchange.
    pids = sorted({r["prospect_id"] for r in rows if r.get("prospect_id")})
    emails: dict[str, list] = {}
    if pids:
        for e in await query_db(
                "SELECT prospect_id, campaign_id, step, subject, body, sent_at FROM outbox "
                f"WHERE kind = 'sequence' AND status = 'sent' AND prospect_id IN ({_in(pids)}) "
                "ORDER BY sent_at", tuple(pids)):
            emails.setdefault(e.pop("prospect_id"), []).append(e)
    cids = [r["id"] for r in rows]
    drafts = {d.pop("conversation_id"): d for d in (await query_db(
        "SELECT id, conversation_id, subject, body, send_at FROM outbox "
        f"WHERE kind = 'reply' AND status = 'pending_review' AND conversation_id IN ({_in(cids)}) "
        "ORDER BY created_at", tuple(cids)) if cids else [])}
    for row in rows:
        try:
            row["thread"] = json.loads(row.get("thread_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["thread"] = []
        # A prospect in two campaigns: show the sequence this reply answers.
        camp = row.get("campaign_id") or ""
        row["emails"] = [{k: v for k, v in e.items() if k != "campaign_id"}
                         for e in emails.get(row.get("prospect_id"), [])
                         if not camp or e.get("campaign_id") == camp]
        row["draft"] = drafts.get(row["id"])
    if awaiting:
        rows += await _awaiting_reply()
    return rows


async def _awaiting_reply() -> list[dict]:
    """Prospects Xorora Prospector emailed who haven't replied yet, shaped like a
    conversation so the tab can show every sent email, not only the ones
    that got an answer. Read-only: no conversation row is created, and the
    card moves to the real conversation the moment a reply arrives."""
    waiting: dict[str, dict] = {}
    # Only people still waiting: anyone whose company has a conversation (even
    # one past the tab's latest 100), a bounced address, or who's otherwise done
    # isn't. Bounded because this runs on every load of the tab.
    sent = await query_db(
        "SELECT o.prospect_id, o.step, o.subject, o.body, o.sent_at, "
        "p.first_name, p.last_name, p.email AS prospect_email, p.company, "
        "p.status AS prospect_status FROM outbox o "
        "JOIN prospects p ON p.id = o.prospect_id "
        "WHERE o.kind = 'sequence' AND o.status = 'sent' "
        "AND p.status NOT IN ('replied', 'opted_out', 'lost', 'meeting', 'closed') "
        "AND COALESCE(p.email_status, '') != 'invalid' " + _cw("p.company_id")[0] + " "
        "AND NOT EXISTS (SELECT 1 FROM conversations c JOIN prospects q ON q.id = c.prospect_id "
        "WHERE q.id = p.id OR (q.company_id = p.company_id AND COALESCE(p.company_id, '') != '')) "
        "ORDER BY o.sent_at DESC LIMIT 2000", _cw("p.company_id")[1])
    for e in reversed(sent):
        pid = e["prospect_id"]
        row = waiting.setdefault(pid, {
            "id": "awaiting-" + pid, "prospect_id": pid, "campaign_id": "",
            "channel": "email", "intent": "", "stage": "", "status": "awaiting_reply",
            "thread": [], "draft": None, "emails": [],
            "first_name": e.get("first_name") or "", "last_name": e.get("last_name") or "",
            "prospect_email": e.get("prospect_email") or "", "company": e.get("company") or "",
            "prospect_status": e.get("prospect_status") or "",
        })
        row["emails"].append({"step": e["step"], "subject": e["subject"],
                              "body": e["body"], "sent_at": e["sent_at"]})
        row["created_at"] = row.get("created_at") or e["sent_at"]
        row["updated_at"] = e["sent_at"]
    return sorted(waiting.values(), key=lambda r: r.get("updated_at") or "", reverse=True)


@app.post("/api/conversations/{convo_id}/status")
async def set_conversation_status(convo_id: str, request: Request):
    """Close a conversation, or hand one Xorora Prospector escalated back to it.

    Xorora Prospector answers new replies only on open conversations, so "open" means
    "Xorora Prospector, carry on" and "closed" means it's done."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    status = data.get("status") if isinstance(data, dict) else None
    if status not in ("open", "closed"):
        return JSONResponse({"success": False, "message": "status must be open or closed"},
                            status_code=400)
    rows = await query_db("SELECT status, prospect_id FROM conversations WHERE id = ?", (convo_id,))
    if not rows or not _mine_prospect(rows[0].get("prospect_id")):
        return JSONResponse({"success": False, "message": "No such conversation."},
                            status_code=404)
    state = _state()
    await state.update_conversation(convo_id, status=status)
    await state.log_action("conversation_status", "dashboard",
                           {"conversation_id": convo_id, "from": rows[0]["status"], "to": status})
    return {"success": True, "status": status}


@app.get("/api/trend")
async def get_trend(days: int = 14, tz: int = 0):
    """Daily counts for the last `days` days in the viewer's time zone
    (`tz`: minutes east of UTC; stored times are UTC): contacts found,
    emails sent, first replies. Days with nothing are 0."""
    from datetime import datetime, timedelta, timezone
    days = max(1, min(90, days))
    tz = max(-840, min(840, tz))
    shift = f"{tz:+d} minutes"
    today = (datetime.now(timezone.utc) + timedelta(minutes=tz)).date()
    labels = [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]

    async def per_day(table: str, column: str, where: str = "", scope_col: str = "",
                      kind: str = "companies") -> list[int]:
        extra, args = _cw(scope_col, "AND", kind) if scope_col else ("", ())
        rows = await query_db(
            f"SELECT date({column}, ?) AS d, COUNT(*) AS n FROM {table} "
            f"WHERE date({column}, ?) >= ? {where}{extra} GROUP BY d", (shift, shift, labels[0], *args))
        counts = {r["d"]: r["n"] for r in rows}
        return [counts.get(day, 0) for day in labels]

    return {
        "days": labels,
        "found": await per_day("prospects", "created_at", scope_col="company_id"),
        "sent": await per_day("outbox", "sent_at", "AND status = 'sent'", "prospect_id", "prospects"),
        "replies": await per_day("conversations", "created_at", scope_col="prospect_id", kind="prospects"),
    }


@app.get("/api/activity")
async def get_activity():
    rows = await query_db("SELECT * FROM actions ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        try:
            row["details"] = json.loads(row.get("details_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            row["details"] = {}
    return rows


# ── Dashboard UI ──


# ── Discovery: the provider menu, an estimate, and a run ──
#
# This is the only stage that spends money, so the UI never starts one without
# showing what it will cost first.

_discovery_task: asyncio.Task | None = None
_discovery_report: dict | None = None
_discovery_kind = "discover"   # or "profile": reading sites has no stop switch


def _discovery_queries(body: dict, config):
    from harvey.collectors.discover import build_queries, preset_problem

    preset = str(body.get("preset") or "")
    problem = preset_problem(preset, str(body.get("provider") or ""), config=config)
    if problem:
        raise ValueError(problem)
    cities = [c.strip() for c in (body.get("cities") or []) if c.strip()] or None
    return build_queries(
        config, cities=cities,
        depth=int(body.get("depth") or 30),
        limit=int(body.get("limit") or 100),
        preset=preset,
    )


@app.get("/api/discover/providers")
async def get_discovery_providers():
    """What each source does, what it costs, and whether it's ready to use."""
    try:
        from harvey.collectors.discover import DEFAULT_PROVIDER, provider_menu
        from harvey.config import load_env

        state = _state()
        await state.init_db()
        return {
            "providers": provider_menu(load_env().model_dump()),
            "default": DEFAULT_PROVIDER,
            "selected": await state.get_setting("discovery_provider") or DEFAULT_PROVIDER,
            "paused": await state.get_setting("discovery_paused"),
            "running": bool(_discovery_task and not _discovery_task.done()),
            "running_kind": _discovery_kind,
            "unprofiled": await state.count_companies_needing_profile(),
            "last_report": _discovery_report,
        }
    except Exception as e:
        logger.exception("discovery providers failed")
        return {"providers": [], "error": str(e)}


@app.post("/api/discover/estimate")
async def estimate_discovery(request: Request):
    """Projected spend and the exact query list, before anything is called."""
    try:
        from harvey.collectors.discover import PROVIDERS, estimate_cost
        from harvey.config import load_config

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"error": f"unknown provider {provider!r}"},
                                status_code=400)

        try:
            queries = _discovery_queries(body, load_config())
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {
            "provider": provider,
            "queries": [q.keyword() for q in queries],
            "query_count": len(queries),
            "estimated_cost": round(estimate_cost(provider, queries), 4),
            "free": PROVIDERS[provider].estimate(queries) == 0,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/discover/run")
async def start_discovery(request: Request):
    """Kick off a run in the background and hand back immediately.

    Discovery takes minutes, not milliseconds, holding the request open
    would just time out. Progress shows up in the run log.
    """
    global _discovery_task, _discovery_report

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from harvey.collectors.discover import PROVIDERS
        from harvey.config import load_config
        from harvey.pipeline import run_prospecting

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"success": False,
                                 "message": f"unknown provider {provider!r}"},
                                status_code=400)

        config = load_config()
        try:
            queries = _discovery_queries(body, config)
        except ValueError as e:
            return JSONResponse({"success": False, "message": str(e)}, status_code=400)
        max_spend = float(body.get("max_spend") or 1.0)

        state = _state()
        await state.init_db()
        await state.set_setting("discovery_provider", provider)
        await state.set_setting("discovery_paused", "")

        global _discovery_kind
        _discovery_kind = "discover"

        async def _go():
            global _discovery_report
            try:
                # Discovery chains straight into profiling: reading the sites
                # is free, and it is what makes the results worth anything.
                result = await run_prospecting(state, config, provider, queries,
                                               max_spend=max_spend)
                _discovery_report = {
                    **(result.discover or {}),
                    "profiled_companies": result.profiled_companies,
                    "profile_observations": result.profile_observations,
                    "errors": result.errors,
                }
            except Exception as exc:
                logger.exception("discovery run failed")
                _discovery_report = {"errors": [str(exc)], "stopped": "failed"}

        _discovery_report = None
        _discovery_task = asyncio.create_task(_go())
        return {"success": True, "queries": len(queries)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/profile/run")
async def start_profile():
    """Read the websites of everything discovered but not yet looked at.

    Free and model-free, so there is nothing to estimate and no cap to set.
    """
    global _discovery_task, _discovery_report

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from harvey.pipeline import run_profile_stage

        state = _state()
        await state.init_db()
        pending = await state.count_companies_needing_profile()
        if not pending:
            return {"success": True, "pending": 0}

        async def _go():
            global _discovery_report
            try:
                try:
                    from harvey.config import load_config
                    config = load_config()
                except Exception:
                    config = None   # still profiles; no role is singled out
                companies, observations, _ = await run_profile_stage(
                    state, limit=200, config=config)
                _discovery_report = {
                    "profiled_companies": companies,
                    "profile_observations": observations,
                }
            except Exception as exc:
                logger.exception("profile run failed")
                _discovery_report = {"errors": [str(exc)], "stopped": "failed"}

        global _discovery_kind
        _discovery_kind = "profile"
        _discovery_report = None
        _discovery_task = asyncio.create_task(_go())
        return {"success": True, "pending": pending}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/discover/stop")
async def stop_discovery():
    """Kill switch. Read between batches, so an in-flight run stops cleanly."""
    try:
        state = _state()
        await state.init_db()
        await state.set_setting("discovery_paused", "stopped from dashboard")
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


def _parse_ts(value) -> datetime | None:
    """SQLite CURRENT_TIMESTAMP / isoformat strings -> naive UTC datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "").split("+")[0])
    except ValueError:
        return None


async def _scalar(sql: str, params: tuple = ()) -> int:
    rows = await query_db(sql, params)
    return int(next(iter(rows[0].values())) or 0) if rows else 0


async def _first(sql: str, params: tuple = (), key: str = "t"):
    """First row's `key`, or None, query_db returns [] on any DB problem."""
    rows = await query_db(sql, params)
    return rows[0].get(key) if rows else None


# A run row still says 'running' after its process was killed (closed window,
# reboot). No new facts for this long means it is not coming back.
STALE_RUN_SECONDS = 900
# Contact-finding has no run row; a recent prospects_checked_at means it is active.
CONTACT_ACTIVE_SECONDS = 180
# Moved on from "waiting to be emailed" (opted_out too: never offered again).
SENT_STATUSES = ("contacted", "replied", "meeting", "closed", "lost", "opted_out")
REPLY_STATUSES = ("replied", "meeting", "closed")


def _eta_text(minutes) -> str:
    if minutes is None:
        return ""
    return "less than a minute left" if minutes < 1 else f"about {round(minutes)} min left"


@app.get("/api/live-status")
async def get_live_status():
    """Plain-language progress: each pipeline step, what is running, how long is left.

    Built for someone who would otherwise keep asking "what's the status?".
    Everything is derived from the database, so it is true whatever started
    the work, `xorora-prospector run`, a CLI command, or a one-off script. Every total
    counts only what the step can act on (a business with no website is never
    read), so a finished step always reaches 100% instead of sticking.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # DB stores naive UTC
    kw, ka = _cw("id")
    has_site = "COALESCE(domain, '') != ''" + kw      # scoped to the caller's companies
    companies = await _scalar("SELECT COUNT(*) FROM companies WHERE 1" + kw, ka)
    with_site = await _scalar(f"SELECT COUNT(*) FROM companies WHERE {has_site}", ka)
    no_site = companies - with_site
    profiled = await _scalar(
        f"SELECT COUNT(*) FROM companies WHERE {has_site} AND profile_checked_at IS NOT NULL", ka)
    checked = await _scalar(
        f"SELECT COUNT(*) FROM companies WHERE {has_site} "
        "AND profile_checked_at IS NOT NULL AND prospects_checked_at IS NOT NULL", ka)

    pw, pa = _cw("company_id", "WHERE")
    prospects = await query_db(f"SELECT id, email, email_status, status FROM prospects {pw}", pa)
    with_email = [p for p in prospects if p.get("email")]
    by_status: dict[str, int] = {}
    for p in with_email:
        k = (p.get("email_status") or "guess").lower()
        by_status[k] = by_status.get(k, 0) + 1
    deliverable = {p["id"] for p in with_email
                   if (p.get("email_status") or "") in ("verified", "risky")}
    unconfirmed = sum(n for k, n in by_status.items()
                      if k not in ("verified", "risky", "invalid"))

    # Drafts are counted per person, not per campaign: one campaign can cover many.
    drafted: set[str] = set()
    mine_ids = {p["id"] for p in prospects}
    for row in await query_db("SELECT prospect_ids_json FROM campaigns"):
        try:
            ids = json.loads(row.get("prospect_ids_json") or "[]")
        except (json.JSONDecodeError, TypeError):
            ids = []
        if isinstance(ids, list):
            drafted.update(i for i in ids if isinstance(i, str) and (_scope() is None or i in mine_ids))
    sent = {p["id"] for p in prospects if p.get("status") in SENT_STATUSES}
    # Who is left for the user to send by hand: only when Xorora Prospector can't send
    # itself, and never someone Xorora Prospector has queued, sent to, or can't reach.
    harvey_sends = _harvey_can_send()
    ow, oa = _cw("prospect_id", "AND", "prospects")
    awaiting_review = await _scalar("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review'" + ow, oa)
    by_hand: set[str] = set()
    if not harvey_sends:
        skip = await _harvey_committed_ids() | await _hand_sent_ids()
        by_hand = {p["id"] for p in prospects
                   if p["id"] in drafted and p["id"] not in skip
                   and (p.get("status") or "new") in HAND_SENDABLE_STATUSES
                   and (p.get("email_status") or "guess") in HAND_SENDABLE_EMAIL}
    replies = sum(1 for p in prospects if p.get("status") in REPLY_STATUSES)
    cost = float(await _first(
        "SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs", key="c") or 0)

    running, stopped = [], []
    for run in await query_db(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY started_at"):
        started = _parse_ts(run.get("started_at"))
        elapsed = max(0.0, (now - started).total_seconds()) if started else 0.0
        last = _parse_ts(await _first(
            "SELECT MAX(observed_at) AS t FROM observations WHERE run_id = ?",
            (run["id"],))) or started
        item = {"stage": run.get("stage"), "provider": run.get("provider"),
                "elapsed_min": round(elapsed / 60, 1), "eta_min": None}
        if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
            stopped.append(item)
            continue
        if run.get("stage") == "profile" and started:
            done_in_run = await _scalar(
                f"SELECT COUNT(*) FROM companies WHERE {has_site} AND profile_checked_at >= ?",
                (*ka, run["started_at"]))
            left = max(0, with_site - profiled)
            if left and done_in_run and elapsed > 0:
                item["eta_min"] = left / (done_in_run / elapsed) / 60
        running.append(item)

    last_contact = _parse_ts(await _first(
        "SELECT MAX(prospects_checked_at) AS t FROM companies"))
    finding_emails = bool(
        last_contact and (now - last_contact).total_seconds() < CONTACT_ACTIVE_SECONDS)
    reading = any(r["stage"] == "profile" for r in running)
    finding = any(r["stage"] == "discover" for r in running)

    def step(label, done, total, active, detail, info=False):
        if active:
            state_ = "running"
        elif info:
            state_ = "done" if done else "waiting"
        elif total and done >= total:
            state_ = "done"
        elif done:
            state_ = "partial"
        else:
            state_ = "waiting"
        return {"label": label, "done": done, "total": total,
                "state": state_, "detail": detail}

    profile_eta = next((r["eta_min"] for r in running if r["stage"] == "profile"), None)
    read_detail = f"{profiled} of {with_site} read"
    if reading and profile_eta is not None:
        read_detail += " · " + _eta_text(profile_eta)
    if no_site:
        read_detail += f" · {no_site} have no website (skipped)"

    steps = [
        step("Find businesses", companies, 0, finding,
             f"{companies} businesses found", info=True),
        step("Read their websites", profiled, with_site, reading, read_detail),
        step("Find emails", checked, profiled, finding_emails,
             f"{checked} of {profiled} checked · {len(with_email)} emails found"),
        step("Check emails are real", len(with_email) - unconfirmed, len(with_email), False,
             f"{by_status.get('verified', 0)} verified · {by_status.get('risky', 0)} risky · "
             f"{unconfirmed} not confirmed"
             + (f" · {by_status['invalid']} bad" if by_status.get("invalid") else "")),
        step("Write first emails", len(drafted), len(deliverable | drafted), False,
             f"{len(drafted)} written · see Campaigns"),
        step("Sent", len(sent & drafted), len(drafted), False,
             f"{len(sent & drafted)} of {len(drafted)} sent"),
        step("Replies", replies, 0, False,
             f"{replies} replies" if replies else "No replies yet", info=True),
    ]

    next_tab = ""   # where the next step is done, when it's done in the dashboard
    if reading or finding:
        next_up = "Wait, Xorora Prospector is working. This page updates by itself."
    elif finding_emails:
        next_up = "Wait, emails are being found."
    elif stopped:
        next_up, next_tab = "A job stopped before finishing. Run it again.", "discover"
    elif harvey_sends and awaiting_review:
        next_up, next_tab = f"Review the {awaiting_review} draft(s) waiting in the Outbox.", "outbox"
    elif by_hand:
        next_up, next_tab = f"Open Campaigns and send the {len(by_hand)} unsent email(s).", "campaigns"
    elif profiled > checked:
        next_up, next_tab = f"Find emails for the {profiled - checked} businesses not checked yet.", "controls"
    elif deliverable - drafted:
        next_up, next_tab = f"Write first emails for {len(deliverable - drafted)} more contact(s).", "controls"
    elif not companies:
        next_up, next_tab = "Search for businesses to get started.", "discover"
    else:
        next_up, next_tab = "Search more cities or industries to get more leads.", "discover"

    return {
        "steps": steps,
        "running": [dict(r, eta_text=_eta_text(r["eta_min"])) for r in running],
        "stopped": stopped,
        "next": next_up,
        "next_tab": next_tab,
        "cost_usd": round(cost, 4),
        "updated_at": now.isoformat() + "Z",
    }


def _require_approval_now() -> bool:
    try:
        from harvey.config import load_config
        return bool(load_config(str(_resolve_config_file())).channels.email.require_approval)
    except Exception:
        return True


@app.get("/api/today")
async def get_today():
    """What needs a human, right now.

    The dashboard opens here rather than on a setup checklist: the question a
    returning user actually has is "is anything waiting on me?", and the
    answer is usually a short list or nothing at all.
    """
    items: list[dict] = []
    stats: dict = {}
    try:
        from harvey.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = await state.get_signal_codes()
        proposed = [c for c in codes if c.get("status") == "proposed"]
        confirmed = [c for c in codes if c.get("status") == "confirmed"]
        sc = _scope()
        cap = 5000 if sc is not None else 200
        pending = [i for i in await state.get_outbox(status="pending_review", limit=cap)
                   if _mine_prospect(i.get("prospect_id"))][:200]
        approved = [i for i in await state.get_outbox(status="approved", limit=cap)
                    if _mine_prospect(i.get("prospect_id"))][:200]
        paused = await state.get_setting("sending_paused")
        if sc is None:
            counts = await state.count_prospects_by_status()
        else:
            pw, pa = _cw("company_id", "WHERE")
            counts = {r["status"]: r["n"] for r in await query_db(
                f"SELECT status, COUNT(*) AS n FROM prospects {pw} GROUP BY status", pa)}

        cw, ca = _cw("prospect_id", "AND", "prospects")
        convos = await query_db(
            f"SELECT COUNT(*) AS n FROM conversations WHERE status = 'open'{cw}", ca
        )
        open_convos = convos[0]["n"] if convos else 0
        handed = await query_db(
            f"SELECT COUNT(*) AS n FROM conversations WHERE status = 'needs_human'{cw}", ca
        )
        needs_human = handed[0]["n"] if handed else 0
        kw, ka = _cw("id", "WHERE")
        companies = await query_db(f"SELECT COUNT(*) AS n FROM companies {kw}", ka)
        n_companies = companies[0]["n"] if companies else 0

        if sc is None:
            unprofiled_count = await state.count_companies_needing_profile()
        else:
            uw, ua = _cw("id")
            unprofiled_count = await _scalar(
                "SELECT COUNT(*) FROM companies WHERE domain != '' AND profile_checked_at IS NULL" + uw, ua)
        stats = {
            "companies": n_companies,
            "prospects": sum(counts.values()),
            "signals_confirmed": len(confirmed),
            "outbox_pending": len(pending),
            "outbox_approved": len(approved),
            "open_conversations": open_convos,
            "needs_human": needs_human,
            "signals_proposed": len(proposed),
            "email_provider": _current_provider(),
            "unprofiled": unprofiled_count,
        }

        # Ordered by how much it blocks Xorora Prospector from doing anything at all.
        brain = await _brain_status()
        if brain.get("status") == "paused":
            who = "Codex" if brain.get("provider") == "codex" else "Xorora Prospector's model"
            items.append({
                "key": "brain-paused", "tone": "bad",
                "title": f"{who} is paused, so Xorora Prospector is waiting",
                "detail": (str(brain.get("reason") or "It can't be used right now.")[:300] +
                           " Replies and queued emails are untouched; Xorora Prospector resumes "
                           "once it's fixed. xorora-prospector status --check says what's wrong."),
                "action": "Open the log", "tab": "controls",
            })

        from harvey.agents.writer import EMAIL_RULES_PROBLEM_KEY
        rules_problem = await state.get_setting(EMAIL_RULES_PROBLEM_KEY)
        if rules_problem:
            items.append({
                "key": "email-rules", "tone": "bad",
                "title": "Xorora Prospector isn't writing emails: your email rules can't be used",
                "detail": (f"{rules_problem}. Fix the file, or clear writer.email_rules "
                           "to use Xorora Prospector's built-in rules."),
                "action": "Open the log", "tab": "controls",
            })

        hold = await state.current_sends_held()
        if hold:
            from harvey.sending_mode import held_message
            title, detail = held_message(hold, waiting=len(approved))
            items.append({
                "key": "sends-held", "tone": "bad", "title": title, "detail": detail,
                "action": "Open the log", "tab": "controls",
            })

        if paused:
            items.append({
                "key": "paused", "tone": "bad",
                "title": "Sending is paused",
                "detail": str(paused) + ". Nothing will go out until you resume it.",
                "action": "Review and resume", "tab": "outbox",
            })

        # Cold email waiting on the optional Precleared check: its free
        # checks ran out, the key was rejected, or it couldn't be reached.
        from harvey.integrations.precleared import current_hold, hold_message
        try:
            from harvey.config import load_config
            loaded = load_config(str(_resolve_config_file()))
        except Exception:
            loaded = None  # can't judge: show the hold rather than hide it
        precleared_hold = await current_hold(state, loaded)
        if precleared_hold:
            message = hold_message(precleared_hold)
            items.append({
                "key": "precleared-hold", "tone": message["tone"],
                "title": message["title"], "detail": message["detail"],
                "action": "Open the log", "tab": "controls",
                "url": message["url"], "url_label": message["url_label"],
            })

        setup = await get_setup_status()
        if isinstance(setup, dict) and setup.get("percent", 100) < 100:
            missing = [c["label"] for c in setup.get("checks", [])
                       if c.get("required") and not c.get("done")]
            items.append({
                "key": "setup", "tone": "warn",
                "title": "Finish setting Xorora Prospector up",
                "detail": ", ".join(missing[:3]) or "Some required steps are incomplete.",
                "action": "Open setup", "tab": "settings",
            })

        if proposed:
            items.append({
                "key": "signals", "tone": "warn",
                "title": (f"{len(proposed)} signal waiting for your confirmation"
                          if len(proposed) == 1
                          else f"{len(proposed)} signals waiting for your confirmation"),
                "detail": ("Xorora Prospector won't collect anything you haven't approved. "
                           "Confirm which signals define a good prospect for you."),
                "action": "Review signals", "tab": "signals",
            })
        elif not confirmed:
            items.append({
                "key": "signals-none", "tone": "warn",
                "title": "No signals confirmed",
                "detail": "Every signal is rejected, so prospecting has nothing to collect.",
                "action": "Review signals", "tab": "signals",
            })

        if not n_companies and confirmed:
            items.append({
                "key": "discover", "tone": "good",
                "title": "No companies yet",
                "detail": ("Signals are confirmed but nothing has been collected. "
                           "Discovery is free to try, no account needed."),
                "action": "Find businesses", "tab": "discover",
            })

        unprofiled = unprofiled_count
        if unprofiled:
            items.append({
                "key": "profile", "tone": "good",
                "title": f"{unprofiled} companies not looked at yet",
                "detail": ("Reading their websites is free and it is what makes "
                           "an email specific, who their agency is, what they "
                           "are missing, whether they are spending on ads."),
                "action": "Read their sites", "tab": "discover",
            })

        if needs_human:
            items.append({
                "key": "needs-human", "tone": "bad",
                "title": (f"{needs_human} conversation needs you" if needs_human == 1
                          else f"{needs_human} conversations need you"),
                "detail": ("Xorora Prospector stopped and handed these over: an escalation, or a "
                           "reply it had no safe answer for. Reply yourself, then close "
                           "it or hand it back."),
                "action": "Read them", "tab": "conversations",
            })

        if pending:
            items.append({
                "key": "outbox", "tone": "warn",
                "title": (f"{len(pending)} email waiting for approval" if len(pending) == 1
                          else f"{len(pending)} emails waiting for approval"),
                "detail": ("Nothing sends until you approve it. Read them one at a time."
                           if _require_approval_now() else
                           "Autopilot sends the rest on its own; these wait for you. "
                           "Read them one at a time."),
                "action": "Open the decisions desk", "tab": "outbox",
            })

        if open_convos:
            items.append({
                "key": "replies", "tone": "good",
                "title": (f"{open_convos} live conversation" if open_convos == 1
                          else f"{open_convos} live conversations"),
                "detail": "People replied. Check how Xorora Prospector is handling them.",
                "action": "Read conversations", "tab": "conversations",
            })

        return {"items": items, "stats": stats}
    except Exception as e:
        logger.exception("today load failed")
        return {"items": [], "stats": stats, "error": str(e)}


# ── Signals: Xorora Prospector proposes, the user confirms ──
#
# Nothing is collected until a human has said yes to it. This is the gate the
# whole prospecting pipeline hangs off: collectors ask `state.confirmed_signal_codes()`
# and skip anything that isn't in the set.

CATEGORY_META = {
    "discovery": {
        "label": "Discovery, who exists",
        "blurb": "How Xorora Prospector finds businesses at all, and how visible they are. "
                 "This is the only stage that costs money.",
    },
    "profile": {
        "label": "Profile, what they are",
        "blurb": "Read from the pages a business already publishes. Free, no AI "
                 "tokens, three HTTP requests per company. These are the signals "
                 "that make an email specific.",
    },
    "people": {
        "label": "People, who decides",
        "blurb": "Named humans and whether they're the one who can say yes.",
    },
    "verification": {
        "label": "Contactability, can you reach them",
        "blurb": "Whether the address will actually deliver, and what to do when "
                 "it won't.",
    },
}
CATEGORY_ORDER = ["discovery", "profile", "people", "verification"]


@app.get("/api/signals")
async def get_signals():
    """The signal vocabulary, grouped for review, with live cohort sizes."""
    try:
        from harvey.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        # Seeding is idempotent and never overrides a decision the user made,
        # so it is safe to run on every load, new signals shipped in an
        # upgrade show up as `proposed` without any migration step.
        await seed_signal_catalog(state)

        codes = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}

        groups, summary = [], {"proposed": 0, "confirmed": 0, "rejected": 0}
        for cat in CATEGORY_ORDER:
            rows = []
            for sig in codes:
                if sig.get("category") != cat:
                    continue
                seen = counts.get(sig["code"], {})
                rows.append({
                    **sig,
                    "companies": seen.get("companies", 0),
                    "observations": seen.get("observations", 0),
                })
            if rows:
                meta = CATEGORY_META.get(cat, {})
                groups.append({
                    "key": cat,
                    "label": meta.get("label", cat.title()),
                    "blurb": meta.get("blurb", ""),
                    "signals": rows,
                })
        for sig in codes:
            summary[sig.get("status", "proposed")] = (
                summary.get(sig.get("status", "proposed"), 0) + 1
            )

        return {"summary": summary, "groups": groups, "total": len(codes)}
    except Exception as e:
        logger.exception("signals load failed")
        return {"error": str(e), "groups": [], "summary": {}}


@app.post("/api/signals/status")
async def set_signals_status(request: Request):
    """Confirm or reject one signal, or a whole category at once."""
    try:
        body = await request.json()
        status = (body.get("status") or "").strip()
        codes = body.get("codes") or ([body["code"]] if body.get("code") else [])
        if status not in ("proposed", "confirmed", "rejected"):
            return JSONResponse(
                {"success": False, "message": f"invalid status: {status!r}"},
                status_code=400,
            )
        if not codes:
            return JSONResponse(
                {"success": False, "message": "no signal codes given"}, status_code=400
            )

        from harvey.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        # Seed first: a confirm that arrives before anything has loaded the
        # catalog would otherwise report success while changing nothing.
        await seed_signal_catalog(state)

        changed, unknown = 0, []
        for code in codes:
            if await state.set_signal_status(code, status):
                changed += 1
            else:
                unknown.append(code)
        return {
            "success": True, "changed": changed,
            "status": status, "unknown": unknown,
        }
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


COHORT_PAGE_SIZE = 50
COHORT_MAX_PAGE_SIZE = 200


async def _cohort_companies(require: list[str], exclude: list[str],
                            offset: int = 0, limit: int | None = None,
                            ranges: list[dict] | None = None):
    state = _state()
    await state.init_db()
    return await state.cohort_companies(require, exclude, offset, limit,
                                        ranges=ranges)


def _codes(raw) -> list[str]:
    """Accept lists or CSV strings, with bounded SQL parameter counts."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list) or any(not isinstance(c, str) for c in raw):
        raise ValueError("signals must be a list of strings or comma-separated string")
    codes = list(dict.fromkeys(c.strip() for c in raw if c.strip()))
    if len(codes) > 200 or any(len(c) > 128 for c in codes):
        raise ValueError("too many or oversized signal codes")
    return codes


COHORT_MAX_RANGES = 20


def _range_bound(raw):
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError("range bounds must be numbers")
    value = float(raw)
    if value != value or abs(value) > 1e12:
        raise ValueError("range bound is out of bounds")
    return value


def _ranges(raw) -> list[dict]:
    """Numeric cohort filters: a list of {code, min, max}, or the CSV query
    form "SERP_RANK:11:30,MAPS_RANK::3". At least one bound per range."""
    if raw is None or raw == "" or raw == []:
        return []
    if isinstance(raw, str):
        items = []
        for part in raw.split(","):
            if not part.strip():
                continue
            pieces = part.split(":")
            if len(pieces) != 3:
                raise ValueError("ranges look like CODE:min:max")
            items.append({"code": pieces[0], "min": pieces[1], "max": pieces[2]})
        raw = items
    if not isinstance(raw, list) or any(not isinstance(r, dict) for r in raw):
        raise ValueError("ranges must be a list of {code, min, max}")
    if len(raw) > COHORT_MAX_RANGES:
        raise ValueError("too many ranges")
    out = []
    for r in raw:
        code = r.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > 128:
            raise ValueError("each range needs a signal code")
        low, high = _range_bound(r.get("min")), _range_bound(r.get("max"))
        if low is None and high is None:
            raise ValueError(f"range on {code} needs a min or a max")
        if low is not None and high is not None and low > high:
            raise ValueError(f"range on {code}: min is above max")
        out.append({"code": code.strip(), "min": low, "max": high})
    return out


def _cohort_integer(raw, default):
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        raise ValueError("offset and limit must be integers")
    value = int(raw)
    if abs(value) > 9223372036854775807:
        raise ValueError("offset or limit is too large")
    return value


@app.post("/api/cohort")
async def preview_cohort(request: Request):
    """How many companies carry ALL these signals and none of those.

    The payoff for confirming signals: a cohort is a query, not a list. Set
    intersection happens in SQL, intersecting in JS over a capped fetch
    silently returns the wrong answer. ``offset``/``limit`` page through the
    matches; ``size`` is always the whole cohort. ``ranges`` adds numeric
    filters, ``[{"code": "SERP_RANK", "min": 11, "max": 30}]``.
    """
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        require = _codes(body.get("require"))
        exclude = _codes(body.get("exclude"))
        ranges = _ranges(body.get("ranges"))
        offset = max(0, _cohort_integer(body.get("offset"), 0))
        limit = min(max(1, _cohort_integer(body.get("limit"), COHORT_PAGE_SIZE)),
                    COHORT_MAX_PAGE_SIZE)
        if not require and not ranges:
            return {"size": 0, "offset": 0, "limit": limit, "companies": []}

        size, rows = await _cohort_companies(require, exclude, offset, limit,
                                             ranges=ranges)
        return {"size": size, "offset": offset, "limit": limit,
                "companies": rows}
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"size": 0, "companies": [], "error": str(e)}, status_code=500)


def _csv_cell(value):
    text = str(value or "")
    # Spreadsheet software may ignore leading whitespace before formulas.
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


@app.get("/api/cohort.csv")
async def export_cohort(require: str = "", exclude: str = "", ranges: str = ""):
    """The whole cohort as a CSV download, every match, not just one page."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "domain", "website", "industry", "location"])
    try:
        codes = _codes(require)
        excluded = _codes(exclude)
        bounds = _ranges(ranges)
        if codes or bounds:
            _, rows = await _cohort_companies(codes, excluded, ranges=bounds)
            for r in rows:
                if not _mine_company(r.get("id")):
                    continue
                writer.writerow([_csv_cell(r.get(k)) for k in
                                 ("name", "domain", "website", "industry", "location")])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.warning("Cohort export failed: %s", e)
        return JSONResponse({"error": "Cohort export failed"}, status_code=500)
    return PlainTextResponse(
        buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="cohort.csv"'},
    )


@app.get("/api/runs")
async def get_runs_api():
    """The collector run log, what ran, when, what it produced and cost."""
    try:
        state = _state()
        await state.init_db()
        await state.sweep_stale_runs()
        runs = await state.get_runs(limit=25)
        # Same rule as the live panel: a "running" run that has recorded
        # nothing for STALE_RUN_SECONDS has stopped, whatever its row says.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for run in runs:
            if run.get("status") != "running":
                continue
            last = _parse_ts(await _first(
                "SELECT MAX(observed_at) AS t FROM observations WHERE run_id = ?",
                (run["id"],))) or _parse_ts(run.get("started_at"))
            if last and (now - last).total_seconds() > STALE_RUN_SECONDS:
                run["status"] = "stalled"
        return runs
    except Exception as e:
        return {"error": str(e)}


WEB_DIR = (Path(__file__).resolve().parent / "web")


TEXT_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
BINARY_TYPES = {".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png"}


@app.get("/static/{path:path}")
async def static_file(path: str):
    """Serve the dashboard's own assets from disk.

    Read per-request rather than cached at import: editing app.css and hitting
    reload is the whole point of having them as real files. Fonts are vendored
    rather than fetched from a CDN, this is a local tool and it should work
    with the network off.
    """
    target = (WEB_DIR / path).resolve()
    root = WEB_DIR.resolve()
    if not target.is_file() or not target.is_relative_to(root):
        return PlainTextResponse("not found", status_code=404)

    if target.suffix in BINARY_TYPES:
        return Response(
            target.read_bytes(),
            media_type=BINARY_TYPES[target.suffix],
            headers={"Cache-Control": "public, max-age=604800"},
        )
    return PlainTextResponse(
        target.read_text(encoding="utf-8"),
        media_type=TEXT_TYPES.get(target.suffix, "text/plain"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (WEB_DIR / "index.html").read_text(encoding="utf-8")


class DashboardSetupError(Exception):
    """The dashboard can't start safely; the message says what to do."""


def _check_loopback_bind(host: str) -> str:
    """The host to bind, which must be a loopback address. The dashboard has
    no remote-access mode: from another machine, use an SSH tunnel."""
    name = (host or "").strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not auth.is_loopback(name):
        raise DashboardSetupError(
            f"Refusing to listen on {host!r}: the dashboard only listens on this machine "
            "(127.0.0.1, ::1 or localhost). To reach it from elsewhere, tunnel instead: "
            "ssh -L 5555:127.0.0.1:5555 <this-host>")
    return name


def dashboard_url(host: str = "127.0.0.1", port: int = 5555) -> str:
    """The unlock link. The token rides in the #fragment, which browsers
    never send to the server (so it stays out of logs and Referer); the
    page moves it to sessionStorage and strips it from the address bar."""
    name = _check_loopback_bind(host)
    try:
        if ipaddress.ip_address(name).version == 6:
            name = f"[{name}]"
    except ValueError:
        pass
    return f"http://{name}:{port}/#token={token_store.get()}"


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server (loopback only, token required)."""
    import uvicorn

    try:
        bind = _check_loopback_bind(host)
        url = dashboard_url(host, port)
    except (DashboardSetupError, auth.TokenError) as e:
        raise DashboardSetupError(str(e)) from e

    print(f"\n  Xorora Prospector Dashboard running. Open this link to unlock it:\n\n    {url}\n")
    print(f"  The token is stored in {token_store.path} (owner-only). Keep the link")
    print("  private; anyone with it can use the dashboard. Press Ctrl+C to stop.\n")
    if os.name == "nt":
        print("  Windows: the token file is protected by your folder's permissions,")
        print("  not POSIX file modes.\n")
    uvicorn.run(app, host=bind, port=port, log_level="warning",
                proxy_headers=False, server_header=False, date_header=False)
