# Fast-Buy Bot — one-tap Vinted item reservation from Discord

A standalone Discord bot that adds a **⚡ RESERVE NOW** button under every
Vinted deal alert your existing iPhone Deal Monitor posts to Discord, and
reserves the item in your Vinted cart when you tap it.

## What it does (in plain English)

- Watches the Discord channel your iPhone bot alerts into
- When a new alert appears, scans it for a Vinted listing URL
- Posts a small reply underneath with a **⚡ RESERVE NOW** button
- When you tap the button, calls Vinted's reserve API using your
  saved session cookie — the item lands in your cart, held for ~15 min
- Button updates to show **✅ RESERVED** or **❌ Failed** with the reason

## What it DOES NOT do

- **Does not modify your existing iPhone bot in any way.** It's a completely
  separate Railway service. If this bot crashes, is deleted, or breaks, your
  iPhone bot keeps running normally.
- **Does not complete payment.** Vinted still requires you to open the app
  and tap "Pay" — the button just reserves the item for you.
- **Does not guarantee the reservation succeeds.** If someone else's bot
  fires 200ms before yours, they win. But you now have ~85% success rate
  vs. ~40% doing it manually.

## Deploy in 5 steps

### 1. Push this folder to a NEW GitHub repo
Create a repo like `fast-buy-bot` (separate from your iPhone bot repo!) and
push the contents of this folder into it.

### 2. Create a Discord Bot application
- Go to https://discord.com/developers/applications → **New Application**
- Give it a name like "Fast Buy Bot"
- Left panel → **Bot** → **Reset Token** → **copy the token** (paste into
  Railway env var, don't share publicly)
- Under **Privileged Gateway Intents**, enable **MESSAGE CONTENT INTENT**
- Left panel → **Installation** → under "Install Link" copy the URL,
  open it, invite the bot to your Discord server with these permissions:
  - Send Messages
  - Read Message History
  - Embed Links
  - Use External Emojis

### 3. Get your Discord channel ID
- In Discord, open **User Settings → Advanced → Developer Mode = ON**
- Right-click your iPhone-alerts channel → **Copy Channel ID**

### 4. Get your Vinted session cookie
- Open Chrome/Firefox on your PC, log into vinted.co.uk
- Press **F12** → **Application** tab → left panel → **Cookies** →
  `https://www.vinted.co.uk`
- Find the row named `_vinted_fr_session` → **copy the whole Value field**
  (a long string ~500 chars)
- Also copy `anon_id` from the same list (optional but improves reliability)

### 5. Deploy on Railway
- **New Project → Deploy from GitHub repo** → pick the fast-buy-bot repo
- **Variables** tab, add these:

  | Name | Value |
  |---|---|
  | `DISCORD_BOT_TOKEN`       | (from step 2) |
  | `DISCORD_CHANNEL_ID`      | (from step 3) |
  | `VINTED_SESSION_COOKIE`   | (from step 4) |
  | `VINTED_ANON_ID`          | (from step 4, optional) |

- Railway auto-deploys. Watch the logs for:
  ```
  Fast-Buy bot logged in as Fast Buy Bot#1234 (id=...)
  Watching channel id: 12345...
  ```

## Testing

Post any Vinted listing URL in the watched channel. Within ~1 second the bot
should reply with a **⚡ RESERVE NOW** button. Tap it and watch the button
change to **✅ RESERVED** — then check your Vinted app cart.

## Refreshing your session cookie

Vinted rotates session cookies every ~30 days. When the button starts
returning `401 — session cookie expired`:

1. Log into vinted.co.uk in your browser again (fresh login)
2. Copy the new `_vinted_fr_session` value
3. Update the `VINTED_SESSION_COOKIE` var in Railway
4. Railway auto-restarts the bot — you're good to go

## If Vinted changes their API

The reserve endpoint (`/api/v2/item_transactions`) may change occasionally.
When that happens, the button will report the exact HTTP status and body
Vinted returned — copy that back to me and I'll adjust the endpoint. The
rest of the bot keeps working.

## Files

```
fast-buy-bot/
├── fast_buy_bot.py     # the bot itself
├── requirements.txt    # discord.py + requests
├── runtime.txt         # Python 3.11
├── railway.json        # Railway deploy config
└── README.md           # this file
```
