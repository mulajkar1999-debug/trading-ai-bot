import os
import time
import threading
from datetime import datetime
from collections import deque
from statistics import mean

import requests
from flask import Flask, jsonify, render_template_string


# ============================================================
# RULEBOOK PAPER-TRADING BOT
# Daily -> 4H -> 1H
# TGL/TJL 1-2-3 structure
# 1H -> 1M CHOCH
# 4H -> 5M CHOCH
# Daily -> 1H CHOCH -> 1M confirmation
# 2 consecutive closes = CHOCH
# Fakeout filter
# Level tap -> confirmation -> entry
# One level = one trade
# Paper trading only
# ============================================================


# ------------------------- CONFIG ----------------------------

BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
]

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.5"))  # 0.5% default
MIN_RR = float(os.getenv("MIN_RR", "1.5"))
A_PLUS_ONLY = os.getenv("A_PLUS_ONLY", "true").lower() == "true"

# Volatility rule:
# If ATR/price >= this value, TGL1 can be considered.
HIGH_VOLATILITY_ATR_PCT = float(
    os.getenv("HIGH_VOLATILITY_ATR_PCT", "0.012")
)

TELEGRAM_BOT_TOKEN = os.getenv("8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM", "")
TELEGRAM_CHAT_ID = os.getenv("1317739622", "")

MAX_HISTORY = 500
MAX_MESSAGES = 100


# ------------------------- STATE ------------------------------

recent_messages = deque(maxlen=MAX_MESSAGES)
trade_history = deque(maxlen=MAX_HISTORY)
active_trades = {}
played_levels = set()

market_matrix = {}

stats = {
    "status": "STARTING",
    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "last_scan": "-",
    "signals": 0,
    "wins": 0,
    "losses": 0,
    "waits": 0,
    "invalidated_levels": 0,
    "total_r": 0.0,
}


# ------------------------- BASIC HELPERS ----------------------

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def f(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def symbol_clean(symbol):
    return symbol.replace("/", "").upper()


def fmt_price(price):
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.8f}"


def body(c):
    return abs(c["close"] - c["open"])


def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


def avg_body(candles, n=20):
    x = candles[-n:]
    return mean(body(c) for c in x) if x else 0.0


# ------------------------- TELEGRAM ---------------------------

def telegram(text):
    recent_messages.appendleft({
        "time": now(),
        "message": text,
    })

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as exc:
        print("Telegram error:", exc)


# ------------------------- BINANCE DATA -----------------------

def get_klines(symbol, interval, limit=250):
    try:
        r = requests.get(
            BINANCE_KLINES,
            params={
                "symbol": symbol_clean(symbol),
                "interval": interval,
                "limit": limit,
            },
            timeout=10,
        )
        r.raise_for_status()

        candles = []
        for x in r.json():
            candles.append({
                "open_time": int(x[0]),
                "open": f(x[1]),
                "high": f(x[2]),
                "low": f(x[3]),
                "close": f(x[4]),
                "volume": f(x[5]),
                "close_time": int(x[6]),
            })

        # Do not use the still-forming candle for structure/CHOCH.
        if len(candles) > 2:
            candles = candles[:-1]

        return candles

    except Exception as exc:
        print(f"Kline error {symbol} {interval}: {exc}")
        return []


def live_price(symbol):
    try:
        r = requests.get(
            BINANCE_TICKER,
            params={"symbol": symbol_clean(symbol)},
            timeout=10,
        )
        r.raise_for_status()
        return f(r.json()["price"])
    except Exception as exc:
        print(f"Ticker error {symbol}: {exc}")
        return 0.0


# ------------------------- SWINGS -----------------------------

def pivot_high(candles, i):
    if i < 2 or i > len(candles) - 3:
        return False
    return (
        candles[i]["high"] > candles[i-1]["high"]
        and candles[i]["high"] > candles[i-2]["high"]
        and candles[i]["high"] >= candles[i+1]["high"]
        and candles[i]["high"] >= candles[i+2]["high"]
    )


def pivot_low(candles, i):
    if i < 2 or i > len(candles) - 3:
        return False
    return (
        candles[i]["low"] < candles[i-1]["low"]
        and candles[i]["low"] < candles[i-2]["low"]
        and candles[i]["low"] <= candles[i+1]["low"]
        and candles[i]["low"] <= candles[i+2]["low"]
    )


def pivots(candles):
    highs, lows = [], []

    for i in range(2, len(candles) - 2):
        if pivot_high(candles, i):
            highs.append({"index": i, "price": candles[i]["high"]})
        if pivot_low(candles, i):
            lows.append({"index": i, "price": candles[i]["low"]})

    return highs, lows


# ------------------------- RULEBOOK STRUCTURE -----------------

def confirmed_bullish_high(candles, start=5):
    """
    Rulebook 2CR:
    High is confirmed after two consecutive retracement candles.
    Bullish structure: second retracement candle closes below
    first retracement candle low.
    """
    for i in range(len(candles) - 3, start - 1, -1):
        c1 = candles[i]
        c2 = candles[i + 1]

        if bearish(c1) and bearish(c2) and c2["close"] < c1["low"]:
            high_idx = max(0, i - 1)
            high_price = max(
                c["high"] for c in candles[max(0, high_idx - 3):i + 1]
            )
            return {
                "index": i,
                "price": high_price,
                "confirm_index": i + 1,
            }
    return None


def confirmed_bearish_low(candles, start=5):
    """
    Mirror of bullish 2CR:
    Low is confirmed after two consecutive bullish retracement candles.
    Second candle closes above first candle high.
    """
    for i in range(len(candles) - 3, start - 1, -1):
        c1 = candles[i]
        c2 = candles[i + 1]

        if bullish(c1) and bullish(c2) and c2["close"] > c1["high"]:
            low_idx = max(0, i - 1)
            low_price = min(
                c["low"] for c in candles[max(0, low_idx - 3):i + 1]
            )
            return {
                "index": i,
                "price": low_price,
                "confirm_index": i + 1,
            }
    return None


def latest_structure(candles):
    """
    Builds the latest usable 1-2-3 structure.

    Bullish:
      Level 1 = confirmed high wick
      wait for break + close above Level 1
      Level 2 = lowest point between break + close
                 and the confirmed high
      Level 3 = next valid 2-candle retracement

    Bearish is the exact mirror:
      Level 1 = confirmed low wick
      break + close below Level 1
      Level 2 = highest point between break + close
                 and the confirmed low
      Level 3 = next valid 2-candle retracement
    """
    if len(candles) < 30:
        return None

    # Try latest bullish structure
    bh = confirmed_bullish_high(candles)
    if bh:
        break_idx = None
        for j in range(bh["confirm_index"] + 1, len(candles)):
            if candles[j]["close"] > bh["price"]:
                break_idx = j
                break

        if break_idx is not None:
            between = candles[bh["confirm_index"]:break_idx + 1]
            level2 = min(c["low"] for c in between)

            # Latest confirmed retracement after the break = Level 3
            level3 = None
            for j in range(break_idx + 1, len(candles) - 1):
                c1, c2 = candles[j], candles[j + 1]
                if bearish(c1) and bearish(c2) and c2["close"] < c1["low"]:
                    level3 = min(c1["low"], c2["low"])
                    break

            if level3 is None:
                level3 = candles[-1]["low"]

            return {
                "trend": "BULLISH",
                "level1": bh["price"],
                "level2": level2,
                "level3": level3,
                "level1_index": bh["index"],
                "break_index": break_idx,
                "level2_index": min(
                    range(
                        bh["confirm_index"],
                        break_idx + 1
                    ),
                    key=lambda k: candles[k]["low"]
                ),
            }

    # Try latest bearish structure
    bl = confirmed_bearish_low(candles)
    if bl:
        break_idx = None
        for j in range(bl["confirm_index"] + 1, len(candles)):
            if candles[j]["close"] < bl["price"]:
                break_idx = j
                break

        if break_idx is not None:
            between = candles[bl["confirm_index"]:break_idx + 1]
            level2 = max(c["high"] for c in between)

            level3 = None
            for j in range(break_idx + 1, len(candles) - 1):
                c1, c2 = candles[j], candles[j + 1]
                if bullish(c1) and bullish(c2) and c2["close"] > c1["high"]:
                    level3 = max(c1["high"], c2["high"])
                    break

            if level3 is None:
                level3 = candles[-1]["high"]

            return {
                "trend": "BEARISH",
                "level1": bl["price"],
                "level2": level2,
                "level3": level3,
                "level1_index": bl["index"],
                "break_index": break_idx,
                "level2_index": max(
                    range(
                        bl["confirm_index"],
                        break_idx + 1
                    ),
                    key=lambda k: candles[k]["high"]
                ),
            }

    return None


# ------------------------- TGL / TJL --------------------------

def atr(candles, n=14):
    if len(candles) < n + 1:
        return 0.0

    trs = []
    for i in range(-n, 0):
        c = candles[i]
        p = candles[i - 1]
        trs.append(
            max(
                c["high"] - c["low"],
                abs(c["high"] - p["close"]),
                abs(c["low"] - p["close"]),
            )
        )
    return mean(trs)


def tgl_levels(candles):
    s = latest_structure(candles)
    if not s:
        return None

    current = candles[-1]["close"]
    a = atr(candles)
    high_vol = a > 0 and (a / current) >= HIGH_VOLATILITY_ATR_PCT

    # Rulebook:
    # Level 1 wick -> TGL/TJL 1
    # Level 2 wick -> TGL/TJL 2
    # Normal market -> TGL2 preferred
    # Highly volatile -> TGL1 consideration
    if s["trend"] == "BULLISH":
        preferred = "TGL1" if high_vol else "TGL2"
    else:
        preferred = "TGL1" if high_vol else "TGL2"

    return {
        "trend": s["trend"],
        "tgl1": s["level1"],
        "tgl2": s["level2"],
        "level3": s["level3"],
        "preferred": preferred,
        "high_volatility": high_vol,
        "structure": s,
    }


# ------------------------- CHOCH ------------------------------

def choch(candles, trend):
    """
    Exact supplied rule:
      Bullish -> bearish: 2 consecutive closes below LSM/support.
      Bearish -> bullish: 2 consecutive closes above LRM/resistance.
    Fakeout:
      one close beyond level followed by close back across it.
    """
    if len(candles) < 10:
        return None

    s = latest_structure(candles)
    if not s:
        return None

    level = s["level2"]

    c1 = candles[-2]
    c2 = candles[-1]

    if trend == "BULLISH":
        if c1["close"] < level and c2["close"] < level:
            return {
                "direction": "BEARISH",
                "level": level,
                "confirmed": True,
                "fakeout": False,
            }

    if trend == "BEARISH":
        if c1["close"] > level and c2["close"] > level:
            return {
                "direction": "BULLISH",
                "level": level,
                "confirmed": True,
                "fakeout": False,
            }

    # Explicit fakeout check:
    if trend == "BULLISH":
        if c1["close"] < level and c2["close"] >= level:
            return {
                "direction": "NONE",
                "level": level,
                "confirmed": False,
                "fakeout": True,
            }

    if trend == "BEARISH":
        if c1["close"] > level and c2["close"] <= level:
            return {
                "direction": "NONE",
                "level": level,
                "confirmed": False,
                "fakeout": True,
            }

    return None


# ------------------------- MTF DIRECTION ----------------------

def direction_of(candles):
    s = latest_structure(candles)
    return s["trend"] if s else "UNKNOWN"


def mtf_condition(daily, h4, h1):
    d = direction_of(daily)
    h = direction_of(h4)
    o = direction_of(h1)

    if d == h == o and d in ("BULLISH", "BEARISH"):
        return "C1", d

    # Daily + 4H agree, 1H opposite -> use 4H level
    if d in ("BULLISH", "BEARISH") and h == d and o != d:
        return "C2", d

    # 4H + 1H opposite Daily -> use Daily level
    if d in ("BULLISH", "BEARISH") and h != d and o != d:
        return "C3", d

    return "WAIT", "WAIT"


# ------------------------- TAP / CONFIRMATION ----------------

def touched(price, level, candles, tolerance=0.0015):
    # Historical/current touch check, not direct-entry permission.
    zone = max(price * tolerance, atr(candles) * 0.35)
    return abs(price - level) <= zone


def level_tapped(price, levels, candles):
    for name in ("tgl1", "tgl2"):
        if touched(price, levels[name], candles):
            return name
    return None


def confirmation_candle(candles, direction):
    if not candles:
        return False
    c = candles[-1]
    return bullish(c) if direction == "BUY" else bearish(c)


# ------------------------- SUPPLY / DEMAND --------------------

def supply_demand(candles):
    zones = []
    if len(candles) < 30:
        return zones

    for i in range(20, len(candles) - 2):
        base = candles[i]
        impulse = candles[i + 1]
        av = avg_body(candles[max(0, i - 20):i])

        if av <= 0:
            continue

        if bearish(base) and bullish(impulse) and body(impulse) >= av * 1.5:
            zones.append({
                "type": "DEMAND",
                "low": base["low"],
                "high": base["high"],
            })

        if bullish(base) and bearish(impulse) and body(impulse) >= av * 1.5:
            zones.append({
                "type": "SUPPLY",
                "low": base["low"],
                "high": base["high"],
            })

    return zones[-10:]


def in_matching_zone(price, zones, direction):
    wanted = "DEMAND" if direction == "BUY" else "SUPPLY"
    for z in reversed(zones):
        if z["type"] == wanted and z["low"] <= price <= z["high"]:
            return True
    return False


# ------------------------- SL / TP ----------------------------

def trade_levels(direction, entry, level_candles, higher_candles):
    """
    User rulebook framework:
      SL outside structure.
      TP next higher-timeframe reaction zone.
      Minimum RR filter.
    """
    hs, ls = pivots(level_candles)
    hhs, hls = pivots(higher_candles)

    if direction == "BUY":
        below = [x["price"] for x in ls if x["price"] < entry]
        targets = [x["price"] for x in hhs if x["price"] > entry]

        if not below or not targets:
            return None

        sl = min(below[-3:]) * 0.999
        tp = min(targets)

    else:
        above = [x["price"] for x in hs if x["price"] > entry]
        targets = [x["price"] for x in hls if x["price"] < entry]

        if not above or not targets:
            return None

        sl = max(above[-3:]) * 1.001
        tp = max(targets)

    risk = abs(entry - sl)
    reward = abs(tp - entry)

    if risk <= 0:
        return None

    rr = reward / risk

    if rr < MIN_RR:
        return None

    return {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": rr,
    }


# ------------------------- A+ CONFLUENCE ---------------------

def score_setup(direction, condition, level_name, levels,
                tap_zone, confirmation, choch_ok):
    score = 0
    reasons = []

    # MTF direction
    score += 25
    reasons.append("MTF directional bias")

    # Preferred TGL2 is A+ according to TJL rule.
    if level_name == "TGL2":
        score += 30
        reasons.append("TGL2 A+")
    else:
        score += 15
        reasons.append("TGL1")

    if condition == "C1":
        score += 20
        reasons.append("Daily/4H/1H aligned")
    elif condition == "C2":
        score += 15
        reasons.append("4H level priority")
    elif condition == "C3":
        score += 10
        reasons.append("Daily level priority")

    if tap_zone:
        score += 10
        reasons.append("Demand/Supply confluence")

    if choch_ok:
        score += 15
        reasons.append("Confirmed CHOCH")

    if confirmation:
        score += 10
        reasons.append("Confirmation candle")

    grade = "A+" if score >= 90 else "A" if score >= 75 else "B"

    return score, grade, reasons


# ------------------------- SIGNAL ENGINE ---------------------

def create_signal(symbol):
    symbol = symbol_clean(symbol)

    daily = get_klines(symbol, "1d", 220)
    h4 = get_klines(symbol, "4h", 220)
    h1 = get_klines(symbol, "1h", 250)

    if not daily or not h4 or not h1:
        return None

    condition, bias = mtf_condition(daily, h4, h1)

    if condition == "WAIT":
        return None

    if condition == "C1":
        level_tf = "1H"
        level_candles = h1
        confirmation_tf = "1M"
        confirmation_candles = get_klines(symbol, "1m", 150)
    elif condition == "C2":
        level_tf = "4H"
        level_candles = h4
        confirmation_tf = "5M"
        confirmation_candles = get_klines(symbol, "5m", 150)
    else:
        level_tf = "DAILY"
        level_candles = daily
        confirmation_tf = "1H"
        confirmation_candles = get_klines(symbol, "1h", 150)

    if not confirmation_candles:
        return None

    levels = tgl_levels(level_candles)
    if not levels:
        return None

    price = live_price(symbol)
    if price <= 0:
        return None

    tapped_level = level_tapped(price, levels, level_candles)

    if not tapped_level:
        return None

    # One level should not be repeatedly traded.
    level_key = (
        f"{symbol}:{level_tf}:{tapped_level}:"
        f"{levels[tapped_level]:.12f}"
    )
    if level_key in played_levels:
        return None

    # Confirmation hierarchy:
    # 1H level -> 1M CHOCH
    # 4H level -> 5M CHOCH
    # Daily level -> 1H CHOCH -> 1M confirmation
    level_trend = levels["trend"]

    c = choch(confirmation_candles, level_trend)

    expected = "BEARISH" if bias == "BULLISH" else "BULLISH"

    # For trend-joining TJL, the lower-TF CHOCH must not be
    # opposite to the intended entry. It is used as reaction/
    # confirmation at the level.
    if not c or c.get("fakeout") or c.get("direction") != expected:
        return None

    if condition == "C3":
        h1_confirmation = get_klines(symbol, "1h", 150)
        h1c = choch(h1_confirmation, level_trend)

        if not h1c or h1c.get("fakeout") or h1c.get("direction") != expected:
            return None

        final_1m = get_klines(symbol, "1m", 100)
        if not final_1m:
            return None

        if not confirmation_candle(final_1m, "BUY" if bias == "BULLISH" else "SELL"):
            return None
    else:
        if not confirmation_candle(
            confirmation_candles,
            "BUY" if bias == "BULLISH" else "SELL"
        ):
            return None

    direction = "BUY" if bias == "BULLISH" else "SELL"

    zones = supply_demand(level_candles)
    zone_match = in_matching_zone(price, zones, direction)

    score, grade, reasons = score_setup(
        direction,
        condition,
        tapped_level,
        levels,
        zone_match,
        True,
        True,
    )

    if A_PLUS_ONLY and grade != "A+":
        return None

    # Higher TF reaction zone for target.
    if condition == "C1":
        higher = h4
    elif condition == "C2":
        higher = daily
    else:
        higher = daily

    tl = trade_levels(
        direction,
        price,
        level_candles,
        higher,
    )

    if not tl:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "condition": condition,
        "bias": bias,
        "level_tf": level_tf,
        "confirmation_tf": confirmation_tf,
        "level_name": tapped_level,
        "tgl1": levels["tgl1"],
        "tgl2": levels["tgl2"],
        "preferred_tgl": levels["preferred"],
        "entry": tl["entry"],
        "sl": tl["sl"],
        "tp": tl["tp"],
        "rr": tl["rr"],
        "score": score,
        "grade": grade,
        "reasons": reasons,
        "level_key": level_key,
        "created_at": now(),
    }


# ------------------------- PAPER TRADING ----------------------

def send_signal(signal):
    text = (
        "🟢 <b>RULEBOOK PAPER SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>{signal['symbol']}</b> | <b>{signal['direction']}</b>\n"
        f"🧭 MTF: <b>{signal['condition']}</b>\n"
        f"📐 Level TF: <b>{signal['level_tf']}</b>\n"
        f"🎯 Level: <b>{signal['level_name']}</b>\n"
        f"⭐ Grade: <b>{signal['grade']}</b> ({signal['score']})\n"
        f"💰 Entry: <b>{fmt_price(signal['entry'])}</b>\n"
        f"🛑 SL: <b>{fmt_price(signal['sl'])}</b>\n"
        f"🎯 TP: <b>{fmt_price(signal['tp'])}</b>\n"
        f"📊 RR: <b>{signal['rr']:.2f}</b>\n"
        f"🔐 Risk: <b>{RISK_PERCENT:.2f}%</b>\n"
        f"🧠 Confirmation: <b>{signal['confirmation_tf']} CHOCH</b>\n"
        f"📌 TGL1: {fmt_price(signal['tgl1'])}\n"
        f"📌 TGL2: {fmt_price(signal['tgl2'])}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📝 <b>PAPER TRADE OPENED</b>\n"
        f"⏰ {now()}"
    )
    telegram(text)


def send_result(trade, result, r):
    emoji = "🟢" if result == "WIN" else "🔴"
    telegram(
        f"{emoji} <b>TRADE RESULT: {result}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📍 {trade['symbol']} {trade['direction']}\n"
        f"Entry: {fmt_price(trade['entry'])}\n"
        f"Exit: {fmt_price(trade['exit'])}\n"
        f"SL: {fmt_price(trade['sl'])}\n"
        f"TP: {fmt_price(trade['tp'])}\n"
        f"R: <b>{r:.2f}R</b>\n"
        f"Grade: {trade['grade']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now()}"
    )


def open_trade(signal):
    symbol = signal["symbol"]

    if symbol in active_trades:
        return False

    trade = dict(signal)
    trade["opened_at"] = now()
    trade["status"] = "OPEN"

    active_trades[symbol] = trade
    played_levels.add(signal["level_key"])

    stats["signals"] += 1
    send_signal(signal)

    return True


def monitor_trades():
    for symbol, trade in list(active_trades.items()):
        price = live_price(symbol)
        if price <= 0:
            continue

        direction = trade["direction"]
        result = None
        exit_price = None
        r = 0.0

        if direction == "BUY":
            if price <= trade["sl"]:
                result = "LOSS"
                exit_price = trade["sl"]
                r = -1.0
            elif price >= trade["tp"]:
                result = "WIN"
                exit_price = trade["tp"]
                r = trade["rr"]

        else:
            if price >= trade["sl"]:
                result = "LOSS"
                exit_price = trade["sl"]
                r = -1.0
            elif price <= trade["tp"]:
                result = "WIN"
                exit_price = trade["tp"]
                r = trade["rr"]

        if result:
            trade["exit"] = exit_price
            trade["closed_at"] = now()
            trade["status"] = result
            trade["r_multiple"] = r

            trade_history.appendleft(dict(trade))

            if result == "WIN":
                stats["wins"] += 1
            else:
                stats["losses"] += 1

            stats["total_r"] += r
            send_result(trade, result, r)

            del active_trades[symbol]


# ------------------------- MARKET MATRIX ----------------------

def update_market():
    for symbol in SYMBOLS:
        daily = get_klines(symbol, "1d", 120)
        h4 = get_klines(symbol, "4h", 120)
        h1 = get_klines(symbol, "1h", 120)

        if not daily or not h4 or not h1:
            continue

        d = direction_of(daily)
        h = direction_of(h4)
        o = direction_of(h1)
        p = live_price(symbol)

        tl = tgl_levels(h1)

        market_matrix[symbol] = {
            "price": fmt_price(p),
            "daily": d,
            "4h": h,
            "1h": o,
            "tgl1": fmt_price(tl["tgl1"]) if tl else "-",
            "tgl2": fmt_price(tl["tgl2"]) if tl else "-",
            "preferred": tl["preferred"] if tl else "-",
        }


# ------------------------- SCANNER ----------------------------

def scanner():
    stats["status"] = "ONLINE"

    telegram(
        "🤖 <b>RULEBOOK PAPER BOT ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Daily → 4H → 1H\n"
        "TGL/TJL 1-2-3\n"
        "2-close CHOCH\n"
        "Fakeout filter\n"
        "Tap → CHOCH → confirmation\n"
        "Paper trading only\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    while True:
        try:
            stats["last_scan"] = now()

            monitor_trades()
            update_market()

            for symbol in SYMBOLS:
                if symbol in active_trades:
                    continue

                try:
                    signal = create_signal(symbol)
                    if signal:
                        open_trade(signal)
                    else:
                        stats["waits"] += 1
                except Exception as exc:
                    print(f"Signal error {symbol}: {exc}")

                time.sleep(1)

            time.sleep(SCAN_INTERVAL)

        except Exception as exc:
            print("Scanner error:", exc)
            time.sleep(10)


# ------------------------- STATISTICS -------------------------

def performance():
    total = stats["wins"] + stats["losses"]

    return {
        "total_trades": total,
        "wins": stats["wins"],
        "losses": stats["losses"],
        "win_rate": round(
            (stats["wins"] / total * 100) if total else 0.0,
            2,
        ),
        "total_r": round(stats["total_r"], 2),
        "average_r": round(
            (stats["total_r"] / total) if total else 0.0,
            3,
        ),
        "active_trades": len(active_trades),
        "played_levels": len(played_levels),
    }


# ------------------------- FLASK ------------------------------

app = Flask(__name__)

HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rulebook Paper Trading</title>
<style>
body{margin:0;background:#0d1117;color:#c9d1d9;font-family:Arial,sans-serif}
.wrap{max-width:1400px;margin:auto;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card,table,.logs{background:#161b22;border:1px solid #30363d;border-radius:10px}
.card{padding:18px}.label{font-size:12px;color:#8b949e}.value{font-size:25px;font-weight:bold;margin-top:7px}
section{margin-top:28px}table{width:100%;border-collapse:collapse}th,td{padding:11px;border-bottom:1px solid #30363d;text-align:left}
th{color:#8b949e;background:#21262d}.buy,.win{color:#3fb950}.sell,.loss{color:#f85149}.wait{color:#e3b341}
.logs{padding:15px;max-height:400px;overflow:auto}.log{padding:10px;border-bottom:1px solid #30363d;font-family:monospace;font-size:12px}
.small{color:#8b949e;font-size:12px}
</style>
</head>
<body>
<div class="wrap">
<h1>⚡ Rulebook Paper Trading Bot</h1>
<div class="small">
Daily → 4H → 1H | TGL/TJL | CHOCH | Fakeout Filter | Paper Only
</div>

<div class="grid">
<div class="card"><div class="label">STATUS</div><div class="value">{{stats.status}}</div></div>
<div class="card"><div class="label">TOTAL TRADES</div><div class="value">{{p.total_trades}}</div></div>
<div class="card"><div class="label">WINS</div><div class="value win">{{p.wins}}</div></div>
<div class="card"><div class="label">LOSSES</div><div class="value loss">{{p.losses}}</div></div>
<div class="card"><div class="label">WIN RATE</div><div class="value">{{p.win_rate}}%</div></div>
<div class="card"><div class="label">TOTAL R</div><div class="value">{{p.total_r}}R</div></div>
<div class="card"><div class="label">ACTIVE</div><div class="value">{{p.active_trades}}</div></div>
</div>

<section>
<h2>📊 Market Matrix</h2>
<table>
<tr><th>Symbol</th><th>Price</th><th>Daily</th><th>4H</th><th>1H</th><th>TGL1</th><th>TGL2</th><th>Preferred</th></tr>
{% for s,x in market.items() %}
<tr>
<td><b>{{s}}</b></td><td>{{x.price}}</td>
<td>{{x.daily}}</td><td>{{x["4h"]}}</td><td>{{x["1h"]}}</td>
<td>{{x.tgl1}}</td><td>{{x.tgl2}}</td><td>{{x.preferred}}</td>
</tr>
{% endfor %}
</table>
</section>

<section>
<h2>📌 Active Paper Trades</h2>
<table>
<tr><th>Symbol</th><th>Direction</th><th>Level</th><th>Entry</th><th>SL</th><th>TP</th><th>RR</th><th>Grade</th></tr>
{% for s,t in active.items() %}
<tr>
<td>{{s}}</td>
<td class="{{'buy' if t.direction=='BUY' else 'sell'}}">{{t.direction}}</td>
<td>{{t.level_name}}</td>
<td>{{"%.8f"|format(t.entry)}}</td>
<td>{{"%.8f"|format(t.sl)}}</td>
<td>{{"%.8f"|format(t.tp)}}</td>
<td>{{"%.2f"|format(t.rr)}}</td>
<td>{{t.grade}}</td>
</tr>
{% else %}
<tr><td colspan="8">No active paper trades.</td></tr>
{% endfor %}
</table>
</section>

<section>
<h2>📈 Trade History</h2>
<table>
<tr><th>Symbol</th><th>Direction</th><th>Result</th><th>Entry</th><th>Exit</th><th>R</th><th>Grade</th><th>Opened</th><th>Closed</th></tr>
{% for t in history %}
<tr>
<td>{{t.symbol}}</td><td>{{t.direction}}</td>
<td class="{{'win' if t.status=='WIN' else 'loss'}}">{{t.status}}</td>
<td>{{"%.8f"|format(t.entry)}}</td><td>{{"%.8f"|format(t.exit)}}</td>
<td>{{"%.2f"|format(t.r_multiple)}}R</td><td>{{t.grade}}</td>
<td>{{t.opened_at}}</td><td>{{t.closed_at}}</td>
</tr>
{% else %}
<tr><td colspan="9">No completed trades yet.</td></tr>
{% endfor %}
</table>
</section>

<section>
<h2>📱 Telegram Log</h2>
<div class="logs">
{% for m in messages %}
<div class="log"><div class="small">{{m.time}}</div>{{m.message|safe}}</div>
{% else %}
<div>No messages.</div>
{% endfor %}
</div>
</section>

<section class="small">
Last scan: {{stats.last_scan}} |
Risk: {{risk}}% |
A+ only: {{a_plus}}
<br><br>
<a href="/api/stats">JSON API</a>
</section>
</div>
</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(
        HTML,
        stats=stats,
        p=performance(),
        market=market_matrix,
        active=active_trades,
        history=list(trade_history),
        messages=list(recent_messages),
        risk=RISK_PERCENT,
        a_plus=A_PLUS_ONLY,
    )


@app.route("/api/stats")
def api_stats():
    return jsonify({
        "bot": stats,
        "performance": performance(),
        "market": market_matrix,
        "active_trades": list(active_trades.values()),
        "trade_history": list(trade_history),
        "played_levels": list(played_levels),
        "telegram_messages": list(recent_messages),
    })


@app.route("/api/signal/<symbol>")
def api_signal(symbol):
    signal = create_signal(symbol_clean(symbol))
    if not signal:
        return jsonify({
            "status": "WAIT",
            "symbol": symbol_clean(symbol),
            "message": "No valid Rulebook setup."
        })
    return jsonify({"status": "SIGNAL", "signal": signal})


@app.route("/api/health")
def health():
    return jsonify({
        "status": stats["status"],
        "last_scan": stats["last_scan"],
        "active_trades": len(active_trades),
    })


# ------------------------- START ------------------------------

if __name__ == "__main__":
    print("Starting Rulebook Paper Trading Bot...")
    print("Paper trading only. No real orders are placed.")

    t = threading.Thread(target=scanner, daemon=True)
    t.start()

    port = int(os.getenv("PORT", "5000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
