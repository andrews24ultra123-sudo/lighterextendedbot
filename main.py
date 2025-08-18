# main.py
import os
import time
import hmac
import hashlib
import asyncio
from typing import Dict, Optional, Tuple, List

import httpx
from pydantic import BaseModel
from telegram import Update
from telegram.error import TelegramError, Forbidden, NetworkError
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

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

# Pairs to track (change live via /setpairs)
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

# Debug switch for Lighter requests (logs status & short body)
LIGHTER_DEBUG = os.environ.get("LIGHTER_DEBUG", "0") == "1"

LIGHTER_ENABLED = bool(LIGHTER_API_BASE and LIGHTER_KEY and LIGHTER_SECRET)

# Fees only (bps) — no slippage
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
# Lighter REST uses market_id. We'll discover the ids from /orderBooks, but keep desired symbols here:
LIGHTER_SYMBOLS = {"BTC": "BTC-PERP", "ETH": "ETH-PERP", "SOL": "SOL-PERP"}

# Cache for Lighter symbol -> market_id
_LIGHTER_MARKET_ID: Dict[str, int] = {}

def _mask(s: Optional[str], keep: int = 4) -> str:
    if not s:
        return "(empty)"
    if len(s) <= keep*2:
        return s[0:keep] + "..." + s[-keep:]
    return s[0:keep] + "..." + s[-keep:]

# ---------- Extended ----------
async def fetch_extended_tob(client: httpx.AsyncClient, asset: str) -> Optional[TopOfBook]:
    try:
        market = EXT_MARKETS.get(asset)
        if not market:
            return None
        url = f"{EXT_BASE}/api/v1/info/markets/{market}/orderbook"
        r = await client.get(url, timeout=10)
        r.raise_for_status()
        j = r.json()
        data = j.get("data", {}) if isinstance(j, dict) else {}
        bids = data.get("bid", []); asks = data.get("ask", [])
        if not bids or not asks:
            return None
        b0 = bids[0]; a0 = asks[0]
        bid = float(b0.get("price", b0[1] if isinstance(b0, list) else b0))
        ask = float(a0.get("price", a0[0] if isinstance(a0, list) else a0))
        return TopOfBook(bid=bid, ask=ask)
    except Exception:
        return None

# ---------- Lighter market discovery ----------
async def lighter_discover_markets(client: httpx.AsyncClient) -> None:
    """
    Populate _LIGHTER_MARKET_ID from GET /orderBooks.
    We try to match the 'symbol' field to LIGHTER_SYMBOLS (e.g., 'BTC-PERP').
    """
    if not LIGHTER_ENABLED:
        return
    try:
        url = f"{LIGHTER_API_BASE}/orderBooks"
        r = await client.get(url, timeout=10)
        if LIGHTER_DEBUG and r.status_code != 200:
            body = r.text
            if len(body) > 300:
                body = body[:300] + "...(truncated)"
            print(f"[LIGHTER_DEBUG] {r.status_code} {url}  resp={body}")
        r.raise_for_status()
        data = r.json()
        # Expect an array of markets; figure out fields
        # We'll try common field names: 'id' or 'market_id', and 'symbol' or 'name'
        if isinstance(data, dict) and "data" in data:
            markets = data["data"]
        else:
            markets = data
        for m in markets or []:
            symbol = m.get("symbol") or m.get("name") or m.get("market") or ""
            mid = m.get("id") if "id" in m else m.get("market_id")
            if symbol and mid is not None:
                _LIGHTER_MARKET_ID[str(symbol).upper()] = int(mid)
        if LIGHTER_DEBUG:
            print("[LIGHTER_DEBUG] discovered markets:",
                  {k: v for k, v in _LIGHTER_MARKET_ID.items() if k in {v.upper() for v in LIGHTER_SYMBOLS.values()}})
    except Exception as e:
        if LIGHTER_DEBUG:
            print("[LIGHTER_DEBUG] market discovery failed:", e)

def lighter_sign(ts: str, method: str, path: str, query: str) -> Dict[str, str]:
    prehash = f"{ts}{method}{path}?{query}"
    sig = hmac.new((LIGHTER_SECRET or "").encode(), prehash.encode(), hashlib.sha256).hexdigest()
    return {
        LIGHTER_HDR_KEY: (LIGHTER_KEY or ""),
        LIGHTER_HDR_SIG: sig,
        LIGHTER_HDR_TS: ts,
    }

# ---------- Lighter order book ----------
async def fetch_lighter_tob(client: httpx.AsyncClient, asset: str) -> Optional[TopOfBook]:
    if not LIGHTER_ENABLED:
        return None
    try:
        desired_symbol = LIGHTER_SYMBOLS.get(asset)
        if not desired_symbol:
            return None

        # ensure we have the market_id
        if not _LIGHTER_MARKET_ID:
            await lighter_discover_markets(client)

        market_id = _LIGHTER_MARKET_ID.get(desired_symbol.upper())
        if market_id is None:
            # Try a second discovery (maybe API returned different key names)
            await lighter_discover_markets(client)
            market_id = _LIGHTER_MARKET_ID.get(desired_symbol.upper())
            if market_id is None:
                if LIGHTER_DEBUG:
                    print(f"[LIGHTER_DEBUG] no market_id for {desired_symbol} yet")
                return None

        ts = str(int(time.time() * 1000))
        method = "GET"
        path = "/orderBookOrders"
        query = f"market_id={market_id}"
        headers = lighter_sign(ts, method, path, query)

        url = f"{LIGHTER_API_BASE}{path}"
        r = await client.get(url, params={"market_id": market_id}, headers=headers, timeout=10)

        if LIGHTER_DEBUG and r.status_code != 200:
            body = r.text
            if len(body) > 300:
                body = body[:300] + "...(truncated)"
            print(f"[LIGHTER_DEBUG] {r.status_code} {url}  resp={body}")

        r.raise_for_status()
        j = r.json()
        bids = j.get("bids") or j.get("bid") or []
        asks = j.get("asks") or j.get("ask") or []
        if not bids or not asks:
            return None
        b0 = bids[0]; a0 = asks[0]
        bid = float(b0[0] if isinstance(b0, list) else b0.get("price"))
        ask = float(a0[0] if isinstance(a0, list) else a0.get("price"))
        return TopOfBook(bid=bid, ask=ask)
    except Exception as e:
        if LIGHTER_DEBUG:
            print(f"[LIGHTER_DEBUG] Exception for {asset}: {e}")
        return None

async def get_quotes(client: httpx.AsyncClient, asset: str) -> VenueQuotes:
    ext = await fetch_extended_tob(client, asset)
    lig = await fetch_lighter_tob(client, asset)
    return VenueQuotes(extended=ext, lighter=lig)

def _roundtrip_bps(direction: str) -> float:
    """Total fees (bps) across round-trip (entry+exit on both venues)."""
    if direction == "EXT->LIG":
        return FEE_BPS_EXT_OPEN + FEE_BPS_LIG_OPEN + FEE_BPS_EXT_CLOSE + FEE_BPS_LIG_CLOSE
    return FEE_BPS_LIG_OPEN + FEE_BPS_EXT_OPEN + FEE_BPS_LIG_CLOSE + FEE_BPS_EXT_CLOSE

def best_net_edge(q: VenueQuotes) -> Tuple[float, str, str]:
    """Returns (net_edge_pct, direction, detail) or (0, 'N/A', reason)."""
    ext, lig = q.extended, q.lighter
    if not ext or not lig:
        return (0.0, "N/A", "Lighter disabled or missing data")
    gross1 = (lig.bid - ext.ask) / ext.ask   # EXT->LIG: buy ask EXT, sell bid LIG
    gross2 = (ext.bid - lig.ask) / lig.ask   # LIG->EXT: buy ask LIG, sell bid EXT
    net1 = gross1 - _roundtrip_bps("EXT->LIG") / 10000.0
    net2 = gross2 - _roundtrip_bps("LIG->EXT") / 10000.0
    if net1 >= net2:
        return (net1 * 100, "EXT->LIG", f"buy ask EXT {ext.ask:.2f} / sell bid LIG {lig.bid:.2f}")
    else:
        return (net2 * 100, "LIG->EXT", f"buy ask LIG {lig.ask:.2f} / sell bid EXT {ext.bid:.2f}")

# =========================
# Bot state & background
# =========================
LAST_ALERT_TS: Dict[str, float] = {}
ALERT_COOLDOWN = 120
PAUSED = False

async def check_and_alert(application) -> None:
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
                    except (TelegramError, Forbidden, NetworkError) as e:
                        print("Telegram send error:", e)

async def background_loop(application):
    # Notify on startup so you know it's alive
    try:
        await application.bot.send_message(chat_id=CHAT_ID, text="✅ Bot started. Send /start or /top.")
    except Exception as e:
        print("Startup DM failed (check TELEGRAM_CHAT_ID & token):", e)

    # Simple loop, no JobQueue needed
    while True:
        try:
            await check_and_alert(application)
        except Exception as e:
            print("background error:", e)
        await asyncio.sleep(POLL_SECONDS)

# =========================
# Telegram command handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Hi! Monitoring {', '.join(ASSETS)}.\n"
        f"Global threshold: {THRESHOLD_PCT}%\n"
        f"Per-pair: {THRESHOLDS_PER_PAIR or '(none)'}\n"
        f"Use /top to view current edges, /setpairs to change pairs, /probe BTC to inspect feeds."
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
        return await update.message.reply_text("Usage: /setpairs BTC,ETH,SOL or /setpairs BTC ETH SOL")
    ASSETS = parts
    await update.message.reply_text("Pairs set to: " + ", ".join(ASSETS))

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"chat id: {update.effective_chat.id}")

async def cmd_probe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inspect raw best bid/ask from both venues for a given asset (default BTC)."""
    asset = (context.args[0].upper() if context.args else "BTC")
    async with httpx.AsyncClient() as client:
        ext = await fetch_extended_tob(client, asset)
        lig = await fetch_lighter_tob(client, asset)

    lines = [f"🔍 PROBE {asset}"]
    lines.append(f"EXTENDED  bid={getattr(ext,'bid','—')}  ask={getattr(ext,'ask','—')}")
    lines.append(f"LIGHTER   bid={getattr(lig,'bid','—')}  ask={getattr(lig,'ask','—')}")
    await update.message.reply_text("\n".join(lines))

async def cmd_lighter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["Lighter status:"]
    lines.append(f"enabled: {LIGHTER_ENABLED}")
    lines.append(f"base: {LIGHTER_API_BASE or '(unset)'}")
    lines.append(f"hdr_key: {LIGHTER_HDR_KEY}, hdr_sig: {LIGHTER_HDR_SIG}, hdr_ts: {LIGHTER_HDR_TS}")
    lines.append(f"api_key (masked): {_mask(LIGHTER_KEY)}")
    lines.append(f"secret (masked):  {_mask(LIGHTER_SECRET)}")
    lines.append(f"debug: {'on' if LIGHTER_DEBUG else 'off'}")
    # Also show discovered IDs:
    if _LIGHTER_MARKET_ID:
        lines.append("market_id map: " + ", ".join(f"{k}={v}" for k, v in _LIGHTER_MARKET_ID.items()))
    else:
        lines.append("market_id map: (empty)")
    await update.message.reply_text("\n".join(lines))

# =========================
# App bootstrap (async)
# =========================
async def async_main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.")

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start",     cmd_start))
    application.add_handler(CommandHandler("top",       cmd_top))
    application.add_handler(CommandHandler("setpairs",  cmd_setpairs))
    application.add_handler(CommandHandler("ping",      cmd_ping))
    application.add_handler(CommandHandler("id",        cmd_id))
    application.add_handler(CommandHandler("probe",     cmd_probe))
    application.add_handler(CommandHandler("lighter",   cmd_lighter))

    # Start bot & our background loop
    await application.initialize()
    await application.start()
    print("Bot started (async).")
    asyncio.create_task(background_loop(application))

    # Start receiving updates & keep process alive
    await application.updater.start_polling()
    await asyncio.Event().wait()

def main():
    asyncio.run(async_main())

if __name__ == "__main__":
    main()
