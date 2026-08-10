import os
import json
import time
import threading
import requests
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify, request

# ============================================================
# APP SETUP
# ============================================================

app = Flask(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ============================================================
# TELEGRAM CONFIGURATION
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
# SETTINGS & PROFILES
# ============================================================

TIMEFRAME = "5M"
GRANULARITY = 300

SCAN_INTERVAL = 30
MARKET_CACHE_SECONDS = 20

MODE_PROFILES = {
    "HIGH_WINRATE": {
        "signal_cooldown_minutes": 8,
        "min_signal_score": 85,
        "min_direction_edge": 15,
        "base_rr": 1.60,
        "break_even_trigger_r": 0.80,
        "break_even_buffer_r": 0.04,
        "trail_trigger_r": 1.20,
        "trail_atr_multiplier": 0.80,
        "atr_sl_multiplier": 1.10,
        "min_net_rr_after_costs": 1.10,
        "min_adx_for_trend": 22,
        "optimal_rsi_buy_min": 55,
        "optimal_rsi_buy_max": 65,
        "optimal_rsi_sell_min": 35,
        "optimal_rsi_sell_max": 45,
        "min_volume_ratio": 1.30,
        "max_trades_per_hour_per_asset": 2,
    },
    "HIGH_FREQUENCY": {
        "signal_cooldown_minutes": 2,
        "min_signal_score": 62,
        "min_direction_edge": 6,
        "base_rr": 0.90,
        "break_even_trigger_r": 0.45,
        "break_even_buffer_r": 0.03,
        "trail_trigger_r": 0.70,
        "trail_atr_multiplier": 0.60,
        "atr_sl_multiplier": 0.70,
        "min_net_rr_after_costs": 0.60,
        "min_adx_for_trend": 18,
        "optimal_rsi_buy_min": 52,
        "optimal_rsi_buy_max": 70,
        "optimal_rsi_sell_min": 30,
        "optimal_rsi_sell_max": 48,
        "min_volume_ratio": 1.20,
        "max_trades_per_hour_per_asset": 6,
    },
}

ROUND_TRIP_FEE_PERCENT = 0.012
ROUND_TRIP_SLIPPAGE_PERCENT = 0.0015

_active_mode = os.getenv("SCALP_MODE", "HIGH_WINRATE")
if _active_mode not in MODE_PROFILES:
    _active_mode = "HIGH_WINRATE"

settings_lock = threading.Lock()

SETTINGS = {
    "mode": _active_mode,
    **MODE_PROFILES[_active_mode],
}

def switch_mode(new_mode):
    if new_mode not in MODE_PROFILES:
        return False
    with settings_lock:
        SETTINGS["mode"] = new_mode
        SETTINGS.update(MODE_PROFILES[new_mode])
    return True

def get_setting(key):
    with settings_lock:
        return SETTINGS[key]

# CIRCUIT BREAKER
MAX_CONSECUTIVE_LOSSES = 2
MAX_DAILY_LOSS_R = 2.5

# ============================================================
# ASSETS
# ============================================================

ASSETS = {
    "PAXG": {
        "product": "PAXG-USD",
        "display": "PAXGUSD",
        "lot": 0.03,
        "minimum_sl": 0.80
    },
    "BTC": {
        "product": "BTC-USD",
        "display": "BTCUSD",
        "lot": 0.01,
        "minimum_sl": 70.0
    }
}

# ============================================================
# STATE MANAGEMENT
# ============================================================

active_signals = []
latest_signal_data = {}

last_signal_time = {
    "PAXG": None,
    "BTC": None
}

recent_trade_times = {
    "PAXG": [],
    "BTC": [],
}

trade_history = {
    "total_signals": 0,
    "wins": 0,
    "losses": 0,
    "breakeven": 0,
    "win_rate": 0.0,
    "profit_factor": 0.0,
    "gross_profit_r": 0.0,
    "gross_loss_r": 0.0
}

circuit_breaker = {
    "paused": False,
    "reason": "",
    "consecutive_losses": 0,
    "daily_loss_r": 0.0,
    "day": None
}

market_cache = {}

state_lock = threading.Lock()
cache_lock = threading.Lock()

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "bot_state.json"
)

def save_state():
    try:
        with state_lock:
            data = {
                "trade_history": dict(trade_history),
                "circuit_breaker": dict(circuit_breaker)
            }
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("State save error:", e)

def load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        with state_lock:
            trade_history.update(data.get("trade_history", {}))
            circuit_breaker.update(data.get("circuit_breaker", {}))
        print("Loaded saved trade history.")
    except Exception as e:
        print("State load error:", e)

# ============================================================
# TELEGRAM HELPER (WITH STRICT TIMEOUT)
# ============================================================

def send_telegram_alert(message, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram configuration missing.")
        return None

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        # Timeout set to 5 seconds to avoid freezing threads
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            return response.json().get("result", {}).get("message_id")

        print("Telegram error:", response.status_code, response.text)
    except Exception as e:
        print("Telegram send error:", e)

    return None

def edit_telegram_alert(message_id, message, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print("Telegram edit error:", e)

# ============================================================
# COINBASE DATA FETCHING (OPTIMIZED TIMEOUTS)
# ============================================================

def fetch_coinbase_candles(asset, limit=120):
    if asset not in ASSETS:
        return None

    product_id = ASSETS[asset]["product"]
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?granularity={GRANULARITY}"
    headers = {"User-Agent": "5M-Scalping-Bot/1.0"}

    delay = 1
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, timeout=5)

            if response.status_code == 429:
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code != 200:
                return None

            raw = response.json()
            if not isinstance(raw, list):
                return None

            raw = sorted(raw, key=lambda x: x[0])
            now_ts = int(time.time())

            completed = [
                candle for candle in raw
                if (int(candle[0]) + GRANULARITY <= now_ts)
            ]

            if len(completed) < 60:
                return None

            completed = completed[-limit:]

            return {
                "opens": [float(x[3]) for x in completed],
                "highs": [float(x[2]) for x in completed],
                "lows": [float(x[1]) for x in completed],
                "closes": [float(x[4]) for x in completed],
                "volumes": [float(x[5]) for x in completed],
                "timestamps": [int(x[0]) for x in completed]
            }

        except Exception as e:
            time.sleep(delay)
            delay *= 2

    return None

def fetch_coinbase_ticker(asset):
    if asset not in ASSETS:
        return None

    product_id = ASSETS[asset]["product"]
    url = f"https://api.exchange.coinbase.com/products/{product_id}/ticker"
    headers = {"User-Agent": "5M-Scalping-Bot/1.0"}

    try:
        response = requests.get(url, headers=headers, timeout=4)
        if response.status_code == 200:
            data = response.json()
            price = data.get("price")
            if price is not None:
                return float(price)
    except Exception as e:
        print(f"Coinbase ticker error {asset}: {e}")
    return None

def get_market_data(asset, limit=120, force_refresh=False):
    now = time.time()
    with cache_lock:
        cached = market_cache.get(asset)
        if cached and not force_refresh and (now - cached["time"] < MARKET_CACHE_SECONDS):
            return cached["data"]

    data = fetch_coinbase_candles(asset, limit)
    if data:
        with cache_lock:
            market_cache[asset] = {
                "time": time.time(),
                "data": data
            }
        return data

    return None

# ============================================================
# TECHNICAL INDICATORS
# ============================================================

def calculate_ema(values, period):
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    result = [current]
    for value in values[period:]:
        current = ((value - current) * multiplier) + current
        result.append(current)
    return result

def calculate_rsi(values, period=14):
    if len(values) <= period:
        return []
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result = []

    for i in range(period - 1, len(gains)):
        if i >= period:
            avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
            avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

        if avg_loss == 0:
            value = 100.0
        else:
            rs = avg_gain / avg_loss
            value = 100 - (100 / (1 + rs))
        result.append(value)

    return result

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return []
    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return []

    current = sum(true_ranges[:period]) / period
    result = [current]
    for tr in true_ranges[period:]:
        current = ((current * (period - 1)) + tr) / period
        result.append(current)
    return result

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period * 2 + 5:
        return []
    tr_list, plus_dm, minus_dm = [], [], []

    for i in range(1, len(closes)):
        high_diff = highs[i] - highs[i - 1]
        low_diff = lows[i - 1] - lows[i]

        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        tr_list.append(tr)

        plus_dm.append(high_diff if (high_diff > low_diff and high_diff > 0) else 0)
        minus_dm.append(low_diff if (low_diff > high_diff and low_diff > 0) else 0)

    if len(tr_list) < period:
        return []

    atr_value = sum(tr_list[:period]) / period
    plus_value = sum(plus_dm[:period]) / period
    minus_value = sum(minus_dm[:period]) / period
    dx_values = []

    for i in range(period, len(tr_list)):
        atr_value = ((atr_value * (period - 1)) + tr_list[i]) / period
        plus_value = ((plus_value * (period - 1)) + plus_dm[i]) / period
        minus_value = ((minus_value * (period - 1)) + minus_dm[i]) / period

        if atr_value == 0:
            continue

        plus_di = 100 * plus_value / atr_value
        minus_di = 100 * minus_value / atr_value
        denominator = plus_di + minus_di

        dx = 0 if denominator == 0 else (100 * abs(plus_di - minus_di) / denominator)
        dx_values.append(dx)

    if len(dx_values) < period:
        return []

    adx = sum(dx_values[:period]) / period
    result = [adx]
    for dx in dx_values[period:]:
        adx = ((adx * (period - 1)) + dx) / period
        result.append(adx)

    return result

# ============================================================
# SWING POINTS & PATTERN RECOGNITION
# ============================================================

def find_swing_highs(highs, lookback=2):
    points = []
    for i in range(lookback, len(highs) - lookback):
        if highs[i] >= max(highs[i - lookback:i]) and highs[i] >= max(highs[i + 1:i + lookback + 1]):
            points.append((i, highs[i]))
    return points

def find_swing_lows(lows, lookback=2):
    points = []
    for i in range(lookback, len(lows) - lookback):
        if lows[i] <= min(lows[i - lookback:i]) and lows[i] <= min(lows[i + 1:i + lookback + 1]):
            points.append((i, lows[i]))
    return points

def detect_patterns(highs, lows, closes):
    patterns = []
    swing_highs = find_swing_highs(highs)
    swing_lows = find_swing_lows(lows)

    recent_highs = swing_highs[-5:]
    recent_lows = swing_lows[-5:]

    # DOUBLE TOP / BOTTOM
    if len(recent_highs) >= 2:
        h1, h2 = recent_highs[-2][1], recent_highs[-1][1]
        if abs(h1 - h2) <= abs(h1) * 0.003 and closes[-1] < h2:
            patterns.append(("DOUBLE_TOP", "SELL", 14))

    if len(recent_lows) >= 2:
        l1, l2 = recent_lows[-2][1], recent_lows[-1][1]
        if abs(l1 - l2) <= abs(l1) * 0.003 and closes[-1] > l2:
            patterns.append(("DOUBLE_BOTTOM", "BUY", 14))

    # TRIPLE TOP / BOTTOM
    if len(recent_highs) >= 3:
        values = [x[1] for x in recent_highs[-3:]]
        avg = sum(values) / 3
        if max(values) - min(values) <= avg * 0.004 and closes[-1] < avg:
            patterns.append(("TRIPLE_TOP", "SELL", 17))

    if len(recent_lows) >= 3:
        values = [x[1] for x in recent_lows[-3:]]
        avg = sum(values) / 3
        if max(values) - min(values) <= avg * 0.004 and closes[-1] > avg:
            patterns.append(("TRIPLE_BOTTOM", "BUY", 17))

    # HEAD AND SHOULDERS
    if len(recent_highs) >= 3:
        left, head, right = [x[1] for x in recent_highs[-3:]]
        if head > left and head > right and abs(left - right) <= abs(left) * 0.01 and closes[-1] < right:
            patterns.append(("HEAD_SHOULDERS", "SELL", 18))

    if len(recent_lows) >= 3:
        left, head, right = [x[1] for x in recent_lows[-3:]]
        if head < left and head < right and abs(left - right) <= abs(left) * 0.01 and closes[-1] > right:
            patterns.append(("INVERSE_HEAD_SHOULDERS", "BUY", 18))

    return patterns

# ============================================================
# PRICE ACTION CONFIRMATIONS
# ============================================================

def candle_confirmation(opens, highs, lows, closes, direction):
    candle_range = highs[-1] - lows[-1]
    if candle_range <= 0:
        return False

    body = abs(closes[-1] - opens[-1])
    body_ratio = body / candle_range
    upper_wick = highs[-1] - max(opens[-1], closes[-1])
    lower_wick = min(opens[-1], closes[-1]) - lows[-1]

    if direction == "BUY":
        return closes[-1] > opens[-1] and body_ratio >= 0.50 and upper_wick <= body * 0.75
    if direction == "SELL":
        return closes[-1] < opens[-1] and body_ratio >= 0.50 and lower_wick <= body * 0.75

    return False

def breakout_confirmation(highs, lows, closes, volumes, atr_value, direction):
    if len(closes) < 10:
        return False

    resistance = max(highs[-9:-1])
    support = min(lows[-9:-1])
    avg_volume = sum(volumes[-9:-1]) / 8

    if avg_volume <= 0:
        return False

    volume_ratio = volumes[-1] / avg_volume

    if direction == "BUY":
        return closes[-1] > (resistance + atr_value * 0.08) and volume_ratio >= 1.20
    if direction == "SELL":
        return closes[-1] < (support - atr_value * 0.08) and volume_ratio >= 1.20

    return False

def retest_confirmation(highs, lows, closes, direction):
    if len(closes) < 8:
        return False

    level_high = max(highs[-8:-2])
    level_low = min(lows[-8:-2])

    if direction == "BUY":
        return closes[-2] > level_high and lows[-1] >= (level_high * 0.999) and closes[-1] > closes[-2]
    if direction == "SELL":
        return closes[-2] < level_low and highs[-1] <= (level_low * 1.001) and closes[-1] < closes[-2]

    return False

def get_market_regime(price, ema9, ema21, adx):
    if adx < 18:
        return "CHOPPY"
    if ema9 > ema21 and price > ema9:
        return "BULLISH"
    if ema9 < ema21 and price < ema9:
        return "BEARISH"
    return "TRANSITION"

# ============================================================
# ANALYSIS & SIGNAL GENERATION
# ============================================================

def clean_old_trade_times(asset, now_ts):
    window = 3600
    times = recent_trade_times.get(asset, [])
    recent_trade_times[asset] = [t for t in times if now_ts - t <= window]

def trades_in_last_hour(asset):
    now_ts = time.time()
    clean_old_trade_times(asset, now_ts)
    return len(recent_trade_times.get(asset, []))

def record_trade_timestamp(asset):
    now_ts = time.time()
    clean_old_trade_times(asset, now_ts)
    recent_trade_times[asset].append(now_ts)

def analyze_asset(asset):
    global latest_signal_data

    with settings_lock:
        active_settings = dict(SETTINGS)

    market = get_market_data(asset, 120)
    if not market:
        return None

    opens, highs, lows, closes, volumes = (
        market["opens"], market["highs"], market["lows"], market["closes"], market["volumes"]
    )

    if len(closes) < 70:
        return None

    ema9_values = calculate_ema(closes, 9)
    ema21_values = calculate_ema(closes, 21)
    rsi_values = calculate_rsi(closes, 14)
    atr_values = calculate_atr(highs, lows, closes, 14)
    adx_values = calculate_adx(highs, lows, closes, 14)

    if not all([ema9_values, ema21_values, rsi_values, atr_values, adx_values]):
        return None

    price, ema9, ema21, rsi, atr, adx = (
        closes[-1], ema9_values[-1], ema21_values[-1], rsi_values[-1], atr_values[-1], adx_values[-1]
    )

    if atr <= 0:
        return None

    regime = get_market_regime(price, ema9, ema21, adx)
    if regime in ("CHOPPY", "TRANSITION"):
        return None

    if adx < active_settings.get("min_adx_for_trend", 20):
        return None

    if (highs[-1] - lows[-1]) > (atr * 2.5):
        return None

    avg_volume = sum(volumes[-9:-1]) / 8
    if avg_volume <= 0:
        return None

    volume_ratio = volumes[-1] / avg_volume
    if volume_ratio < active_settings.get("min_volume_ratio", 1.20):
        return None

    patterns = detect_patterns(highs, lows, closes)
    buy_score, sell_score = 0, 0
    buy_reasons, sell_reasons = [], []

    # SCORE CALCULATIONS
    if ema9 > ema21 and regime == "BULLISH":
        buy_score += 20
        buy_reasons.append("EMA9>EMA21")
    if ema9 < ema21 and regime == "BEARISH":
        sell_score += 20
        sell_reasons.append("EMA9<EMA21")

    if price > ema9 and regime == "BULLISH":
        buy_score += 10
        buy_reasons.append("Price above EMA9")
    if price < ema9 and regime == "BEARISH":
        sell_score += 10
        sell_reasons.append("Price below EMA9")

    opt_b_min = active_settings.get("optimal_rsi_buy_min", 52)
    opt_b_max = active_settings.get("optimal_rsi_buy_max", 68)
    opt_s_min = active_settings.get("optimal_rsi_sell_min", 32)
    opt_s_max = active_settings.get("optimal_rsi_sell_max", 48)

    if opt_b_min <= rsi <= opt_b_max and regime == "BULLISH":
        buy_score += 14
        buy_reasons.append("RSI sweet spot (buy)")
    elif 50 <= rsi <= 70 and regime == "BULLISH":
        buy_score += 8
        buy_reasons.append("RSI momentum (buy)")

    if opt_s_min <= rsi <= opt_s_max and regime == "BEARISH":
        sell_score += 14
        sell_reasons.append("RSI sweet spot (sell)")
    elif 30 <= rsi <= 50 and regime == "BEARISH":
        sell_score += 8
        sell_reasons.append("RSI momentum (sell)")

    if adx >= active_settings.get("min_adx_for_trend", 20):
        if regime == "BULLISH":
            buy_score += 14
            buy_reasons.append("Strong ADX trend")
        elif regime == "BEARISH":
            sell_score += 14
            sell_reasons.append("Strong ADX trend")

    if volume_ratio >= active_settings.get("min_volume_ratio", 1.20):
        if regime == "BULLISH":
            buy_score += 10
            buy_reasons.append(f"Volume {volume_ratio:.2f}x")
        elif regime == "BEARISH":
            sell_score += 10
            sell_reasons.append(f"Volume {volume_ratio:.2f}x")

    buy_candle = candle_confirmation(opens, highs, lows, closes, "BUY")
    sell_candle = candle_confirmation(opens, highs, lows, closes, "SELL")

    if buy_candle:
        buy_score += 8
        buy_reasons.append("Strong candle")
    if sell_candle:
        sell_score += 8
        sell_reasons.append("Strong candle")

    buy_breakout = breakout_confirmation(highs, lows, closes, volumes, atr, "BUY")
    sell_breakout = breakout_confirmation(highs, lows, closes, volumes, atr, "SELL")

    if buy_breakout:
        buy_score += 15
        buy_reasons.append("Breakout")
    if sell_breakout:
        sell_score += 15
        sell_reasons.append("Breakdown")

    buy_retest = retest_confirmation(highs, lows, closes, "BUY")
    sell_retest = retest_confirmation(highs, lows, closes, "SELL")

    if buy_retest:
        buy_score += 12
        buy_reasons.append("Retest confirmed")
    if sell_retest:
        sell_score += 12
        sell_reasons.append("Retest confirmed")

    detected_pattern = None
    for pattern, direction, points in patterns:
        if direction == "BUY":
            buy_score += points
            buy_reasons.append(pattern)
            if not detected_pattern:
                detected_pattern = pattern
        elif direction == "SELL":
            sell_score += points
            sell_reasons.append(pattern)
            if not detected_pattern:
                detected_pattern = pattern

    buy_confirmation = (buy_breakout and buy_candle) or buy_retest or any(p[1] == "BUY" for p in patterns)
    sell_confirmation = (sell_breakout and sell_candle) or sell_retest or any(p[1] == "SELL" for p in patterns)

    if trades_in_last_hour(asset) >= active_settings.get("max_trades_per_hour_per_asset", 3):
        return None

    action, score, reasons = None, 0, []

    if (
        buy_score >= active_settings["min_signal_score"]
        and buy_confirmation
        and buy_score >= sell_score
        and (buy_score - sell_score) >= active_settings["min_direction_edge"]
    ):
        action, score, reasons = "BUY", buy_score, buy_reasons

    elif (
        sell_score >= active_settings["min_signal_score"]
        and sell_confirmation
        and sell_score >= buy_score
        and (sell_score - buy_score) >= active_settings["min_direction_edge"]
    ):
        action, score, reasons = "SELL", sell_score, sell_reasons

    if not action:
        return None

    minimum_sl = ASSETS[asset]["minimum_sl"]
    sl_distance = max(atr * active_settings["atr_sl_multiplier"], minimum_sl)
    base_rr = active_settings["base_rr"]
    tp_distance = sl_distance * base_rr

    sl = (price - sl_distance) if action == "BUY" else (price + sl_distance)
    tp = (price + tp_distance) if action == "BUY" else (price - tp_distance)

    cost_percent = ROUND_TRIP_FEE_PERCENT + ROUND_TRIP_SLIPPAGE_PERCENT
    cost_in_r = (price * cost_percent) / sl_distance if sl_distance > 0 else 999
    net_rr = base_rr - cost_in_r

    if net_rr < active_settings["min_net_rr_after_costs"]:
        return None

    signal = {
        "asset_key": asset,
        "asset": ASSETS[asset]["display"],
        "action": action,
        "price": round(price, 2),
        "tp": round(tp, 2),
        "sl": round(sl, 2),
        "initial_sl": round(sl, 2),
        "risk_distance": round(sl_distance, 4),
        "net_rr_after_costs": round(net_rr, 2),
        "score": min(score, 100),
        "ema9": round(ema9, 2),
        "ema21": round(ema21, 2),
        "rsi": round(rsi, 2),
        "atr": round(atr, 4),
        "adx": round(adx, 2),
        "volume_ratio": round(volume_ratio, 2),
        "regime": regime,
        "pattern": detected_pattern or "BREAKOUT",
        "reasons": reasons[:10],
        "recommended_lot": ASSETS[asset]["lot"]
    }

    with state_lock:
        latest_signal_data = signal

    record_trade_timestamp(asset)
    return signal

# ============================================================
# TRADE CONTROLS & MANAGEMENT
# ============================================================

def has_active_signal(asset):
    with state_lock:
        return any(s["asset_key"] == asset for s in active_signals)

def cooldown_active(asset):
    with state_lock:
        last = last_signal_time.get(asset)
    if not last:
        return False
    return (datetime.now() - last) < timedelta(minutes=get_setting("signal_cooldown_minutes"))

def analyze_and_trigger(asset):
    with state_lock:
        if circuit_breaker["paused"] or cooldown_active(asset) or has_active_signal(asset):
            return

    signal = analyze_asset(asset)
    if not signal:
        return

    be_trigger_r = get_setting("break_even_trigger_r")
    be_buffer_r = get_setting("break_even_buffer_r")
    trail_trigger_r = get_setting("trail_trigger_r")
    trail_atr_multiplier = get_setting("trail_atr_multiplier")
    mode_label = get_setting("mode")

    signal_time = datetime.now(IST).strftime("%I:%M:%S %p | %d %b")
    chart_symbol = "PAXGUSD" if signal["asset"] == "PAXGUSD" else "COINBASE:BTCUSD"
    chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"

    reply_markup = {
        "inline_keyboard": [[{"text": "📈 TradingView Chart", "url": chart_link}]]
    }

    emoji = "🟢" if signal["action"] == "BUY" else "🔴"
    reasons = ", ".join(signal["reasons"])

    message = (
        "🎯 *5M SCALPING SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💹 *{signal['asset']}*\n"
        f"⏰ `{signal_time}`\n\n"
        f"{emoji} Action: *{signal['action']}*\n"
        f"💰 Entry: `{signal['price']}`\n"
        f"🎯 TP: `{signal['tp']}`\n"
        f"🛑 SL: `{signal['sl']}`\n\n"
        f"⭐ Score: *{signal['score']}/100*\n"
        f"💵 Net R:R (after fees): `{signal['net_rr_after_costs']}`\n"
        f"📊 Pattern: `{signal['pattern']}`\n"
        f"📈 EMA9: `{signal['ema9']}`\n"
        f"📉 EMA21: `{signal['ema21']}`\n"
        f"RSI: `{signal['rsi']}`\n"
        f"ADX: `{signal['adx']}`\n"
        f"ATR: `{signal['atr']}`\n"
        f"Volume: `{signal['volume_ratio']}x`\n"
        f"Regime: `{signal['regime']}`\n\n"
        f"🧠 *Confirmation:*\n`{reasons}`\n\n"
        f"📦 Lot: `{signal['recommended_lot']}`\n\n"
        f"🧬 Mode: `{mode_label}`\n"
        f"🛡️ BE: `+{be_trigger_r}R`\n"
        f"📈 Trail: `+{trail_trigger_r}R`\n\n"
        "Status: *ACTIVE ⏳*"
    )

    msg_id = send_telegram_alert(message, reply_markup)
    if not msg_id:
        return

    now = datetime.now()
    trade = {
        "msg_id": msg_id,
        "asset_key": asset,
        "asset": signal["asset"],
        "action": signal["action"],
        "price": signal["price"],
        "tp": signal["tp"],
        "sl": signal["sl"],
        "initial_sl": signal["initial_sl"],
        "risk_distance": signal["risk_distance"],
        "score": signal["score"],
        "pattern": signal["pattern"],
        "created_at": signal_time,
        "created_timestamp": now.isoformat(),
        "best_price": signal["price"],
        "breakeven_done": False,
        "trailing_active": False,
        "be_trigger_r": be_trigger_r,
        "be_buffer_r": be_buffer_r,
        "trail_trigger_r": trail_trigger_r,
        "trail_atr_multiplier": trail_atr_multiplier,
        "mode": mode_label
    }

    with state_lock:
        active_signals.append(trade)
        last_signal_time[asset] = now

def update_trade_management(signal, current_price, current_atr):
    entry = signal["price"]
    risk = signal["risk_distance"]
    if risk <= 0:
        return

    is_buy = (signal["action"] == "BUY")
    favorable_move = (current_price - entry) if is_buy else (entry - current_price)
    current_r = favorable_move / risk

    if is_buy:
        signal["best_price"] = max(signal["best_price"], current_price)
    else:
        signal["best_price"] = min(signal["best_price"], current_price)

    # BREAK EVEN
    if not signal["breakeven_done"] and current_r >= signal["be_trigger_r"]:
        buffer = risk * signal["be_buffer_r"]
        new_sl = (entry - buffer) if is_buy else (entry + buffer)
        if (is_buy and new_sl > signal["sl"]) or (not is_buy and new_sl < signal["sl"]):
            signal["sl"] = round(new_sl, 2)
        signal["breakeven_done"] = True

    # TRAILING
    if signal["breakeven_done"] and current_r >= signal["trail_trigger_r"]:
        trail_distance = current_atr * signal["trail_atr_multiplier"]
        new_sl = (signal["best_price"] - trail_distance) if is_buy else (signal["best_price"] + trail_distance)
        if (is_buy and new_sl > signal["sl"]) or (not is_buy and new_sl < signal["sl"]):
            signal["sl"] = round(new_sl, 2)
            signal["trailing_active"] = True

def calculate_result_r(signal, exit_price):
    risk = signal["risk_distance"]
    if risk <= 0:
        return 0.0
    return ((exit_price - signal["price"]) / risk) if signal["action"] == "BUY" else ((signal["price"] - exit_price) / risk)

def update_history(result, r_value):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    should_pause = False
    pause_reason = ""

    with state_lock:
        if circuit_breaker["day"] != today:
            circuit_breaker["day"] = today
            circuit_breaker["daily_loss_r"] = 0.0

        if result == "WIN":
            trade_history["wins"] += 1
            trade_history["gross_profit_r"] += max(r_value, 0)
            circuit_breaker["consecutive_losses"] = 0
        elif result == "LOSS":
            trade_history["losses"] += 1
            loss_amount = abs(min(r_value, 0))
            trade_history["gross_loss_r"] += loss_amount
            circuit_breaker["consecutive_losses"] += 1
            circuit_breaker["daily_loss_r"] += loss_amount
        else:
            trade_history["breakeven"] += 1

        total = trade_history["wins"] + trade_history["losses"] + trade_history["breakeven"]
        trade_history["total_signals"] = total
        decisive = trade_history["wins"] + trade_history["losses"]

        if decisive > 0:
            trade_history["win_rate"] = round((trade_history["wins"] / decisive) * 100, 2)

        gross_profit = trade_history["gross_profit_r"]
        gross_loss = trade_history["gross_loss_r"]
        if gross_loss > 0:
            trade_history["profit_factor"] = round(gross_profit / gross_loss, 2)

        if not circuit_breaker["paused"] and circuit_breaker["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES:
            should_pause = True
            pause_reason = f"{MAX_CONSECUTIVE_LOSSES} consecutive losses"
        elif not circuit_breaker["paused"] and circuit_breaker["daily_loss_r"] >= MAX_DAILY_LOSS_R:
            should_pause = True
            pause_reason = f"Daily loss limit hit ({round(circuit_breaker['daily_loss_r'], 2)}R)"

        if should_pause:
            circuit_breaker["paused"] = True
            circuit_breaker["reason"] = pause_reason

    if should_pause:
        send_telegram_alert(
            "🛑 *CIRCUIT BREAKER TRIGGERED*\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Reason: `{pause_reason}`\n\n"
            "New signals are *paused*. Review the market/strategy, then call `/api/resume` to continue."
        )
        save_state()

# ============================================================
# BACKGROUND WORKERS
# ============================================================

def monitor_active_trades():
    while True:
        try:
            with state_lock:
                current_active = list(active_signals)

            for signal in current_active:
                asset = signal["asset_key"]
                live_price = fetch_coinbase_ticker(asset)
                market = get_market_data(asset, 40)

                if not market:
                    continue

                closes = market["closes"]
                if not closes:
                    continue

                current_price = live_price if live_price is not None else closes[-1]
                atr_values = calculate_atr(market["highs"], market["lows"], closes, 14)
                current_atr = atr_values[-1] if atr_values else signal["risk_distance"]

                update_trade_management(signal, current_price, current_atr)

                is_buy = (signal["action"] == "BUY")
                tp_hit = (current_price >= signal["tp"]) if is_buy else (current_price <= signal["tp"])
                sl_hit = (current_price <= signal["sl"]) if is_buy else (current_price >= signal["sl"])

                if not (tp_hit or sl_hit):
                    continue

                if tp_hit and sl_hit:
                    exit_price = signal["sl"]
                    result = "LOSS"
                    status = "⚠️ TP & SL same candle — counted as LOSS"
                elif tp_hit:
                    exit_price = signal["tp"]
                    result = "WIN"
                    status = "✅ TARGET HIT 🎯"
                else:
                    exit_price = signal["sl"]
                    r_val = calculate_result_r(signal, exit_price)
                    if signal["breakeven_done"] and r_val >= -0.10:
                        result = "BREAKEVEN"
                        status = "🛡️ BREAK-EVEN"
                    else:
                        result = "LOSS"
                        status = "❌ STOP LOSS"

                r_value = calculate_result_r(signal, exit_price)
                update_history(result, r_value)

                chart_symbol = "PAXGUSD" if signal["asset"] == "PAXGUSD" else "COINBASE:BTCUSD"
                chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"
                reply_markup = {
                    "inline_keyboard": [[{"text": "📈 TradingView Chart", "url": chart_link}]]
                }

                message = (
                    "🎯 *5M SCALPING RESULT*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"💹 *{signal['asset']}*\n"
                    f"📊 Pattern: `{signal['pattern']}`\n\n"
                    f"{status}\n\n"
                    f"Action: *{signal['action']}*\n"
                    f"Entry: `{signal['price']}`\n"
                    f"Exit: `{round(exit_price, 2)}`\n"
                    f"TP: `{signal['tp']}`\n"
                    f"Final SL: `{signal['sl']}`\n\n"
                    f"Result: `{round(r_value, 2)}R`\n"
                    f"BE: `{'YES' if signal['breakeven_done'] else 'NO'}`\n"
                    f"Trailing: `{'YES' if signal['trailing_active'] else 'NO'}`\n\n"
                    "📊 *Performance*\n"
                    f"Trades: `{trade_history['total_signals']}`\n"
                    f"Wins: `{trade_history['wins']}`\n"
                    f"Losses: `{trade_history['losses']}`\n"
                    f"BE: `{trade_history['breakeven']}`\n"
                    f"Win Rate: *{trade_history['win_rate']}%*\n"
                    f"Profit Factor: `{trade_history['profit_factor']}`"
                )

                edit_telegram_alert(signal["msg_id"], message, reply_markup)

                with state_lock:
                    if signal in active_signals:
                        active_signals.remove(signal)

        except Exception as e:
            print("Monitor error:", e)

        time.sleep(10)

def continuous_auto_scanner():
    while True:
        try:
            for asset in ("PAXG", "BTC"):
                analyze_and_trigger(asset)
                time.sleep(2)
        except Exception as e:
            print("Scanner error:", e)

        time.sleep(SCAN_INTERVAL)

# ============================================================
# API ENDPOINTS (FAST & NON-BLOCKING)
# ============================================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "5M Scalping Bot Active",
        "mode": get_setting("mode"),
        "assets": ["PAXGUSD", "BTCUSD"],
        "timeframe": "5M",
        "filters": [
            "EMA 9/21", "RSI", "ADX", "ATR", "Relative Volume",
            "Breakout", "Retest", "Candle Confirmation", "Chart Patterns",
            "Choppy Market Filter", "Volatility Spike Guard", "Fee/Slippage Filter",
            "Break Even", "Trailing Stop", "Circuit Breaker", "Hourly Trade Cap"
        ]
    })

@app.route("/api/mode", methods=["GET", "POST"])
def mode_endpoint():
    if request.method == "GET":
        return jsonify({
            "mode": get_setting("mode"),
            "available_modes": list(MODE_PROFILES.keys()),
            "settings": {k: get_setting(k) for k in MODE_PROFILES[get_setting("mode")]}
        })

    body = request.get_json(silent=True) or {}
    new_mode = body.get("mode", "").upper()

    if not switch_mode(new_mode):
        return jsonify({
            "status": "error",
            "message": "Invalid mode. Choose one of: " + ", ".join(MODE_PROFILES.keys())
        }), 400

    send_telegram_alert(
        f"🧬 *Scalp mode switched to* `{new_mode}`\n"
        "Note: this only affects NEW signals — trades already open keep their original BE/trailing rules."
    )

    return jsonify({"status": "switched", "mode": new_mode})

@app.route("/api/stats", methods=["GET"])
def get_stats():
    with state_lock:
        return jsonify({
            "performance": dict(trade_history),
            "active_signals_count": len(active_signals)
        }), 200

@app.route("/api/latest_signal", methods=["GET"])
def get_latest_signal():
    with state_lock:
        return jsonify({
            "status": "success",
            "latest_signal": dict(latest_signal_data),
            "active_signals": list(active_signals)
        }), 200

@app.route("/api/resume", methods=["POST", "GET"])
def resume_bot():
    with state_lock:
        circuit_breaker["paused"] = False
        circuit_breaker["reason"] = ""
        circuit_breaker["consecutive_losses"] = 0

    save_state()
    send_telegram_alert("✅ *Bot resumed* — new signals will generate again.")
    return jsonify({"status": "resumed"}), 200

@app.route("/api/health", methods=["GET"])
def health():
    with state_lock:
        breaker_status = dict(circuit_breaker)
        active_count = len(active_signals)

    return jsonify({
        "status": "healthy",
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "assets": ["PAXGUSD", "BTCUSD"],
        "timeframe": "5M",
        "active_signals": active_count,
        "circuit_breaker": breaker_status
    }), 200

# ============================================================
# STARTUP DAEMON THREADS
# ============================================================

load_state()

threading.Thread(target=continuous_auto_scanner, daemon=True).start()
threading.Thread(target=monitor_active_trades, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
