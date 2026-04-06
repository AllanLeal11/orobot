import os
import time
import requests
from datetime import datetime, timezone
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN",  "")
CHAT_ID         = os.environ.get("CHAT_ID",          "")
SYMBOL          = "XAU/USD"
CHECK_INTERVAL  = 300
BLACKOUT_HOURS  = [(7, 30, 8, 30), (12, 30, 13, 30)]

# ── Telegram ────────────────────────────────────────────────────
def send_telegram(msg):
    url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logger.error("Telegram error: {}".format(e))

# ── Blackout ─────────────────────────────────────────────────────
def is_blackout():
    now   = datetime.now(timezone.utc)
    total = now.hour * 60 + now.minute
    for (sh, sm, eh, em) in BLACKOUT_HOURS:
        if sh * 60 + sm <= total <= eh * 60 + em:
            return True
    return False

# ── Data fetching ────────────────────────────────────────────────
def get_candles(interval, outputsize=100):
    """Returns list of dicts: [{open, high, low, close, datetime}, ...]"""
    params = {
        "symbol": SYMBOL, "interval": interval,
        "outputsize": outputsize, "apikey": TWELVE_DATA_KEY, "timezone": "UTC",
    }
    try:
        r    = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=15)
        data = r.json()
        if "values" not in data:
            logger.warning("API error ({}): {}".format(interval, data.get("message", data)))
            return None
        candles = []
        for v in reversed(data["values"]):   # oldest first
            candles.append({
                "open":  float(v["open"]),
                "high":  float(v["high"]),
                "low":   float(v["low"]),
                "close": float(v["close"]),
                "dt":    v["datetime"],
            })
        return candles
    except Exception as e:
        logger.error("get_candles error: {}".format(e))
        return None

# ── SMC helpers ──────────────────────────────────────────────────
def get_bias(candles):
    n = len(candles)
    if n < 10:
        return "neutral"
    highs = [c["high"] for c in candles]
    lows  = [c["low"]  for c in candles]

    swing_highs = [i for i in range(2, n-2) if highs[i] == max(highs[i-2:i+3])]
    swing_lows  = [i for i in range(2, n-2) if lows[i]  == min(lows[i-2:i+3])]

    if not swing_highs or not swing_lows:
        return "neutral"

    close = candles[-1]["close"]
    if close > highs[swing_highs[-1]]:
        return "bullish"
    if close < lows[swing_lows[-1]]:
        return "bearish"
    return "neutral"

def find_order_blocks(candles, bias):
    obs = []
    n   = len(candles)
    for i in range(3, n - 2):
        c  = candles[i]
        nx = candles[i + 1]
        if bias == "bullish" and c["close"] < c["open"] and nx["close"] > c["high"]:
            obs.append({"type": "OB_bull", "high": c["high"], "low": c["low"]})
        elif bias == "bearish" and c["close"] > c["open"] and nx["close"] < c["low"]:
            obs.append({"type": "OB_bear", "high": c["high"], "low": c["low"]})
    return obs[-3:]

def find_fvg(candles, bias):
    fvgs = []
    n    = len(candles)
    for i in range(1, n - 1):
        prev = candles[i - 1]
        curr = candles[i]
        nxt  = candles[i + 1]
        if bias == "bullish" and nxt["low"] > prev["high"]:
            fvgs.append({"type": "FVG_bull", "high": nxt["low"], "low": prev["high"]})
        elif bias == "bearish" and nxt["high"] < prev["low"]:
            fvgs.append({"type": "FVG_bear", "high": prev["low"], "low": nxt["high"]})
    return fvgs[-3:]

def is_engulfing(candles, bias):
    if len(candles) < 2:
        return False
    p = candles[-2]
    c = candles[-1]
    if bias == "bullish":
        return (p["close"] < p["open"] and c["close"] > c["open"]
                and c["close"] > p["open"] and c["open"] < p["close"])
    if bias == "bearish":
        return (p["close"] > p["open"] and c["close"] < c["open"]
                and c["close"] < p["open"] and c["open"] > p["close"])
    return False

def price_in_poi(price, poi, buf=0.5):
    return (poi["low"] - buf) <= price <= (poi["high"] + buf)

def calc_sl_tp(entry, bias, poi):
    if bias == "bullish":
        sl = round(poi["low"] - 0.5, 2)
        tp = round(entry + (entry - sl) * 2, 2)
    else:
        sl = round(poi["high"] + 0.5, 2)
        tp = round(entry - (sl - entry) * 2, 2)
    return sl, tp

# ── Main analysis ────────────────────────────────────────────────
def analyze():
    # 1. H4 bias
    h4 = get_candles("4h", 50)
    if not h4:
        return None
    bias = get_bias(h4)
    logger.info("H4 bias: {}".format(bias))
    if bias == "neutral":
        return None

    # 2. H1 confirm
    h1 = get_candles("1h", 50)
    if not h1:
        return None
    if get_bias(h1) != bias:
        return None

    # 3. POIs on H1
    pois = find_order_blocks(h1, bias) + find_fvg(h1, bias)
    if not pois:
        return None

    price = h1[-1]["close"]
    logger.info("Price: {} | POIs: {}".format(price, len(pois)))

    poi = next((p for p in reversed(pois) if price_in_poi(price, p)), None)
    if poi is None:
        return None

    # 4. M15 engulfing confirmation
    m15 = get_candles("15min", 20)
    if not m15:
        return None
    if not is_engulfing(m15, bias):
        logger.info("No engulfing on M15")
        return None

    sl, tp = calc_sl_tp(price, bias, poi)
    return {
        "bias":     bias,
        "poi_type": poi["type"],
        "entry":    price,
        "sl":       sl,
        "tp":       tp,
        "time":     datetime.now(timezone.utc).strftime("%H:%M UTC"),
    }

def format_signal(sig):
    direction = "🟢 LONG" if sig["bias"] == "bullish" else "🔴 SHORT"
    poi_label = "Order Block" if "OB" in sig["poi_type"] else "Fair Value Gap"
    return (
        "*🥇 OROBOT — SEÑAL XAU/USD*\n\n"
        "*Dirección:* {}\n"
        "*POI:* {}\n"
        "*Entry:* `{}`\n"
        "*Stop Loss:* `{}`\n"
        "*Take Profit:* `{}`\n"
        "*RR:* 1:2\n\n"
        "_H4+H1 alineados | Envolvente M15_\n"
        "🕐 {}"
    ).format(direction, poi_label, sig["entry"], sig["sl"], sig["tp"], sig["time"])

# ── Loop ─────────────────────────────────────────────────────────
def main():
    send_telegram("🤖 *Orobot iniciado* — Monitoreando XAU/USD\n_SMC: H4→H1→M15 | OB+FVG | Envolvente_")
    logger.info("Orobot started")
    last_signal = 0

    while True:
        try:
            if is_blackout():
                logger.info("Blackout — skipping")
            else:
                sig = analyze()
                if sig and time.time() - last_signal > 3600:
                    send_telegram(format_signal(sig))
                    logger.info("Signal sent: {}".format(sig))
                    last_signal = time.time()
                elif not sig:
                    logger.info("No setup found")
        except Exception as e:
            logger.error("Loop error: {}".format(e))
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
