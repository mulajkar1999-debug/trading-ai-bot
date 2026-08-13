import os
import time
import threading
from datetime import datetime
from collections import deque
from statistics import mean

import requests
from flask import Flask, jsonify, render_template_string


# ============================================================
# RULEBOOK SMC PAPER TRADING BOT v2
#
# PAPER TRADING ONLY
#
# Rulebook:
# Daily -> 4H -> 1H directional analysis
# C1: All aligned -> 1H level
# C2: 1H opposite -> 4H level
# C3: 4H + 1H opposite Daily -> Daily level
#
# 1H -> 1M CHOCH
# 4H -> 5M CHOCH
# Daily -> 1H CHOCH -> 1M confirmation
#
# CHOCH = 2 consecutive CLOSED candles beyond level
# Fakeout filter
# TGL Level 1 / Level 2
# 2 opposite closes = level invalid
# Tap -> confirmation -> entry
# Green confirmation -> BUY
# Red confirmation -> SELL
# A+ confluence
# Already played / retested level avoidance
# Counter-trend avoidance
# Structure based SL
# HTF reaction target
# 0.5% risk default
#
# NO REAL ORDERS
# ============================================================


# ============================================================
# 1. CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get(
    "8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM",
    ""
)

TELEGRAM_CHAT_ID = os.environ.get(
    "1317739622",
    ""
)

BINANCE_KLINES_URL = (
    "https://api.binance.com/api/v3/klines"
)

BINANCE_TICKER_URL = (
    "https://api.binance.com/api/v3/ticker/price"
)

SCAN_INTERVAL = int(
    os.environ.get("SCAN_INTERVAL", "30")
)

RISK_PER_TRADE = float(
    os.environ.get("RISK_PER_TRADE", "0.005")
)

MIN_RR = float(
    os.environ.get("MIN_RR", "1.5")
)

A_PLUS_ONLY = (
    os.environ.get(
        "A_PLUS_ONLY",
        "true"
    ).lower()
    == "true"
)

MAX_MESSAGES = 100
MAX_TRADES = 1000

SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
]


# ============================================================
# TIMEFRAME CONFIG
# ============================================================

TF = {
    "DAILY": "1d",
    "4H": "4h",
    "1H": "1h",
    "5M": "5m",
    "1M": "1m",
}


# ============================================================
# GLOBAL STATE
# ============================================================

recent_messages = deque(
    maxlen=MAX_MESSAGES
)

trade_history = deque(
    maxlen=MAX_TRADES
)

active_trades = {}

played_levels = {}

market_pairs = {}

bot_stats = {
    "status": "STARTING",
    "started_at": datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    ),
    "last_scan": "Initializing...",
    "signals": 0,
    "wins": 0,
    "losses": 0,
    "waits": 0,
    "invalidated": 0,
    "total_r": 0.0,
}


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def f(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def normalize_symbol(symbol):
    return symbol.replace(
        "/", ""
    ).upper()


def price_format(price):
    if price >= 1000:
        return f"{price:,.2f}"

    if price >= 1:
        return f"{price:.4f}"

    return f"{price:.8f}"


def direction_from_trend(trend):
    if trend == "BULLISH":
        return "BUY"

    if trend == "BEARISH":
        return "SELL"

    return "WAIT"


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message, kind=None):

    if (
        TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
    ):

        url = (
            "https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }

        try:
            requests.post(
                url,
                json=payload,
                timeout=10
            )
        except Exception as exc:
            print(
                "Telegram error:",
                exc
            )

    recent_messages.appendleft({
        "time": now(),
        "message": message,
        "type": kind or "INFO",
    })


def signal_alert(signal):

    emoji = (
        "🟢"
        if signal["direction"] == "BUY"
        else "🔴"
    )

    message = (
        f"{emoji} <b>RULEBOOK A+ SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Asset: <b>{signal['symbol']}</b>\n"
        f"Direction: <b>{signal['direction']}</b>\n"
        f"Condition: <b>{signal['condition']}</b>\n"
        f"Level TF: <b>{signal['level_tf']}</b>\n"
        f"Confirmation: <b>{signal['confirmation_tf']}</b>\n"
        f"CHOCH: <b>{signal['choch']}</b>\n"
        f"Entry: <b>{price_format(signal['entry'])}</b>\n"
        f"SL: <b>{price_format(signal['sl'])}</b>\n"
        f"TP: <b>{price_format(signal['tp'])}</b>\n"
        f"RR: <b>{signal['rr']:.2f}</b>\n"
        f"Score: <b>{signal['score']}/100</b>\n"
        f"Risk: <b>{RISK_PER_TRADE * 100:.2f}%</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Reason: {', '.join(signal['reasons'])}\n"
        f"Time: {now()}"
    )

    telegram(
        message,
        "SIGNAL"
    )


def result_alert(
    trade,
    result,
    r_multiple
):

    emoji = (
        "🟢"
        if result == "WIN"
        else "🔴"
    )

    message = (
        f"{emoji} <b>TRADE RESULT: {result}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Asset: <b>{trade['symbol']}</b>\n"
        f"Direction: <b>{trade['direction']}</b>\n"
        f"Entry: <b>{price_format(trade['entry'])}</b>\n"
        f"Exit: <b>{price_format(trade['exit'])}</b>\n"
        f"R: <b>{r_multiple:.2f}R</b>\n"
        f"Grade: <b>{trade['grade']}</b>\n"
        f"Condition: <b>{trade['condition']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Time: {now()}"
    )

    telegram(
        message,
        result
    )


# ============================================================
# BINANCE DATA
# ============================================================

def get_klines(
    symbol,
    interval,
    limit=250
):

    params = {
        "symbol": normalize_symbol(symbol),
        "interval": interval,
        "limit": limit,
    }

    try:

        response = requests.get(
            BINANCE_KLINES_URL,
            params=params,
            timeout=10
        )

        response.raise_for_status()

        rows = response.json()

        candles = []

        for row in rows:

            candles.append({
                "open_time": int(row[0]),
                "open": f(row[1]),
                "high": f(row[2]),
                "low": f(row[3]),
                "close": f(row[4]),
                "volume": f(row[5]),
                "close_time": int(row[6]),
            })

        # Last Binance candle can still be forming.
        # Rulebook confirmation must use CLOSED candles.
        if len(candles) > 2:

            current_ms = int(
                time.time() * 1000
            )

            if (
                candles[-1]["close_time"]
                > current_ms
            ):
                candles.pop()

        return candles

    except Exception as exc:

        print(
            f"Kline error "
            f"{symbol} {interval}:",
            exc
        )

        return []


def get_price(symbol):

    try:

        response = requests.get(
            BINANCE_TICKER_URL,
            params={
                "symbol":
                    normalize_symbol(symbol)
            },
            timeout=10
        )

        response.raise_for_status()

        return f(
            response.json()["price"]
        )

    except Exception as exc:

        print(
            "Price error:",
            exc
        )

        return 0.0


# ============================================================
# CANDLE FUNCTIONS
# ============================================================

def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


def body(c):
    return abs(
        c["close"] - c["open"]
    )


def range_size(c):
    return c["high"] - c["low"]


def average_body(
    candles,
    length=20
):

    sample = candles[
        -length:
    ]

    if not sample:
        return 0

    return mean(
        body(c)
        for c in sample
    )


# ============================================================
# SWING DETECTION
# ============================================================

def find_swings(candles):

    highs = []
    lows = []

    if len(candles) < 7:
        return highs, lows

    for i in range(
        2,
        len(candles) - 2
    ):

        c = candles[i]

        if (
            c["high"]
            > candles[i - 1]["high"]
            and
            c["high"]
            > candles[i - 2]["high"]
            and
            c["high"]
            >= candles[i + 1]["high"]
            and
            c["high"]
            >= candles[i + 2]["high"]
        ):

            highs.append({
                "index": i,
                "price": c["high"],
                "time":
                    c["open_time"],
            })

        if (
            c["low"]
            < candles[i - 1]["low"]
            and
            c["low"]
            < candles[i - 2]["low"]
            and
            c["low"]
            <= candles[i + 1]["low"]
            and
            c["low"]
            <= candles[i + 2]["low"]
        ):

            lows.append({
                "index": i,
                "price": c["low"],
                "time":
                    c["open_time"],
            })

    return highs, lows


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure(candles):

    highs, lows = find_swings(
        candles
    )

    result = {
        "trend": "UNKNOWN",
        "highs": highs,
        "lows": lows,
        "last_high": None,
        "last_low": None,
    }

    if (
        len(highs) < 2
        or len(lows) < 2
    ):
        return result

    latest_high = highs[-1]["price"]
    previous_high = highs[-2]["price"]

    latest_low = lows[-1]["price"]
    previous_low = lows[-2]["price"]

    if (
        latest_high > previous_high
        and latest_low > previous_low
    ):

        result["trend"] = "BULLISH"

    elif (
        latest_high < previous_high
        and latest_low < previous_low
    ):

        result["trend"] = "BEARISH"

    else:

        result["trend"] = "SIDEWAYS"

    result["last_high"] = highs[-1]
    result["last_low"] = lows[-1]

    return result


# ============================================================
# CHOCH ENGINE
#
# EXACT RULE:
# Relevant structure level ke beyond
# 2 consecutive CLOSED candle closes.
#
# Fakeout:
# A single close beyond level is NOT CHOCH.
# Second close must remain beyond level.
#
# Additional rejection:
# If second candle closes back inside,
# no CHOCH.
# ============================================================

def detect_choch(candles):

    s = structure(candles)

    if len(candles) < 3:
        return None

    c1 = candles[-2]
    c2 = candles[-1]

    # Bullish structure -> bearish CHOCH
    if s["trend"] == "BULLISH":

        if not s["last_low"]:
            return None

        level = s["last_low"]["price"]

        first_break = (
            c1["close"] < level
        )

        second_break = (
            c2["close"] < level
        )

        if (
            first_break
            and second_break
        ):

            return {
                "direction": "BEARISH",
                "level": level,
                "time": c2["open_time"],
                "close1": c1["close"],
                "close2": c2["close"],
            }

    # Bearish structure -> bullish CHOCH
    if s["trend"] == "BEARISH":

        if not s["last_high"]:
            return None

        level = s["last_high"]["price"]

        first_break = (
            c1["close"] > level
        )

        second_break = (
            c2["close"] > level
        )

        if (
            first_break
            and second_break
        ):

            return {
                "direction": "BULLISH",
                "level": level,
                "time": c2["open_time"],
                "close1": c1["close"],
                "close2": c2["close"],
            }

    return None


# ============================================================
# TGL ENGINE
#
# IMPORTANT:
# Supplied Rulebook gives TGL Level 1/2 concept,
# but does NOT provide a complete mathematical formula.
#
# Therefore this is a transparent STRUCTURAL implementation,
# not a claimed exact proprietary TGL formula.
# ============================================================

def calculate_tgl(candles):

    s = structure(candles)

    highs = s["highs"]
    lows = s["lows"]

    levels = []

    if (
        len(highs) < 2
        or len(lows) < 2
    ):
        return levels

    if s["trend"] == "BULLISH":

        level1 = lows[-1]["price"]

        # Structural retracement reference.
        level2 = (
            highs[-2]["price"]
            + lows[-1]["price"]
        ) / 2

        levels.append({
            "name": "TGL-1",
            "price": level1,
            "side": "SUPPORT",
            "created": lows[-1]["time"],
        })

        levels.append({
            "name": "TGL-2",
            "price": level2,
            "side": "SUPPORT",
            "created": lows[-1]["time"],
        })

    elif s["trend"] == "BEARISH":

        level1 = highs[-1]["price"]

        level2 = (
            lows[-2]["price"]
            + highs[-1]["price"]
        ) / 2

        levels.append({
            "name": "TGL-1",
            "price": level1,
            "side": "RESISTANCE",
            "created": highs[-1]["time"],
        })

        levels.append({
            "name": "TGL-2",
            "price": level2,
            "side": "RESISTANCE",
            "created": highs[-1]["time"],
        })

    return levels


# ============================================================
# SUPPLY / DEMAND
# ============================================================

def detect_zones(candles):

    zones = []

    if len(candles) < 30:
        return zones

    for i in range(
        20,
        len(candles) - 1
    ):

        base = candles[i]
        impulse = candles[i + 1]

        avg = average_body(
            candles[
                max(0, i - 20):i
            ]
        )

        if avg <= 0:
            continue

        strong = (
            body(impulse)
            >= avg * 1.5
        )

        if (
            strong
            and bullish(impulse)
            and bearish(base)
        ):

            zones.append({
                "type": "DEMAND",
                "low": base["low"],
                "high": base["high"],
                "time": base["open_time"],
                "played": False,
            })

        if (
            strong
            and bearish(impulse)
            and bullish(base)
        ):

            zones.append({
                "type": "SUPPLY",
                "low": base["low"],
                "high": base["high"],
                "time": base["open_time"],
                "played": False,
            })

    return zones[-20:]


# ============================================================
# LEVEL TOUCH
# ============================================================

def touched(
    price,
    level,
    tolerance
):

    return (
        abs(price - level)
        <= tolerance
    )


def find_touched_level(
    price,
    levels
):

    if not levels:
        return None

    # Dynamic tolerance.
    tolerance = price * 0.001

    for level in reversed(levels):

        if touched(
            price,
            level["price"],
            tolerance
        ):

            return level

    return None


def find_touched_zone(
    price,
    zones
):

    for zone in reversed(zones):

        if (
            zone["low"]
            <= price
            <= zone["high"]
        ):

            return zone

    return None


# ============================================================
# LEVEL INVALIDATION
#
# Rulebook:
# Level ke opposite 2 consecutive closes
# => level invalid.
# ============================================================

def level_invalidated(
    candles,
    level,
    direction
):

    if len(candles) < 3:
        return False

    c1 = candles[-2]
    c2 = candles[-1]

    price = level["price"]

    if direction == "BUY":

        return (
            c1["close"] < price
            and
            c2["close"] < price
        )

    if direction == "SELL":

        return (
            c1["close"] > price
            and
            c2["close"] > price
        )

    return True


# ============================================================
# PLAYED LEVEL TRACKING
#
# Once a level produces a completed setup/trade,
# don't repeatedly trade same level.
# ============================================================

def level_key(
    symbol,
    level_tf,
    level
):

    return (
        f"{symbol}|"
        f"{level_tf}|"
        f"{level['name']}|"
        f"{level['created']}"
    )


def is_level_played(
    symbol,
    level_tf,
    level
):

    key = level_key(
        symbol,
        level_tf,
        level
    )

    return key in played_levels


def mark_level_played(
    symbol,
    level_tf,
    level
):

    key = level_key(
        symbol,
        level_tf,
        level
    )

    played_levels[key] = {
        "time": now(),
        "price": level["price"],
    }


# ============================================================
# MTF RULEBOOK
# ============================================================

def determine_condition(
    daily,
    h4,
    h1
):

    d = daily["trend"]
    h = h4["trend"]
    o = h1["trend"]

    # C1
    if (
        d in (
            "BULLISH",
            "BEARISH"
        )
        and
        d == h == o
    ):

        return "C1"

    # C2
    # Daily and 4H aligned,
    # 1H opposite.
    if (
        d in (
            "BULLISH",
            "BEARISH"
        )
        and
        h == d
        and
        o != d
    ):

        return "C2"

    # C3
    # 4H + 1H opposite Daily.
    if (
        d in (
            "BULLISH",
            "BEARISH"
        )
        and
        h != d
        and
        o != d
    ):

        return "C3"

    return "WAIT"


def condition_direction(
    condition,
    daily,
    h4,
    h1
):

    if condition == "C1":
        return h1["trend"]

    if condition == "C2":
        return h4["trend"]

    if condition == "C3":
        return daily["trend"]

    return "WAIT"


# ============================================================
# CONFIRMATION MAPPING
# ============================================================

def confirmation_mapping(
    condition
):

    if condition == "C1":

        return (
            "1H",
            "1M",
        )

    if condition == "C2":

        return (
            "4H",
            "5M",
        )

    if condition == "C3":

        # Daily level
        # 1H CHOCH
        # 1M final confirmation
        return (
            "DAILY",
            "1M",
        )

    return (
        None,
        None
    )


# ============================================================
# CONFIRMATION CANDLE
# ============================================================

def confirmation_candle(
    candles,
    direction
):

    if not candles:
        return False

    candle = candles[-1]

    if direction == "BUY":
        return bullish(candle)

    if direction == "SELL":
        return bearish(candle)

    return False


# ============================================================
# A+ SCORE
#
# Maximum 100
#
# MTF direction       20
# Relevant level      20
# Tap                  15
# CHOCH               20
# Confirmation        15
# RR >= minimum       10
# ============================================================

def score_setup(
    direction,
    condition,
    level_hit,
    zone_hit,
    choch,
    confirmation_ok,
    rr
):

    score = 0
    reasons = []

    if condition == "C1":
        score += 20
        reasons.append(
            "Daily-4H-1H aligned"
        )

    elif condition == "C2":
        score += 20
        reasons.append(
            "4H direction priority"
        )

    elif condition == "C3":
        score += 20
        reasons.append(
            "Daily direction priority"
        )

    if level_hit:
        score += 20
        reasons.append(
            "TGL level tap"
        )

    if zone_hit:
        score += 15
        reasons.append(
            "Supply/Demand"
        )

    if choch:
        score += 20
        reasons.append(
            "2-close CHOCH"
        )

    if confirmation_ok:
        score += 15
        reasons.append(
            "Confirmation candle"
        )

    if rr >= MIN_RR:
        score += 10
        reasons.append(
            "RR valid"
        )

    grade = (
        "A+"
        if score >= 85
        else
        "A"
        if score >= 75
        else
        "B"
        if score >= 60
        else
        "WAIT"
    )

    return (
        score,
        grade,
        reasons
    )


# ============================================================
# SL / TP ENGINE
# ============================================================

def calculate_sl_tp(
    direction,
    entry,
    level,
    structure_data,
    higher_candles
):

    highs, lows = find_swings(
        higher_candles
    )

    if direction == "BUY":

        candidates = [
            x["price"]
            for x in lows
            if x["price"] < entry
        ]

        if not candidates:
            return None

        structure_low = min(
            candidates[-3:]
        )

        # SL outside structure
        buffer = (
            abs(entry - structure_low)
            * 0.10
        )

        sl = (
            structure_low
            - buffer
        )

        targets = [
            x["price"]
            for x in highs
            if x["price"] > entry
        ]

        if not targets:
            return None

        tp = min(targets)

    else:

        candidates = [
            x["price"]
            for x in highs
            if x["price"] > entry
        ]

        if not candidates:
            return None

        structure_high = max(
            candidates[-3:]
        )

        buffer = (
            abs(structure_high - entry)
            * 0.10
        )

        sl = (
            structure_high
            + buffer
        )

        targets = [
            x["price"]
            for x in lows
            if x["price"] < entry
        ]

        if not targets:
            return None

        tp = max(targets)

    risk = abs(
        entry - sl
    )

    reward = abs(
        tp - entry
    )

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
# CREATE SIGNAL
# ============================================================

def create_signal(symbol):

    # --------------------------------------------------------
    # LOAD HTF
    # --------------------------------------------------------

    daily_candles = get_klines(
        symbol,
        TF["DAILY"],
        250
    )

    h4_candles = get_klines(
        symbol,
        TF["4H"],
        250
    )

    h1_candles = get_klines(
        symbol,
        TF["1H"],
        250
    )

    if not (
        daily_candles
        and h4_candles
        and h1_candles
    ):

        return None

    daily = structure(
        daily_candles
    )

    h4 = structure(
        h4_candles
    )

    h1 = structure(
        h1_candles
    )

    # --------------------------------------------------------
    # MTF CONDITION
    # --------------------------------------------------------

    condition = determine_condition(
        daily,
        h4,
        h1
    )

    if condition == "WAIT":

        return None

    direction = (
        condition_direction(
            condition,
            daily,
            h4,
            h1
        )
    )

    if direction not in (
        "BULLISH",
        "BEARISH"
    ):

        return None

    trade_direction = (
        direction_from_trend(
            direction
        )
    )

    # --------------------------------------------------------
    # SELECT LEVEL TF
    # --------------------------------------------------------

    level_tf, confirmation_tf = (
        confirmation_mapping(
            condition
        )
    )

    if condition == "C1":

        level_candles = h1_candles

    elif condition == "C2":

        level_candles = h4_candles

    else:

        level_candles = daily_candles

    # --------------------------------------------------------
    # TGL
    # --------------------------------------------------------

    tgl_levels = calculate_tgl(
        level_candles
    )

    if not tgl_levels:
        return None

    # --------------------------------------------------------
    # PRICE
    # --------------------------------------------------------

    price = get_price(
        symbol
    )

    if price <= 0:
        return None

    # --------------------------------------------------------
    # LEVEL INVALIDATION
    # --------------------------------------------------------

    valid_levels = []

    for level in tgl_levels:

        if not level_invalidated(
            level_candles,
            level,
            trade_direction
        ):

            valid_levels.append(
                level
            )

        else:

            bot_stats[
                "invalidated"
            ] += 1

    if not valid_levels:
        return None

    # --------------------------------------------------------
    # PRICE TAP
    # --------------------------------------------------------

    tapped_level = (
        find_touched_level(
            price,
            valid_levels
        )
    )

    zones = detect_zones(
        level_candles
    )

    tapped_zone = (
        find_touched_zone(
            price,
            zones
        )
    )

    if (
        not tapped_level
        and not tapped_zone
    ):

        bot_stats["waits"] += 1

        return None

    # --------------------------------------------------------
    # ALREADY PLAYED LEVEL
    # --------------------------------------------------------

    if tapped_level:

        if is_level_played(
            symbol,
            level_tf,
            tapped_level
        ):

            return None

    # --------------------------------------------------------
    # COUNTER TREND FILTER
    # --------------------------------------------------------

    if condition == "C1":

        allowed_trend = (
            h1["trend"]
        )

    elif condition == "C2":

        allowed_trend = (
            h4["trend"]
        )

    else:

        allowed_trend = (
            daily["trend"]
        )

    if (
        direction
        != allowed_trend
    ):

        return None

    # --------------------------------------------------------
    # CONFIRMATION DATA
    # --------------------------------------------------------

    confirmation_candles = (
        get_klines(
            symbol,
            TF[
                confirmation_tf
            ],
            200
        )
    )

    if not confirmation_candles:
        return None

    # --------------------------------------------------------
    # CHOCH
    # --------------------------------------------------------

    if condition == "C1":

        # 1H level -> 1M CHOCH
        choch = detect_choch(
            confirmation_candles
        )

        expected = direction

    elif condition == "C2":

        # 4H level -> 5M CHOCH
        choch = detect_choch(
            confirmation_candles
        )

        expected = direction

    else:

        # Daily:
        # Daily -> 1H CHOCH
        h1_choch = detect_choch(
            h1_candles
        )

        if not h1_choch:
            return None

        if (
            h1_choch["direction"]
            != direction
        ):
            return None

        # Then 1M final confirmation
        confirmation_candles = (
            get_klines(
                symbol,
                TF["1M"],
                200
            )
        )

        choch = h1_choch

        expected = direction

    if not choch:

        return None

    if (
        choch["direction"]
        != expected
    ):

        return None

    # --------------------------------------------------------
    # FINAL CONFIRMATION
    # --------------------------------------------------------

    confirmation_ok = (
        confirmation_candle(
            confirmation_candles,
            trade_direction
        )
    )

    if not confirmation_ok:

        return None

    # --------------------------------------------------------
    # ENTRY
    # --------------------------------------------------------

    entry = price

    level_structure = structure(
        level_candles
    )

    # --------------------------------------------------------
    # SL / TP
    # --------------------------------------------------------

    levels = calculate_sl_tp(
        trade_direction,
        entry,
        tapped_level,
        level_structure,
        level_candles
    )

    if not levels:

        return None

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    score, grade, reasons = (
        score_setup(
            trade_direction,
            condition,
            tapped_level is not None,
            tapped_zone is not None,
            choch is not None,
            confirmation_ok,
            levels["rr"],
        )
    )

    if A_PLUS_ONLY:

        if grade != "A+":

            return None

    else:

        if score < 75:

            return None

    # --------------------------------------------------------
    # MARK LEVEL PLAYED
    # --------------------------------------------------------

    if tapped_level:

        mark_level_played(
            symbol,
            level_tf,
            tapped_level
        )

    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    return {
        "symbol": symbol,
        "direction": trade_direction,
        "condition": condition,
        "level_tf": level_tf,
        "confirmation_tf": confirmation_tf,
        "choch": choch["direction"],
        "entry": levels["entry"],
        "sl": levels["sl"],
        "tp": levels["tp"],
        "rr": levels["rr"],
        "score": score,
        "grade": grade,
        "reasons": reasons,
        "created_at": now(),
    }


# ============================================================
# PAPER TRADE
# ============================================================

def open_trade(signal):

    symbol = signal["symbol"]

    if symbol in active_trades:
        return False

    trade = {
        "id": (
            f"{symbol}_"
            f"{int(time.time())}"
        ),
        "symbol": symbol,
        "direction":
            signal["direction"],
        "entry":
            signal["entry"],
        "sl":
            signal["sl"],
        "tp":
            signal["tp"],
        "rr":
            signal["rr"],
        "score":
            signal["score"],
        "grade":
            signal["grade"],
        "condition":
            signal["condition"],
        "level_tf":
            signal["level_tf"],
        "confirmation_tf":
            signal["confirmation_tf"],
        "opened_at": now(),
        "status": "OPEN",
    }

    active_trades[
        symbol
    ] = trade

    bot_stats[
        "signals"
    ] += 1

    signal_alert(
        signal
    )

    return True


# ============================================================
# PAPER TRADE MONITOR
# ============================================================

def monitor_trades():

    for symbol, trade in list(
        active_trades.items()
    ):

        price = get_price(
            symbol
        )

        if price <= 0:
            continue

        result = None
        exit_price = None
        r = 0.0

        if trade["direction"] == "BUY":

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

        if not result:
            continue

        trade["exit"] = (
            exit_price
        )

        trade["status"] = (
            result
        )

        trade["closed_at"] = (
            now()
        )

        trade["r_multiple"] = r

        trade_history.appendleft(
            trade.copy()
        )

        if result == "WIN":

            bot_stats[
                "wins"
            ] += 1

        else:

            bot_stats[
                "losses"
            ] += 1

        bot_stats[
            "total_r"
        ] += r

        result_alert(
            trade,
            result,
            r
        )

        del active_trades[
            symbol
        ]


# ============================================================
# STATISTICS
# ============================================================

def statistics():

    wins = bot_stats[
        "wins"
    ]

    losses = bot_stats[
        "losses"
    ]

    total = (
        wins + losses
    )

    win_rate = (
        wins / total * 100
        if total
        else 0
    )

    avg_r = (
        bot_stats["total_r"]
        / total
        if total
        else 0
    )

    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate":
            round(
                win_rate,
                2
            ),
        "total_r":
            round(
                bot_stats[
                    "total_r"
                ],
                2
            ),
        "average_r":
            round(
                avg_r,
                3
            ),
        "active_trades":
            len(
                active_trades
            ),
    }


# ============================================================
# MARKET MATRIX
# ============================================================

def update_market():

    for symbol in SYMBOLS:

        try:

            price = get_price(
                symbol
            )

            daily = get_klines(
                symbol,
                TF["DAILY"],
                100
            )

            h4 = get_klines(
                symbol,
                TF["4H"],
                100
            )

            h1 = get_klines(
                symbol,
                TF["1H"],
                100
            )

            if not (
                daily
                and h4
                and h1
            ):
                continue

            d = structure(
                daily
            )

            h = structure(
                h4
            )

            o = structure(
                h1
            )

            condition = (
                determine_condition(
                    d,
                    h,
                    o
                )
            )

            h1_choch = (
                detect_choch(
                    h1
                )
            )

            choch_text = (
                h1_choch[
                    "direction"
                ]
                if h1_choch
                else "-"
            )

            market_pairs[
                symbol
            ] = {

                "price":
                    price_format(
                        price
                    ),

                "daily":
                    d["trend"],

                "4h":
                    h["trend"],

                "1h":
                    o["trend"],

                "condition":
                    condition,

                "choch":
                    choch_text,
            }

        except Exception as exc:

            print(
                "Market update error:",
                symbol,
                exc
            )


# ============================================================
# SCANNER
# ============================================================

def scanner():

    print(
        "Rulebook SMC "
        "Paper Trading Bot started."
    )

    bot_stats[
        "status"
    ] = "ONLINE"

    telegram(
        "🤖 <b>RULEBOOK SMC BOT ONLINE</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Paper Trading Only\n"
        "Daily → 4H → 1H\n"
        "TGL → Tap → CHOCH\n"
        "Fakeout Filter\n"
        "A+ Confluence\n"
        "0.5% Risk Framework\n"
        "━━━━━━━━━━━━━━━━━━"
    )

    while True:

        try:

            bot_stats[
                "last_scan"
            ] = now()

            update_market()

            monitor_trades()

            for symbol in SYMBOLS:

                if symbol in active_trades:
                    continue

                try:

                    signal = (
                        create_signal(
                            symbol
                        )
                    )

                    if signal:

                        opened = (
                            open_trade(
                                signal
                            )
                        )

                        if opened:

                            print(
                                "OPEN:",
                                signal[
                                    "symbol"
                                ],
                                signal[
                                    "direction"
                                ],
                                signal[
                                    "entry"
                                ]
                            )

                    time.sleep(1)

                except Exception as exc:

                    print(
                        "Signal error:",
                        symbol,
                        exc
                    )

            time.sleep(
                SCAN_INTERVAL
            )

        except Exception as exc:

            print(
                "Scanner error:",
                exc
            )

            time.sleep(10)


# ============================================================
# FLASK
# ============================================================

app = Flask(
    __name__
)


HTML = """
<!DOCTYPE html>

<html>

<head>

<meta charset="UTF-8">

<meta name="viewport"
content="width=device-width,initial-scale=1">

<title>
Rulebook SMC Paper Trading
</title>

<style>

* {
    box-sizing:border-box;
}

body {
    margin:0;
    background:#0d1117;
    color:#c9d1d9;
    font-family:
    -apple-system,
    BlinkMacSystemFont,
    "Segoe UI",
    sans-serif;
}

.container {
    max-width:1400px;
    margin:auto;
    padding:20px;
}

.header {
    display:flex;
    justify-content:space-between;
    align-items:center;
    border-bottom:
    1px solid #30363d;
    padding-bottom:18px;
}

.online {
    color:#3fb950;
    font-weight:bold;
}

.grid {
    display:grid;
    grid-template-columns:
    repeat(auto-fit,minmax(160px,1fr));
    gap:15px;
    margin:25px 0;
}

.card {
    background:#161b22;
    border:1px solid #30363d;
    border-radius:12px;
    padding:18px;
}

.card h3 {
    margin:0 0 10px;
    color:#8b949e;
    font-size:12px;
    text-transform:uppercase;
}

.value {
    font-size:26px;
    font-weight:bold;
}

.win {
    color:#3fb950;
}

.loss {
    color:#f85149;
}

.wait {
    color:#e3b341;
}

.blue {
    color:#58a6ff;
}

table {
    width:100%;
    border-collapse:collapse;
    background:#161b22;
    border:1px solid #30363d;
    border-radius:10px;
    overflow:hidden;
}

th,td {
    padding:11px;
    border-bottom:
    1px solid #30363d;
    text-align:left;
}

th {
    background:#21262d;
    color:#8b949e;
    font-size:12px;
}

.bull {
    color:#3fb950;
    font-weight:bold;
}

.bear {
    color:#f85149;
    font-weight:bold;
}

.section {
    margin-top:30px;
}

.log {
    background:#161b22;
    border:1px solid #30363d;
    border-radius:10px;
    padding:15px;
    max-height:400px;
    overflow:auto;
}

.log-item {
    padding:10px;
    border-bottom:
    1px solid #21262d;
    font-family:monospace;
    font-size:12px;
}

.small {
    color:#8b949e;
    font-size:12px;
}

.badge {
    padding:4px 8px;
    border-radius:5px;
    background:#21262d;
}

</style>

</head>

<body>

<div class="container">

<div class="header">

<div>

<h1>
⚡ Rulebook SMC
</h1>

<div class="small">
Paper Trading • MTF • TGL • CHOCH
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
<div class="value blue">
{{ performance.total_r }}R
</div>
</div>

<div class="card">
<h3>Average R</h3>
<div class="value blue">
{{ performance.average_r }}R
</div>
</div>

<div class="card">
<h3>Active</h3>
<div class="value">
{{ performance.active_trades }}
</div>
</div>

<div class="card">
<h3>Risk / Trade</h3>
<div class="value">
{{ risk }}%
</div>
</div>

</div>


<div class="section">

<h2>
📊 MTF Market Matrix
</h2>

<table>

<tr>
<th>Symbol</th>
<th>Price</th>
<th>Daily</th>
<th>4H</th>
<th>1H</th>
<th>Condition</th>
<th>1H CHOCH</th>
</tr>

{% for symbol,data in pairs.items() %}

<tr>

<td>
<strong>{{ symbol }}</strong>
</td>

<td>
{{ data.price }}
</td>

<td class="
{{ 'bull'
if data.daily == 'BULLISH'
else 'bear'
if data.daily == 'BEARISH'
else 'wait' }}
">
{{ data.daily }}
</td>

<td class="
{{ 'bull'
if data['4h'] == 'BULLISH'
else 'bear'
if data['4h'] == 'BEARISH'
else 'wait' }}
">
{{ data['4h'] }}
</td>

<td class="
{{ 'bull'
if data['1h'] == 'BULLISH'
else 'bear'
if data['1h'] == 'BEARISH'
else 'wait' }}
">
{{ data['1h'] }}
</td>

<td>
<span class="badge">
{{ data.condition }}
</span>
</td>

<td>
{{ data.choch }}
</td>

</tr>

{% endfor %}

</table>

</div>


<div class="section">

<h2>
📌 Active Paper Trades
</h2>

<table>

<tr>
<th>Symbol</th>
<th>Direction</th>
<th>Entry</th>
<th>SL</th>
<th>TP</th>
<th>RR</th>
<th>Grade</th>
<th>Condition</th>
</tr>

{% for symbol,trade in active.items() %}

<tr>

<td>{{ symbol }}</td>

<td class="
{{ 'bull'
if trade.direction == 'BUY'
else 'bear' }}">
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

<td>
{{ trade.condition }}
</td>

</tr>

{% else %}

<tr>
<td colspan="8">
No active paper trades.
</td>
</tr>

{% endfor %}

</table>

</div>


<div class="section">

<h2>
📈 Completed Trades
</h2>

<table>

<tr>
<th>Symbol</th>
<th>Direction</th>
<th>Result</th>
<th>R</th>
<th>Grade</th>
<th>Condition</th>
<th>Opened</th>
<th>Closed</th>
</tr>

{% for trade in history %}

<tr>

<td>{{ trade.symbol }}</td>

<td>
{{ trade.direction }}
</td>

<td class="
{{ 'win'
if trade.status == 'WIN'
else 'loss' }}">
{{ trade.status }}
</td>

<td>
{{ "%.2f"|format(trade.r_multiple) }}R
</td>

<td>
{{ trade.grade }}
</td>

<td>
{{ trade.condition }}
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
<td colspan="8">
No completed trades yet.
</td>
</tr>

{% endfor %}

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
{{ msg.time }}
</div>

<div>
{{ msg.message | safe }}
</div>

</div>

{% else %}

No Telegram messages.

{% endfor %}

</div>

</div>


<div class="section small">

Last scan:
{{ stats.last_scan }}

<br><br>

Paper Trading Only

<br><br>

<a href="/api/stats">
API /api/stats
</a>

</div>

</div>


<script>

setTimeout(
    function() {
        location.reload();
    },
    15000
);

</script>

</body>

</html>
"""


# ============================================================
# ROUTES
# ============================================================

@app.route("/")
def dashboard():

    return render_template_string(
        HTML,
        stats=bot_stats,
        performance=statistics(),
        pairs=market_pairs,
        active=active_trades,
        history=list(
            trade_history
        ),
        messages=list(
            recent_messages
        ),
        risk=(
            RISK_PER_TRADE
            * 100
        ),
    )


@app.route(
    "/api/stats"
)
def api_stats():

    return jsonify({

        "bot": bot_stats,

        "performance":
            statistics(),

        "active_trades":
            list(
                active_trades.values()
            ),

        "trade_history":
            list(
                trade_history
            ),

        "played_levels":
            played_levels,

        "market":
            market_pairs,

    })


@app.route(
    "/api/signal/<symbol>"
)
def api_signal(symbol):

    symbol = normalize_symbol(
        symbol
    )

    signal = create_signal(
        symbol
    )

    if not signal:

        return jsonify({

            "status": "WAIT",

            "symbol": symbol,

            "message":
                "No valid Rulebook setup.",

        })

    return jsonify({

        "status":
            "SIGNAL",

        "signal":
            signal,

    })


@app.route(
    "/api/health"
)
def health():

    return jsonify({

        "status":
            bot_stats[
                "status"
            ],

        "last_scan":
            bot_stats[
                "last_scan"
            ],

        "active_trades":
            len(
                active_trades
            ),

    })


# ============================================================
# START
# ============================================================

def start_bot():

    thread = threading.Thread(
        target=scanner,
        daemon=True
    )

    thread.start()


if __name__ == "__main__":

    start_bot()

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    print(
        f"""
========================================
 RULEBOOK SMC PAPER TRADING BOT
========================================

 Dashboard:
 http://localhost:{port}

 API:
 http://localhost:{port}/api/stats

 Mode:
 PAPER TRADING ONLY

 Risk:
 {RISK_PER_TRADE * 100:.2f}% per trade

 A+ ONLY:
 {A_PLUS_ONLY}

========================================
"""
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
