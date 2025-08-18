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

# Global single threshold (percent). Used if no per-pair or levels provided.
THRESHOLD_PCT = float(os.environ.get("THRESHOLD_PCT", "0.1"))

# Optional multi-level thresholds (percent list).
# Example: THRESHOLD_LEVELS="0.10,0.15,0.20,0.25"
_levels_raw = os.environ.get("THRESHOLD_LEVELS", "").strip()
THRESHOLD_LEVELS = (
    sorted([float(x) for x in _levels_raw.split(",") if x.strip()], key=float)
    if _levels_raw else []
)

# Optional per-pair single-threshold overrides (percent).
# Example: THRESHOLD_PER_PAIR="ETH=0.15,BTC=0.20"
_pair_raw = os.environ.get("THRESHOLD_PER_PAIR", "").strip()
THRESHOLD_PER_PAIR: Dict[str, float] = {}
if _pair_raw:
    for part in _pair_raw.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip().upper()
            v = v.strip()
            try:
                THRESHOLD_PER_PAIR[k] = float(v)
            except Exception:
                pass

# Pairs to track (tickers; mapping to venue symbols below)
ASSETS: List[str] = [
    s.strip().upper() for s in os.environ.get("PAIRS", "BTC,ETH,SOL").split(",") if s.strip()
]

# Venue API bases
EXT_BASE = os.environ.get("EXTENDED_API_BASE", "https://api.starknet.extended.exchange")
LIGHTER_API_BASE = os.environ.get("LIGHTER_API_BASE")
LIGHTER_KEY = os.environ.get("LIGHTER_API_KEY")
LIGHTER_SECRET = os.environ.get("LIGHTER_API_SECRET")

# Optional header name overrides for Lighter signing (if their docs use different names)
LIGHTER_HDR_KEY = os.environ.get("LIGHTER_HDR_KEY", "X-API-KEY")
LIGHTER_HDR_SIG = os.environ.get("LIGHTER_HDR_SIG", "X-SIGN")
LIGHTER_HDR_TS = os.environ.get("LIGHTER_HDR_TS", "X-TS")

LIGHTER_ENABLED = bool(LIGHTER_API_BASE and LIGHTER_KEY and LIGHTER_SECRET)

# Fees & slippage (basis points)
# Lighter: 0 bps open/close; Extended: 22 bps open + 22 bps close (your request)
FEE_BPS_LIG_OPEN = float(os.environ.get("FEE_BPS_LIG_OPEN", "0"))
FEE_BPS_LIG_CLOSE = float(os.environ.get("FEE_BPS_LIG_CLOSE", "0"))
FEE_BPS_EXT_OPEN = float(os.environ.get("FEE_BPS_EXT_OPEN", "22"))
FEE_BPS_EXT_CLOSE = float(os.environ.get("FEE_BPS_EXT_CLOSE", "22"))
# Slippage cushion per leg (4 legs per round-trip: enter+exit on both venues)
SLIPPAGE_BPS_PER_LEG = float(os.environ.get("SLIPPAGE_BPS_PER_LEG", "5"))

# (Optional; not used yet but left for future) funding adjustment weight (0..1)
FUNDING_ADJUST_WEIGHT = float(os.environ.get("FUNDING_ADJUST_WEIGHT", "0"))

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
# Adjust these if Lighter uses different symbols
LIGHTER_MARKETS = {"BTC": "BTC-PERP", "ETH": "ETH-PERP", "SOL": "SOL-PERP"}

async def fetch_extended_tob(client: httpx.AsyncClient, asset: str) -> Optional[TopOfBook]:
    market = EXT_MARKETS.get(asset)
    if not market:
        return None
    url = f"{EXT_BASE}/api/v1/info/markets/{market}/orderbook"
    resp = await client.get(url, timeout=10)
    resp.raise_for_status()
    j = resp.json()
    data = j.get("data", {}) if isinstance(j, dict) else {}
    bids = data.get("bid", [])
    asks = data.get("ask", [])
    if not bids or not asks:
        return None
    # Extended may return list-of-dicts or list-of-lists; support both
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
    # Adjust to Lighter's exact spec if needed (change header names via env vars; tweak prehash if doc differs)
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
        resp = await client.get(
            f"{LIGHTER_API_BASE}{path}",
            params={"market": symbol},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        j = resp.json()
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
    Total round-trip costs (fees + slippage), in bps, across 4 legs:
    - EXT open + LIG open + EXT close + LIG close + 4 * slippage_per_leg
    """
    if direction == "EXT->LIG":
        fee_bps = FEE_BPS_EXT_OPEN + FEE_BPS_LIG_OPEN + FEE_BPS_EXT_CLOSE + FEE_BPS_LIG_CLOSE
    else:  # "LIG->EXT"
        fee_bps = FEE_BPS_LIG_OPEN + FEE_BPS_EXT_OPEN + FEE_BPS_LIG_CLOSE + FEE_BPS_EXT_CLOSE
    slip_bps = 4 * SLIPPAGE_BPS_PER_LEG
    return fee_bps + slip_bps

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
    # Subtract total costs (bps → fraction)
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
# Track last "level index" to avoid repeating the same level alert repeatedly.
LAST_LEVEL_IDX: Dict[str, int] = {}
ALERT_COOLDOWN = 120  # seconds per pair
PAUSED = False

def _min_threshold_for(asset: str) -> float:
    """Returns the minimum trigger threshold to use for an asset (percent)."""
    if THRESHOLD_LEVELS:
        return THRESHOLD_LEVELS[0]
    return THRESHOLD_PER_PAIR.get(asset, THRESHOLD_PCT)

async def check_and_alert(application) -> None:
    """Fetch prices, compute net edges, sort by largest, and send Telegram alerts."""
    global LAST_ALERT_TS, LAST_LEVEL_IDX
    if PAUSED:
        return
    async with httpx.AsyncClient() as client:
        rows = []
        for asset in ASSETS:
            q = await get_quotes(client, asset)
            pct, direction, detail = best_net_edge(q)
            rows.append((asset, pct, direction, detail, q))
        rows.sort(key=lambda x: x[1], reverse=True)

        for (asset, pct, direction, detail, q) in rows:
            trigger = False
            level_hit = None
            now = time.time()

            if THRESHOLD_LEVELS:
                # Find highest level crossed.
                idx = -1
                for i, lvl in enumerate(THRESHOLD_LEVELS):
                    if pct >= lvl:
                        idx = i
                if idx >= 0:
                    prev = LAST_LEVEL_IDX.get(asset, -1)
                    # Alert if new higher level crossed, or cooldown elapsed.
                    if idx > prev or (now - LAST_ALERT_TS.get(asset, 0) >= ALERT_COOLDOWN):
                        trigger = True
                        level_hit = THRESHOLD_LEVELS[idx]
                        LAST_LEVEL_IDX[asset] = idx
            else:
                # Single-threshold mode (global or per-pair)
                if pct >= _min_threshold_for(asset) and (now - LAST_ALERT_TS.get(asset, 0) >= ALERT_COOLDOWN):
                    trigger = True

            if trigger:
                msg_lines = [
                    f"🟢 Arb {asset} — net {pct:.3f}% ({direction})",
                    f"EXT bid/ask: {q.extended.bid if q.extended else '—'} / {q.extended.ask if q.extended else '—'}",
                    f"LIG bid/ask: {q.lighter.bid if q.lighter else '—'} / {q.lighter.ask if q.lighter else '—'}",
                    f"{detail}",
                ]
                if level_hit is not None:
                    msg_lines.insert(1, f"⚑ Level: {level_hit:.2f}%")
                try:
                    await application.bot.send_message(chat_id=CHAT_ID, text="\n".join(msg_lines))
                    LAST_ALERT_TS[asset] = now
                except Exception as e:
                    print("Telegram send error:", e)

# =========================
# Telegram command handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I’ll check Extended↔Lighter every %ds and alert when net edge ≥ %.3f%%.\n"
        "Commands: /top /setpairs /thresh /threshpair /levels /setlevels /pause /resume /fees"
        % (POLL_SECONDS, THRESHOLD_PCT)
    )

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with httpx.AsyncClient() as client:
        rows = []
        for asset in ASSETS:
            q = await get_quotes(client, asset)
            pct, direction, detail = best_net_edge(q)
            rows.append((asset, pct, direction, detail))
        rows.sort(key=lambda x: x[1], reverse=True)
        if not rows:
            return await update.message.reply_text("No data.")
        lines = [f"{a}: {pct:.3f}% — {d} | {detail}" for (a, pct, d, detail) in rows]
        await update.message.reply_text("\n".join(lines))

async def cmd_setpairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ASSETS
    try:
        raw = " ".join(context.args)
        parts = [p.strip().upper() for p in raw.replace(",", " ").split() if p.strip()]
        if not parts:
            return await update.message.reply_text("Usage: /setpairs BTC,ETH,SOL or /setpairs BTC ETH SOL")
        ASSETS = parts
        await update.message.reply_text("Pairs set to: " + ", ".join(ASSETS))
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_thresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global THRESHOLD_PCT
    try:
        arg = context.args[0]
        THRESHOLD_PCT = float(arg)
        await update.message.reply_text(f"Global threshold set to {THRESHOLD_PCT:.3f}% net edge.")
    except Exception:
        await update.message.reply_text("Usage: /thresh 0.15  (percent)")

async def cmd_threshpair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set per-pair single threshold. Usage: /threshpair ETH 0.15"""
    try:
        asset = context.args[0].upper()
        pct = float(context.args[1])
        THRESHOLD_PER_PAIR[asset] = pct
        await update.message.reply_text(f"Threshold for {asset} set to {pct:.3f}%")
    except Exception:
        await update.message.reply_text("Usage: /threshpair <ASSET> <percent>  e.g. /threshpair ETH 0.15")

async def cmd_levels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if THRESHOLD_LEVELS:
        await update.message.reply_text("Levels: " + ", ".join(f"{x:.2f}%" for x in THRESHOLD_LEVELS))
    else:
        await update.message.reply_text("Levels not set. Use /setlevels 0.10,0.15,0.20")

async def cmd_setlevels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global THRESHOLD_LEVELS, LAST_LEVEL_IDX
    try:
        raw = " ".join(context.args)
        if not raw:
            return await update.message.reply_text("Usage: /setlevels 0.10,0.15,0.20")
        THRESHOLD_LEVELS = sorted([float(x) for x in raw.replace(" ", "").split(",") if x], key=float)
        LAST_LEVEL_IDX.clear()
        await update.message.reply_text("Levels set to: " + ", ".join(f"{x:.2f}%" for x in THRESHOLD_LEVELS))
    except Exception:
        await update.message.reply_text("Usage: /setlevels 0.10,0.15,0.20")

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PAUSED
    PAUSED = True
    await update.message.reply_text("⏸️ Paused alerts.")

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PAUSED
    PAUSED = False
    await update.message.reply_text("▶️ Resumed alerts.")

async def cmd_fees(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"Fees (bps) — EXT open {FEE_BPS_EXT_OPEN}, EXT close {FEE_BPS_EXT_CLOSE}, "
        f"LIG open {FEE_BPS_LIG_OPEN}, LIG close {FEE_BPS_LIG_CLOSE}; "
        f"slippage per leg {SLIPPAGE_BPS_PER_LEG}.\n"
        f"Total bps per round-trip per direction ≈ fees_sum + 4×slippage."
    )
    await update.message.reply_text(msg)

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
    app.add_handler(CommandHandler("thresh", cmd_thresh))
    app.add_handler(CommandHandler("threshpair", cmd_threshpair))
    app.add_handler(CommandHandler("levels", cmd_levels))
    app.add_handler(CommandHandler("setlevels", cmd_setlevels))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("fees", cmd_fees))

    # Use JobQueue to run background price checks every POLL_SECONDS
    # First run after 5s, then repeat
    app.job_queue.run_repeating(lambda ctx: ctx.application.create_task(check_and_alert(ctx.application)),
                                interval=POLL_SECONDS, first=5)

    # This starts polling and blocks (cleaner than manual event loop management)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
