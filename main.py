import os
import time
import sqlite3
import threading
from datetime import datetime, timezone

import requests
import ccxt
from flask import Flask, render_template_string, jsonify


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM", "")
TELEGRAM_CHAT_ID = os.getenv("1317739622", "")

DB_FILE = os.getenv("DB_FILE", "trading_bot.db")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))

# Core symbols
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
]

# Rulebook timeframes
TF_DAILY = "1d"
TF_4H = "4h"
TF_1H = "1h"
TF_15M = "15m"
TF_5M = "5m"
TF_1M = "1m"

# Risk rules
MIN_RR = 1.50
MAX_SL_PERCENT = 1.50

# How many candles to fetch
CANDLE_LIMIT = 150

# Don't repeatedly trade same level
LEVEL_COOLDOWN_MINUTES = 60

# Maximum number of simultaneous paper trades
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
    "options": {
        "defaultType": "spot",
    }
})


# ============================================================
# GLOBAL STATE
# ============================================================

active_trades = []
trade_logs = []

state_lock = threading.Lock()

last_signal_key = {}
invalidated_levels = set()


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    return sqlite3.connect(
        DB_FILE,
        check_same_thread=False
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
            "INSERT INTO events(timestamp,event) VALUES (?,?)",
            (datetime.now(timezone.utc).isoformat(), event)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB event error:", e)


def db_save_trade(trade, result, exit_price, r_multiple):
    try:
        conn = db_connect()

        conn.execute("""
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
        """, (
            datetime.now(timezone.utc).isoformat(),
            trade["symbol"],
            trade["type"],
            trade["entry"],
            trade["sl"],
            trade["tp1"],
            trade["tp2"],
            result,
            exit_price,
            r_multiple,
            trade.get("score", 0),
            trade.get("reason", "")
        ))

        conn.commit()
        conn.close()

    except Exception as e:
        print("DB trade error:", e)


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram disabled]")
        return

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=8
        )

        if response.status_code != 200:
            print("Telegram error:", response.text)

    except Exception as e:
        print("Telegram exception:", e)


# ============================================================
# LOGGING
# ============================================================

def log_event(message):
    print(message)

    with state_lock:
        trade_logs.append(
            f"{datetime.now().strftime('%H:%M:%S')} | {message}"
        )

        # Prevent unlimited RAM growth
        if len(trade_logs) > 500:
            del trade_logs[:-500]

    db_log_event(message)


# ============================================================
# MARKET DATA
# ============================================================

def fetch_candles(symbol, timeframe, limit=CANDLE_LIMIT):
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
            f"Candle error | {symbol} | {timeframe} | {e}"
        )
        return []


def fetch_price(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        return float(ticker["last"])
    except Exception as e:
        print("Price error:", symbol, e)
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
    return abs(candle_close(c) - candle_open(c))


def candle_range(c):
    return candle_high(c) - candle_low(c)


def is_bullish(c):
    return candle_close(c) > candle_open(c)


def is_bearish(c):
    return candle_close(c) < candle_open(c)


def body_ratio(c):
    r = candle_range(c)

    if r <= 0:
        return 0

    return candle_body(c) / r


# ============================================================
# SWING DETECTION
# ============================================================

def find_swing_highs(candles, left=2, right=2):
    highs = []

    for i in range(left, len(candles) - right):
        h = candle_high(candles[i])

        left_highs = [
            candle_high(candles[j])
            for j in range(i - left, i)
        ]

        right_highs = [
            candle_high(candles[j])
            for j in range(i + 1, i + right + 1)
        ]

        if h > max(left_highs) and h >= max(right_highs):
            highs.append((i, h))

    return highs


def find_swing_lows(candles, left=2, right=2):
    lows = []

    for i in range(left, len(candles) - right):
        l = candle_low(candles[i])

        left_lows = [
            candle_low(candles[j])
            for j in range(i - left, i)
        ]

        right_lows = [
            candle_low(candles[j])
            for j in range(i + 1, i + right + 1)
        ]

        if l < min(left_lows) and l <= min(right_lows):
            lows.append((i, l))

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

    highs = find_swing_highs(candles)
    lows = find_swing_lows(candles)

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

    if last_high > prev_high and last_low > prev_low:
        trend = "BULLISH"

    elif last_high < prev_high and last_low < prev_low:
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


# ============================================================
# MULTI TIMEFRAME TREND
# ============================================================

def get_structure(symbol, timeframe):
    candles = fetch_candles(symbol, timeframe)

    if not candles:
        return None, []

    return detect_structure(candles), candles


def get_mtf_bias(symbol):
    daily_structure, _ = get_structure(symbol, TF_DAILY)
    h4_structure, _ = get_structure(symbol, TF_4H)
    h1_structure, _ = get_structure(symbol, TF_1H)
    m15_structure, _ = get_structure(symbol, TF_15M)

    if not daily_structure or not h4_structure or not h1_structure:
        return {
            "direction": "NEUTRAL",
            "daily": "NEUTRAL",
            "h4": "NEUTRAL",
            "h1": "NEUTRAL",
            "m15": "NEUTRAL"
        }

    daily = daily_structure["trend"]
    h4 = h4_structure["trend"]
    h1 = h1_structure["trend"]

    bullish_score = 0
    bearish_score = 0

    for trend in [daily, h4, h1]:
        if trend == "BULLISH":
            bullish_score += 1

        elif trend == "BEARISH":
            bearish_score += 1

    if bullish_score >= 2 and bullish_score > bearish_score:
        direction = "BULLISH"

    elif bearish_score >= 2 and bearish_score > bullish_score:
        direction = "BEARISH"

    else:
        direction = "NEUTRAL"

    return {
        "direction": direction,
        "daily": daily,
        "h4": h4,
        "h1": h1,
        "m15": (
            m15_structure["trend"]
            if m15_structure else "NEUTRAL"
        )
    }


# ============================================================
# CHOCH
# ============================================================

def detect_choch(candles, expected_direction):
    """
    Conservative CHOCH:
    Bullish = closed candle breaks recent swing high.
    Bearish = closed candle breaks recent swing low.
    """

    if len(candles) < 20:
        return False

    # Ignore currently forming candle
    closed = candles[:-1]

    swings_high = find_swing_highs(closed)
    swings_low = find_swing_lows(closed)

    if expected_direction == "BUY":

        if not swings_high:
            return False

        recent_high = swings_high[-1][1]
        last_close = candle_close(closed[-1])

        return last_close > recent_high

    if expected_direction == "SELL":

        if not swings_low:
            return False

        recent_low = swings_low[-1][1]
        last_close = candle_close(closed[-1])

        return last_close < recent_low

    return False


# ============================================================
# CONFIRMATION CANDLE
# ============================================================

def confirmation_candle(candle, direction):
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


# ============================================================
# ENGULFING
# ============================================================

def bullish_engulfing(prev, current):
    return (
        is_bearish(prev)
        and is_bullish(current)
        and candle_open(current) <= candle_close(prev)
        and candle_close(current) >= candle_open(prev)
    )


def bearish_engulfing(prev, current):
    return (
        is_bullish(prev)
        and is_bearish(current)
        and candle_open(current) >= candle_close(prev)
        and candle_close(current) <= candle_open(prev)
    )


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(candles, direction):
    if len(candles) < 15:
        return False

    closed = candles[:-1]

    recent = closed[-8:-1]

    if not recent:
        return False

    previous_high = max(
        candle_high(c)
        for c in recent
    )

    previous_low = min(
        candle_low(c)
        for c in recent
    )

    last = closed[-1]

    if direction == "BUY":

        # Low swept and price closed back above it
        return (
            candle_low(last) < previous_low
            and candle_close(last) > previous_low
        )

    if direction == "SELL":

        # High swept and price closed back below it
        return (
            candle_high(last) > previous_high
            and candle_close(last) < previous_high
        )

    return False


# ============================================================
# FVG
# ============================================================

def detect_fvg(candles, direction):
    if len(candles) < 5:
        return False

    closed = candles[:-1]

    a = closed[-3]
    c = closed[-1]

    if direction == "BUY":

        return candle_low(c) > candle_high(a)

    if direction == "SELL":

        return candle_high(c) < candle_low(a)

    return False


# ============================================================
# DOUBLE TOP / DOUBLE BOTTOM
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
    if len(candles) < 10:
        return None

    closed = candles[:-1]

    for i in range(len(closed) - 4, max(2, len(closed) - 15), -1):

        base = closed[i]

        if is_bearish(base):

            next1 = closed[i + 1]

            if is_bullish(next1) and candle_body(next1) > candle_body(base):
                return {
                    "low": candle_low(base),
                    "high": candle_high(base),
                    "index": i
                }

    return None


def detect_supply_zone(candles):
    if len(candles) < 10:
        return None

    closed = candles[:-1]

    for i in range(len(closed) - 4, max(2, len(closed) - 15), -1):

        base = closed[i]

        if is_bullish(base):

            next1 = closed[i + 1]

            if is_bearish(next1) and candle_body(next1) > candle_body(base):
                return {
                    "low": candle_low(base),
                    "high": candle_high(base),
                    "index": i
                }

    return None


def price_inside_zone(price, zone):
    if not zone:
        return False

    return (
        zone["low"] <= price <= zone["high"]
    )


# ============================================================
# TGL
# ============================================================

def detect_tgl(candles, direction):
    """
    Conservative TGL approximation.

    Uses recent strong impulse + opposite candle.
    """

    if len(candles) < 20:
        return None

    closed = candles[:-1]

    for i in range(len(closed) - 3, max(3, len(closed) - 20), -1):

        c = closed[i]

        if direction == "BUY":

            if is_bearish(c):

                following = closed[i + 1]

                if (
                    is_bullish(following)
                    and body_ratio(following) > 0.55
                ):
                    return {
                        "low": candle_low(c),
                        "high": candle_high(c),
                        "index": i
                    }

        if direction == "SELL":

            if is_bullish(c):

                following = closed[i + 1]

                if (
                    is_bearish(following)
                    and body_ratio(following) > 0.55
                ):
                    return {
                        "low": candle_low(c),
                        "high": candle_high(c),
                        "index": i
                    }

    return None


# ============================================================
# LEVEL INVALIDATION
# ============================================================

def level_invalidated(candles, zone, direction):
    if not zone or len(candles) < 3:
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
    """
    Conservative crypto filter.

    Crypto runs 24/7, therefore this is not a hard
    exchange-hours filter.
    """

    hour = datetime.now(timezone.utc).hour

    # Avoid very thin early UTC hours
    if 0 <= hour < 4:
        return False

    return True


# ============================================================
# SETUP SCORE
# ============================================================

def calculate_setup(symbol, direction, price):
    """
    Rulebook hierarchy:

    Core:
      1. MTF structure
      2. Level
      3. CHOCH
      4. Candle confirmation

    Extra:
      FVG
      Liquidity sweep
      Demand/Supply
      Double top/bottom
      TGL
    """

    if not session_filter():
        return None

    bias = get_mtf_bias(symbol)

    if direction == "BUY" and bias["direction"] != "BULLISH":
        return None

    if direction == "SELL" and bias["direction"] != "BEARISH":
        return None

    h1 = fetch_candles(symbol, TF_1H)
    m15 = fetch_candles(symbol, TF_15M)
    m5 = fetch_candles(symbol, TF_5M)
    m1 = fetch_candles(symbol, TF_1M)

    if not h1 or not m15 or not m5 or not m1:
        return None

    # --------------------------------------------------------
    # Primary Level
    # --------------------------------------------------------

    if direction == "BUY":

        demand = detect_demand_zone(h1)
        tgl = detect_tgl(h1, "BUY")

        level = demand or tgl

        if not level:
            return None

        if level_invalidated(h1, level, "BUY"):
            return None

        price_in_level = price_inside_zone(price, level)

    else:

        supply = detect_supply_zone(h1)
        tgl = detect_tgl(h1, "SELL")

        level = supply or tgl

        if not level:
            return None

        if level_invalidated(h1, level, "SELL"):
            return None

        price_in_level = price_inside_zone(price, level)

    # --------------------------------------------------------
    # Price must actually interact with level
    # --------------------------------------------------------

    if not price_in_level:

        # Small tolerance around level
        zone_size = level["high"] - level["low"]

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

    # --------------------------------------------------------
    # 1M CHOCH
    # --------------------------------------------------------

    choch = detect_choch(
        m1,
        direction
    )

    if not choch:
        return None

    # --------------------------------------------------------
    # 1M Confirmation Candle
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    score = 0
    reasons = []

    # CORE
    score += 30
    reasons.append("MTF_STRUCTURE")

    score += 25
    reasons.append("VALID_LEVEL")

    score += 25
    reasons.append("1M_CHOCH")

    score += 10
    reasons.append("CONFIRMATION")

    # EXTRA CONFIRMATION
    if detect_fvg(m15, direction):
        score += 5
        reasons.append("FVG")

    if detect_liquidity_sweep(m15, direction):
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

    # --------------------------------------------------------
    # Minimum setup quality
    # --------------------------------------------------------

    if score < 80:
        return None

    # --------------------------------------------------------
    # SL
    # --------------------------------------------------------

    if direction == "BUY":

        recent_lows = find_swing_lows(m1)

        if recent_lows:
            swing_low = recent_lows[-1][1]
        else:
            swing_low = min(
                candle_low(c)
                for c in m1[-10:-1]
            )

        sl = swing_low * 0.999

        if sl >= price:
            return None

        risk = price - sl

        if risk / price * 100 > MAX_SL_PERCENT:
            return None

        # Structural target
        highs = find_swing_highs(m15)

        targets = [
            h[1]
            for h in highs
            if h[1] > price
        ]

        if targets:
            tp2 = min(targets)
        else:
            tp2 = price + risk * 2.0

        tp1 = price + risk * 1.0

    else:

        recent_highs = find_swing_highs(m1)

        if recent_highs:
            swing_high = recent_highs[-1][1]
        else:
            swing_high = max(
                candle_high(c)
                for c in m1[-10:-1]
            )

        sl = swing_high * 1.001

        if sl <= price:
            return None

        risk = sl - price

        if risk / price * 100 > MAX_SL_PERCENT:
            return None

        lows = find_swing_lows(m15)

        targets = [
            l[1]
            for l in lows
            if l[1] < price
        ]

        if targets:
            tp2 = max(targets)
        else:
            tp2 = price - risk * 2.0

        tp1 = price - risk * 1.0

    # --------------------------------------------------------
    # RR FILTER
    # --------------------------------------------------------

    if direction == "BUY":
        rr = (tp2 - price) / risk
    else:
        rr = (price - tp2) / risk

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
# DUPLICATE SIGNAL PROTECTION
# ============================================================

def signal_allowed(symbol, setup):
    key = (
        symbol,
        setup["direction"],
        round(setup["level"]["low"], 6),
        round(setup["level"]["high"], 6)
    )

    now = time.time()

    previous = last_signal_key.get(key)

    if previous:
        if now - previous < LEVEL_COOLDOWN_MINUTES * 60:
            return False

    last_signal_key[key] = now

    return True


# ============================================================
# ACTIVE TRADE CHECK
# ============================================================

def has_active_trade(symbol):
    with state_lock:
        return any(
            t["symbol"] == symbol
            for t in active_trades
        )


# ============================================================
# CREATE TRADE
# ============================================================

def create_trade(symbol, setup):
    if has_active_trade(symbol):
        return

    with state_lock:
        if len(active_trades) >= MAX_ACTIVE_TRADES:
            return

    if not signal_allowed(symbol, setup):
        return

    trade = {
        "id": f"{symbol}-{int(time.time())}",
        "symbol": symbol,
        "type": setup["direction"],
        "entry": setup["entry"],
        "sl": setup["sl"],
        "initial_sl": setup["sl"],
        "tp1": setup["tp1"],
        "tp2": setup["tp2"],
        "rr": setup["rr"],
        "score": setup["score"],
        "reason": setup["reason"],
        "tp1_hit": False,
        "created_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    with state_lock:
        active_trades.append(trade)

    icon = "🟢" if trade["type"] == "BUY" else "🔴"

    message = (
        f"<b>⚡ RULEBOOK SIGNAL {icon}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Asset:</b> {symbol}\n"
        f"📈 <b>Direction:</b> {trade['type']}\n"
        f"💰 <b>Entry:</b> {trade['entry']:.4f}\n"
        f"🛑 <b>SL:</b> {trade['sl']:.4f}\n"
        f"🎯 <b>TP1:</b> {trade['tp1']:.4f}\n"
        f"🎯 <b>TP2:</b> {trade['tp2']:.4f}\n"
        f"📊 <b>RR:</b> {trade['rr']:.2f}\n"
        f"⭐ <b>Score:</b> {trade['score']}\n"
        f"🧠 <b>Reason:</b> {trade['reason']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>Paper Trade Only</b>"
    )

    send_telegram(message)

    log_event(
        f"NEW {trade['type']} | "
        f"{symbol} | "
        f"Entry={trade['entry']:.4f} | "
        f"Score={trade['score']} | "
        f"RR={trade['rr']:.2f}"
    )


# ============================================================
# TRADE RESULT
# ============================================================

def close_trade(trade, result, exit_price):
    entry = trade["entry"]
    initial_sl = trade["initial_sl"]

    risk = abs(entry - initial_sl)

    if risk <= 0:
        r_multiple = 0
    else:

        if trade["type"] == "BUY":
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
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Asset:</b> {trade['symbol']}\n"
        f"📈 <b>Type:</b> {trade['type']}\n"
        f"📌 <b>Result:</b> {result}\n"
        f"💰 <b>Entry:</b> {entry:.4f}\n"
        f"🚪 <b>Exit:</b> {exit_price:.4f}\n"
        f"📊 <b>R:</b> {r_multiple:.2f}\n"
        f"━━━━━━━━━━━━━━━━━━"
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
# ACTIVE TRADE MANAGEMENT
# ============================================================

def manage_trade(trade, price):
    direction = trade["type"]

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    if direction == "BUY":

        # TP2
        if price >= trade["tp2"]:

            close_trade(
                trade,
                "TP2",
                price
            )

            return

        # TP1
        if (
            price >= trade["tp1"]
            and not trade["tp1_hit"]
        ):

            trade["tp1_hit"] = True

            # Break-even
            trade["sl"] = trade["entry"]

            send_telegram(
                f"<b>🎯 TP1 HIT</b>\n"
                f"{trade['symbol']}\n"
                f"SL moved to Break-Even."
            )

            log_event(
                f"TP1 HIT | "
                f"{trade['symbol']} | "
                f"SL → BE"
            )

        # SL
        if price <= trade["sl"]:

            if trade["tp1_hit"]:
                result = "BREAK_EVEN"
            else:
                result = "SL"

            close_trade(
                trade,
                result,
                price
            )

    # --------------------------------------------------------
    # SELL
    # --------------------------------------------------------

    elif direction == "SELL":

        # TP2
        if price <= trade["tp2"]:

            close_trade(
                trade,
                "TP2",
                price
            )

            return

        # TP1
        if (
            price <= trade["tp1"]
            and not trade["tp1_hit"]
        ):

            trade["tp1_hit"] = True

            trade["sl"] = trade["entry"]

            send_telegram(
                f"<b>🎯 TP1 HIT</b>\n"
                f"{trade['symbol']}\n"
                f"SL moved to Break-Even."
            )

            log_event(
                f"TP1 HIT | "
                f"{trade['symbol']} | "
                f"SL → BE"
            )

        # SL
        if price >= trade["sl"]:

            if trade["tp1_hit"]:
                result = "BREAK_EVEN"
            else:
                result = "SL"

            close_trade(
                trade,
                result,
                price
            )


# ============================================================
# SCANNER
# ============================================================

def scan_symbol(symbol):

    price = fetch_price(symbol)

    if price is None:
        return

    # --------------------------------------------------------
    # Manage existing trade
    # --------------------------------------------------------

    with state_lock:
        trades = [
            t for t in active_trades
            if t["symbol"] == symbol
        ]

    for trade in trades:
        manage_trade(
            trade,
            price
        )

    # --------------------------------------------------------
    # New setup
    # --------------------------------------------------------

    if has_active_trade(symbol):
        return

    # BUY
    try:

        buy_setup = calculate_setup(
            symbol,
            "BUY",
            price
        )

        if buy_setup:
            create_trade(
                symbol,
                buy_setup
            )
            return

    except Exception as e:
        print(
            f"BUY setup error {symbol}: {e}"
        )

    # SELL
    try:

        sell_setup = calculate_setup(
            symbol,
            "SELL",
            price
        )

        if sell_setup:
            create_trade(
                symbol,
                sell_setup
            )
            return

    except Exception as e:
        print(
            f"SELL setup error {symbol}: {e}"
        )


# ============================================================
# BACKGROUND ENGINE
# ============================================================

def background_trading_scanner():

    print(
        "================================================"
    )
    print(
        "🚀 RULEBOOK SMC SCANNER STARTED"
    )
    print(
        "Exchange: Bybit"
    )
    print(
        "Mode: PAPER / SIGNAL ONLY"
    )
    print(
        "Symbols:",
        ", ".join(SYMBOLS)
    )
    print(
        "================================================"
    )

    send_telegram(
        "<b>🤖 Rulebook SMC Bot Started</b>\n"
        "Mode: Paper / Signal Only\n"
        "Exchange: Bybit"
    )

    while True:

        started = time.time()

        try:

            for symbol in SYMBOLS:

                try:
                    scan_symbol(symbol)

                except Exception as e:
                    print(
                        f"Symbol scanner error "
                        f"{symbol}: {e}"
                    )

                # Small delay between symbols
                time.sleep(1)

        except Exception as e:

            print(
                "Scanner main error:",
                e
            )

        elapsed = time.time() - started

        sleep_time = max(
            5,
            SCAN_INTERVAL - elapsed
        )

        time.sleep(sleep_time)


# ============================================================
# STATISTICS
# ============================================================

def get_statistics():

    try:

        conn = db_connect()

        rows = conn.execute("""
            SELECT
                result,
                r_multiple
            FROM trades
        """).fetchall()

        conn.close()

        total = len(rows)

        wins = sum(
            1 for r in rows
            if r[0] == "TP2"
        )

        losses = sum(
            1 for r in rows
            if r[0] == "SL"
        )

        breakeven = sum(
            1 for r in rows
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

        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": round(win_rate, 2),
            "total_r": round(total_r, 2)
        }

    except Exception as e:

        print("Stats error:", e)

        return {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "win_rate": 0,
            "total_r": 0
        }


# ============================================================
# API
# ============================================================

@app.route("/api/status")
def api_status():

    stats = get_statistics()

    with state_lock:
        trades = list(active_trades)

    return jsonify({
        "status": "ONLINE",
        "mode": "PAPER",
        "exchange": "BYBIT",
        "active_trades": trades,
        "statistics": stats,
        "events": len(trade_logs)
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
    font-family:Arial, sans-serif;
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
    repeat(auto-fit,minmax(180px,1fr));
    gap:15px;
    margin:25px 0;
}

.card {
    background:#1e293b;
    border:1px solid #334155;
    border-radius:10px;
    padding:20px;
    text-align:center;
}

.card h3 {
    color:#94a3b8;
    font-size:13px;
}

.card p {
    font-size:25px;
    font-weight:bold;
}

table {
    width:100%;
    border-collapse:collapse;
    background:#1e293b;
    margin-bottom:30px;
}

th,td {
    padding:12px;
    border-bottom:1px solid #334155;
    text-align:left;
}

th {
    color:#94a3b8;
}

.buy {
    color:#4ade80;
    font-weight:bold;
}

.sell {
    color:#f87171;
    font-weight:bold;
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

.online {
    color:#4ade80;
}

.paper {
    color:#facc15;
}

</style>

</head>

<body>

<div class="container">

<div class="header">

<h1>🤖 Rulebook SMC Trading Bot</h1>

<p>
Structure → Level → MTF → CHOCH →
Confirmation → Risk
</p>

<p class="online">
🟢 ONLINE
</p>

<p class="paper">
🟡 PAPER / SIGNAL MODE
</p>

</div>


<div class="cards">

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
<p class="sell">{{ stats.losses }}</p>
</div>

<div class="card">
<h3>Break Even</h3>
<p>{{ stats.breakeven }}</p>
</div>

<div class="card">
<h3>Win Rate</h3>
<p>{{ stats.win_rate }}%</p>
</div>

<div class="card">
<h3>Total R</h3>
<p>{{ stats.total_r }}</p>
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

<td class="{{ 'buy' if trade.type == 'BUY'
else 'sell' }}">
{{ trade.type }}
</td>

<td>{{ "%.4f"|format(trade.entry) }}</td>

<td>{{ "%.4f"|format(trade.sl) }}</td>

<td>{{ "%.4f"|format(trade.tp1) }}</td>

<td>{{ "%.4f"|format(trade.tp2) }}</td>

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


<h2>📱 Activity</h2>

<div class="logs">

{% for log in trade_logs|reverse %}

<div>> {{ log }}</div>

{% else %}

<div>> Bot started. Waiting for valid Rulebook setup...</div>

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
        stats=stats
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({
        "status": "ok",
        "time": datetime.now(
            timezone.utc
        ).isoformat()
    })


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    init_db()

    scanner_thread = threading.Thread(
        target=background_trading_scanner,
        daemon=True
    )

    scanner_thread.start()

    port = int(
        os.getenv("PORT", "5000")
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
