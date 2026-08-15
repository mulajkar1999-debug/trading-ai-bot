import os
import sys
import time
import json
import sqlite3
import threading
import requests
from flask import Flask, jsonify

# ============================================================
# APP & ENVIRONMENT CONFIGURATION
# ============================================================
app = Flask(__name__)

TRADING_MODE = os.getenv("TRADING_MODE", "PAPER / SIGNAL ONLY")
REAL_ORDERS_ENABLED = os.getenv("REAL_ORDERS_ENABLED", "false").lower() == "true"
RENDER_DIAGNOSTICS = os.getenv("RENDER_DIAGNOSTICS", "true").lower() == "true"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Indicators Setup
EMA_FAST = int(os.getenv("EMA_FAST", 9))
EMA_SLOW = int(os.getenv("EMA_SLOW", 21))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", 14))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", 14))
VOLUME_PERIOD = int(os.getenv("VOLUME_PERIOD", 20))

USE_EMA_FILTER = os.getenv("USE_EMA_FILTER", "true").lower() == "true"
USE_RSI_FILTER = os.getenv("USE_RSI_FILTER", "true").lower() == "true"
USE_ATR_FILTER = os.getenv("USE_ATR_FILTER", "true").lower() == "true"
USE_VOLUME_FILTER = os.getenv("USE_VOLUME_FILTER", "true").lower() == "true"

# ============================================================
# BINANCE EXCHANGE CONFIGURATION
# ============================================================
BINANCE_BASE_URL = "https://api.binance.com"

REQUESTED_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT"]
SYMBOLS = []

binance_status = "INITIALIZING"
binance_request_count = 0
binance_403_count = 0
binance_429_count = 0
binance_5xx_count = 0
binance_last_http_status = None
binance_last_request_at = None
binance_last_success_at = None
binance_last_url = ""
last_binance_error = None
fatal_binance_error = False
fatal_binance_error_at = None
market_load_error = None

# Scanner State Tracking
scanner_status = "IDLE"
scanner_cycle_count = 0
scanner_error_count = 0
last_scanner_run = None
scanner_last_duration = None
startup_attempts = 0
startup_failed_reason = None

# Telegram State
telegram_status = "UNCONFIGURED"
last_telegram_check = None
last_telegram_error = None

# Process & Thread Safety
process_pid = os.getpid()
bot_started_at = time.time()
state_lock = threading.Lock()
active_trades = []
render_instance = os.getenv("RENDER_INSTANCE_ID", "local")

# ============================================================
# DATABASE SETUP
# ============================================================
def init_db():
    conn = sqlite3.connect("trading_bot.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            side TEXT,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

# ============================================================
# TELEGRAM HELPER
# ============================================================
def telegram_configured():
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

def send_telegram_message(message):
    global telegram_status, last_telegram_check, last_telegram_error
    if not telegram_configured():
        telegram_status = "NOT_CONFIGURED"
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        last_telegram_check = time.time()
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            telegram_status = "OK"
            return True
        else:
            telegram_status = f"HTTP_{resp.status_code}"
            last_telegram_error = resp.text
            return False
    except Exception as e:
        telegram_status = "ERROR"
        last_telegram_error = str(e)
        return False

# ============================================================
# BINANCE MARKET DATA FETCHING
# ============================================================
def binance_get_klines(symbol, interval="15m", limit=100):
    global binance_request_count, binance_last_http_status, binance_last_request_at
    global binance_last_success_at, binance_last_url, last_binance_error
    global binance_403_count, binance_429_count, binance_5xx_count

    url = f"{BINANCE_BASE_URL}/api/v3/klines"
    params = {
        "symbol": symbol.replace("/", ""),
        "interval": interval,
        "limit": limit
    }

    binance_request_count += 1
    binance_last_request_at = time.time()
    binance_last_url = url

    try:
        response = requests.get(url, params=params, timeout=10)
        binance_last_http_status = response.status_code

        if response.status_code == 200:
            binance_last_success_at = time.time()
            data = response.json()
            
            parsed_candles = []
            for c in data:
                parsed_candles.append({
                    "timestamp": int(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5])
                })
            return parsed_candles

        elif response.status_code == 403:
            binance_403_count += 1
        elif response.status_code == 429:
            binance_429_count += 1
        elif response.status_code >= 500:
            binance_5xx_count += 1

        last_binance_error = f"BINANCE HTTP {response.status_code} | Response={response.text}"
        return None

    except Exception as e:
        last_binance_error = f"BINANCE FETCH EXCEPTION: {str(e)}"
        return None

# ============================================================
# SMC TRADING SCANNER ENGINE
# ============================================================
def background_trading_scanner():
    global scanner_status, scanner_cycle_count, scanner_error_count
    global last_scanner_run, scanner_last_duration, SYMBOLS, binance_status

    scanner_status = "RUNNING"
    SYMBOLS = REQUESTED_SYMBOLS
    binance_status = "CONNECTED"

    if telegram_configured():
        send_telegram_message("🤖 <b>Rulebook SMC Bot Started Successfully (Binance Engine)</b>")

    while True:
        cycle_start = time.time()
        scanner_cycle_count += 1
        last_scanner_run = cycle_start

        try:
            for symbol in SYMBOLS:
                candles = binance_get_klines(symbol, interval="15m", limit=100)
                if candles and len(candles) >= 50:
                    # Basic Indicator logic placeholder
                    latest_close = candles[-1]["close"]
                    prev_close = candles[-2]["close"]

                    # Example condition (Placeholder logic for SMC trigger)
                    if latest_close > prev_close * 1.02:
                        msg = f"🚀 <b>SIGNAL DETECTED</b>\nSymbol: {symbol}\nPrice: {latest_close}\nSide: BUY"
                        send_telegram_message(msg)

        except Exception as e:
            scanner_error_count += 1

        scanner_last_duration = round(time.time() - cycle_start, 2)
        time.sleep(60)  # Run scanner every 60 seconds

def health_monitor():
    while True:
        # Periodic internal health checks
        time.sleep(300)

# ============================================================
# FLASK WEB ROUTES & RENDER DIAGNOSTICS
# ============================================================

@app.route("/")
def home():
    return jsonify({
        "status": "ONLINE",
        "bot": "Rulebook SMC Trading Bot v2.0",
        "exchange": "Binance Spot"
    })

@app.route("/api/diagnostics")
def api_diagnostics():
    uptime_seconds = time.time() - bot_started_at

    with state_lock:
        active_count = len(active_trades)

    scanner_thread = next(
        (t for t in threading.enumerate() if t.name == "RulebookScanner"),
        None
    )

    health_thread = next(
        (t for t in threading.enumerate() if t.name == "HealthMonitor"),
        None
    )

    return jsonify({
        "application": {
            "name": "Rulebook SMC Trading Bot",
            "version": "2.0",
            "mode": TRADING_MODE,
            "real_orders_enabled": REAL_ORDERS_ENABLED
        },
        "process": {
            "pid": process_pid,
            "uptime_seconds": round(uptime_seconds, 2),
            "uptime_hours": round(uptime_seconds / 3600, 2)
        },
        "render": {
            "instance": render_instance,
            "port": os.getenv("PORT", "5000"),
            "diagnostics_enabled": RENDER_DIAGNOSTICS
        },
        "scanner": {
            "status": scanner_status,
            "thread_alive": bool(scanner_thread and scanner_thread.is_alive()),
            "cycle_count": scanner_cycle_count,
            "error_count": scanner_error_count,
            "last_run": last_scanner_run,
            "last_duration": scanner_last_duration,
            "startup_attempts": startup_attempts,
            "startup_failed_reason": startup_failed_reason
        },
        "binance": {
            "base_url": BINANCE_BASE_URL,
            "status": binance_status,
            "fatal_error": fatal_binance_error,
            "fatal_error_at": fatal_binance_error_at,
            "last_http_status": binance_last_http_status,
            "last_request_at": binance_last_request_at,
            "last_success_at": binance_last_success_at,
            "request_count": binance_request_count,
            "http_403_count": binance_403_count,
            "http_429_count": binance_429_count,
            "http_5xx_count": binance_5xx_count,
            "last_url": binance_last_url,
            "last_error": last_binance_error,
            "market_load_error": market_load_error
        },
        "telegram": {
            "status": telegram_status,
            "configured": telegram_configured(),
            "last_check": last_telegram_check,
            "last_error": last_telegram_error
        },
        "markets": {
            "requested": REQUESTED_SYMBOLS,
            "loaded": SYMBOLS,
            "count": len(SYMBOLS)
        },
        "indicators": {
            "ema_fast": EMA_FAST,
            "ema_slow": EMA_SLOW,
            "rsi_period": RSI_PERIOD,
            "atr_period": ATR_PERIOD,
            "volume_period": VOLUME_PERIOD,
            "ema_filter": USE_EMA_FILTER,
            "rsi_filter": USE_RSI_FILTER,
            "atr_filter": USE_ATR_FILTER,
            "volume_filter": USE_VOLUME_FILTER
        }
    })

# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    init_db()

    # Start background scanner thread
    t_scanner = threading.Thread(
        target=background_trading_scanner,
        name="RulebookScanner",
        daemon=True
    )
    t_scanner.start()

    # Start health monitor background thread
    t_health = threading.Thread(
        target=health_monitor,
        name="HealthMonitor",
        daemon=True
    )
    t_health.start()

    # Run Web Server
    port = int(os.getenv("PORT", "5000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )
