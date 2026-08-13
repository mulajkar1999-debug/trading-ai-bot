import os
import threading
import time
from datetime import datetime, timezone
from collections import deque
from statistics import mean

import requests
from flask import Flask, jsonify, render_template_string


# ============================================================
# SMC + TGL + MTF + CHOCH TRADING BOT
# Based on User Trading Rulebook
#
# IMPORTANT:
# - PAPER TRADING ONLY
# - No real order execution
# - Rule-based signal engine
# - Public Binance market data
# ============================================================


# ============================================================
# 1. CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1317739622")

BINANCE_URL = "https://api.binance.com/api/v3/klines"
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "30"))

RISK_PER_TRADE = float(
    os.environ.get("RISK_PER_TRADE", "0.005")
)  # 0.5%

MIN_RR = float(
    os.environ.get("MIN_RR", "1.5")
)

MAX_MESSAGES = 50
MAX_TRADES = 500

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
]

# Rulebook timeframe mapping
TIMEFRAMES = {
    "daily": "1d",
    "4h": "4h",
    "1h": "1h",
    "5m": "5m",
    "1m": "1m",
}


# ============================================================
# 2. GLOBAL STATE
# ============================================================

recent_telegram_messages = deque(maxlen=MAX_MESSAGES)

trade_history = deque(maxlen=MAX_TRADES)

active_trades = {}

market_pairs = {}

bot_stats = {
    "status": "STARTING",
    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "last_scan": "Initializing...",
    "total_signals": 0,
    "wins": 0,
    "losses": 0,
    "waits": 0,
    "invalid_setups": 0,
    "total_r": 0.0,
}


# ============================================================
# 3. HELPERS
# ============================================================

def now_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_float(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def format_price(price):
    if price >= 1000:
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:.4f}"
    return f"{price:.8f}"


def normalize_symbol(symbol):
    return symbol.replace("/", "").upper()


# ============================================================
# 4. TELEGRAM
# ============================================================

def send_telegram_message(message_text, signal_type=None):
    timestamp = now_string()

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message_text,
            "parse_mode": "HTML",
        }

        try:
            response = requests.post(
                url,
                json=payload,
                timeout=10
            )

            if response.status_code != 200:
                print(
                    "Telegram HTTP error:",
                    response.status_code,
                    response.text[:300]
                )

        except Exception as exc:
            print("Telegram error:", exc)

    recent_telegram_messages.appendleft({
        "timestamp": timestamp,
        "message": message_text,
    })

    if signal_type == "SIGNAL":
        bot_stats["total_signals"] += 1


def send_trade_signal(signal):
    direction = signal["direction"]

    emoji = "🟢" if direction == "BUY" else "🔴"

    message = (
        f"{emoji} <b>SMC RULEBOOK SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Asset:</b> {signal['symbol']}\n"
        f"📌 <b>Direction:</b> {direction}\n"
        f"⭐ <b>Grade:</b> {signal['grade']}\n"
        f"📊 <b>Condition:</b> {signal['condition']}\n"
        f"⏱ <b>Level TF:</b> {signal['level_tf']}\n"
        f"🔎 <b>Confirmation TF:</b> {signal['confirmation_tf']}\n"
        f"🧠 <b>CHOCH:</b> {signal['choch']}\n"
        f"🎯 <b>Entry:</b> {format_price(signal['entry'])}\n"
        f"🛑 <b>SL:</b> {format_price(signal['sl'])}\n"
        f"🎯 <b>TP:</b> {format_price(signal['tp'])}\n"
        f"📐 <b>RR:</b> {signal['rr']:.2f}\n"
        f"🔐 <b>Risk:</b> {RISK_PER_TRADE * 100:.2f}%\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now_string()}"
    )

    send_telegram_message(
        message,
        signal_type="SIGNAL"
    )


def send_result_alert(trade, result, r_multiple):
    emoji = "🟢" if result == "WIN" else "🔴"

    message = (
        f"{emoji} <b>TRADE RESULT: {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Asset:</b> {trade['symbol']}\n"
        f"📌 <b>Direction:</b> {trade['direction']}\n"
        f"🎯 <b>Entry:</b> {format_price(trade['entry'])}\n"
        f"🚪 <b>Exit:</b> {format_price(trade['exit'])}\n"
        f"📊 <b>R:</b> {r_multiple:.2f}R\n"
        f"⭐ <b>Grade:</b> {trade['grade']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now_string()}"
    )

    send_telegram_message(message)


# ============================================================
# 5. BINANCE DATA
# ============================================================

def get_klines(symbol, interval, limit=250):

    params = {
        "symbol": normalize_symbol(symbol),
        "interval": interval,
        "limit": limit,
    }

    try:
        response = requests.get(
            BINANCE_URL,
            params=params,
            timeout=10
        )

        response.raise_for_status()

        raw = response.json()

        candles = []

        for row in raw:
            candles.append({
                "open_time": int(row[0]),
                "open": safe_float(row[1]),
                "high": safe_float(row[2]),
                "low": safe_float(row[3]),
                "close": safe_float(row[4]),
                "volume": safe_float(row[5]),
                "close_time": int(row[6]),
            })

        return candles

    except Exception as exc:
        print(
            f"Kline error {symbol} {interval}:",
            exc
        )
        return []


def get_live_price(symbol):

    try:

        response = requests.get(
            BINANCE_TICKER_URL,
            params={"symbol": normalize_symbol(symbol)},
            timeout=10
        )

        response.raise_for_status()

        return safe_float(
            response.json()["price"]
        )

    except Exception as exc:
        print("Ticker error:", exc)
        return 0.0


# ============================================================
# 6. CANDLE / STRUCTURE ENGINE
# ============================================================

def is_bullish(c):
    return c["close"] > c["open"]


def is_bearish(c):
    return c["close"] < c["open"]


def candle_body(c):
    return abs(c["close"] - c["open"])


def average_body(candles, length=20):

    sample = candles[-length:]

    if not sample:
        return 0

    return mean(
        candle_body(c)
        for c in sample
    )


def is_strong_bullish_impulse(candles):

    if len(candles) < 21:
        return False

    last = candles[-1]

    avg = average_body(candles[:-1])

    return (
        is_bullish(last)
        and candle_body(last) > avg * 1.5
        and last["close"] > last["open"]
    )


def is_strong_bearish_impulse(candles):

    if len(candles) < 21:
        return False

    last = candles[-1]

    avg = average_body(candles[:-1])

    return (
        is_bearish(last)
        and candle_body(last) > avg * 1.5
        and last["close"] < last["open"]
    )


# ============================================================
# 7. SWING DETECTION
#
# Rulebook:
# 2-candle retracement concept
# ============================================================

def find_swings(candles):

    highs = []
    lows = []

    if len(candles) < 5:
        return highs, lows

    for i in range(2, len(candles) - 2):

        c = candles[i]

        left1 = candles[i - 1]
        left2 = candles[i - 2]

        right1 = candles[i + 1]
        right2 = candles[i + 2]

        # Swing high
        if (
            c["high"] > left1["high"]
            and c["high"] > left2["high"]
            and c["high"] >= right1["high"]
            and c["high"] >= right2["high"]
        ):
            highs.append({
                "index": i,
                "price": c["high"],
                "candle": c,
            })

        # Swing low
        if (
            c["low"] < left1["low"]
            and c["low"] < left2["low"]
            and c["low"] <= right1["low"]
            and c["low"] <= right2["low"]
        ):
            lows.append({
                "index": i,
                "price": c["low"],
                "candle": c,
            })

    return highs, lows


def detect_structure(candles):

    highs, lows = find_swings(candles)

    if len(highs) < 2 or len(lows) < 2:
        return {
            "trend": "UNKNOWN",
            "highs": highs,
            "lows": lows,
            "last_high": None,
            "last_low": None,
        }

    h1 = highs[-1]["price"]
    h2 = highs[-2]["price"]

    l1 = lows[-1]["price"]
    l2 = lows[-2]["price"]

    if h1 > h2 and l1 > l2:
        trend = "BULLISH"

    elif h1 < h2 and l1 < l2:
        trend = "BEARISH"

    else:
        trend = "SIDEWAYS"

    return {
        "trend": trend,
        "highs": highs,
        "lows": lows,
        "last_high": highs[-1],
        "last_low": lows[-1],
    }


# ============================================================
# 8. CHOCH
#
# Rulebook:
# bullish -> bearish:
# 2 consecutive closes below LSM
#
# bearish -> bullish:
# 2 consecutive closes above LRM
# ============================================================

def detect_choch(candles):

    structure = detect_structure(candles)

    trend = structure["trend"]

    if len(candles) < 3:
        return None

    c1 = candles[-2]
    c2 = candles[-1]

    # Bullish -> Bearish
    if trend == "BULLISH":

        last_low = structure["last_low"]

        if last_low:

            level = last_low["price"]

            if (
                c1["close"] < level
                and c2["close"] < level
            ):
                return {
                    "direction": "BEARISH",
                    "level": level,
                    "candle": c2,
                }

    # Bearish -> Bullish
    if trend == "BEARISH":

        last_high = structure["last_high"]

        if last_high:

            level = last_high["price"]

            if (
                c1["close"] > level
                and c2["close"] > level
            ):
                return {
                    "direction": "BULLISH",
                    "level": level,
                    "candle": c2,
                }

    return None


# ============================================================
# 9. TGL LEVELS
#
# Rulebook:
# latest 1-2-3 structure
# Level 1 + Level 2
#
# Exact TGL mathematical formula is not fully formalized
# in supplied rulebook, so this implementation derives
# structural levels transparently.
# ============================================================

def calculate_tgl(candles):

    structure = detect_structure(candles)

    levels = []

    highs = structure["highs"]
    lows = structure["lows"]

    if len(highs) >= 2 and len(lows) >= 2:

        latest_high = highs[-1]["price"]
        previous_high = highs[-2]["price"]

        latest_low = lows[-1]["price"]
        previous_low = lows[-2]["price"]

        if structure["trend"] == "BULLISH":

            # TGL Level 1
            level1 = latest_low

            # TGL Level 2:
            # structural midpoint / retracement reference
            level2 = min(
                latest_high,
                max(
                    latest_low,
                    previous_high
                )
            )

            levels = [
                {
                    "name": "TGL-1",
                    "price": level1,
                    "type": "SUPPORT",
                },
                {
                    "name": "TGL-2",
                    "price": level2,
                    "type": "SUPPORT",
                }
            ]

        elif structure["trend"] == "BEARISH":

            level1 = latest_high

            level2 = max(
                latest_low,
                min(
                    latest_high,
                    previous_low
                )
            )

            levels = [
                {
                    "name": "TGL-1",
                    "price": level1,
                    "type": "RESISTANCE",
                },
                {
                    "name": "TGL-2",
                    "price": level2,
                    "type": "RESISTANCE",
                }
            ]

    return levels


# ============================================================
# 10. DEMAND / SUPPLY
#
# Rulebook:
# Strong impulse + opposite candle before impulse
# ============================================================

def detect_demand_supply(candles):

    if len(candles) < 25:
        return []

    zones = []

    for i in range(3, len(candles) - 1):

        impulse = candles[i + 1]
        base = candles[i]

        avg = average_body(
            candles[max(0, i - 20):i]
        )

        # Demand
        if (
            is_bullish(impulse)
            and candle_body(impulse) > avg * 1.5
            and is_bearish(base)
        ):
            zones.append({
                "type": "DEMAND",
                "low": base["low"],
                "high": base["high"],
                "index": i,
            })

        # Supply
        if (
            is_bearish(impulse)
            and candle_body(impulse) > avg * 1.5
            and is_bullish(base)
        ):
            zones.append({
                "type": "SUPPLY",
                "low": base["low"],
                "high": base["high"],
                "index": i,
            })

    return zones[-10:]


# ============================================================
# 11. LEVEL TAP
# ============================================================

def price_tapped_level(price, level, tolerance):

    return abs(price - level) <= tolerance


def detect_zone_tap(price, zones):

    for zone in reversed(zones):

        if zone["low"] <= price <= zone["high"]:

            return zone

    return None


# ============================================================
# 12. CONFIRMATION CANDLE
# ============================================================

def bullish_confirmation(candle):
    return is_bullish(candle)


def bearish_confirmation(candle):
    return is_bearish(candle)


# ============================================================
# 13. MTF CONDITION
#
# Rulebook:
#
# C1:
# D = 4H = 1H
# -> trade 1H level
#
# C2:
# 1H opposite
# -> trade 4H level
#
# C3:
# 4H + 1H opposite Daily
# -> trade Daily level
# ============================================================

def determine_condition(daily, h4, h1):

    d = daily["trend"]
    h4t = h4["trend"]
    h1t = h1["trend"]

    if (
        d == h4t
        and h4t == h1t
        and d in ("BULLISH", "BEARISH")
    ):
        return "C1"

    if (
        d in ("BULLISH", "BEARISH")
        and h4t == d
        and h1t != d
    ):
        return "C2"

    if (
        d in ("BULLISH", "BEARISH")
        and h4t != d
        and h1t != d
    ):
        return "C3"

    return "WAIT"


def condition_direction(condition, daily, h4, h1):

    if condition == "C1":
        return h1["trend"]

    if condition == "C2":
        return h4["trend"]

    if condition == "C3":
        return daily["trend"]

    return "WAIT"


# ============================================================
# 14. TARGET / SL
#
# Rulebook:
# SL structure/zone ke opposite side.
# TP next HTF reaction zone.
# ============================================================

def calculate_trade_levels(
    direction,
    entry,
    structure,
    zones,
    higher_candles
):

    highs, lows = find_swings(
        higher_candles
    )

    if direction == "BUY":

        candidate_lows = [
            x["price"] for x in lows
            if x["price"] < entry
        ]

        if not candidate_lows:
            return None

        sl = min(candidate_lows[-3:])

        candidate_targets = [
            x["price"] for x in highs
            if x["price"] > entry
        ]

        if not candidate_targets:
            return None

        tp = min(candidate_targets)

    else:

        candidate_highs = [
            x["price"] for x in highs
            if x["price"] > entry
        ]

        if not candidate_highs:
            return None

        sl = max(candidate_highs[-3:])

        candidate_targets = [
            x["price"] for x in lows
            if x["price"] < entry
        ]

        if not candidate_targets:
            return None

        tp = max(candidate_targets)

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


# ============================================================
# 15. A+ SCORE
# ============================================================

def calculate_setup_score(
    direction,
    condition,
    structure,
    tgl_levels,
    zones,
    price,
    choch
):

    score = 0
    reasons = []

    # Core: MTF structure
    if (
        structure["trend"] == direction
    ):
        score += 30
        reasons.append("HTF Structure")

    # Condition
    if condition == "C1":
        score += 20
        reasons.append("MTF Aligned")

    elif condition == "C2":
        score += 15
        reasons.append("4H Direction")

    elif condition == "C3":
        score += 10
        reasons.append("Daily Direction")

    # TGL
    tolerance = price * 0.002

    tgl_hit = False

    for level in tgl_levels:

        if price_tapped_level(
            price,
            level["price"],
            tolerance
        ):
            tgl_hit = True
            break

    if tgl_hit:
        score += 20
        reasons.append("TGL Tap")

    # Demand / Supply
    zone = detect_zone_tap(
        price,
        zones
    )

    if zone:

        if (
            direction == "BUY"
            and zone["type"] == "DEMAND"
        ):
            score += 15
            reasons.append("Demand")

        elif (
            direction == "SELL"
            and zone["type"] == "SUPPLY"
        ):
            score += 15
            reasons.append("Supply")

    # CHOCH
    if choch:
        score += 15
        reasons.append("CHOCH")

    if score >= 80:
        grade = "A+"

    elif score >= 65:
        grade = "A"

    elif score >= 50:
        grade = "B"

    else:
        grade = "WAIT"

    return score, grade, reasons


# ============================================================
# 16. TRADE ENGINE
# ============================================================

def create_signal(symbol):

    # -------------------------------
    # HTF DATA
    # -------------------------------

    daily_candles = get_klines(
        symbol,
        TIMEFRAMES["daily"],
        200
    )

    h4_candles = get_klines(
        symbol,
        TIMEFRAMES["4h"],
        200
    )

    h1_candles = get_klines(
        symbol,
        TIMEFRAMES["1h"],
        250
    )

    if not (
        daily_candles
        and h4_candles
        and h1_candles
    ):
        return None

    daily_structure = detect_structure(
        daily_candles
    )

    h4_structure = detect_structure(
        h4_candles
    )

    h1_structure = detect_structure(
        h1_candles
    )

    condition = determine_condition(
        daily_structure,
        h4_structure,
        h1_structure
    )

    if condition == "WAIT":
        return None

    direction = condition_direction(
        condition,
        daily_structure,
        h4_structure,
        h1_structure
    )

    if direction not in (
        "BULLISH",
        "BEARISH"
    ):
        return None

    # -------------------------------
    # Select trading level
    # -------------------------------

    if condition == "C1":

        level_tf = "1H"
        level_candles = h1_candles
        confirmation_tf = "1M"

    elif condition == "C2":

        level_tf = "4H"
        level_candles = h4_candles
        confirmation_tf = "5M"

    else:

        level_tf = "DAILY"
        level_candles = daily_candles
        confirmation_tf = "1H"

    # -------------------------------
    # TGL
    # -------------------------------

    tgl_levels = calculate_tgl(
        level_candles
    )

    # -------------------------------
    # Demand / Supply
    # -------------------------------

    zones = detect_demand_supply(
        level_candles
    )

    # -------------------------------
    # Current price
    # -------------------------------

    price = get_live_price(symbol)

    if price <= 0:
        return None

    tolerance = price * 0.002

    # Price must reach relevant level
    level_tapped = any(
        price_tapped_level(
            price,
            level["price"],
            tolerance
        )
        for level in tgl_levels
    )

    zone_tapped = detect_zone_tap(
        price,
        zones
    )

    # Rulebook:
    # Level देखकर direct trade nahi.
    # Price must come to relevant area.

    if not level_tapped and not zone_tapped:
        return None

    # -------------------------------
    # Confirmation timeframe
    # -------------------------------

    confirmation_candles = get_klines(
        symbol,
        TIMEFRAMES[
            {
                "1M": "1m",
                "5M": "5m",
                "1H": "1h",
            }[confirmation_tf]
        ],
        200
    )

    if not confirmation_candles:
        return None

    choch = detect_choch(
        confirmation_candles
    )

    if not choch:
        return None

    expected_choch = (
        "BULLISH"
        if direction == "BULLISH"
        else "BEARISH"
    )

    if choch["direction"] != expected_choch:
        return None

    # Daily special:
    # Daily -> 1H CHOCH -> 1M confirmation
    if condition == "C3":

        one_hour_choch = detect_choch(
            h1_candles
        )

        if not one_hour_choch:
            return None

        if (
            one_hour_choch["direction"]
            != expected_choch
        ):
            return None

        final_confirmation_candles = get_klines(
            symbol,
            "1m",
            100
        )

        if not final_confirmation_candles:
            return None

        final_candle = final_confirmation_candles[-1]

    else:

        final_candle = confirmation_candles[-1]

    # -------------------------------
    # Confirmation candle
    # -------------------------------

    if direction == "BULLISH":

        if not bullish_confirmation(
            final_candle
        ):
            return None

        signal_direction = "BUY"

    else:

        if not bearish_confirmation(
            final_candle
        ):
            return None

        signal_direction = "SELL"

    # -------------------------------
    # Setup score
    # -------------------------------

    score, grade, reasons = calculate_setup_score(
        direction,
        condition,
        level_candles_structure := detect_structure(
            level_candles
        ),
        tgl_levels,
        zones,
        price,
        choch
    )

    # Only high quality setups
    if score < 65:
        return None

    # Beginner/A+ mode
    if os.environ.get(
        "A_PLUS_ONLY",
        "false"
    ).lower() == "true":

        if grade != "A+":
            return None

    # -------------------------------
    # SL / TP
    # -------------------------------

    levels = calculate_trade_levels(
        signal_direction,
        price,
        level_candles_structure,
        zones,
        level_candles
    )

    if not levels:
        return None

    return {
        "symbol": symbol,
        "direction": signal_direction,
        "condition": condition,
        "level_tf": level_tf,
        "confirmation_tf": confirmation_tf,
        "choch": "BULLISH" if signal_direction == "BUY"
        else "BEARISH",
        "entry": levels["entry"],
        "sl": levels["sl"],
        "tp": levels["tp"],
        "rr": levels["rr"],
        "score": score,
        "grade": grade,
        "reasons": reasons,
        "created_at": now_string(),
    }


# ============================================================
# 17. PAPER TRADE MANAGEMENT
# ============================================================

def open_paper_trade(signal):

    symbol = signal["symbol"]

    # Only one active trade per symbol
    if symbol in active_trades:
        return False

    trade_id = (
        f"{symbol}_"
        f"{int(time.time())}"
    )

    trade = {
        "id": trade_id,
        "symbol": symbol,
        "direction": signal["direction"],
        "entry": signal["entry"],
        "sl": signal["sl"],
        "tp": signal["tp"],
        "rr": signal["rr"],
        "score": signal["score"],
        "grade": signal["grade"],
        "condition": signal["condition"],
        "level_tf": signal["level_tf"],
        "confirmation_tf": signal["confirmation_tf"],
        "opened_at": now_string(),
        "status": "OPEN",
    }

    active_trades[symbol] = trade

    send_trade_signal(signal)

    return True


def monitor_active_trades():

    for symbol, trade in list(
        active_trades.items()
    ):

        price = get_live_price(symbol)

        if price <= 0:
            continue

        direction = trade["direction"]

        result = None
        exit_price = None
        r_multiple = 0.0

        if direction == "BUY":

            if price <= trade["sl"]:

                result = "LOSS"
                exit_price = trade["sl"]
                r_multiple = -1.0

            elif price >= trade["tp"]:

                result = "WIN"
                exit_price = trade["tp"]
                r_multiple = trade["rr"]

        else:

            if price >= trade["sl"]:

                result = "LOSS"
                exit_price = trade["sl"]
                r_multiple = -1.0

            elif price <= trade["tp"]:

                result = "WIN"
                exit_price = trade["tp"]
                r_multiple = trade["rr"]

        if result:

            trade["exit"] = exit_price
            trade["status"] = result
            trade["closed_at"] = now_string()
            trade["r_multiple"] = r_multiple

            trade_history.appendleft(
                trade.copy()
            )

            if result == "WIN":
                bot_stats["wins"] += 1

            else:
                bot_stats["losses"] += 1

            bot_stats["total_r"] += r_multiple

            send_result_alert(
                trade,
                result,
                r_multiple
            )

            del active_trades[symbol]


# ============================================================
# 18. MARKET MATRIX
# ============================================================

def update_market_matrix():

    for symbol in SYMBOLS:

        price = get_live_price(symbol)

        daily = get_klines(
            symbol,
            "1d",
            100
        )

        h1 = get_klines(
            symbol,
            "1h",
            100
        )

        if not daily or not h1:
            continue

        daily_structure = detect_structure(
            daily
        )

        h1_structure = detect_structure(
            h1
        )

        h1_choch = detect_choch(
            h1
        )

        structure_text = (
            h1_choch["direction"] + " CHOCH"
            if h1_choch
            else h1_structure["trend"]
        )

        market_pairs[symbol] = {
            "price": format_price(price),
            "trend_daily": daily_structure["trend"],
            "trend_1h": h1_structure["trend"],
            "structure": structure_text,
        }


# ============================================================
# 19. BACKGROUND SCANNER
# ============================================================

def background_trading_scanner():

    print(
        "🚀 Rulebook Trading Scanner Started"
    )

    bot_stats["status"] = "ONLINE"

    send_telegram_message(
        "🤖 <b>RULEBOOK SMC BOT ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Strategy:\n"
        "Daily → 4H → 1H\n"
        "TGL + Structure + CHOCH\n"
        "Confirmation Entry\n"
        "Paper Trading Mode\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    while True:

        try:

            bot_stats["last_scan"] = now_string()

            update_market_matrix()

            # First manage existing trades
            monitor_active_trades()

            # Then search for new signals
            for symbol in SYMBOLS:

                try:

                    if symbol in active_trades:
                        continue

                    signal = create_signal(
                        symbol
                    )

                    if signal:

                        opened = open_paper_trade(
                            signal
                        )

                        if opened:
                            print(
                                "NEW SIGNAL:",
                                signal
                            )

                    time.sleep(1)

                except Exception as exc:

                    print(
                        f"Signal error {symbol}:",
                        exc
                    )

            time.sleep(
                SCAN_INTERVAL
            )

        except Exception as exc:

            print(
                "Scanner loop error:",
                exc
            )

            time.sleep(10)


# ============================================================
# 20. STATISTICS
# ============================================================

def get_statistics():

    wins = bot_stats["wins"]
    losses = bot_stats["losses"]

    total = wins + losses

    win_rate = (
        wins / total * 100
        if total > 0
        else 0
    )

    avg_r = (
        bot_stats["total_r"] / total
        if total > 0
        else 0
    )

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(
            win_rate,
            2
        ),
        "total_r": round(
            bot_stats["total_r"],
            2
        ),
        "average_r": round(
            avg_r,
            3
        ),
        "active_trades": len(
            active_trades
        ),
    }


# ============================================================
# 21. FLASK APP
# ============================================================

app = Flask(__name__)


DASHBOARD_HTML = """

<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width, initial-scale=1.0">

<title>
Rulebook SMC AI Dashboard
</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #0d1117;
    color: #c9d1d9;
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
}

.container {
    max-width: 1300px;
    margin: auto;
    padding: 20px;
}

.header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 15px;
    padding-bottom: 20px;
    border-bottom: 1px solid #30363d;
}

.online {
    color: #3fb950;
    font-weight: bold;
}

.grid {
    display: grid;
    grid-template-columns:
        repeat(auto-fit, minmax(170px, 1fr));
    gap: 15px;
    margin: 25px 0;
}

.card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 12px;
    padding: 18px;
}

.card h3 {
    margin: 0 0 10px;
    color: #8b949e;
    font-size: 12px;
    text-transform: uppercase;
}

.value {
    font-size: 25px;
    font-weight: bold;
}

table {
    width: 100%;
    border-collapse: collapse;
    background: #161b22;
    margin-top: 15px;
    border-radius: 10px;
    overflow: hidden;
}

th,
td {
    padding: 12px;
    border-bottom: 1px solid #30363d;
    text-align: left;
}

th {
    background: #21262d;
    color: #8b949e;
}

.bull {
    color: #3fb950;
    font-weight: bold;
}

.bear {
    color: #f85149;
    font-weight: bold;
}

.wait {
    color: #e3b341;
}

.section {
    margin-top: 30px;
}

.trade-open {
    color: #58a6ff;
}

.win {
    color: #3fb950;
}

.loss {
    color: #f85149;
}

.log {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 15px;
    max-height: 400px;
    overflow-y: auto;
}

.log-item {
    padding: 10px;
    border-bottom: 1px solid #21262d;
    font-family: monospace;
    font-size: 12px;
}

.small {
    color: #8b949e;
    font-size: 12px;
}

</style>

</head>

<body>

<div class="container">

<div class="header">

<div>

<h1>
⚡ Rulebook SMC AI
</h1>

<div class="small">
TGL + MTF + CHOCH + Price Action
</div>

</div>

<div class="online">
● {{ stats.status }}
</div>

</div>


<div class="grid">

<div class="card">
<h3>Total Trades</h3>
<div class="value">
{{ performance.total_trades }}
</div>
</div>

<div class="card">
<h3>Wins</h3>
<div class="value win">
{{ performance.wins }}
</div>
</div>

<div class="card">
<h3>Losses</h3>
<div class="value loss">
{{ performance.losses }}
</div>
</div>

<div class="card">
<h3>Win Rate</h3>
<div class="value">
{{ performance.win_rate }}%
</div>
</div>

<div class="card">
<h3>Total R</h3>
<div class="value">
{{ performance.total_r }}R
</div>
</div>

<div class="card">
<h3>Average R</h3>
<div class="value">
{{ performance.average_r }}R
</div>
</div>

<div class="card">
<h3>Active Trades</h3>
<div class="value trade-open">
{{ performance.active_trades }}
</div>
</div>

</div>


<div class="section">

<h2>
📊 Market Matrix
</h2>

<table>

<thead>

<tr>
<th>Symbol</th>
<th>Price</th>
<th>Daily</th>
<th>1H</th>
<th>Structure</th>
</tr>

</thead>

<tbody>

{% for symbol, data in pairs.items() %}

<tr>

<td>
<strong>{{ symbol }}</strong>
</td>

<td>
{{ data.price }}
</td>

<td class="
{{ 'bull' if data.trend_daily == 'BULLISH'
else 'bear' if data.trend_daily == 'BEARISH'
else 'wait' }}
">

{{ data.trend_daily }}

</td>

<td class="
{{ 'bull' if data.trend_1h == 'BULLISH'
else 'bear' if data.trend_1h == 'BEARISH'
else 'wait' }}
">

{{ data.trend_1h }}

</td>

<td>
{{ data.structure }}
</td>

</tr>

{% endfor %}

</tbody>

</table>

</div>


<div class="section">

<h2>
📌 Active Paper Trades
</h2>

<table>

<thead>

<tr>
<th>Symbol</th>
<th>Direction</th>
<th>Entry</th>
<th>SL</th>
<th>TP</th>
<th>RR</th>
<th>Grade</th>
</tr>

</thead>

<tbody>

{% for symbol, trade in active.items() %}

<tr>

<td>{{ symbol }}</td>

<td class="
{{ 'bull'
if trade.direction == 'BUY'
else 'bear' }}
">

{{ trade.direction }}

</td>

<td>
{{ "%.6f"|format(trade.entry) }}
</td>

<td>
{{ "%.6f"|format(trade.sl) }}
</td>

<td>
{{ "%.6f"|format(trade.tp) }}
</td>

<td>
{{ "%.2f"|format(trade.rr) }}
</td>

<td>
{{ trade.grade }}
</td>

</tr>

{% else %}

<tr>
<td colspan="7">
No active trades
</td>
</tr>

{% endfor %}

</tbody>

</table>

</div>


<div class="section">

<h2>
📈 Trade History
</h2>

<table>

<thead>

<tr>
<th>Symbol</th>
<th>Direction</th>
<th>Result</th>
<th>R</th>
<th>Grade</th>
<th>Opened</th>
<th>Closed</th>
</tr>

</thead>

<tbody>

{% for trade in history %}

<tr>

<td>{{ trade.symbol }}</td>

<td>{{ trade.direction }}</td>

<td class="
{{ 'win'
if trade.status == 'WIN'
else 'loss' }}
">

{{ trade.status }}

</td>

<td>
{{ "%.2f"|format(trade.r_multiple) }}R
</td>

<td>
{{ trade.grade }}
</td>

<td>
{{ trade.opened_at }}
</td>

<td>
{{ trade.closed_at }}
</td>

</tr>

{% else %}

<tr>
<td colspan="7">
No completed trades yet.
</td>
</tr>

{% endfor %}

</tbody>

</table>

</div>


<div class="section">

<h2>
📱 Telegram Log
</h2>

<div class="log">

{% for msg in messages %}

<div class="log-item">

<div class="small">
{{ msg.timestamp }}
</div>

<div>
{{ msg.message | safe }}
</div>

</div>

{% else %}

<div>
No messages.
</div>

{% endfor %}

</div>

</div>


<div class="section small">

Last scan:
{{ stats.last_scan }}

<br><br>

<a href="/api/stats">
Raw JSON API
</a>

</div>

</div>

<script>

setTimeout(
    () => location.reload(),
    15000
);

</script>

</body>

</html>

"""


@app.route("/")
def home():

    return render_template_string(
        DASHBOARD_HTML,
        stats=bot_stats,
        performance=get_statistics(),
        pairs=market_pairs,
        active=active_trades,
        history=list(trade_history),
        messages=list(
            recent_telegram_messages
        ),
    )


@app.route("/api/stats")
def api_stats():

    return jsonify({

        "bot": bot_stats,

        "performance":
            get_statistics(),

        "active_trades":
            list(
                active_trades.values()
            ),

        "trade_history":
            list(trade_history),

        "market":
            market_pairs,

        "telegram_messages":
            list(
                recent_telegram_messages
            ),

    })


@app.route("/api/signal/<symbol>")
def api_signal(symbol):

    symbol = normalize_symbol(symbol)

    signal = create_signal(
        symbol
    )

    if not signal:

        return jsonify({
            "status": "WAIT",
            "symbol": symbol,
            "message":
                "No valid Rulebook setup."
        })

    return jsonify({
        "status": "SIGNAL",
        "signal": signal
    })


@app.route("/api/health")
def health():

    return jsonify({
        "status": bot_stats["status"],
        "last_scan": bot_stats["last_scan"],
        "active_trades":
            len(active_trades),
    })


# ============================================================
# 22. START
# ============================================================

def start_scanner():

    thread = threading.Thread(
        target=background_trading_scanner,
        daemon=True
    )

    thread.start()


if __name__ == "__main__":

    start_scanner()

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    print(
        f"Dashboard running on port {port}"
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
