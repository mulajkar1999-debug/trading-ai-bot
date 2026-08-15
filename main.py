import os
import time
import sqlite3
import threading
import traceback
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template_string


# ============================================================
# CONFIGURATION
# ============================================================

# IMPORTANT:
# Do NOT put the actual Telegram token/chat ID here.
# Add them in Render Environment Variables.
#
# TELEGRAM_BOT_TOKEN = your Telegram bot token
# TELEGRAM_CHAT_ID   = your Telegram chat ID

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

DB_FILE = os.getenv("DB_FILE", "trading_bot.db")

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))

HEALTH_CHECK_INTERVAL = 3 * 60 * 60

API_TIMEOUT = int(os.getenv("API_TIMEOUT", "15"))

BYBIT_BASE_URL = os.getenv(
    "BYBIT_BASE_URL",
    "https://api.bybit.com"
).rstrip("/")

BYBIT_CATEGORY = "spot"

BYBIT_MAX_RETRIES = int(
    os.getenv("BYBIT_MAX_RETRIES", "5")
)

BYBIT_RETRY_DELAY = int(
    os.getenv("BYBIT_RETRY_DELAY", "5")
)


# ============================================================
# MODE
# ============================================================

TRADING_MODE = "PAPER / SIGNAL ONLY"

# NEVER changed automatically.
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

# Bybit native symbol map:
#
# BTC/USDT -> BTCUSDT
#
BYBIT_SYMBOL_MAP = {}


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

last_bybit_error = None
last_telegram_error = None

bybit_request_count = 0


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

        print(
            "DB event error:",
            repr(e)
        )


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

        print(
            "DB trade error:",
            repr(e)
        )


# ============================================================
# LOGGING
# ============================================================

def log_event(message):

    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    full_message = (
        f"{timestamp} | {message}"
    )

    print(full_message)

    with state_lock:

        trade_logs.append(full_message)

        if len(trade_logs) > 500:

            del trade_logs[:-500]

    db_log_event(message)


def log_exception(
    prefix,
    exc
):

    print(
        f"❌ {prefix}: "
        f"{type(exc).__name__}: {exc}"
    )

    traceback.print_exc()

    log_event(
        f"{prefix} | "
        f"{type(exc).__name__}: {exc}"
    )


# ============================================================
# GENERIC HTTP REQUEST
# ============================================================

def bybit_get(
    path,
    params=None,
    retries=None
):

    global bybit_request_count
    global last_bybit_error

    if retries is None:
        retries = BYBIT_MAX_RETRIES

    url = (
        BYBIT_BASE_URL
        + path
    )

    last_exception = None

    for attempt in range(
        1,
        retries + 1
    ):

        try:

            bybit_request_count += 1

            response = requests.get(
                url,
                params=params or {},
                timeout=API_TIMEOUT,
                headers={
                    "User-Agent":
                        "Rulebook-SMC-Bot/1.0"
                }
            )

            status_code = response.status_code

            response_text = response.text[:1000]

            if status_code != 200:

                error_message = (
                    f"HTTP {status_code} | "
                    f"URL={url} | "
                    f"Response={response_text}"
                )

                last_bybit_error = error_message

                print(
                    f"[Bybit] Attempt "
                    f"{attempt}/{retries} FAILED: "
                    f"{error_message}"
                )

                if (
                    status_code in
                    [429, 500, 502, 503, 504]
                ):

                    time.sleep(
                        BYBIT_RETRY_DELAY
                        * attempt
                    )

                    continue

                raise RuntimeError(
                    error_message
                )

            try:

                data = response.json()

            except Exception as e:

                raise RuntimeError(
                    "Invalid JSON from Bybit | "
                    f"HTTP={status_code} | "
                    f"Response={response_text}"
                ) from e

            ret_code = data.get(
                "retCode"
            )

            if ret_code != 0:

                ret_msg = data.get(
                    "retMsg",
                    "Unknown Bybit error"
                )

                error_message = (
                    f"Bybit retCode={ret_code} | "
                    f"retMsg={ret_msg} | "
                    f"URL={url}"
                )

                last_bybit_error = error_message

                print(
                    f"[Bybit] Attempt "
                    f"{attempt}/{retries} FAILED: "
                    f"{error_message}"
                )

                if attempt < retries:

                    time.sleep(
                        BYBIT_RETRY_DELAY
                        * attempt
                    )

                    continue

                raise RuntimeError(
                    error_message
                )

            last_bybit_error = None

            return data

        except Exception as e:

            last_exception = e

            last_bybit_error = (
                f"{type(e).__name__}: {e}"
            )

            print(
                f"[Bybit] Attempt "
                f"{attempt}/{retries} exception: "
                f"{type(e).__name__}: {e}"
            )

            if attempt < retries:

                time.sleep(
                    BYBIT_RETRY_DELAY
                    * attempt
                )

    if last_exception:

        raise last_exception

    raise RuntimeError(
        "Bybit request failed without exception."
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram_configured():

    return bool(
        TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
    )


def send_telegram(message):

    global telegram_status
    global last_telegram_check
    global last_telegram_error

    if not TELEGRAM_BOT_TOKEN:

        telegram_status = "NOT_CONFIGURED"

        last_telegram_error = (
            "TELEGRAM_BOT_TOKEN missing"
        )

        print(
            "❌ Telegram token missing."
        )

        return False

    if not TELEGRAM_CHAT_ID:

        telegram_status = "NOT_CONFIGURED"

        last_telegram_error = (
            "TELEGRAM_CHAT_ID missing"
        )

        print(
            "❌ Telegram chat ID missing."
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

        response_text = response.text[:1000]

        if response.status_code != 200:

            telegram_status = "ERROR"

            last_telegram_error = (
                f"HTTP {response.status_code} | "
                f"{response_text}"
            )

            print(
                "[Telegram] ERROR:",
                last_telegram_error
            )

            return False

        data = response.json()

        if data.get("ok"):

            telegram_status = "OK"

            last_telegram_check = time.time()

            last_telegram_error = None

            return True

        telegram_status = "ERROR"

        last_telegram_error = (
            f"Telegram API returned ok=false | "
            f"{response_text}"
        )

        print(
            "[Telegram] API error:",
            last_telegram_error
        )

        return False

    except Exception as e:

        telegram_status = "ERROR"

        last_telegram_error = (
            f"{type(e).__name__}: {e}"
        )

        print(
            "[Telegram] Exception:",
            last_telegram_error
        )

        return False


# ============================================================
# TELEGRAM DIAGNOSTICS
# ============================================================

def check_telegram_without_message():

    global telegram_status
    global last_telegram_check
    global last_telegram_error

    if not TELEGRAM_BOT_TOKEN:

        telegram_status = "NOT_CONFIGURED"

        last_telegram_error = (
            "TELEGRAM_BOT_TOKEN missing"
        )

        return False

    if not TELEGRAM_CHAT_ID:

        telegram_status = "NOT_CONFIGURED"

        last_telegram_error = (
            "TELEGRAM_CHAT_ID missing"
        )

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

        response_text = response.text[:1000]

        if response.status_code != 200:

            telegram_status = "ERROR"

            last_telegram_error = (
                f"getMe HTTP "
                f"{response.status_code} | "
                f"{response_text}"
            )

            print(
                "❌ Telegram getMe:",
                last_telegram_error
            )

            return False

        data = response.json()

        if not data.get("ok"):

            telegram_status = "ERROR"

            last_telegram_error = (
                f"getMe ok=false | "
                f"{response_text}"
            )

            print(
                "❌ Telegram getMe:",
                last_telegram_error
            )

            return False

        bot_info = data.get(
            "result",
            {}
        )

        bot_username = bot_info.get(
            "username",
            "unknown"
        )

        telegram_status = "OK"

        last_telegram_check = time.time()

        last_telegram_error = None

        print(
            "✅ Telegram API OK | "
            f"Bot=@{bot_username}"
        )

        return True

    except Exception as e:

        telegram_status = "ERROR"

        last_telegram_error = (
            f"{type(e).__name__}: {e}"
        )

        print(
            "❌ Telegram health exception:",
            last_telegram_error
        )

        return False


def check_telegram_chat():

    if not telegram_configured():

        return False

    url = (
        "https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/getChat"
    )

    try:

        response = requests.get(
            url,
            params={
                "chat_id":
                    TELEGRAM_CHAT_ID
            },
            timeout=10
        )

        data = response.json()

        if not data.get("ok"):

            print(
                "❌ Telegram chat diagnostic failed:",
                response.text[:1000]
            )

            return False

        chat = data.get(
            "result",
            {}
        )

        print(
            "✅ Telegram chat OK | "
            f"id={chat.get('id')} | "
            f"type={chat.get('type')} | "
            f"title={chat.get('title', '')}"
        )

        return True

    except Exception as e:

        print(
            "❌ Telegram chat diagnostic exception:",
            repr(e)
        )

        return False


def telegram_connection_test():

    global telegram_status

    print(
        "=========================================="
    )

    print(
        "📱 TESTING TELEGRAM"
    )

    print(
        "=========================================="
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
            "❌ Telegram bot API test failed."
        )

        return False

    check_telegram_chat()

    message = (
        "<b>🟢 TELEGRAM CONNECTION OK</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Telegram Bot: ✅ Connected\n"
        "Chat ID: ✅ Valid\n"
        "Backend: ✅ Starting\n"
        "Bybit: ⏳ Checking...\n"
        "Mode: 🟡 PAPER / SIGNAL ONLY\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Rulebook scanner is starting."
    )

    success = send_telegram(
        message
    )

    if success:

        print(
            "✅ Telegram message test successful."
        )

    else:

        print(
            "❌ Telegram message test failed."
        )

    return success


# ============================================================
# BYBIT PUBLIC SERVER TIME
# ============================================================

def check_bybit_server_time():

    try:

        data = bybit_get(
            "/v5/market/time",
            retries=3
        )

        result = data.get(
            "result",
            {}
        )

        print(
            "✅ Bybit server time OK | "
            f"timeSecond={result.get('timeSecond')}"
        )

        return True

    except Exception as e:

        log_exception(
            "BYBIT SERVER TIME CHECK FAILED",
            e
        )

        return False


# ============================================================
# BYBIT PUBLIC MARKET LOADING
# ============================================================

def load_bybit_public_markets():

    global SYMBOLS
    global BYBIT_SYMBOL_MAP
    global bybit_status
    global last_bybit_check
    global market_load_error

    print(
        "=========================================="
    )

    print(
        "📊 BYBIT PUBLIC MARKET LOADING"
    )

    print(
        f"Endpoint: "
        f"{BYBIT_BASE_URL}/v5/market/instruments-info"
    )

    print(
        f"Category: {BYBIT_CATEGORY}"
    )

    print(
        "=========================================="
    )

    try:

        check_bybit_server_time()

        data = bybit_get(
            "/v5/market/instruments-info",
            params={
                "category":
                    BYBIT_CATEGORY,
                "status":
                    "Trading"
            }
        )

        result = data.get(
            "result",
            {}
        )

        market_list = result.get(
            "list",
            []
        )

        if not market_list:

            raise RuntimeError(
                "Bybit returned zero spot markets."
            )

        available_native = {
            item.get("symbol")
            for item in market_list
            if item.get("symbol")
        }

        available = []

        new_map = {}

        for requested in REQUESTED_SYMBOLS:

            native_symbol = (
                requested
                .replace("/", "")
                .upper()
            )

            if native_symbol in available_native:

                available.append(
                    requested
                )

                new_map[
                    requested
                ] = native_symbol

                print(
                    f"✅ Market available: "
                    f"{requested} -> "
                    f"{native_symbol}"
                )

            else:

                print(
                    f"⚠️ Market unavailable: "
                    f"{requested}"
                )

        SYMBOLS = available

        BYBIT_SYMBOL_MAP = new_map

        if not SYMBOLS:

            raise RuntimeError(
                "None of REQUESTED_SYMBOLS "
                "are available on Bybit Spot."
            )

        bybit_status = "OK"

        last_bybit_check = time.time()

        market_load_error = None

        print(
            "=========================================="
        )

        print(
            "✅ BYBIT PUBLIC MARKETS LOADED"
        )

        print(
            f"Requested: "
            f"{len(REQUESTED_SYMBOLS)}"
        )

        print(
            f"Available: "
            f"{len(SYMBOLS)}"
        )

        print(
            f"Symbols: "
            f"{', '.join(SYMBOLS)}"
        )

        print(
            "=========================================="
        )

        return True

    except Exception as e:

        bybit_status = "ERROR"

        market_load_error = (
            f"{type(e).__name__}: {e}"
        )

        log_exception(
            "BYBIT PUBLIC MARKET LOAD FAILED",
            e
        )

        return False


# ============================================================
# BYBIT HEALTH CHECK
# ============================================================

def check_bybit_connection():

    global bybit_status
    global last_bybit_check
    global market_load_error

    try:

        data = bybit_get(
            "/v5/market/tickers",
            params={
                "category":
                    BYBIT_CATEGORY,
                "symbol":
                    "BTCUSDT"
            },
            retries=3
        )

        ticker_list = (
            data
            .get("result", {})
            .get("list", [])
        )

        if not ticker_list:

            raise RuntimeError(
                "BTCUSDT ticker response empty."
            )

        last_price = ticker_list[0].get(
            "lastPrice"
        )

        if not last_price:

            raise RuntimeError(
                "BTCUSDT lastPrice missing."
            )

        bybit_status = "OK"

        last_bybit_check = time.time()

        market_load_error = None

        print(
            "✅ Bybit public API health OK | "
            f"BTCUSDT={last_price}"
        )

        return True

    except Exception as e:

        bybit_status = "ERROR"

        market_load_error = (
            f"{type(e).__name__}: {e}"
        )

        log_exception(
            "BYBIT HEALTH CHECK FAILED",
            e
        )

        return False


# ============================================================
# BYBIT SYMBOL CONVERSION
# ============================================================

def native_symbol(symbol):

    native = BYBIT_SYMBOL_MAP.get(
        symbol
    )

    if native:

        return native

    return (
        symbol
        .replace("/", "")
        .upper()
    )


# ============================================================
# TIMEFRAME CONVERSION
# ============================================================

BYBIT_INTERVALS = {

    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",

    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",

    "1d": "D",
    "1w": "W",
    "1M": "M"
}


# ============================================================
# CANDLE DATA
# ============================================================

def fetch_candles(
    symbol,
    timeframe,
    limit=CANDLE_LIMIT
):

    native = native_symbol(
        symbol
    )

    interval = BYBIT_INTERVALS.get(
        timeframe
    )

    if interval is None:

        print(
            f"❌ Unsupported timeframe: "
            f"{timeframe}"
        )

        return []

    try:

        data = bybit_get(
            "/v5/market/kline",
            params={
                "category":
                    BYBIT_CATEGORY,
                "symbol":
                    native,
                "interval":
                    interval,
                "limit":
                    min(
                        int(limit),
                        1000
                    )
            }
        )

        rows = (
            data
            .get("result", {})
            .get("list", [])
        )

        if not rows:

            print(
                f"⚠️ Empty candles | "
                f"{symbol} | "
                f"{timeframe}"
            )

            return []

        candles = []

        for row in rows:

            if len(row) < 6:
                continue

            candles.append([
                int(row[0]),
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5])
            ])

        # Bybit returns newest first.
        # Strategy expects oldest -> newest.
        candles.reverse()

        return candles

    except Exception as e:

        print(
            f"❌ Candle error | "
            f"{symbol} | "
            f"{timeframe} | "
            f"{type(e).__name__}: {e}"
        )

        return []


def fetch_price(symbol):

    native = native_symbol(
        symbol
    )

    try:

        data = bybit_get(
            "/v5/market/tickers",
            params={
                "category":
                    BYBIT_CATEGORY,
                "symbol":
                    native
            },
            retries=3
        )

        rows = (
            data
            .get("result", {})
            .get("list", [])
        )

        if not rows:

            return None

        last = rows[0].get(
            "lastPrice"
        )

        if last is None:

            return None

        return float(last)

    except Exception as e:

        print(
            f"❌ Price error | "
            f"{symbol} | "
            f"{type(e).__name__}: {e}"
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

        h = candle_high(
            candles[i]
        )

        left_highs = [
            candle_high(
                candles[j]
            )
            for j in range(
                i - left,
                i
            )
        ]

        right_highs = [
            candle_high(
                candles[j]
            )
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

        l = candle_low(
            candles[i]
        )

        left_lows = [
            candle_low(
                candles[j]
            )
            for j in range(
                i - left,
                i
            )
        ]

        right_lows = [
            candle_low(
                candles[j]
            )
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

    highs = find_swing_highs(
        closed
    )

    lows = find_swing_lows(
        closed
    )

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

    bullish = trends.count(
        "BULLISH"
    )

    bearish = trends.count(
        "BEARISH"
    )

    if (
        bullish >= 2
        and bullish > bearish
    ):

        direction = "BULLISH"

    elif (
        bearish >= 2
        and bearish > bullish
    ):

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

    highs = find_swing_highs(
        closed
    )

    lows = find_swing_lows(
        closed
    )

    if direction == "BUY":

        if not highs:
            return False

        previous_high = highs[-1][1]

        return (
            candle_close(
                closed[-1]
            )
            > previous_high
        )

    if direction == "SELL":

        if not lows:
            return False

        previous_low = lows[-1][1]

        return (
            candle_close(
                closed[-1]
            )
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

    lows = find_swing_lows(
        candles
    )

    if len(lows) < 2:
        return False

    l1 = lows[-2][1]
    l2 = lows[-1][1]

    tolerance = abs(l1) * 0.002

    return abs(
        l1 - l2
    ) <= tolerance


def detect_double_top(candles):

    highs = find_swing_highs(
        candles
    )

    if len(highs) < 2:
        return False

    h1 = highs[-2][1]
    h2 = highs[-1][1]

    tolerance = abs(h1) * 0.002

    return abs(
        h1 - h2
    ) <= tolerance


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
                "low":
                    candle_low(base),

                "high":
                    candle_high(base),

                "index":
                    i
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
                "low":
                    candle_low(base),

                "high":
                    candle_high(base),

                "index":
                    i
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
                and body_ratio(following)
                >= 0.55
            ):

                return {
                    "low":
                        candle_low(current),

                    "high":
                        candle_high(current),

                    "index":
                        i
                }

        elif direction == "SELL":

            if (
                is_bullish(current)
                and is_bearish(following)
                and body_ratio(following)
                >= 0.55
            ):

                return {
                    "low":
                        candle_low(current),

                    "high":
                        candle_high(current),

                    "index":
                        i
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
            candle_close(c1)
            < zone["low"]
            and candle_close(c2)
            < zone["low"]
        )

    if direction == "SELL":

        return (
            candle_close(c1)
            > zone["high"]
            and candle_close(c2)
            > zone["high"]
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

    bias = get_mtf_bias(
        symbol
    )

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

        demand = detect_demand_zone(
            h1
        )

        tgl = detect_tgl(
            h1,
            "BUY"
        )

        level = (
            demand
            or tgl
        )

    else:

        supply = detect_supply_zone(
            h1
        )

        tgl = detect_tgl(
            h1,
            "SELL"
        )

        level = (
            supply
            or tgl
        )

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
    reasons.append(
        "MTF_STRUCTURE"
    )

    score += 25
    reasons.append(
        "VALID_LEVEL"
    )

    score += 25
    reasons.append(
        "1M_CHOCH"
    )

    score += 10
    reasons.append(
        "CANDLE_CONFIRMATION"
    )

    if detect_fvg(
        m15,
        direction
    ):

        score += 5

        reasons.append(
            "FVG"
        )

    if detect_liquidity_sweep(
        m15,
        direction
    ):

        score += 10

        reasons.append(
            "LIQUIDITY_SWEEP"
        )

    if direction == "BUY":

        if detect_double_bottom(
            m15
        ):

            score += 5

            reasons.append(
                "DOUBLE_BOTTOM"
            )

    else:

        if detect_double_top(
            m15
        ):

            score += 5

            reasons.append(
                "DOUBLE_TOP"
            )

    if score < MIN_SETUP_SCORE:

        return None

    # ========================================================
    # STRUCTURAL SL
    # ========================================================

    if direction == "BUY":

        swing_lows = find_swing_lows(
            m1
        )

        if swing_lows:

            swing_low = (
                swing_lows[-1][1]
            )

        else:

            swing_low = min(
                candle_low(c)
                for c in m1[-10:-1]
            )

        sl = (
            swing_low
            * 0.999
        )

        if sl >= price:

            return None

        risk = (
            price - sl
        )

    else:

        swing_highs = find_swing_highs(
            m1
        )

        if swing_highs:

            swing_high = (
                swing_highs[-1][1]
            )

        else:

            swing_high = max(
                candle_high(c)
                for c in m1[-10:-1]
            )

        sl = (
            swing_high
            * 1.001
        )

        if sl <= price:

            return None

        risk = (
            sl - price
        )

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

        m15_highs = find_swing_highs(
            m15
        )

        resistance = [
            h[1]
            for h in m15_highs
            if h[1] > price
        ]

        tp1 = (
            price
            + risk * 1.0
        )

        if resistance:

            tp2 = min(
                resistance
            )

            if (
                (tp2 - price)
                / risk
                < MIN_RR
            ):

                tp2 = (
                    price
                    + risk * MIN_RR
                )

        else:

            tp2 = (
                price
                + risk * 2.0
            )

    else:

        m15_lows = find_swing_lows(
            m15
        )

        support = [
            l[1]
            for l in m15_lows
            if l[1] < price
        ]

        tp1 = (
            price
            - risk * 1.0
        )

        if support:

            tp2 = max(
                support
            )

            if (
                (price - tp2)
                / risk
                < MIN_RR
            ):

                tp2 = (
                    price
                    - risk * MIN_RR
                )

        else:

            tp2 = (
                price
                - risk * 2.0
            )

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
        "direction":
            direction,

        "entry":
            price,

        "sl":
            sl,

        "tp1":
            tp1,

        "tp2":
            tp2,

        "rr":
            rr,

        "score":
            score,

        "reason":
            ", ".join(reasons),

        "bias":
            bias,

        "level":
            level
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

    previous = last_signal_key.get(
        key
    )

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

def has_active_trade(
    symbol
):

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

    if has_active_trade(
        symbol
    ):

        return

    with state_lock:

        if (
            len(active_trades)
            >= MAX_ACTIVE_TRADES
        ):

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

        active_trades.append(
            trade
        )

    icon = (
        "🟢"
        if trade["type"] == "BUY"
        else "🔴"
    )

    message = (

        f"<b>⚡ RULEBOOK SIGNAL {icon}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Asset:</b> {symbol}\n"
        f"📈 <b>Direction:</b> "
        f"{trade['type']}\n"
        f"💰 <b>Entry:</b> "
        f"{trade['entry']:.6f}\n"
        f"🛑 <b>SL:</b> "
        f"{trade['sl']:.6f}\n"
        f"🎯 <b>TP1:</b> "
        f"{trade['tp1']:.6f}\n"
        f"🎯 <b>TP2:</b> "
        f"{trade['tp2']:.6f}\n"
        f"📊 <b>RR:</b> "
        f"{trade['rr']:.2f}\n"
        f"⭐ <b>Score:</b> "
        f"{trade['score']}\n"
        f"🧠 <b>Reason:</b> "
        f"{trade['reason']}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>🟡 PAPER TRADE ONLY</b>\n"
        "No real Bybit order was placed."
    )

    telegram_sent = send_telegram(
        message
    )

    log_event(
        f"NEW {trade['type']} | "
        f"{symbol} | "
        f"Entry={trade['entry']:.6f} | "
        f"Score={trade['score']} | "
        f"RR={trade['rr']:.2f} | "
        f"Telegram="
        f"{'OK' if telegram_sent else 'FAILED'}"
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

    initial_sl = trade[
        "initial_sl"
    ]

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
        f"📍 <b>Asset:</b> "
        f"{trade['symbol']}\n"
        f"📈 <b>Type:</b> "
        f"{trade['type']}\n"
        f"📌 <b>Result:</b> "
        f"{result}\n"
        f"💰 <b>Entry:</b> "
        f"{entry:.6f}\n"
        f"🚪 <b>Exit:</b> "
        f"{exit_price:.6f}\n"
        f"📊 <b>R:</b> "
        f"{r_multiple:.2f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>🟡 PAPER TRADE</b>"
    )

    send_telegram(
        message
    )

    log_event(
        f"RESULT {result} | "
        f"{trade['symbol']} | "
        f"R={r_multiple:.2f}"
    )

    with state_lock:

        if trade in active_trades:

            active_trades.remove(
                trade
            )


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

            trade["sl"] = (
                trade["entry"]
            )

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

            trade["sl"] = (
                trade["entry"]
            )

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

def scan_symbol(
    symbol
):

    price = fetch_price(
        symbol
    )

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

    if has_active_trade(
        symbol
    ):

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

        log_exception(
            f"BUY setup error | {symbol}",
            e
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

        log_exception(
            f"SELL setup error | {symbol}",
            e
        )


# ============================================================
# HEALTH MONITOR
# ============================================================

def health_monitor():

    time.sleep(60)

    while True:

        try:

            telegram_ok = (
                check_telegram_without_message()
            )

            bybit_ok = (
                check_bybit_connection()
            )

            with state_lock:

                active_count = len(
                    active_trades
                )

            uptime_seconds = (
                time.time()
                - bot_started_at
            )

            uptime_hours = (
                uptime_seconds
                / 3600
            )

            scanner_ok = (
                scanner_status
                == "RUNNING"
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

                title = (
                    "🟢 BOT HEALTH CHECK"
                )

                final = (
                    "Backend is healthy. ✅"
                )

            else:

                title = (
                    "🔴 BOT HEALTH ALERT"
                )

                final = (
                    "⚠️ Check Render logs."
                )

            message = (

                f"<b>{title}</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"Telegram: "
                f"{telegram_text}\n"
                f"Bybit API: "
                f"{bybit_text}\n"
                f"Scanner: "
                f"{scanner_text}\n"
                f"Mode: 🟡 "
                f"{TRADING_MODE}\n"
                f"Symbols: "
                f"{len(SYMBOLS)}\n"
                f"Active Trades: "
                f"{active_count}\n"
                f"Uptime: "
                f"{uptime_hours:.1f} hours\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"{final}"
            )

            send_telegram(
                message
            )

        except Exception as e:

            log_exception(
                "Health monitor error",
                e
            )

        time.sleep(
            HEALTH_CHECK_INTERVAL
        )


# ============================================================
# STARTUP DIAGNOSTICS
# ============================================================

def startup_diagnostics():

    print(
        "=========================================="
    )

    print(
        "🔍 STARTUP DIAGNOSTICS"
    )

    print(
        "=========================================="
    )

    print(
        f"Bybit URL: "
        f"{BYBIT_BASE_URL}"
    )

    print(
        f"Bybit Category: "
        f"{BYBIT_CATEGORY}"
    )

    print(
        f"Telegram configured: "
        f"{'YES' if telegram_configured() else 'NO'}"
    )

    # Never print actual token/chat ID.

    if telegram_configured():

        print(
            "Telegram credentials: "
            "PRESENT"
        )

    else:

        print(
            "⚠️ Telegram credentials "
            "are incomplete."
        )

    print(
        f"Requested symbols: "
        f"{', '.join(REQUESTED_SYMBOLS)}"
    )

    print(
        f"Scan interval: "
        f"{SCAN_INTERVAL}s"
    )

    print(
        f"API timeout: "
        f"{API_TIMEOUT}s"
    )

    print(
        f"Bybit max retries: "
        f"{BYBIT_MAX_RETRIES}"
    )

    print(
        "Real orders: DISABLED"
    )

    print(
        "=========================================="
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

    startup_diagnostics()

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    telegram_connection_test()

    # --------------------------------------------------------
    # Bybit PUBLIC market loading
    # --------------------------------------------------------

    loaded = (
        load_bybit_public_markets()
    )

    retry_round = 0

    while not loaded:

        retry_round += 1

        scanner_status = (
            "BYBIT_ERROR"
        )

        error_text = (
            market_load_error
            or "Unknown Bybit error"
        )

        print(
            "=========================================="
        )

        print(
            "🔴 BYBIT MARKET LOAD FAILED"
        )

        print(
            f"Retry round: "
            f"{retry_round}"
        )

        print(
            f"Detailed error: "
            f"{error_text}"
        )

        print(
            "=========================================="
        )

        send_telegram(

            "<b>🔴 BOT STARTUP ALERT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Telegram: ✅ Checked\n"
            "Bybit: ❌ Market loading failed\n"
            "Scanner: ⚠️ Retrying\n"
            f"Error: <code>{error_text[:500]}</code>\n"
            "Mode: 🟡 PAPER / SIGNAL ONLY"
        )

        time.sleep(
            min(
                60,
                10 * retry_round
            )
        )

        loaded = (
            load_bybit_public_markets()
        )

    if not SYMBOLS:

        scanner_status = (
            "WAITING_FOR_SYMBOLS"
        )

        send_telegram(
            "<b>🔴 NO BYBIT SYMBOLS</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Public market API responded,\n"
            "but requested symbols were not found.\n"
            "Scanner stopped."
        )

        return

    # --------------------------------------------------------
    # Initial health check
    # --------------------------------------------------------

    check_bybit_connection()

    scanner_status = "RUNNING"

    send_telegram(

        "<b>🟢 SCANNER READY</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Telegram: ✅ Connected\n"
        "Bybit Public API: ✅ Connected\n"
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

            # Periodic public API health check.
            if (
                last_bybit_check is None
                or
                time.time()
                - last_bybit_check
                > 300
            ):

                if not check_bybit_connection():

                    scanner_status = (
                        "BYBIT_ERROR"
                    )

                    time.sleep(10)

                    continue

                scanner_status = (
                    "RUNNING"
                )

            for symbol in list(
                SYMBOLS
            ):

                try:

                    scan_symbol(
                        symbol
                    )

                except Exception as e:

                    log_exception(
                        f"Scanner error | "
                        f"{symbol}",
                        e
                    )

                time.sleep(1)

            last_scanner_run = (
                time.time()
            )

            scanner_status = (
                "RUNNING"
            )

            elapsed = (
                time.time()
                - started
            )

            print(
                f"🔄 Scanner cycle complete | "
                f"Symbols={len(SYMBOLS)} | "
                f"Time={elapsed:.1f}s"
            )

        except Exception as e:

            scanner_status = "ERROR"

            log_exception(
                "Main scanner error",
                e
            )

        elapsed = (
            time.time()
            - started
        )

        time.sleep(
            max(
                5,
                SCAN_INTERVAL
                - elapsed
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
            gross_profit
            / gross_loss
            if gross_loss > 0
            else 0
        )

        return {

            "total":
                total,

            "wins":
                wins,

            "losses":
                losses,

            "breakeven":
                breakeven,

            "win_rate":
                round(
                    win_rate,
                    2
                ),

            "total_r":
                round(
                    total_r,
                    2
                ),

            "profit_factor":
                round(
                    profit_factor,
                    2
                )
        }

    except Exception as e:

        print(
            "Statistics error:",
            repr(e)
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

        trades = list(
            active_trades
        )

    uptime = (
        time.time()
        - bot_started_at
    )

    return jsonify({

        "status":
            scanner_status,

        "mode":
            "PAPER",

        "real_orders_enabled":
            REAL_ORDERS_ENABLED,

        "exchange":
            "BYBIT",

        "bybit_base_url":
            BYBIT_BASE_URL,

        "bybit_category":
            BYBIT_CATEGORY,

        "telegram":
            telegram_status,

        "telegram_configured":
            telegram_configured(),

        "telegram_last_error":
            last_telegram_error,

        "bybit":
            bybit_status,

        "bybit_last_error":
            last_bybit_error,

        "bybit_request_count":
            bybit_request_count,

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

<meta
    http-equiv="refresh"
    content="15"
>

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

.info {
    color:#60a5fa;
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

.error-box {
    background:#450a0a;
    border:1px solid #991b1b;
    color:#fecaca;
    padding:15px;
    border-radius:10px;
    margin:20px 0;
    word-break:break-word;
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
<b class="online">
{{ scanner_status }}
</b>
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

<h3>Symbols</h3>

<p>
{{ symbols|length }}
</p>

</div>


<div class="card">

<h3>Active Trades</h3>

<p>
{{ active_trades|length }}
</p>

</div>


<div class="card">

<h3>Total Trades</h3>

<p>
{{ stats.total }}
</p>

</div>


<div class="card">

<h3>Wins</h3>

<p class="online">
{{ stats.wins }}
</p>

</div>


<div class="card">

<h3>Losses</h3>

<p class="error">
{{ stats.losses }}
</p>

</div>


<div class="card">

<h3>Win Rate</h3>

<p>
{{ stats.win_rate }}%
</p>

</div>


<div class="card">

<h3>Profit Factor</h3>

<p>
{{ stats.profit_factor }}
</p>

</div>

</div>


{% if market_load_error %}

<div class="error-box">

<b>⚠️ Last Bybit Error:</b>

<br><br>

{{ market_load_error }}

</div>

{% endif %}


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

<td>
<b>{{ trade.symbol }}</b>
</td>

<td>
{{ trade.type }}
</td>

<td>
{{ "%.6f"|format(trade.entry) }}
</td>

<td>
{{ "%.6f"|format(trade.sl) }}
</td>

<td>
{{ "%.6f"|format(trade.tp1) }}
</td>

<td>
{{ "%.6f"|format(trade.tp2) }}
</td>

<td>
{{ "%.2f"|format(trade.rr) }}
</td>

<td>
{{ trade.score }}
</td>

</tr>

{% else %}

<tr>

<td
    colspan="8"
    style="text-align:center;"
>

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

        trades = list(
            active_trades
        )

        logs = list(
            trade_logs
        )

    return render_template_string(
        HTML_TEMPLATE,
        active_trades=trades,
        trade_logs=logs,
        stats=stats,
        scanner_status=scanner_status,
        telegram_status=telegram_status,
        bybit_status=bybit_status,
        symbols=SYMBOLS,
        market_load_error=market_load_error
    )


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    return jsonify({

        "status":
            "ok",

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
        "Bybit: PUBLIC REST API"
    )

    print(
        "=========================================="
    )

    scanner_thread = threading.Thread(
        target=background_trading_scanner,
        daemon=True,
        name="RulebookScanner"
    )

    scanner_thread.start()

    health_thread = threading.Thread(
        target=health_monitor,
        daemon=True,
        name="HealthMonitor"
    )

    health_thread.start()

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
