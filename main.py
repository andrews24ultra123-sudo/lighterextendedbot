# main.py
import os, time, json
from typing import Dict, Optional, Tuple, List, Any

import httpx
from pydantic import BaseModel
from telegram import Bot

# =========================
# Config (env or defaults)
# =========================
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in Railway Variables.")

# Snapshot every 5 minutes (300s)
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "300"))

# Thresholds
THRESHOLD_PCT = float(os.environ.get("THRESHOLD_PCT", "0.1"))  # global percent
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

# Assets to track
ASSETS = [s.strip().upper() for s in os.environ.get("ASSETS", "BTC,ETH,SOL").split(",") if s.strip()]

# Endpoints
EXT_BASE     = os.environ.get("EXTENDED_API_BASE", "https://api.starknet.extended.exchange")
LIGHTER_BASE = os.environ.get("LIGHTER_API_BASE", "https://mainnet.zklighter.elliot.ai/api/v1")

# Optional manual overrides for Lighter market IDs: "BTC-PERP:101,ETH-PERP:102"
_manual_ids = os.environ.get("LIGHTER_MARKET_IDS", "").strip()
LIGHTER_MANUAL: Dict[str, int] = {}
if _manual_ids:
    for part in _manual_ids.split(","):
        if ":" in part:
            s, mid = part.split(":", 1)
            try:
                LIGHTER_MANUAL[s.strip().upper()] = int(mid.strip())
            except Exception:
                pass

# Symbols mapping
EXT_MARKETS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
# Lighter naming (symbol strings). Adjust if your symbols differ.
LIGHTER_SYMBOLS = {"BTC": "BTC-PERP", "ETH": "ETH-PERP", "SOL": "SOL-PERP"}

# Fees only (bps) — per your request (no slippage)
FEE_BPS_EXT_OPEN  = 22.0
FEE_BPS_EXT_CLOSE = 22.0
FEE_BPS_LIG_OPEN  = 0.0
FEE_BPS_LIG_CLOSE = 0.0

# =========================
# Models
# =========================
class TopOfBook(BaseModel):
    bid: float
    ask: float

# =========================
# Helpers
# =========================
def _roundtrip_bps(direction: str) -> float:
    """Total fees (bps) for round trip across both venues (no slippage)."""
    if direction == "EXT->LIG":
        return FEE_BPS_EXT_OPEN + FEE_BPS_LIG_OPEN + FEE_BPS_EXT_CLOSE + FEE_BPS_LIG_CLOSE
    return FEE_BPS_LIG_OPEN + FEE_BPS_EXT_OPEN + FEE_BPS_LIG_CLOSE + FEE_BPS_EXT_CLOSE

def best_net_edge(ext: Optional[TopOfBook], lig: Optional[TopOfBook]) -> Tuple[float, str, str]:
    """Return (net_edge_pct, direction, detail) — or (0,'N/A',reason) if missing."""
    if not ext or not lig:
        return (0.0, "N/A", "missing data")

    # Crossed spreads (use crossed executable prices)
    gross1 = (lig.bid - ext.ask) / ext.ask   # EXT->LIG: buy ask EXT, sell bid LIG
    gross2 = (ext.bid - lig.ask) / lig.ask   # LIG->EXT: buy ask LIG, sell bid EXT

    net1 = gross1 - _roundtrip_bps("EXT->LIG") / 10000.0
    net2 = gross2 - _roundtrip_bps("LIG->EXT") / 10000.0

    if net1 >= net2:
        return (net1 * 100, "EXT->LIG", f"buy ask EXT {ext.ask:.2f} / sell bid LIG {lig.bid:.2f}")
    else:
        return (net2 * 100, "LIG->EXT", f"buy ask LIG {lig.ask:.2f} / sell bid EXT {ext.bid:.2f}")

# =========================
# Extended (REST)
# =========================
def fetch_extended_tob(client: httpx.Client, asset: str) -> Optional[TopOfBook]:
    try:
        market = EXT_MARKETS.get(asset)
        if not market:
            return None
        url = f"{EXT_BASE}/api/v1/info/markets/{market}/orderbook"
        r = client.get(url, timeout=10)
        r.raise_for_status()
        j = r.json()
        data = j.get("data", {}) if isinstance(j, dict) else {}
        bids = data.get("bid", [])
        asks = data.get("ask", [])
        if not bids or not asks:
            return None
        b0 = bids[0]; a0 = asks[0]
        bid = float(b0.get("price", b0[1] if isinstance(b0, list) else b0))
        ask = float(a0.get("price", a0[0] if isinstance(a0, list) else a0))
        return TopOfBook(bid=bid, ask=ask)
    except Exception:
        return None

# =========================
# Lighter (REST)
# =========================
def _safe_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        return text

def discover_lighter_market_ids(client: httpx.Client, wanted_symbols: List[str]) -> Dict[str, int]:
    """
    Try to fetch markets and map symbol -> market_id. Supports multiple shapes.
    Uses manual overrides if present.
    """
    # manual overrides first
    out: Dict[str, int] = dict(LIGHTER_MANUAL) if LIGHTER_MANUAL else {}

    if len(out) >= len(wanted_symbols):
        return out

    try:
        r = client.get(f"{LIGHTER_BASE}/markets", timeout=10)
        # Accept non-200 but still attempt to parse; some APIs return JSON error shape
        obj = _safe_json(r.text)
        markets = obj.get("data", obj) if isinstance(obj, dict) else obj
        if isinstance(markets, list):
            for m in markets:
                if not isinstance(m, dict):
                    continue
                symbol = (m.get("symbol") or m.get("name") or m.get("market") or "").upper()
                mid = m.get("id") or m.get("market_id") or m.get("marketId")
                if symbol and mid is not None and symbol in {s.upper() for s in wanted_symbols}:
                    try:
                        out[symbol] = int(mid)
                    except Exception:
                        pass
    except Exception:
        pass

    return out

def fetch_lighter_tob(client: httpx.Client, market_id: int) -> Optional[TopOfBook]:
    try:
        url = f"{LIGHTER_BASE}/orderBookOrders"
        r = client.get(url, params={"market_id": market_id}, timeout=10)
        # Parse regardless of status (some APIs error but include payload)
        obj = _safe_json(r.text)
        if not isinstance(obj, dict):
            return None
        bids = obj.get("bids") or obj.get("bid") or []
        asks = obj.get("asks") or obj.get("ask") or []
        if not bids or not asks:
            return None
        b0 = bids[0]; a0 = asks[0]
        bid = float(b0[0] if isinstance(b0, list) else b0.get("price"))
        ask = float(a0[0] if isinstance(a0, list) else a0.get("price"))
        return TopOfBook(bid=bid, ask=ask)
    except Exception:
        return None

# =========================
# Main loop
# =========================
def resolve_lighter_ids(client: httpx.Client) -> Dict[str, int]:
    """Return a dict like {'BTC-PERP': 101, 'ETH-PERP': 102, 'SOL-PERP': 103}."""
    wanted_symbols = [LIGHTER_SYMBOLS[a] for a in ASSETS if a in LIGHTER_SYMBOLS]
    mapping = discover_lighter_market_ids(client, wanted_symbols)
    # If some missing and manual overrides exist, keep those; otherwise leave missing
    missing = [s for s in wanted_symbols if s.upper() not in {k.upper() for k in mapping}]
    if missing:
        print("Lighter market_id missing for:", missing)
        print("You can set LIGHTER_MARKET_IDS env like: BTC-PERP:101,ETH-PERP:102,SOL-PERP:103")
    return mapping

def main():
    bot = Bot(BOT_TOKEN)

    with httpx.Client() as client:
        # Resolve Lighter market IDs once on startup (can re-run if needed)
        lighter_ids = resolve_lighter_ids(client)
        if not lighter_ids:
            print("⚠️ Could not resolve any Lighter market_id. Set LIGHTER_MARKET_IDS env to hardcode.")
        else:
            print("Using Lighter market_ids:", lighter_ids)

        print("Starting 5-min snapshot loop…")
        while True:
            try:
                lines = []
                for asset in ASSETS:
                    # Extended
                    ext = fetch_extended_tob(client, asset)

                    # Lighter
                    lsym = LIGHTER_SYMBOLS.get(asset)
                    lig = None
                    if lsym:
                        # Use resolved id or manual if provided
                        mid = None
                        # Prefer discovery result, then manual override
                        for k, v in lighter_ids.items():
                            if k.upper() == lsym.upper():
                                mid = v
                                break
                        if mid is None and lsym.upper() in LIGHTER_MANUAL:
                            mid = LIGHTER_MANUAL[lsym.upper()]
                        if mid is not None:
                            lig = fetch_lighter_tob(client, mid)

                    # Compute edge
                    pct, direction, detail = best_net_edge(ext, lig)
                    thr = THRESHOLDS_PER_PAIR.get(asset, THRESHOLD_PCT)

                    # Compose line for /snapshot log or debugging
                    line = (f"{asset}: {pct:.3f}% — {direction} | {detail} | "
                            f"EXT {ext.bid if ext else '—'}/{ext.ask if ext else '—'}  "
                            f"LIG {lig.bid if lig else '—'}/{lig.ask if lig else '—'}")
                    print(line)
                    # Send alert only if meets threshold
                    if pct >= thr and direction != "N/A":
                        msg = (
                            f"🟢 Arb {asset} — net {pct:.3f}% ({direction})\n"
                            f"{detail}\n"
                            f"EXT bid/ask: {ext.bid if ext else '—'} / {ext.ask if ext else '—'}\n"
                            f"LIG bid/ask: {lig.bid if lig else '—'} / {lig.ask if lig else '—'}"
                        )
                        try:
                            bot.send_message(chat_id=CHAT_ID, text=msg)
                        except Exception as te:
                            print("Telegram send error:", te)

            except Exception as loop_err:
                print("Loop error:", loop_err)

            time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
