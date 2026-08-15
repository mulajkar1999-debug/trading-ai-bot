import os
import time
import sqlite3
import threading
from datetime import datetime, timezone

import requests
import ccxt
from flask import Flask, jsonify, render_template_string


# ============================================================
# CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM", "").strip()
TELEGRAM_CHAT_ID = os.getenv("1317739622", "").strip()

DB_FILE = os.getenv("DB_FILE", "trading_bot.db")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))

# Telegram health message every 3 hours
HEALTH_CHECK_INTERVAL = 3 * 60 * 60

API_TIMEOUT = 10000

# ============================================================
# MODE
# ============================================================

TRADING_MODE = "PAPER / SIGNAL ONLY"

# IMPORTANT:
# This bot NEVER creates real Bybit orders.
REAL_ORDERS_ENABLED = False


# ============================================================
# BYBIT SYMBOLS
# ============================================================

REQUESTED_SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "BNB/USDT",
    "DOGE/USDT",
    "ADA/USDT",
    "AVAX/USDT",
]

SYMBOLS = []


# ============================================================
# TIMEFRAMES
# ============================================================

TF_DAILY = "1d"
TF_4H = "4h"
TF_1H = "1h"
TF_15M = "15m"
TF_5M = "5m"
TF_1M = "1m"

CANDLE_LIMIT = 150


# ============================================================
# STRATEGY SETTINGS
# ============================================================

MIN_RR = 1.50
MAX_SL_PERCENT = 1.50
MIN_SETUP_SCORE = 80

LEVEL_COOLDOWN_MINUTES = 60

MAX_ACTIVE_TRADES = 3


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# BYBIT
# ============================================================

exchange = ccxt.bybit({
    "enableRateLimit": True,
    "timeout": API_TIMEOUT,
    "options": {
        "defaultType": "spot",
    },
})


# ============================================================
# GLOBAL STATE
# ============================================================

active_trades = []
trade_logs = []

state_lock = threading.Lock()

last_signal_key = {}

bot_started_at = time.time()

last_scanner_run = None
last_bybit_check = None
last_telegram_check = None

bybit_status = "NOT_CHECKED"
telegram_status = "NOT_CHECKED"
scanner_status = "STARTING"

market_load_error = None


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    return sqlite3.connect(
        DB_FILE,
        check_same_thread=False,
        timeout=30
    )


def init_db():

    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            direction TEXT,
            entry REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            result TEXT,
            exit_price REAL,
            r_multiple REAL,
            score INTEGER,
            reason TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            event TEXT
        )
    """)

    conn.commit()
    conn.close()


def db_log_event(event):

    try:

        conn = db_connect()

        conn.execute(
            """
            INSERT INTO events(timestamp,event)
            VALUES (?,?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                event
            )
        )

        conn.commit()
        conn.close()

    except Exception as e:

        print("DB event error:", e)


def db_save_trade(
    trade,
    result,
    exit_price,
    r_multiple
):

    try:

        conn = db_connect()

        conn.execute(
            """
            INSERT INTO trades(
                timestamp,
                symbol,
                direction,
                entry,
                sl,
                tp1,
                tp2,
                result,
                exit_price,
                r_multiple,
                score,
                reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                trade["symbol"],
                trade["type"],
                trade["entry"],
                trade["initial_sl"],
                trade["tp1"],
                trade["tp2"],
                result,
                exit_price,
                r_multiple,
                trade.get("score", 0),
                trade.get("reason", "")
            )
        )

        conn.commit()
        conn.close()

    except Exception as e:

        print("DB trade error:", e)


# ============================================================
# LOGGING
# ============================================================

def log_event(message):

    print(message)

    with state_lock:

        trade_logs.append(
            f"{datetime.now().strftime('%H:%M:%S')} | {message}"
        )

        if len(trade_logs) > 500:
            del trade_logs[:-500]

    db_log_event(message)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    global telegram_status
    global last_telegram_check

    if not TELEGRAM_BOT_TOKEN:

        telegram_status = "NOT_CONFIGURED"

        print(
            "[Telegram] TELEGRAM_BOT_TOKEN is missing."
        )

        return False

    if not TELEGRAM_CHAT_ID:

        telegram_status = "NOT_CONFIGURED"

        print(
            "[Telegram] TELEGRAM_CHAT_ID is missing."
        )

        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=10
        )

        if response.status_code == 200:

            data = response.json()

            if data.get("ok"):

                telegram_status = "OK"
                last_telegram_check = time.time()

                return True

        telegram_status = "ERROR"

        print(
            "[Telegram] API error:",
            response.status_code,
            response.text
        )

        return False

    except Exception as e:

        telegram_status = "ERROR"

        print(
            "[Telegram] Exception:",
            e
        )

        return False


# ============================================================
# TELEGRAM API CHECK
# ============================================================

def check_telegram_without_message():

    global telegram_status
    global last_telegram_check

    if not TELEGRAM_BOT_TOKEN:

        telegram_status = "NOT_CONFIGURED"
        return False

    if not TELEGRAM_CHAT_ID:

        telegram_status = "NOT_CONFIGURED"
        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/getMe"
    )

    try:

        response = requests.get(
            url,
            timeout=10
        )

        if response.status_code != 200:

            telegram_status = "ERROR"

            print(
                "[Telegram] getMe error:",
                response.status_code,
                response.text
            )

            return False

        data = response.json()

        if data.get("ok"):

            telegram_status = "OK"
            last_telegram_check = time.time()

            return True

        telegram_status = "ERROR"

        return False

    except Exception as e:

        telegram_status = "ERROR"

        print(
            "[Telegram] health error:",
            e
        )

        return False


# ============================================================
# TELEGRAM STARTUP TEST
# ============================================================

def telegram_connection_test():

    print(
        "Testing Telegram connection..."
    )

    if not TELEGRAM_BOT_TOKEN:

        print(
            "❌ TELEGRAM_BOT_TOKEN is missing."
        )

        telegram_status = "NOT_CONFIGURED"

        return False

    if not TELEGRAM_CHAT_ID:

        print(
            "❌ TELEGRAM_CHAT_ID is missing."
        )

        telegram_status = "NOT_CONFIGURED"

        return False

    if not check_telegram_without_message():

        print(
            "❌ Telegram API connection failed."
        )

        return False

    message = (
        "<b>🟢 TELEGRAM CONNECTION OK</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Telegram Bot: ✅ Connected\n"
        "Backend: ✅ Starting\n"
        "Bybit: ⏳ Checking...\n"
        "Mode: 🟡 PAPER / SIGNAL ONLY\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Rulebook scanner is starting."
    )

    success = send_telegram(message)

    if success:

        print(
            "✅ Telegram connection test successful."
        )

    else:

        print(
            "❌ Telegram message test failed."
        )

    return success


# ============================================================
# BYBIT MARKET LOADING
# ============================================================

def load_bybit_markets():

    global SYMBOLS
    global bybit_status
    global last_bybit_check
    global market_load_error

    max_attempts = 3

    for attempt in range(
        1,
        max_attempts + 1
    ):

        try:

            print(
                f"Loading Bybit markets... "
                f"Attempt {attempt}/{max_attempts}"
            )

            exchange.load_markets(
                reload=True
            )

            available = []

            for symbol in REQUESTED_SYMBOLS:

                if symbol not in exchange.markets:

                    print(
                        f"[SKIP] {symbol} "
                        "not found on Bybit."
                    )

                    continue

                market = exchange.markets[symbol]

                if not market.get("spot"):

                    print(
                        f"[SKIP] {symbol} "
                        "is not a spot market."
                    )

                    continue

                if market.get("quote") != "USDT":

                    print(
                        f"[SKIP] {symbol} "
                        "is not USDT."
                    )

                    continue

                available.append(symbol)

            SYMBOLS = available

            bybit_status = "OK"
            last_bybit_check = time.time()
            market_load_error = None

            print(
                "=========================================="
            )

            print(
                "✅ BYBIT MARKETS LOADED"
            )

            print(
                f"Available symbols: "
                f"{', '.join(SYMBOLS)}"
            )

            print(
                "=========================================="
            )

            return True

        except Exception as e:

            market_load_error = str(e)
            bybit_status = "ERROR"

            print(
                f"❌ Bybit market load failed "
                f"(attempt {attempt}): {e}"
            )

            if attempt < max_attempts:

                time.sleep(
                    attempt * 5
                )

    return False


# ============================================================
# BYBIT HEALTH CHECK
# ============================================================

def check_bybit_connection():

    global bybit_status
    global last_bybit_check

    try:

        exchange.fetch_time()

        bybit_status = "OK"
        last_bybit_check = time.time()

        return True

    except Exception as e:

        bybit_status = "ERROR"

        print(
            "❌ Bybit health check failed:",
            e
        )

        return False


# ============================================================
# CANDLE DATA
# ============================================================

def fetch_candles(
    symbol,
    timeframe,
    limit=CANDLE_LIMIT
):

    try:

        candles = exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit
        )

        if not candles:

            return []

        return candles

    except Exception as e:

        print(
            f"Candle error | "
            f"{symbol} | "
            f"{timeframe} | {e}"
        )

        return []


def fetch_price(symbol):

    try:

        ticker = exchange.fetch_ticker(
            symbol
        )

        last = ticker.get("last")

        if last is None:
            return None

        return float(last)

    except Exception as e:

        print(
            f"Price error | "
            f"{symbol}: {e}"
        )

        return None


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_open(c):
    return float(c[1])


def candle_high(c):
    return float(c[2])


def candle_low(c):
    return float(c[3])


def candle_close(c):
    return float(c[4])


def candle_body(c):

    return abs(
        candle_close(c)
        - candle_open(c)
    )


def candle_range(c):

    return (
        candle_high(c)
        - candle_low(c)
    )


def is_bullish(c):

    return (
        candle_close(c)
        > candle_open(c)
    )


def is_bearish(c):

    return (
        candle_close(c)
        < candle_open(c)
    )


def body_ratio(c):

    r = candle_range(c)

    if r <= 0:
        return 0

    return candle_body(c) / r


# ============================================================
# SWINGS
# ============================================================

def find_swing_highs(
    candles,
    left=2,
    right=2
):

    highs = []

    if len(candles) < left + right + 1:
        return highs

    for i in range(
        left,
        len(candles) - right
    ):

        h = candle_high(candles[i])

        left_highs = [
            candle_high(candles[j])
            for j in range(
                i - left,
                i
            )
        ]

        right_highs = [
            candle_high(candles[j])
            for j in range(
                i + 1,
                i + right + 1
            )
        ]

        if (
            h > max(left_highs)
            and h >= max(right_highs)
        ):

            highs.append(
                (i, h)
            )

    return highs


def find_swing_lows(
    candles,
    left=2,
    right=2
):

    lows = []

    if len(candles) < left + right + 1:
        return lows

    for i in range(
        left,
        len(candles) - right
    ):

        l = candle_low(candles[i])

        left_lows = [
            candle_low(candles[j])
            for j in range(
                i - left,
                i
            )
        ]

        right_lows = [
            candle_low(candles[j])
            for j in range(
                i + 1,
                i + right + 1
            )
        ]

        if (
            l < min(left_lows)
            and l <= min(right_lows)
        ):

            lows.append(
                (i, l)
            )

    return lows


# ============================================================
# MARKET STRUCTURE
# ============================================================

def detect_structure(candles):

    if len(candles) < 30:

        return {
            "trend": "NEUTRAL",
            "last_high": None,
            "last_low": None,
            "prev_high": None,
            "prev_low": None
        }

    closed = candles[:-1]

    highs = find_swing_highs(closed)
    lows = find_swing_lows(closed)

    if len(highs) < 2 or len(lows) < 2:

        return {
            "trend": "NEUTRAL",
            "last_high": None,
            "last_low": None,
            "prev_high": None,
            "prev_low": None
        }

    prev_high = highs[-2][1]
    last_high = highs[-1][1]

    prev_low = lows[-2][1]
    last_low = lows[-1][1]

    if (
        last_high > prev_high
        and last_low > prev_low
    ):

        trend = "BULLISH"

    elif (
        last_high < prev_high
        and last_low < prev_low
    ):

        trend = "BEARISH"

    else:

        trend = "NEUTRAL"

    return {
        "trend": trend,
        "last_high": last_high,
        "last_low": last_low,
        "prev_high": prev_high,
        "prev_low": prev_low
    }


def get_structure(
    symbol,
    timeframe
):

    candles = fetch_candles(
        symbol,
        timeframe
    )

    if not candles:
        return None, []

    return (
        detect_structure(candles),
        candles
    )


# ============================================================
# MTF BIAS
# ============================================================

def get_mtf_bias(symbol):

    daily, _ = get_structure(
        symbol,
        TF_DAILY
    )

    h4, _ = get_structure(
        symbol,
        TF_4H
    )

    h1, _ = get_structure(
        symbol,
        TF_1H
    )

    m15, _ = get_structure(
        symbol,
        TF_15M
    )

    if not daily or not h4 or not h1:

        return {
            "direction": "NEUTRAL",
            "daily": "NEUTRAL",
            "h4": "NEUTRAL",
            "h1": "NEUTRAL",
            "m15": "NEUTRAL"
        }

    trends = [
        daily["trend"],
        h4["trend"],
        h1["trend"]
    ]

    bullish = trends.count("BULLISH")
    bearish = trends.count("BEARISH")

    if bullish >= 2 and bullish > bearish:

        direction = "BULLISH"

    elif bearish >= 2 and bearish > bullish:

        direction = "BEARISH"

    else:

        direction = "NEUTRAL"

    return {
        "direction": direction,
        "daily": daily["trend"],
        "h4": h4["trend"],
        "h1": h1["trend"],
        "m15": (
            m15["trend"]
            if m15
            else "NEUTRAL"
        )
    }


# ============================================================
# CHOCH
# ============================================================

def detect_choch(
    candles,
    direction
):

    if len(candles) < 25:
        return False

    closed = candles[:-1]

    highs = find_swing_highs(closed)
    lows = find_swing_lows(closed)

    if direction == "BUY":

        if not highs:
            return False

        previous_high = highs[-1][1]

        return (
            candle_close(closed[-1])
            > previous_high
        )

    if direction == "SELL":

        if not lows:
            return False

        previous_low = lows[-1][1]

        return (
            candle_close(closed[-1])
            < previous_low
        )

    return False


# ============================================================
# CONFIRMATION
# ============================================================

def confirmation_candle(
    candle,
    direction
):

    if direction == "BUY":

        return (
            is_bullish(candle)
            and body_ratio(candle) >= 0.45
        )

    if direction == "SELL":

        return (
            is_bearish(candle)
            and body_ratio(candle) >= 0.45
        )

    return False


def bullish_engulfing(
    previous,
    current
):

    return (
        is_bearish(previous)
        and is_bullish(current)
        and candle_open(current)
        <= candle_close(previous)
        and candle_close(current)
        >= candle_open(previous)
    )


def bearish_engulfing(
    previous,
    current
):

    return (
        is_bullish(previous)
        and is_bearish(current)
        and candle_open(current)
        >= candle_close(previous)
        and candle_close(current)
        <= candle_open(previous)
    )


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(
    candles,
    direction
):

    if len(candles) < 15:
        return False

    closed = candles[:-1]

    previous = closed[-8:-1]

    if not previous:
        return False

    previous_high = max(
        candle_high(c)
        for c in previous
    )

    previous_low = min(
        candle_low(c)
        for c in previous
    )

    last = closed[-1]

    if direction == "BUY":

        return (
            candle_low(last)
            < previous_low
            and candle_close(last)
            > previous_low
        )

    if direction == "SELL":

        return (
            candle_high(last)
            > previous_high
            and candle_close(last)
            < previous_high
        )

    return False


# ============================================================
# FVG
# ============================================================

def detect_fvg(
    candles,
    direction
):

    if len(candles) < 5:
        return False

    closed = candles[:-1]

    a = closed[-3]
    c = closed[-1]

    if direction == "BUY":

        return (
            candle_low(c)
            > candle_high(a)
        )

    if direction == "SELL":

        return (
            candle_high(c)
            < candle_low(a)
        )

    return False


# ============================================================
# DOUBLE TOP / BOTTOM
# ============================================================

def detect_double_bottom(candles):

    lows = find_swing_lows(candles)

    if len(lows) < 2:
        return False

    l1 = lows[-2][1]
    l2 = lows[-1][1]

    tolerance = abs(l1) * 0.002

    return abs(l1 - l2) <= tolerance


def detect_double_top(candles):

    highs = find_swing_highs(candles)

    if len(highs) < 2:
        return False

    h1 = highs[-2][1]
    h2 = highs[-1][1]

    tolerance = abs(h1) * 0.002

    return abs(h1 - h2) <= tolerance


# ============================================================
# DEMAND / SUPPLY
# ============================================================

def detect_demand_zone(candles):

    if len(candles) < 15:
        return None

    closed = candles[:-1]

    start = max(
        2,
        len(closed) - 20
    )

    for i in range(
        len(closed) - 3,
        start - 1,
        -1
    ):

        base = closed[i]
        next_candle = closed[i + 1]

        if (
            is_bearish(base)
            and is_bullish(next_candle)
            and candle_body(next_candle)
            > candle_body(base)
        ):

            return {
                "low": candle_low(base),
                "high": candle_high(base),
                "index": i
            }

    return None


def detect_supply_zone(candles):

    if len(candles) < 15:
        return None

    closed = candles[:-1]

    start = max(
        2,
        len(closed) - 20
    )

    for i in range(
        len(closed) - 3,
        start - 1,
        -1
    ):

        base = closed[i]
        next_candle = closed[i + 1]

        if (
            is_bullish(base)
            and is_bearish(next_candle)
            and candle_body(next_candle)
            > candle_body(base)
        ):

            return {
                "low": candle_low(base),
                "high": candle_high(base),
                "index": i
            }

    return None


# ============================================================
# TGL
# ============================================================

def detect_tgl(
    candles,
    direction
):

    if len(candles) < 20:
        return None

    closed = candles[:-1]

    start = max(
        3,
        len(closed) - 20
    )

    for i in range(
        len(closed) - 3,
        start - 1,
        -1
    ):

        current = closed[i]
        following = closed[i + 1]

        if direction == "BUY":

            if (
                is_bearish(current)
                and is_bullish(following)
                and body_ratio(following) >= 0.55
            ):

                return {
                    "low": candle_low(current),
                    "high": candle_high(current),
                    "index": i
                }

        elif direction == "SELL":

            if (
                is_bullish(current)
                and is_bearish(following)
                and body_ratio(following) >= 0.55
            ):

                return {
                    "low": candle_low(current),
                    "high": candle_high(current),
                    "index": i
                }

    return None


# ============================================================
# LEVEL VALIDATION
# ============================================================

def level_invalidated(
    candles,
    zone,
    direction
):

    if not zone or len(candles) < 4:
        return False

    closed = candles[:-1]

    c1 = closed[-1]
    c2 = closed[-2]

    if direction == "BUY":

        return (
            candle_close(c1) < zone["low"]
            and candle_close(c2) < zone["low"]
        )

    if direction == "SELL":

        return (
            candle_close(c1) > zone["high"]
            and candle_close(c2) > zone["high"]
        )

    return False


# ============================================================
# SESSION FILTER
# ============================================================

def session_filter():

    hour = datetime.now(
        timezone.utc
    ).hour

    if 0 <= hour < 4:
        return False

    return True


# ============================================================
# SETUP ENGINE
# ============================================================

def calculate_setup(
    symbol,
    direction,
    price
):

    if not session_filter():
        return None

    bias = get_mtf_bias(symbol)

    if (
        direction == "BUY"
        and bias["direction"] != "BULLISH"
    ):
        return None

    if (
        direction == "SELL"
        and bias["direction"] != "BEARISH"
    ):
        return None

    h1 = fetch_candles(
        symbol,
        TF_1H
    )

    m15 = fetch_candles(
        symbol,
        TF_15M
    )

    m5 = fetch_candles(
        symbol,
        TF_5M
    )

    m1 = fetch_candles(
        symbol,
        TF_1M
    )

    if (
        not h1
        or not m15
        or not m5
        or not m1
    ):
        return None

    if direction == "BUY":

        demand = detect_demand_zone(h1)
        tgl = detect_tgl(h1, "BUY")

        level = demand or tgl

    else:

        supply = detect_supply_zone(h1)
        tgl = detect_tgl(h1, "SELL")

        level = supply or tgl

    if not level:
        return None

    if level_invalidated(
        h1,
        level,
        direction
    ):
        return None

    zone_size = (
        level["high"]
        - level["low"]
    )

    tolerance = max(
        zone_size * 0.25,
        price * 0.0005
    )

    if not (
        level["low"] - tolerance
        <= price
        <= level["high"] + tolerance
    ):
        return None

    if not detect_choch(
        m1,
        direction
    ):
        return None

    last_closed = m1[-2]

    confirmed = confirmation_candle(
        last_closed,
        direction
    )

    engulfing = False

    if len(m1) >= 3:

        previous = m1[-3]

        if direction == "BUY":

            engulfing = bullish_engulfing(
                previous,
                last_closed
            )

        else:

            engulfing = bearish_engulfing(
                previous,
                last_closed
            )

    if not confirmed and not engulfing:
        return None

    # ========================================================
    # SCORE
    # ========================================================

    score = 0
    reasons = []

    score += 30
    reasons.append("MTF_STRUCTURE")

    score += 25
    reasons.append("VALID_LEVEL")

    score += 25
    reasons.append("1M_CHOCH")

    score += 10
    reasons.append("CANDLE_CONFIRMATION")

    if detect_fvg(
        m15,
        direction
    ):

        score += 5
        reasons.append("FVG")

    if detect_liquidity_sweep(
        m15,
        direction
    ):

        score += 10
        reasons.append("LIQUIDITY_SWEEP")

    if direction == "BUY":

        if detect_double_bottom(m15):

            score += 5
            reasons.append("DOUBLE_BOTTOM")

    else:

        if detect_double_top(m15):

            score += 5
            reasons.append("DOUBLE_TOP")

    if score < MIN_SETUP_SCORE:
        return None

    # ========================================================
    # STRUCTURAL SL
    # ========================================================

    if direction == "BUY":

        swing_lows = find_swing_lows(m1)

        if swing_lows:

            swing_low = swing_lows[-1][1]

        else:

            swing_low = min(
                candle_low(c)
                for c in m1[-10:-1]
            )

        sl = swing_low * 0.999

        if sl >= price:
            return None

        risk = price - sl

    else:

        swing_highs = find_swing_highs(m1)

        if swing_highs:

            swing_high = swing_highs[-1][1]

        else:

            swing_high = max(
                candle_high(c)
                for c in m1[-10:-1]
            )

        sl = swing_high * 1.001

        if sl <= price:
            return None

        risk = sl - price

    if risk <= 0:
        return None

    sl_percent = (
        risk / price
    ) * 100

    if sl_percent > MAX_SL_PERCENT:
        return None

    # ========================================================
    # TAKE PROFITS
    # ========================================================

    if direction == "BUY":

        m15_highs = find_swing_highs(m15)

        resistance = [
            h[1]
            for h in m15_highs
            if h[1] > price
        ]

        tp1 = price + risk * 1.0

        if resistance:

            tp2 = min(resistance)

            if (
                (tp2 - price) / risk
                < MIN_RR
            ):

                tp2 = price + risk * MIN_RR

        else:

            tp2 = price + risk * 2.0

    else:

        m15_lows = find_swing_lows(m15)

        support = [
            l[1]
            for l in m15_lows
            if l[1] < price
        ]

        tp1 = price - risk * 1.0

        if support:

            tp2 = max(support)

            if (
                (price - tp2) / risk
                < MIN_RR
            ):

                tp2 = price - risk * MIN_RR

        else:

            tp2 = price - risk * 2.0

    if direction == "BUY":

        rr = (
            tp2 - price
        ) / risk

    else:

        rr = (
            price - tp2
        ) / risk

    if rr < MIN_RR:
        return None

    return {
        "direction": direction,
        "entry": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
        "score": score,
        "reason": ", ".join(reasons),
        "bias": bias,
        "level": level
    }


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def signal_allowed(
    symbol,
    setup
):

    key = (
        symbol,
        setup["direction"],
        round(
            setup["level"]["low"],
            6
        ),
        round(
            setup["level"]["high"],
            6
        )
    )

    now = time.time()

    previous = last_signal_key.get(key)

    if previous:

        if (
            now - previous
            < LEVEL_COOLDOWN_MINUTES * 60
        ):

            return False

    last_signal_key[key] = now

    return True


# ============================================================
# ACTIVE TRADE
# ============================================================

def has_active_trade(symbol):

    with state_lock:

        return any(
            t["symbol"] == symbol
            for t in active_trades
        )


# ============================================================
# CREATE PAPER TRADE
# ============================================================

def create_trade(
    symbol,
    setup
):

    if has_active_trade(symbol):
        return

    with state_lock:

        if len(active_trades) >= MAX_ACTIVE_TRADES:
            return

    if not signal_allowed(
        symbol,
        setup
    ):
        return

    trade = {

        "id":
            f"{symbol}-{int(time.time())}",

        "symbol":
            symbol,

        "type":
            setup["direction"],

        "entry":
            setup["entry"],

        "sl":
            setup["sl"],

        "initial_sl":
            setup["sl"],

        "tp1":
            setup["tp1"],

        "tp2":
            setup["tp2"],

        "rr":
            setup["rr"],

        "score":
            setup["score"],

        "reason":
            setup["reason"],

        "tp1_hit":
            False,

        "created_at":
            datetime.now(
                timezone.utc
            ).isoformat()
    }

    with state_lock:

        active_trades.append(trade)

    icon = (
        "🟢"
        if trade["type"] == "BUY"
        else "🔴"
    )

    message = (

        f"<b>⚡ RULEBOOK SIGNAL {icon}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Asset:</b> {symbol}\n"
        f"📈 <b>Direction:</b> {trade['type']}\n"
        f"💰 <b>Entry:</b> {trade['entry']:.6f}\n"
        f"🛑 <b>SL:</b> {trade['sl']:.6f}\n"
        f"🎯 <b>TP1:</b> {trade['tp1']:.6f}\n"
        f"🎯 <b>TP2:</b> {trade['tp2']:.6f}\n"
        f"📊 <b>RR:</b> {trade['rr']:.2f}\n"
        f"⭐ <b>Score:</b> {trade['score']}\n"
        f"🧠 <b>Reason:</b> {trade['reason']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>🟡 PAPER TRADE ONLY</b>\n"
        "No real Bybit order was placed."
    )

    telegram_sent = send_telegram(message)

    log_event(
        f"NEW {trade['type']} | "
        f"{symbol} | "
        f"Entry={trade['entry']:.6f} | "
        f"Score={trade['score']} | "
        f"RR={trade['rr']:.2f} | "
        f"Telegram={'OK' if telegram_sent else 'FAILED'}"
    )


# ============================================================
# CLOSE TRADE
# ============================================================

def close_trade(
    trade,
    result,
    exit_price
):

    entry = trade["entry"]
    initial_sl = trade["initial_sl"]

    risk = abs(
        entry - initial_sl
    )

    if risk <= 0:

        r_multiple = 0

    elif trade["type"] == "BUY":

        r_multiple = (
            exit_price - entry
        ) / risk

    else:

        r_multiple = (
            entry - exit_price
        ) / risk

    db_save_trade(
        trade,
        result,
        exit_price,
        r_multiple
    )

    if result == "TP2":

        emoji = "🟢"

    elif result == "BREAK_EVEN":

        emoji = "🟡"

    else:

        emoji = "🔴"

    message = (

        f"<b>TRADE RESULT {emoji}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Asset:</b> {trade['symbol']}\n"
        f"📈 <b>Type:</b> {trade['type']}\n"
        f"📌 <b>Result:</b> {result}\n"
        f"💰 <b>Entry:</b> {entry:.6f}\n"
        f"🚪 <b>Exit:</b> {exit_price:.6f}\n"
        f"📊 <b>R:</b> {r_multiple:.2f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>🟡 PAPER TRADE</b>"
    )

    send_telegram(message)

    log_event(
        f"RESULT {result} | "
        f"{trade['symbol']} | "
        f"R={r_multiple:.2f}"
    )

    with state_lock:

        if trade in active_trades:

            active_trades.remove(trade)


# ============================================================
# MANAGE ACTIVE TRADE
# ============================================================

def manage_trade(
    trade,
    price
):

    direction = trade["type"]

    if direction == "BUY":

        if price >= trade["tp2"]:

            close_trade(
                trade,
                "TP2",
                price
            )

            return

        if (
            price >= trade["tp1"]
            and not trade["tp1_hit"]
        ):

            trade["tp1_hit"] = True
            trade["sl"] = trade["entry"]

            send_telegram(
                "<b>🎯 TP1 HIT</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📍 {trade['symbol']}\n"
                "🛡 SL → Break-Even\n"
                "🟡 PAPER TRADE"
            )

            log_event(
                f"TP1 HIT | "
                f"{trade['symbol']} | "
                "SL → BE"
            )

        if price <= trade["sl"]:

            result = (
                "BREAK_EVEN"
                if trade["tp1_hit"]
                else "SL"
            )

            close_trade(
                trade,
                result,
                price
            )

    else:

        if price <= trade["tp2"]:

            close_trade(
                trade,
                "TP2",
                price
            )

            return

        if (
            price <= trade["tp1"]
            and not trade["tp1_hit"]
        ):

            trade["tp1_hit"] = True
            trade["sl"] = trade["entry"]

            send_telegram(
                "<b>🎯 TP1 HIT</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"📍 {trade['symbol']}\n"
                "🛡 SL → Break-Even\n"
                "🟡 PAPER TRADE"
            )

            log_event(
                f"TP1 HIT | "
                f"{trade['symbol']} | "
                "SL → BE"
            )

        if price >= trade["sl"]:

            result = (
                "BREAK_EVEN"
                if trade["tp1_hit"]
                else "SL"
            )

            close_trade(
                trade,
                result,
                price
            )


# ============================================================
# SCAN SYMBOL
# ============================================================

def scan_symbol(symbol):

    price = fetch_price(symbol)

    if price is None:
        return

    with state_lock:

        trades = [
            t
            for t in active_trades
            if t["symbol"] == symbol
        ]

    for trade in trades:

        manage_trade(
            trade,
            price
        )

    if has_active_trade(symbol):
        return

    try:

        setup = calculate_setup(
            symbol,
            "BUY",
            price
        )

        if setup:

            create_trade(
                symbol,
                setup
            )

            return

    except Exception as e:

        print(
            f"BUY setup error | "
            f"{symbol}: {e}"
        )

    try:

        setup = calculate_setup(
            symbol,
            "SELL",
            price
        )

        if setup:

            create_trade(
                symbol,
                setup
            )

            return

    except Exception as e:

        print(
            f"SELL setup error | "
            f"{symbol}: {e}"
        )


# ============================================================
# 3-HOUR HEALTH MONITOR
# ============================================================

def health_monitor():

    time.sleep(60)

    while True:

        try:

            telegram_ok = check_telegram_without_message()

            bybit_ok = check_bybit_connection()

            with state_lock:
                active_count = len(active_trades)

            uptime_seconds = (
                time.time() - bot_started_at
            )

            uptime_hours = (
                uptime_seconds / 3600
            )

            scanner_ok = (
                scanner_status == "RUNNING"
            )

            telegram_text = (
                "✅ OK"
                if telegram_ok
                else "❌ ERROR"
            )

            bybit_text = (
                "✅ OK"
                if bybit_ok
                else "❌ ERROR"
            )

            scanner_text = (
                "✅ RUNNING"
                if scanner_ok
                else "⚠️ CHECK LOGS"
            )

            if (
                telegram_ok
                and bybit_ok
                and scanner_ok
            ):

                title = "🟢 BOT HEALTH CHECK"
                final = "Backend is healthy. ✅"

            else:

                title = "🔴 BOT HEALTH ALERT"
                final = (
                    "⚠️ Please check Render logs."
                )

            message = (

                f"<b>{title}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Telegram: {telegram_text}\n"
                f"Bybit API: {bybit_text}\n"
                f"Scanner: {scanner_text}\n"
                f"Mode: 🟡 {TRADING_MODE}\n"
                f"Symbols: {len(SYMBOLS)}\n"
                f"Active Trades: {active_count}\n"
                f"Uptime: {uptime_hours:.1f} hours\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{final}"
            )

            send_telegram(message)

        except Exception as e:

            print(
                "Health monitor error:",
                e
            )

        time.sleep(
            HEALTH_CHECK_INTERVAL
        )


# ============================================================
# SCANNER
# ============================================================

def background_trading_scanner():

    global scanner_status
    global last_scanner_run

    print(
        "=========================================="
    )

    print(
        "🚀 RULEBOOK SMC SCANNER STARTED"
    )

    print(
        "Exchange: Bybit"
    )

    print(
        f"Mode: {TRADING_MODE}"
    )

    print(
        "Real Orders: DISABLED"
    )

    print(
        "=========================================="
    )

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    telegram_connection_test()

    # --------------------------------------------------------
    # Bybit
    # --------------------------------------------------------

    loaded = load_bybit_markets()

    while not loaded:

        scanner_status = "BYBIT_ERROR"

        send_telegram(
            "<b>🔴 BOT STARTUP ALERT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Telegram: Checked\n"
            "Bybit: ❌ Market loading failed\n"
            "Scanner: ⚠️ Retrying\n"
            "Mode: 🟡 PAPER / SIGNAL ONLY"
        )

        time.sleep(30)

        loaded = load_bybit_markets()

    if not SYMBOLS:

        scanner_status = "WAITING_FOR_SYMBOLS"

        return

    scanner_status = "RUNNING"

    send_telegram(
        "<b>🟢 SCANNER READY</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Telegram: ✅ Connected\n"
        "Bybit API: ✅ Connected\n"
        f"Symbols: {len(SYMBOLS)}\n"
        f"{', '.join(SYMBOLS)}\n"
        "Scanner: ✅ Running\n"
        "Mode: 🟡 PAPER / SIGNAL ONLY\n"
        "Real Orders: ❌ DISABLED\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Waiting for valid Rulebook setups..."
    )

    while True:

        started = time.time()

        try:

            if bybit_status != "OK":

                print(
                    "⚠️ Bybit status not OK. "
                    "Reloading markets..."
                )

                if not load_bybit_markets():

                    scanner_status = "BYBIT_ERROR"

                    time.sleep(30)

                    continue

                scanner_status = "RUNNING"

            for symbol in list(SYMBOLS):

                try:

                    scan_symbol(symbol)

                except Exception as e:

                    print(
                        f"Scanner error | "
                        f"{symbol}: {e}"
                    )

                time.sleep(1)

            last_scanner_run = time.time()

            scanner_status = "RUNNING"

            elapsed = (
                time.time() - started
            )

            print(
                f"🔄 Scanner cycle complete | "
                f"Symbols={len(SYMBOLS)} | "
                f"Time={elapsed:.1f}s"
            )

        except Exception as e:

            scanner_status = "ERROR"

            print(
                "Main scanner error:",
                e
            )

        elapsed = (
            time.time() - started
        )

        time.sleep(
            max(
                5,
                SCAN_INTERVAL - elapsed
            )
        )


# ============================================================
# STATISTICS
# ============================================================

def get_statistics():

    try:

        conn = db_connect()

        rows = conn.execute(
            """
            SELECT result, r_multiple
            FROM trades
            """
        ).fetchall()

        conn.close()

        total = len(rows)

        wins = sum(
            1
            for r in rows
            if r[0] == "TP2"
        )

        losses = sum(
            1
            for r in rows
            if r[0] == "SL"
        )

        breakeven = sum(
            1
            for r in rows
            if r[0] == "BREAK_EVEN"
        )

        total_r = sum(
            float(r[1] or 0)
            for r in rows
        )

        win_rate = (
            wins / total * 100
            if total
            else 0
        )

        gross_profit = sum(
            float(r[1])
            for r in rows
            if float(r[1]) > 0
        )

        gross_loss = abs(
            sum(
                float(r[1])
                for r in rows
                if float(r[1]) < 0
            )
        )

        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else 0
        )

        return {

            "total": total,

            "wins": wins,

            "losses": losses,

            "breakeven": breakeven,

            "win_rate": round(
                win_rate,
                2
            ),

            "total_r": round(
                total_r,
                2
            ),

            "profit_factor": round(
                profit_factor,
                2
            )
        }

    except Exception as e:

        print(
            "Statistics error:",
            e
        )

        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "win_rate": 0,
            "total_r": 0,
            "profit_factor": 0
        }


# ============================================================
# STATUS API
# ============================================================

@app.route("/api/status")
def api_status():

    stats = get_statistics()

    with state_lock:

        trades = list(active_trades)

    uptime = (
        time.time() - bot_started_at
    )

    return jsonify({

        "status": scanner_status,

        "mode": "PAPER",

        "real_orders_enabled":
            REAL_ORDERS_ENABLED,

        "exchange": "BYBIT",

        "telegram":
            telegram_status,

        "bybit":
            bybit_status,

        "symbols":
            SYMBOLS,

        "active_trades":
            trades,

        "statistics":
            stats,

        "uptime_hours":
            round(
                uptime / 3600,
                2
            ),

        "last_scanner_run":
            last_scanner_run,

        "last_bybit_check":
            last_bybit_check,

        "last_telegram_check":
            last_telegram_check,

        "market_load_error":
            market_load_error,

        "events":
            len(trade_logs)
    })


# ============================================================
# DASHBOARD
# ============================================================

HTML_TEMPLATE = """
<!DOCTYPE html>

<html>

<head>

<title>Rulebook SMC Trading Bot</title>

<meta http-equiv="refresh" content="15">

<style>

body {
    margin:0;
    padding:20px;
    background:#0f172a;
    color:#f8fafc;
    font-family:Arial,sans-serif;
}

.container {
    max-width:1200px;
    margin:auto;
}

.header {
    text-align:center;
    border-bottom:1px solid #334155;
    padding-bottom:20px;
}

.cards {
    display:grid;
    grid-template-columns:
    repeat(auto-fit,minmax(170px,1fr));
    gap:15px;
    margin:25px 0;
}

.card {
    background:#1e293b;
    border:1px solid #334155;
    border-radius:10px;
    padding:18px;
    text-align:center;
}

.card h3 {
    color:#94a3b8;
    font-size:13px;
}

.card p {
    font-size:22px;
    font-weight:bold;
}

.online {
    color:#4ade80;
}

.error {
    color:#f87171;
}

.warning {
    color:#facc15;
}

table {
    width:100%;
    border-collapse:collapse;
    background:#1e293b;
    margin-bottom:30px;
}

th,td {
    padding:11px;
    border-bottom:1px solid #334155;
    text-align:left;
}

th {
    color:#94a3b8;
}

.logs {
    background:#020617;
    border:1px solid #334155;
    padding:15px;
    border-radius:10px;
    height:300px;
    overflow:auto;
    font-family:monospace;
}

</style>

</head>

<body>

<div class="container">

<div class="header">

<h1>🤖 Rulebook SMC Trading Bot</h1>

<p>
Structure → Level → MTF →
CHOCH → Confirmation → Risk
</p>

<p>
Status:
<span class="online">
{{ scanner_status }}
</span>
</p>

<p class="warning">
🟡 PAPER / SIGNAL MODE
</p>

<p>
Real Bybit Orders:
<b>❌ DISABLED</b>
</p>

</div>


<div class="cards">

<div class="card">
<h3>Telegram</h3>
<p class="
{% if telegram_status == 'OK' %}
online
{% else %}
error
{% endif %}
">
{{ telegram_status }}
</p>
</div>


<div class="card">
<h3>Bybit API</h3>
<p class="
{% if bybit_status == 'OK' %}
online
{% else %}
error
{% endif %}
">
{{ bybit_status }}
</p>
</div>


<div class="card">
<h3>Active Trades</h3>
<p>{{ active_trades|length }}</p>
</div>


<div class="card">
<h3>Total Trades</h3>
<p>{{ stats.total }}</p>
</div>


<div class="card">
<h3>Wins</h3>
<p class="online">{{ stats.wins }}</p>
</div>


<div class="card">
<h3>Losses</h3>
<p class="error">{{ stats.losses }}</p>
</div>


<div class="card">
<h3>Win Rate</h3>
<p>{{ stats.win_rate }}%</p>
</div>


<div class="card">
<h3>Profit Factor</h3>
<p>{{ stats.profit_factor }}</p>
</div>

</div>


<h2>📊 Active Positions</h2>

<table>

<tr>
<th>Symbol</th>
<th>Direction</th>
<th>Entry</th>
<th>SL</th>
<th>TP1</th>
<th>TP2</th>
<th>RR</th>
<th>Score</th>
</tr>

{% for trade in active_trades %}

<tr>

<td><b>{{ trade.symbol }}</b></td>

<td>{{ trade.type }}</td>

<td>{{ "%.6f"|format(trade.entry) }}</td>

<td>{{ "%.6f"|format(trade.sl) }}</td>

<td>{{ "%.6f"|format(trade.tp1) }}</td>

<td>{{ "%.6f"|format(trade.tp2) }}</td>

<td>{{ "%.2f"|format(trade.rr) }}</td>

<td>{{ trade.score }}</td>

</tr>

{% else %}

<tr>

<td colspan="8" style="text-align:center;">
No active Rulebook setup.
</td>

</tr>

{% endfor %}

</table>


<h2>📱 Activity Logs</h2>

<div class="logs">

{% for log in trade_logs|reverse %}

<div>
&gt; {{ log }}
</div>

{% else %}

<div>
&gt; Waiting for scanner logs...
</div>

{% endfor %}

</div>

</div>

</body>

</html>
"""


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    stats = get_statistics()

    with state_lock:

        trades = list(active_trades)
        logs = list(trade_logs)

    return render_template_string(
        HTML_TEMPLATE,
        active_trades=trades,
        trade_logs=logs,
        stats=stats,
        scanner_status=scanner_status,
        telegram_status=telegram_status,
        bybit_status=bybit_status
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "status": "ok",

        "scanner":
            scanner_status,

        "telegram":
            telegram_status,

        "bybit":
            bybit_status,

        "symbols":
            SYMBOLS,

        "mode":
            "PAPER",

        "real_orders_enabled":
            False,

        "time":
            datetime.now(
                timezone.utc
            ).isoformat()
    })


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    init_db()

    print(
        "=========================================="
    )

    print(
        "🚀 RULEBOOK SMC BOT"
    )

    print(
        "Mode: PAPER / SIGNAL ONLY"
    )

    print(
        "Real Orders: DISABLED"
    )

    print(
        "=========================================="
    )

    # --------------------------------------------------------
    # Scanner
    # --------------------------------------------------------

    scanner_thread = threading.Thread(
        target=background_trading_scanner,
        daemon=True,
        name="RulebookScanner"
    )

    scanner_thread.start()

    # --------------------------------------------------------
    # Health monitor
    # --------------------------------------------------------

    health_thread = threading.Thread(
        target=health_monitor,
        daemon=True,
        name="HealthMonitor"
    )

    health_thread.start()

    # --------------------------------------------------------
    # Render PORT
    # --------------------------------------------------------

    port = int(
        os.getenv(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
