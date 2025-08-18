# main.py
import os
import time
import hmac
import hashlib
from typing import Dict, Optional, Tuple, List

import httpx
from pydantic import BaseModel
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =========================
# Config (from environment)
# =========================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))

# Global single threshold (percent).
THRESHOLD_PCT = float(os.environ.get("THRESHOLD_PCT", "0.1"))

# Optional per-pair thresholds (percent), format: "ETH:0.15,BTC:0.20"
_pair_raw = os.environ.get("THRESHOLDS_PER_PAIR", "").strip()
THRESHOLDS_PER_PAIR: Dict[str, float] = {}
if _pair_raw:
    for part in _pair_raw.split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            try:
                THRESHOLDS_PER_PAIR[k.strip().upper()] = float(v.strip())
            except Exception:
                pass

# Pairs to track
ASSETS: List[str] = ["BTC", "ETH", "SOL"]

# Venue API bases
EXT_BASE = os.environ.get("EXTENDED_API_BASE", "https://api.starknet.extended.exchange")
LIGHTER_API_BASE = os.environ.get("LIGHTER_API_BASE")
LIGHTER_KEY = os.environ.get("LIGHTER_API_KEY")
LIGHTER_SECRET = os.environ.get("LIGHTER_API_SECRET")

# Optional header name overrides for Lighter signing
LIGHTER_HDR_KEY = os.environ.get("LIGHTER_HDR_KEY", "X-API-KEY")
LIGHTER_HDR_SIG = os.environ.get("LIGHTER_HDR_SIG", "X-SIGN")
LIGHTER_HDR_TS  = os.environ.get("LIGHTER_HDR_TS", "X-TS")

LIGHTER_ENABLED = bool(LIGHTER_API_BASE and LIGHTER_KEY and LIGHTER_SECRET)

# Fees only (bps) — no slippage per your request
FEE_BPS_EXT_OPEN  = float(os.environ.get("FEE_BPS_EXT_OPEN",  "22"))
FEE_BPS_EXT_CLOSE = float(os.environ.get("FEE_BPS_EXT_CLOSE", "22"))
FEE_BPS_LIG_OPEN  = float(os.environ.get("FEE_BPS_LIG_OPEN",  "0"))
FEE_BPS_LIG_CLOSE = float(os.environ.get("FEE_BPS_LIG_CLOSE", "0"))

# =========================
# Models & helpers
# =========================
class TopOfBook(BaseModel):
    bid: float
    ask: float

class VenueQuotes(BaseModel):
    extended: Optional[TopOfBook]
    lighter: Optional[TopOfBook]

# Map asset -> market symbol per venue
EXT_MARKETS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
LIGHTER_MARKETS = {"BTC": "BTC-PERP", "ETH": "ETH-PERP", "SOL": "SOL-PERP"}  # adjust if different

async def fetch_extended_tob(client: httpx.AsyncClient, asset: str) -> Optional[TopOfBook]:
    market = EXT_MARKETS.get(asset)
    if not market:
        return None
    url = f"{EXT_BASE}/api/v1/info/markets/{market}/orderbook"
    r = await client.get(url, timeout=10)
    r.raise_for_status()
    j = r.json()
    data = j.get("data", {}) if isinstance(j, dict) else {}
    bids = data.get("bid", [])
    asks = data.get("ask", [])
    if not bids or not asks:
        return None
    b0 = bids[0]
    a0 = asks[0]
    bid = float(b0.get("price", b0[1] if isinstance(b0, list) else b0))
    ask = float(a0.get("price", a0[0] if isinstance(a0, list) else a0))
    return TopOfBook(bid=bid, ask=ask)

async def fetch_lighter_tob(client: httpx.AsyncClient, asset: str) -> Optional[TopOfBook]:
    if not LIGHTER_ENABLED:
        return None
    symbol = LIGHTER_MARKETS.get(asset)
    if not symbol:
        return None
    # --- Lighter signing (generic HMAC-SHA256 template) ---
    ts = str(int(time.time() * 1000))
    method = "GET"
    path = "/orderBookOrders"
    query = f"market={symbol}"
    prehash = f"{ts}{method}{path}?{query}"
    sig = hmac.new(
        bytes(LIGHTER_SECRET or "", "utf-8"),
        prehash.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        LIGHTER_HDR_KEY: LIGHTER_KEY or "",
        LIGHTER_HDR_SIG: sig,
        LIGHTER_HDR_TS: ts,
    }
    try:
        r = await client.get(
            f"{LIGHTER_API_BASE}{path}",
            params={"market": symbol},
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        j = r.json()
        bids = j.get("bids") or j.get("bid") or []
        asks = j.get("asks") or j.get("ask") or []
        if not bids or not asks:
            return None
        b0 = bids[0]
        a0 = asks[0]
        bid = float(b0[0] if isinstance(b0, list) else b0.get("price"))
        ask = float(a0[0] if isinstance(a0, list) else a0.get("price"))
        return TopOfBook(bid=bid, ask=ask)
    except Exception:
        return None

async def get_quotes(client: httpx.AsyncClient, asset: str) -> VenueQuotes:
    ext = await fetch_extended_tob(client, asset)
    lig = await fetch_lighter_tob(client, asset)
    return VenueQuotes(extended=ext, lighter=lig)

def _roundtrip_bps(direction: str) -> float:
    """
    Total fees (bps) across 4 legs (entry+exit on both venues). Slippage is ignored per your request.
    """
    if direction == "EXT->LIG":
        fee_bps = FEE_BPS_EXT_OPEN + FEE_BPS_LIG_OPEN + FEE_BPS_EXT_CLOSE + FEE_BPS_LIG_CLOSE
    else:  # LIG->EXT
        fee_bps = FEE_BPS_LIG_OPEN + FEE_BPS_EXT_OPEN + FEE_BPS_LIG_CLOSE + FEE_BPS_EXT_CLOSE
    return fee_bps

def best_net_edge(quotes: VenueQuotes) -> Tuple[float, str, str]:
    """
    Returns (net_edge_pct, direction, detail)
    direction ∈ {"EXT->LIG", "LIG->EXT", "N/A"}
    """
    ext, lig = quotes.extended, quotes.lighter
    if not ext or not lig:
        return (0.0, "N/A", "Lighter disabled or missing data")

    # Gross crossed spreads
    gross_ext_to_lig = (lig.bid - ext.ask) / ext.ask  # buy ask EXT, sell bid LIG
    gross_lig_to_ext = (ext.bid - lig.ask) / lig.ask  # buy ask LIG, sell bid EXT

    # Subtract fees (bps → fraction)
    net1 = gross_ext_to_lig - _roundtrip_bps("EXT->LIG") / 10000.0
    net2 = gross_lig_to_ext - _roundtrip_bps("LIG->EXT") / 10000.0

    if net1 >= net2:
        return (net1 * 100, "EXT->LIG", f"buy ask EXT {ext.ask:.2f} / sell bid LIG {lig.bid:.2f}")
    else:
        return (net2 * 100, "LIG->EXT", f"buy ask LIG {lig.ask:.2f} / sell bid EXT {ext.bid:.2f}")

# =========================
# Bot state
# =========================
LAST_ALERT_TS: Dict[str, float] = {}
ALERT_COOLDOWN = 120  # seconds per pair
PAUSED = False

async def check_and_alert(application) -> None:
    """Fetch prices for all assets and alert if net edge ≥ threshold."""
    if PAUSED:
        return
    async with httpx.AsyncClient() as client:
        for asset in ASSETS:
            q = await get_quotes(client, asset)
            pct, direction, detail = best_net_edge(q)
            thr = THRESHOLDS_PER_PAIR.get(asset, THRESHOLD_PCT)
            if pct >= thr:
                now = time.time()
                if now - LAST_ALERT_TS.get(asset, 0) >= ALERT_COOLDOWN:
                    msg = (
                        f"🟢 Arb {asset} — net {pct:.3f}% ({direction})\n"
                        f"{detail}\n"
                        f"EXT bid/ask: {q.extended.bid if q.extended else '—'} / {q.extended.ask if q.extended else '—'}\n"
                        f"LIG bid/ask: {q.lighter.bid if q.lighter else '—'} / {q.lighter.ask if q.lighter else '—'}"
                    )
                    try:
                        await application.bot.send_message(chat_id=CHAT_ID, text=msg)
                        LAST_ALERT_TS[asset] = now
                    except Exception as e:
                        print("Telegram send error:", e)

# =========================
# Telegram command handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Hi! Monitoring {', '.join(ASSETS)}.\n"
        f"Global threshold: {THRESHOLD_PCT}%\n"
        f"Per-pair: {THRESHOLDS_PER_PAIR or '(none)'}\n"
        f"Use /top to view current edges, /setpairs to change pairs."
    )

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with httpx.AsyncClient() as client:
        lines = []
        for asset in ASSETS:
            q = await get_quotes(client, asset)
            pct, direction, detail = best_net_edge(q)
            lines.append(f"{asset}: {pct:.3f}% — {direction} | {detail}")
        await update.message.reply_text("\n".join(lines) if lines else "No data.")

async def cmd_setpairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ASSETS
    raw = " ".join(context.args)
    parts = [p.strip().upper() for p in raw.replace(",", " ").split() if p.strip()]
    if not parts:
        return await update.message.reply_text("Usage: /setpairs BTC,ETH,SOL")
    ASSETS = parts
    await update.message.reply_text("Pairs set to: " + ", ".join(ASSETS))

# =========================
# App bootstrap
# =========================
def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("setpairs", cmd_setpairs))

    # Background job: run every POLL_SECONDS
    async def job_check(context):
        await check_and_alert(context.application)

    app.job_queue.run_repeating(job_check, interval=POLL_SECONDS, first=5)

    # Run bot
    print("Bot started. Send /start in Telegram.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
