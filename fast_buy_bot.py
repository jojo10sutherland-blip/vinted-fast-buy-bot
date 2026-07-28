"""
fast_buy_bot.py
---------------
A standalone Discord bot that adds a "⚡ RESERVE NOW" button underneath
every iPhone-deal alert your existing iPhone bot posts into a Discord
channel — and reserves the Vinted item for you when you tap it.

Design goals:
  1. ZERO changes required to your existing iPhone bot code.
  2. Runs as its own Railway service (separate process). If this bot
     crashes or is deleted, your iPhone bot is completely unaffected.
  3. Detects listing IDs automatically by parsing the webhook messages
     posted by your iPhone bot into the target Discord channel.
  4. All secrets (bot token, session cookie, channel id) come from
     environment variables — nothing sensitive is hard-coded.
  5. Extensive error reporting: if the Vinted API changes, you'll see
     exactly what came back in the button reply.

Environment variables (required):
  DISCORD_BOT_TOKEN        - from https://discord.com/developers/applications
  DISCORD_CHANNEL_ID       - the Discord channel ID to watch (numeric)
  VINTED_SESSION_COOKIE    - the `_vinted_fr_session` cookie value from
                             your logged-in browser (see README)

Optional:
  VINTED_ANON_ID           - the `anon_id` cookie (improves auth reliability)
  VINTED_USER_AGENT        - browser UA string to match your session cookie
  LOG_LEVEL                - DEBUG / INFO (default INFO)
"""

import asyncio
import logging
import os
import re
from typing import Optional

import discord
import requests

# ─── configuration from env ──────────────────────────────────────────────────
DISCORD_BOT_TOKEN     = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DISCORD_CHANNEL_ID    = int(os.environ.get("DISCORD_CHANNEL_ID", "0") or 0)
VINTED_SESSION_COOKIE = os.environ.get("VINTED_SESSION_COOKIE", "").strip()
VINTED_ANON_ID        = os.environ.get("VINTED_ANON_ID", "").strip()
VINTED_USER_AGENT     = os.environ.get(
    "VINTED_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
).strip()
LOG_LEVEL             = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fast-buy-bot")


# ─── Vinted client ───────────────────────────────────────────────────────────
VINTED_BASE = "https://www.vinted.co.uk"


def _vinted_headers(csrf: Optional[str] = None) -> dict:
    """Standard headers matching a real logged-in browser session."""
    h = {
        "User-Agent":       VINTED_USER_AGENT,
        "Accept":           "application/json, text/plain, */*",
        "Accept-Language":  "en-GB,en;q=0.9",
        "Referer":          f"{VINTED_BASE}/",
        "Origin":           VINTED_BASE,
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
          header string (semicolon-separated `name=value` pairs — the
          format you can copy from browser DevTools → Network → any
          request → Request Headers → Cookie).  This is the MOST
          RELIABLE way because it captures every cookie Vinted needs,
          including `cf_clearance` (Cloudflare) and any others.
      (b) Otherwise fall back to individual env vars:
          VINTED_SESSION_COOKIE + optional VINTED_ANON_ID +
          optional VINTED_CF_CLEARANCE.
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

    # Fallback — individual vars
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
    Hit /api/v2/users/current to (a) verify our session cookies still work
    and (b) grab a fresh CSRF token.  Checks headers, response body, and
    Set-Cookie values (Vinted's CSRF location has moved around over time).

    Returns the CSRF token or None on failure.
    """
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
            f"{VINTED_BASE}/api/v2/users/current",
            headers=_vinted_headers(),
            cookies=cookies,
            timeout=10,
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

    # ── Handle non-200 responses ─────────────────────────────────────
    if resp.status_code != 200:
        body_preview = resp.text[:400]
        logger.warning("CSRF non-200: body preview: %s", body_preview)
        if resp.status_code == 401:
            logger.warning("→ 401 means your `_vinted_fr_session` cookie is expired or invalid.")
        elif resp.status_code == 403:
            if "cloudflare" in body_preview.lower() or "cf-ray" in resp.headers:
                logger.warning(
                    "→ 403 from Cloudflare — you need the cf_clearance cookie. "
                    "Set VINTED_COOKIES with the full cookie header (recommended)."
                )
            else:
                logger.warning("→ 403 — bot may be flagged, or CSRF mismatch.")
        return None

    # ── Try to extract the CSRF token from multiple locations ────────
    # 1. Response headers (older Vinted API returned it here)
    csrf = resp.headers.get("x-csrf-token") or resp.headers.get("X-CSRF-Token")
    if csrf:
        logger.debug("CSRF found in response header")
        return csrf

    # 2. Response body — user.csrf_token / csrf_token / csrfToken
    try:
        body = resp.json()
        csrf = (
            (body.get("user") or {}).get("csrf_token")
            or body.get("csrf_token")
            or body.get("csrfToken")
        )
        if csrf:
            logger.debug("CSRF found in response JSON body")
            return csrf
    except ValueError:
        pass

    # 3. Set-Cookie — Vinted sometimes rotates CSRF via a cookie
    for cookie_name in ("_vinted_fr_anti_csrf", "csrf_token", "X-CSRF-Token"):
        val = resp.cookies.get(cookie_name)
        if val:
            logger.debug("CSRF found in Set-Cookie (%s)", cookie_name)
            return val

    # 4. HTML meta tag (if we somehow landed on the HTML page)
    if "text/html" in resp.headers.get("content-type", ""):
        m = re.search(r'name="csrf-token"[^>]*content="([^"]+)"', resp.text)
        if m:
            logger.debug("CSRF found in HTML meta tag")
            return m.group(1)

    logger.warning(
        "Could not find CSRF token anywhere. Response headers: %s. Body preview: %s",
        dict(resp.headers), resp.text[:300],
    )
    return None


def reserve_vinted_item(item_id: str) -> tuple[bool, str]:
    """
    Attempt to reserve a Vinted listing (put it in your cart / open checkout).

    Returns (success, human_readable_message).

    This tries the current Vinted UK checkout endpoint. If Vinted has
    changed their API, the returned message will include the raw response
    so you can adjust the endpoint.
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
            "\nCheck the Railway Deploy Logs for exact HTTP status + body from Vinted."
        )

    # Vinted's "single checkout" endpoint — starts the buy flow for one item
    # and creates a transaction that holds the item for ~15 minutes.
    url = f"{VINTED_BASE}/api/v2/item_transactions"
    body = {"transaction": {"item_id": int(item_id), "transaction_id": None}}

    try:
        resp = session.post(
            url,
            json=body,
            headers=_vinted_headers(csrf),
            cookies=_vinted_cookies(),
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
                f"✅ Reserved!  Transaction id `{tx_id}`.\n"
                f"Open the Vinted app → **Wallet & Purchases → Ongoing** "
                f"and complete payment within ~15 minutes."
            )
        except ValueError:
            return True, "✅ Reserved (Vinted returned OK but no JSON body)."

    if resp.status_code == 401:
        return False, (
            "❌ Vinted rejected your session cookie (401). "
            "Log in again and update `VINTED_SESSION_COOKIE` in Railway."
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

    # Any other status
    return False, (
        f"❌ Unexpected response from Vinted: HTTP {resp.status_code}\n"
        f"```{resp.text[:400]}```"
    )


# ─── Discord bot ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True   # need this to read message content for URL parsing

client = discord.Client(intents=intents)

# Regex to extract the numeric Vinted listing ID from any URL that appears
# in the alert message content OR any embed field.
_ITEM_RE = re.compile(r"vinted\.co\.uk/items/(\d+)", re.IGNORECASE)


class ReserveView(discord.ui.View):
    """A single-button view attached to each auto-reply."""

    def __init__(self, item_id: str, listing_url: str):
        # 10-minute timeout — after that the button becomes non-clickable
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
        # Acknowledge immediately so Discord doesn't timeout the interaction
        await interaction.response.defer(ephemeral=False)

        logger.info(
            "Reserve requested by %s for item %s",
            interaction.user, self.item_id,
        )

        # Run the blocking HTTP call in a thread so we don't block the bot
        loop = asyncio.get_running_loop()
        success, message = await loop.run_in_executor(
            None, reserve_vinted_item, self.item_id,
        )

        # Update the button to reflect the outcome
        if success:
            button.label = "✅ RESERVED"
            button.style = discord.ButtonStyle.success
            button.disabled = True
        else:
            button.label = "❌ Failed — see reply"
            button.style = discord.ButtonStyle.secondary
            # Keep enabled so user can retry

        await interaction.edit_original_response(view=self)
        await interaction.followup.send(
            f"{message}\n\nListing: {self.listing_url}",
            ephemeral=False,
        )


def _extract_item_id(message: discord.Message) -> Optional[str]:
    """
    Look in the message content AND every embed's url/description/fields
    for a Vinted listing URL, and return the numeric item ID.
    """
    def scan(text: str) -> Optional[str]:
        if not text:
            return None
        m = _ITEM_RE.search(text)
        return m.group(1) if m else None

    if hit := scan(message.content):
        return hit

    for embed in message.embeds:
        for text in (embed.url, embed.title, embed.description):
            if hit := scan(text or ""):
                return hit
        for f in embed.fields:
            if hit := scan(f.value or ""):
                return hit

    return None


def _build_listing_url(item_id: str) -> str:
    return f"{VINTED_BASE}/items/{item_id}"


@client.event
async def on_ready():
    logger.info("Fast-Buy bot logged in as %s (id=%s)", client.user, client.user.id)
    logger.info("Watching channel id: %s", DISCORD_CHANNEL_ID)
    if not VINTED_SESSION_COOKIE:
        logger.warning(
            "VINTED_SESSION_COOKIE is not set — the RESERVE button will fail "
            "until you add it to Railway env vars."
        )


@client.event
async def on_message(message: discord.Message):
    # Only care about our target channel
    if DISCORD_CHANNEL_ID and message.channel.id != DISCORD_CHANNEL_ID:
        return

    # Don't try to attach a button to our own reply messages
    if message.author.id == (client.user.id if client.user else 0):
        return

    # Look for a Vinted listing ID somewhere in the message
    item_id = _extract_item_id(message)
    if not item_id:
        return

    listing_url = _build_listing_url(item_id)
    logger.info("Detected listing %s in msg %s — posting reserve button.",
                item_id, message.id)

    try:
        await message.reply(
            content=f"⚡ Tap below to reserve `#{item_id}` (holds ~15 min for payment):",
            view=ReserveView(item_id=item_id, listing_url=listing_url),
            mention_author=False,
        )
    except discord.errors.Forbidden:
        logger.error(
            "Bot lacks permission to reply in channel %s — check its role/permissions.",
            message.channel.id,
        )
    except Exception as exc:
        logger.error("Failed to post reserve button: %s", exc)


def main() -> None:
    if not DISCORD_BOT_TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN env var is required.")
    if not DISCORD_CHANNEL_ID:
        raise SystemExit("DISCORD_CHANNEL_ID env var is required.")

    logger.info("Starting Fast-Buy bot…")
    client.run(DISCORD_BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
