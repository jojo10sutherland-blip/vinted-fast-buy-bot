"""
fast_buy_bot.py
---------------
A standalone Discord bot that adds a "⚡ RESERVE NOW" button underneath
every iPhone-deal alert your existing iPhone bot posts into a Discord
channel — and reserves the Vinted item for you when you tap it.
"""
import asyncio
import logging
import os
import re
from typing import Optional
import discord
import requests

# ─── configuration from env ──────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "0") or 0)
VINTED_SESSION_COOKIE = os.environ.get("VINTED_SESSION_COOKIE", "").strip()
VINTED_ANON_ID = os.environ.get("VINTED_ANON_ID", "").strip()
VINTED_USER_AGENT = os.environ.get(
    "VINTED_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
).strip()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fast-buy-bot")

# ─── Vinted client ───────────────────────────────────────────────────────────
VINTED_BASE = "https://www.vinted.co.uk"


def _vinted_headers(csrf: Optional[str] = None) -> dict:
    """Standard headers matching a real logged-in browser session."""
    h = {
        "User-Agent": VINTED_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-GB,en;q=0.9",
        "Referer": f"{VINTED_BASE}/",
        "Origin": VINTED_BASE,
        "X-Requested-With": "XMLHttpRequest",
    }
    if csrf:
        h["X-CSRF-Token"] = csrf
    return h


def _vinted_cookies() -> dict:
    """
    Cookies required for an authenticated Vinted request.
    Two modes:
      (a) If VINTED_COOKIES env var is set, parse it as a whole cookie
          header string (semicolon-separated `name=value` pairs).
      (b) Otherwise fall back to individual env vars.
    """
    raw = os.environ.get("VINTED_COOKIES", "").strip()
    if raw:
        cookies: dict = {}
        for pair in raw.split(";"):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            name, _, value = pair.partition("=")
            if not name.strip():
                continue
            cookies[name.strip()] = value.strip()
        return cookies

    c: dict = {}
    if VINTED_SESSION_COOKIE:
        c["_vinted_fr_session"] = VINTED_SESSION_COOKIE
    if VINTED_ANON_ID:
        c["anon_id"] = VINTED_ANON_ID
    cf = os.environ.get("VINTED_CF_CLEARANCE", "").strip()
    if cf:
        c["cf_clearance"] = cf
    return c


def _fetch_csrf(session: requests.Session) -> Optional[str]:
    """
    Prefer a manually set CSRF token (VINTED_CSRF_TOKEN).
    If not set, try to extract it from the homepage HTML.
    """
    # ── 1. Manual override (recommended for now) ──────────────────────
    manual = os.environ.get("VINTED_CSRF_TOKEN", "").strip()
    if manual:
        logger.info("Using CSRF token from VINTED_CSRF_TOKEN env var")
        return manual

    cookies = _vinted_cookies()
    if not cookies:
        logger.warning("No Vinted cookies configured — set VINTED_COOKIES or VINTED_SESSION_COOKIE.")
        return None

    logger.info(
        "Fetching CSRF from Vinted with %d cookie(s): %s",
        len(cookies), sorted(cookies.keys()),
    )

    try:
        resp = session.get(
            f"{VINTED_BASE}/",
            headers={
                **_vinted_headers(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            cookies=cookies,
            timeout=12,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        logger.warning("CSRF fetch network error: %s", exc)
        return None

    logger.info(
        "CSRF fetch: status=%d, content-type=%s, body-len=%d",
        resp.status_code,
        resp.headers.get("content-type", ""),
        len(resp.content),
    )

    if resp.status_code != 200:
        logger.warning("CSRF non-200: %s", resp.text[:400])
        return None

    html = resp.text

    # Try common patterns
    patterns = [
        r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']csrf-token["\']',
        r'["\']CSRF_TOKEN["\']\s*:\s*["\']([^"\']+)["\']',
        r'csrfToken["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        r'"csrf"\s*:\s*"([^"]+)"',
        r'csrf[^"\']{0,30}["\']([0-9a-fA-F-]{30,})["\']',
    ]

    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m:
            logger.info("CSRF found with pattern: %s", pat[:40])
            return m.group(1)

    # Last resort: look inside __NEXT_DATA__
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL | re.I)
    if m:
        try:
            import json
            data = json.loads(m.group(1))
            for key in ("csrfToken", "csrf", "csrf_token", "CSRF_TOKEN"):
                found = re.search(rf'"{key}"\s*:\s*"([^"]+)"', m.group(1))
                if found:
                    logger.info("CSRF found inside __NEXT_DATA__")
                    return found.group(1)
        except Exception:
            pass

    logger.warning("Could not find CSRF token in HTML. First 800 chars:\n%s", html[:800])
    return None


def reserve_vinted_item(item_id: str) -> tuple[bool, str]:
    """
    Attempt to reserve a Vinted listing.
    Returns (success, human_readable_message).
    """
    if not VINTED_SESSION_COOKIE and not os.environ.get("VINTED_COOKIES"):
        return False, (
            "❌ Vinted cookies not configured on Railway. Set either "
            "`VINTED_COOKIES` (recommended — paste full Cookie header) "
            "or `VINTED_SESSION_COOKIE`."
        )

    session = requests.Session()
    csrf = _fetch_csrf(session)
    if not csrf:
        return False, (
            "❌ Could not authenticate with Vinted. Likely causes:\n"
            "• Your session cookie expired — log into vinted.co.uk in your browser again\n"
            "• Cloudflare blocked us — you need to send the full cookie header (set `VINTED_COOKIES` env var)\n"
            "• CSRF token missing — set VINTED_CSRF_TOKEN\n"
            "\nCheck the Railway Deploy Logs for exact HTTP status + body from Vinted."
        )

    url = f"{VINTED_BASE}/api/v2/item_transactions"
    body = {"transaction": {"item_id": int(item_id), "transaction_id": None}}

    try:
        resp = session.post(
            url,
            json=body,
            headers=_vinted_headers(csrf),
            timeout=15,
        )
    except requests.exceptions.RequestException as exc:
        return False, f"Network error contacting Vinted: {exc}"

    # ── Parse the response ───────────────────────────────────────────────
    if resp.status_code in (200, 201):
        try:
            data = resp.json()
            tx_id = (
                (data.get("transaction") or {}).get("id")
                or data.get("id")
                or "unknown"
            )
            return True, (
                f"✅ Reserved! Transaction id `{tx_id}`.\n"
                f"Open the Vinted app → **Wallet & Purchases → Ongoing** "
                f"and complete payment within ~15 minutes."
            )
        except ValueError:
            return True, "✅ Reserved (Vinted returned OK but no JSON body)."

    if resp.status_code == 401:
        return False, (
            "❌ Vinted rejected your session cookie (401). "
            "Log in again and update your cookies in Railway."
        )
    if resp.status_code == 403:
        return False, (
            "❌ Vinted forbade the request (403). Possible causes: "
            "CSRF token invalid, bot-detected, or account temporarily flagged."
        )
    if resp.status_code == 404:
        return False, "❌ Item no longer available (404 — probably just sold)."
    if resp.status_code == 409:
        return False, "❌ Item already sold or reserved by someone else (409)."
    if resp.status_code == 422:
        return False, (
            f"❌ Vinted rejected the request (422). Body: {resp.text[:250]}"
        )
    if resp.status_code == 429:
        return False, "❌ Vinted rate-limited (429). Try again in a minute."

    return False, (
        f"❌ Unexpected response from Vinted: HTTP {resp.status_code}\n"
        f"```{resp.text[:400]}```"
    )


# ─── Discord bot ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

_ITEM_RE = re.compile(r"vinted\.co\.uk/items/(\d+)", re.IGNORECASE)


class ReserveView(discord.ui.View):
    def __init__(self, item_id: str, listing_url: str):
        super().__init__(timeout=600)
        self.item_id = item_id
        self.listing_url = listing_url

    @discord.ui.button(
        label="⚡ RESERVE NOW",
        style=discord.ButtonStyle.danger,
        custom_id="reserve",
    )
    async def reserve_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.defer(ephemeral=False)

        logger.info(
            "Reserve requested by %s for item %s",
            interaction.user, self.item_id,
        )

        loop = asyncio.get_running_loop()
        success, message = await loop.run_in_executor(
            None, reserve_vinted_item, self.item_id,
        )

        if success:
            button.label = "✅ RESERVED"
            button.style = discord.Button
