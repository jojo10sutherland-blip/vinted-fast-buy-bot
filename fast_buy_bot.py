print("=== FAST BUY BOT STARTING ===", flush=True)
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
    manual = os.environ.get("VINTED_CSRF_TOKEN", "").strip()
    if manual:
        logger.info("Using CSRF token from VINTED_CSRF_TOKEN env var")
        return manual

    cookies = _vinted_cookies()
    if not cookies:
        logger.warning("No Vinted cookies configured.")
        return None

    logger.info("Fetching CSRF from Vinted with %d cookie(s)", len(cookies))

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

    logger.info("CSRF fetch: status=%d, body-len=%d", resp.status_code, len(resp.content))

    if resp.status_code != 200:
        logger.warning("CSRF non-200: %s", resp.text[:400])
        return None

    html = resp.text

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
            logger.info("CSRF found with pattern")
            return m.group(1)

    logger.warning("Could not find CSRF token in HTML")
    return None


def _get_item_seller_id(session: requests.Session, item_id: str, csrf: str) -> Optional[str]:
    """Fetch item details to get the seller's user ID."""
    url = f"{VINTED_BASE}/api/v2/items/{item_id}"
    try:
        resp = session.get(
            url,
            headers=_vinted_headers(csrf),
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Failed to fetch item %s: status=%s", item_id, resp.status_code)
            return None

        data = resp.json()
        # Seller ID is usually under item.user.id
        user = (data.get("item") or {}).get("user") or {}
        seller_id = user.get("id")
        if seller_id:
            logger.info("Found seller_id=%s for item %s", seller_id, item_id)
            return str(seller_id)

        logger.warning("Could not find seller id in item response")
        return None
    except Exception as e:
        logger.warning("Error fetching item seller: %s", e)
        return None


def reserve_vinted_item(item_id: str) -> tuple[bool, str]:
    if not VINTED_SESSION_COOKIE and not os.environ.get("VINTED_COOKIES"):
        return False, "❌ Vinted cookies not configured on Railway."

    session = requests.Session()

    # Always put cookies on the session
    cookies = _vinted_cookies()
    if cookies:
        session.cookies.update(cookies)

    csrf = _fetch_csrf(session)
    if not csrf:
        return False, "❌ Could not get CSRF token. Set VINTED_CSRF_TOKEN or check cookies."

    # 1. Get the seller ID
    seller_id = _get_item_seller_id(session, item_id, csrf)
    if not seller_id:
        return False, "❌ Could not get seller ID for this item."

    # 2. Create the conversation / transaction (this is the real reserve step)
    url = f"{VINTED_BASE}/api/v2/conversations"
    body = {
        "initiator": "buy",
        "item_id": str(item_id),
        "opposite_user_id": str(seller_id),
    }

    logger.info("Sending reserve request to %s with body %s", url, body)
    logger.info("Cookies being sent: %s", list(session.cookies.keys()))

    try:
        resp = session.post(
            url,
            json=body,
            headers=_vinted_headers(csrf),
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        return False, f"Network error: {e}"

    logger.info(
        "Vinted response: status=%s, content-type=%s, body=%s",
        resp.status_code,
        resp.headers.get("content-type", ""),
        resp.text[:600],
    )

    if resp.status_code in (200, 201):
        try:
            data = resp.json()
            conversation = data.get("conversation") or {}
            transaction = conversation.get("transaction") or {}
            tx_id = transaction.get("id") or "unknown"

            return True, (
                f"✅ Reserved / transaction started!\n"
                f"Transaction ID: `{tx_id}`\n"
                f"Open the Vinted app → **Inbox** or **Wallet & Purchases → Ongoing** "
                f"and complete payment."
            )
        except Exception:
            return True, "✅ Request succeeded (check your Vinted app)."

    if resp.status_code == 401:
        return False, "❌ Session cookie rejected (401). Update your cookies."
    if resp.status_code == 403:
        return False, "❌ Forbidden (403). CSRF invalid or blocked."
    if resp.status_code == 404:
        return False, "❌ Item not found or already sold (404)."
    if resp.status_code == 409:
        return False, "❌ Item already reserved/sold (409)."
    if resp.status_code == 422:
        return False, f"❌ Rejected (422): {resp.text[:300]}"
    if resp.status_code == 429:
        return False, "❌ Rate limited (429)."

    return False, f"❌ Unexpected HTTP {resp.status_code}\n```{resp.text[:400]}```"


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
    async def reserve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=False)

        logger.info("Reserve requested by %s for item %s", interaction.user, self.item_id)

        loop = asyncio.get_running_loop()
        success, message = await loop.run_in_executor(None, reserve_vinted_item, self.item_id)

        if success:
            button.label = "✅ RESERVED"
            button.style = discord.ButtonStyle.success
            button.disabled = True
        else:
            button.label = "❌ Failed — see reply"
            button.style = discord.ButtonStyle.secondary

        await interaction.edit_original_response(view=self)
        await interaction.followup.send(f"{message}\n\nListing: {self.listing_url}", ephemeral=False)


def _extract_item_id(message: discord.Message) -> Optional[str]:
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


@client.event
async def on_message(message: discord.Message):
    if DISCORD_CHANNEL_ID and message.channel.id != DISCORD_CHANNEL_ID:
        return
    if message.author.id == (client.user.id if client.user else 0):
        return

    item_id = _extract_item_id(message)
    if not item_id:
        return

    listing_url = _build_listing_url(item_id)
    logger.info("Detected listing %s in msg %s — posting reserve button.", item_id, message.id)

    try:
        await message.reply(
            content=f"⚡ Tap below to reserve `#{item_id}`:",
            view=ReserveView(item_id=item_id, listing_url=listing_url),
            mention_author=False,
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
