import os
import time
import requests
import pandas as pd
from datetime import datetime, timezone
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger(__name__)

# --- CONFIG ---
TWELVE_DATA_KEY = os.environ.get("TWELVE_DATA_KEY", "735296041799443fb113452aed36055b")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN",  "8799785665:AAFBoLMRCAfs8X1It7tGjlQ1GwgfrOz0jAA")
CHAT_ID         = os.environ.get("CHAT_ID",          "8289064694")
SYMBOL          = "XAU/USD"
CHECK_INTERVAL  = 300   # seconds between scans (5 min)

# News / session blackout windows (UTC)
BLACKOUT_HOURS = [(7, 30, 8, 30), (12, 30, 13, 30)]   # London open, NY open ± 30 min

# ---------------------------------------------------------------
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def is_blackout() -> bool:
    now = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    total = h * 60 + m
    for (sh, sm, eh, em) in BLACKOUT_HOURS:
        if sh * 60 + sm <= total <= eh * 60 + em:
            return True
    return False

# ---------------------------------------------------------------
def get_candles(interval: str, outputsize: int = 100) -> pd.DataFrame | None:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol":     SYMBOL,
        "interval":   interval,
        "outputsize": outputsize,
        "apikey":     TWELVE_DATA_KEY,
        "timezone":   "UTC",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if "values" not in data:
            logger.warning(f"Twelve Data error ({interval}): {data}")
            return None
        df = pd.DataFrame(data["values"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        df = df.sort_values("datetime").reset_index(drop=True)
        return df
    except Exception as e:
        logger.error(f"get_candles error: {e}")
        return None

# ---------------------------------------------------------------
# SMC helpers
# ---------------------------------------------------------------
def get_htf_bias(df: pd.DataFrame) -> str:
    """Determine bullish/bearish bias from last 30 candles via BOS/CHoCH."""
    highs = df["high"].values
    lows  = df["low"].values
    n = len(highs)
    if n < 10:
        return "neutral"

    # Find last significant swing high and low
    swing_highs = [i for i in range(2, n-2) if highs[i] == max(highs[i-2:i+3])]
    swing_lows  = [i for i in range(2, n-2) if lows[i]  == min(lows[i-2:i+3])]

    if not swing_highs or not swing_lows:
        return "neutral"

    last_sh = swing_highs[-1]
    last_sl = swing_lows[-1]

    # Bullish if price broke above last swing high (BOS up)
    current_close = df["close"].iloc[-1]
    if current_close > highs[last_sh]:
        return "bullish"
    if current_close < lows[last_sl]:
        return "bearish"
    return "neutral"

def find_order_blocks(df: pd.DataFrame, bias: str) -> list[dict]:
    """Find the most recent valid OB in the direction of bias."""
    obs = []
    n = len(df)
    for i in range(3, n - 2):
        candle = df.iloc[i]
        next_c = df.iloc[i + 1]

        if bias == "bullish":
            # Bearish OB before bullish move: red candle followed by strong up move
            if candle["close"] < candle["open"]:
                if next_c["close"] > candle["high"]:
                    obs.append({
                        "type":  "OB_bull",
                        "high":  candle["high"],
                        "low":   candle["low"],
                        "index": i,
                        "time":  candle["datetime"],
                    })
        elif bias == "bearish":
            # Bullish OB before bearish move: green candle followed by strong down move
            if candle["close"] > candle["open"]:
                if next_c["close"] < candle["low"]:
                    obs.append({
                        "type":  "OB_bear",
                        "high":  candle["high"],
                        "low":   candle["low"],
                        "index": i,
                        "time":  candle["datetime"],
                    })
    return obs[-3:] if obs else []   # return last 3

def find_fvg(df: pd.DataFrame, bias: str) -> list[dict]:
    """Find Fair Value Gaps."""
    fvgs = []
    n = len(df)
    for i in range(1, n - 1):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]
        nxt  = df.iloc[i + 1]

        if bias == "bullish":
            # Bullish FVG: gap between prev high and next low
            if nxt["low"] > prev["high"]:
                fvgs.append({
                    "type":  "FVG_bull",
                    "high":  nxt["low"],
                    "low":   prev["high"],
                    "index": i,
                    "time":  curr["datetime"],
                })
        elif bias == "bearish":
            # Bearish FVG: gap between prev low and next high
            if nxt["high"] < prev["low"]:
                fvgs.append({
                    "type":  "FVG_bear",
                    "high":  prev["low"],
                    "low":   nxt["high"],
                    "index": i,
                    "time":  curr["datetime"],
                })
    return fvgs[-3:] if fvgs else []

def is_engulfing(df: pd.DataFrame, bias: str) -> bool:
    """Check if last closed candle is an engulfing in bias direction."""
    if len(df) < 2:
        return False
    prev = df.iloc[-2]
    curr = df.iloc[-1]

    if bias == "bullish":
        # Bullish engulfing: prev bearish, curr bullish and engulfs prev body
        return (prev["close"] < prev["open"] and
                curr["close"] > curr["open"] and
                curr["close"] > prev["open"] and
                curr["open"]  < prev["close"])

    elif bias == "bearish":
        # Bearish engulfing: prev bullish, curr bearish and engulfs prev body
        return (prev["close"] > prev["open"] and
                curr["close"] < curr["open"] and
                curr["close"] < prev["open"] and
                curr["open"]  > prev["close"])

    return False

def price_in_poi(price: float, poi: dict, buffer: float = 0.5) -> bool:
    """Check if current price is inside or very close to a POI zone."""
    return (poi["low"] - buffer) <= price <= (poi["high"] + buffer)

# ---------------------------------------------------------------
def calculate_sl_tp(entry: float, bias: str, poi: dict) -> tuple[float, float]:
    """Calculate SL below/above POI and TP at 1:2 RR."""
    if bias == "bullish":
        sl = round(poi["low"] - 0.5, 2)
        risk = entry - sl
        tp = round(entry + risk * 2, 2)
    else:
        sl = round(poi["high"] + 0.5, 2)
        risk = sl - entry
        tp = round(entry - risk * 2, 2)
    return sl, tp

# ---------------------------------------------------------------
def analyze() -> dict | None:
    """
    Full SMC analysis pipeline.
    Returns a signal dict or None if no valid setup found.
    """
    # 1. HTF bias from H4
    df_h4 = get_candles("4h", 50)
    if df_h4 is None:
        return None
    bias_h4 = get_htf_bias(df_h4)
    logger.info(f"H4 bias: {bias_h4}")

    if bias_h4 == "neutral":
        return None

    # 2. Confirm bias on H1
    df_h1 = get_candles("1h", 50)
    if df_h1 is None:
        return None
    bias_h1 = get_htf_bias(df_h1)
    logger.info(f"H1 bias: {bias_h1}")

    if bias_h1 != bias_h4:
        return None   # biases don't align

    bias = bias_h4

    # 3. Find POIs on H1 (OB + FVG)
    obs  = find_order_blocks(df_h1, bias)
    fvgs = find_fvg(df_h1, bias)
    pois = obs + fvgs

    if not pois:
        return None

    current_price = df_h1["close"].iloc[-1]
    logger.info(f"Current price: {current_price} | POIs found: {len(pois)}")

    # 4. Check if price is in any POI
    active_poi = None
    for poi in reversed(pois):   # most recent first
        if price_in_poi(current_price, poi):
            active_poi = poi
            break

    if active_poi is None:
        return None

    # 5. Check M15 for engulfing confirmation
    df_m15 = get_candles("15min", 20)
    if df_m15 is None:
        return None

    if not is_engulfing(df_m15, bias):
        logger.info("No engulfing confirmation on M15")
        return None

    # 6. Calculate entry, SL, TP
    entry = current_price
    sl, tp = calculate_sl_tp(entry, bias, active_poi)

    return {
        "bias":      bias,
        "poi_type":  active_poi["type"],
        "entry":     entry,
        "sl":        sl,
        "tp":        tp,
        "risk_reward": "1:2",
        "time":      datetime.now(timezone.utc).strftime("%H:%M UTC"),
    }

# ---------------------------------------------------------------
def format_signal(sig: dict) -> str:
    direction = "🟢 LONG" if sig["bias"] == "bullish" else "🔴 SHORT"
    poi_label = "Order Block" if "OB" in sig["poi_type"] else "Fair Value Gap"
    return (
        f"*🥇 OROBOT — SEÑAL XAU/USD*\n\n"
        f"*Dirección:* {direction}\n"
        f"*POI:* {poi_label}\n"
        f"*Entry:* `{sig['entry']}`\n"
        f"*Stop Loss:* `{sig['sl']}`\n"
        f"*Take Profit:* `{sig['tp']}`\n"
        f"*RR:* {sig['risk_reward']}\n\n"
        f"_Bias H4 + H1 alineados | Confirmación envolvente M15_\n"
        f"🕐 {sig['time']}"
    )

# ---------------------------------------------------------------
def main():
    send_telegram("🤖 *Orobot iniciado* — Monitoreando XAU/USD con estrategia SMC\n_H4 → H1 → M15 | OB + FVG | Envolvente_")
    logger.info("Orobot started")

    last_signal_time = 0

    while True:
        try:
            if is_blackout():
                logger.info("Blackout window — skipping scan")
            else:
                signal = analyze()
                now = time.time()

                if signal:
                    # Avoid spamming same signal within 1 hour
                    if now - last_signal_time > 3600:
                        msg = format_signal(signal)
                        send_telegram(msg)
                        logger.info(f"Signal sent: {signal}")
                        last_signal_time = now
                    else:
                        logger.info("Signal found but cooldown active")
                else:
                    logger.info("No valid setup found")

        except Exception as e:
            logger.error(f"Main loop error: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
