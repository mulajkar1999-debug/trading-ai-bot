import os
import time
import threading
import requests
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify


# ============================================================
# CONFIGURATION
# ============================================================

app = Flask(__name__)

# Render Environment Variables
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1317739622")

TIMEFRAME = "5m"
GRANULARITY = 300

# Scan every 30 seconds.
# Market data is cached, so this does NOT mean 2 API calls
# every 30 seconds.
SCAN_INTERVAL = 30

# Minimum signal score.
# 70 = more opportunities
# 75 = balanced
# 80+ = stricter
MIN_SIGNAL_SCORE = 75

# Signal cooldown
SIGNAL_COOLDOWN_MINUTES = 5

# Cache market data
MARKET_CACHE_SECONDS = 20

# Break-even settings
# When trade reaches this multiple of initial risk,
# move SL to entry.
BREAK_EVEN_TRIGGER_R = 0.65

# Small buffer after break-even to cover fees/slippage.
BREAK_EVEN_BUFFER_R = 0.05

# Trailing activates after this R.
TRAIL_TRIGGER_R = 1.0

# Trail distance as ATR multiplier.
TRAIL_ATR_MULTIPLIER = 0.75

# Risk/reward target.
BASE_RR = 1.40

IST = pytz.timezone("Asia/Kolkata")


# ============================================================
# GLOBAL STATE
# ============================================================

active_signals = []
latest_signal_data = {}

last_signal_time = {
    "PAXG": None,
    "BTC": None
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

market_cache = {}

state_lock = threading.Lock()
cache_lock = threading.Lock()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_alert(message, reply_markup=None):

    if not TELEGRAM_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN missing.")
        return None

    try:

        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }

        if reply_markup:
            payload["reply_markup"] = reply_markup

        response = requests.post(
            url,
            json=payload,
            timeout=10
        )

        if response.status_code == 200:
            return response.json()["result"]["message_id"]

        print(
            "Telegram Send Error:",
            response.status_code,
            response.text
        )

    except Exception as e:
        print("Telegram Send Exception:", e)

    return None


def edit_telegram_alert(
    message_id,
    message,
    reply_markup=None
):

    if not TELEGRAM_BOT_TOKEN:
        return

    try:

        url = (
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        )

        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }

        if reply_markup:
            payload["reply_markup"] = reply_markup

        response = requests.post(
            url,
            json=payload,
            timeout=10
        )

        if response.status_code != 200:
            print(
                "Telegram Edit Error:",
                response.text
            )

    except Exception as e:
        print("Telegram Edit Exception:", e)


# ============================================================
# MARKET DATA
# ============================================================

def get_product_id(asset):

    if asset == "PAXG":
        return "PAXG-USD"

    if asset == "BTC":
        return "BTC-USD"

    return None


def fetch_coinbase_candles(
    asset,
    limit=100
):

    product_id = get_product_id(asset)

    if not product_id:
        return None

    url = (
        f"https://api.exchange.coinbase.com/"
        f"products/{product_id}/candles"
        f"?granularity={GRANULARITY}"
    )

    headers = {
        "User-Agent": "5M-Scalping-Bot/1.0"
    }

    max_retries = 4
    delay = 2

    for attempt in range(max_retries):

        try:

            response = requests.get(
                url,
                headers=headers,
                timeout=10
            )

            # Rate limit
            if response.status_code == 429:

                print(
                    f"Coinbase 429 for {product_id}. "
                    f"Retrying in {delay}s..."
                )

                time.sleep(delay)

                delay *= 2

                continue

            if response.status_code != 200:

                print(
                    f"Coinbase error "
                    f"{product_id}: "
                    f"{response.status_code}"
                )

                return None

            data = response.json()

            if not isinstance(data, list):
                return None

            if len(data) < 30:
                return None

            data = sorted(
                data,
                key=lambda x: x[0]
            )

            now_ts = int(time.time())

            completed = []

            for candle in data:

                candle_time = int(candle[0])

                # Ignore currently forming candle
                if (
                    candle_time
                    + GRANULARITY
                    <= now_ts
                ):
                    completed.append(candle)

            if len(completed) < 30:
                return None

            completed = completed[-limit:]

            return {
                "opens": [
                    float(x[3])
                    for x in completed
                ],

                "highs": [
                    float(x[2])
                    for x in completed
                ],

                "lows": [
                    float(x[1])
                    for x in completed
                ],

                "closes": [
                    float(x[4])
                    for x in completed
                ],

                "volumes": [
                    float(x[5])
                    for x in completed
                ],

                "timestamps": [
                    int(x[0])
                    for x in completed
                ]
            }

        except requests.RequestException as e:

            print(
                f"Coinbase request error "
                f"{product_id}: {e}"
            )

            time.sleep(delay)

            delay *= 2

        except Exception as e:

            print(
                f"Coinbase fetch exception "
                f"{product_id}: {e}"
            )

            return None

    return None


def get_market_klines(
    asset,
    limit=100,
    force_refresh=False
):

    now = time.time()

    with cache_lock:

        cached = market_cache.get(asset)

        if cached and not force_refresh:

            age = now - cached["time"]

            if age < MARKET_CACHE_SECONDS:

                return cached["data"]

    data = fetch_coinbase_candles(
        asset,
        limit
    )

    if data:

        with cache_lock:

            market_cache[asset] = {
                "time": time.time(),
                "data": data
            }

    return data


# ============================================================
# INDICATORS
# ============================================================

def calculate_ema(values, period):

    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    first_value = (
        sum(values[:period])
        / period
    )

    result = [first_value]

    previous = first_value

    for price in values[period:]:

        current = (
            (price - previous)
            * multiplier
            + previous
        )

        result.append(current)

        previous = current

    return result


def calculate_rsi(
    values,
    period=14
):

    if len(values) <= period:
        return []

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = (
            values[i]
            - values[i - 1]
        )

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    result = []

    for i in range(
        period - 1,
        len(gains)
    ):

        if i >= period:

            avg_gain = (
                (
                    avg_gain
                    * (period - 1)
                )
                + gains[i]
            ) / period

            avg_loss = (
                (
                    avg_loss
                    * (period - 1)
                )
                + losses[i]
            ) / period

        if avg_loss == 0:

            result.append(100.0)

        else:

            rs = (
                avg_gain
                / avg_loss
            )

            result.append(
                100
                - (
                    100
                    / (1 + rs)
                )
            )

    return result


def calculate_atr(
    highs,
    lows,
    closes,
    period=14
):

    if len(closes) <= period:
        return []

    true_ranges = []

    for i in range(1, len(closes)):

        tr = max(

            highs[i] - lows[i],

            abs(
                highs[i]
                - closes[i - 1]
            ),

            abs(
                lows[i]
                - closes[i - 1]
            )
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return []

    initial_atr = (
        sum(
            true_ranges[:period]
        )
        / period
    )

    result = [initial_atr]

    previous = initial_atr

    for tr in true_ranges[period:]:

        current = (
            (
                previous
                * (period - 1)
            )
            + tr
        ) / period

        result.append(current)

        previous = current

    return result


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze_asset(asset):

    global latest_signal_data

    market = get_market_klines(
        asset,
        100
    )

    if not market:
        return None

    opens = market["opens"]
    highs = market["highs"]
    lows = market["lows"]
    closes = market["closes"]
    volumes = market["volumes"]

    if len(closes) < 50:
        return None

    ema9 = calculate_ema(
        closes,
        9
    )

    ema21 = calculate_ema(
        closes,
        21
    )

    rsi = calculate_rsi(
        closes,
        14
    )

    atr = calculate_atr(
        highs,
        lows,
        closes,
        14
    )

    if not ema9 or not ema21:
        return None

    if not rsi or not atr:
        return None

    price = closes[-1]

    ema9_now = ema9[-1]
    ema21_now = ema21[-1]

    rsi_now = rsi[-1]
    atr_now = atr[-1]

    if atr_now <= 0:
        return None

    current_open = opens[-1]
    current_high = highs[-1]
    current_low = lows[-1]

    previous_high = max(
        highs[-6:-1]
    )

    previous_low = min(
        lows[-6:-1]
    )

    avg_volume = (
        sum(volumes[-6:-1])
        / 5
    )

    if avg_volume <= 0:
        return None

    volume_ratio = (
        volumes[-1]
        / avg_volume
    )

    candle_range = (
        current_high
        - current_low
    )

    if candle_range <= 0:
        return None

    body = abs(
        price
        - current_open
    )

    body_ratio = (
        body
        / candle_range
    )

    bullish = (
        price
        > current_open
    )

    bearish = (
        price
        < current_open
    )

    # ========================================================
    # BUY SCORE
    # ========================================================

    buy_score = 0

    if ema9_now > ema21_now:
        buy_score += 20

    if price > ema9_now:
        buy_score += 10

    if 52 <= rsi_now <= 68:
        buy_score += 15

    if volume_ratio >= 1.10:
        buy_score += 15

    if (
        bullish
        and body_ratio >= 0.45
    ):
        buy_score += 10

    if price > previous_high:
        buy_score += 20

    if (
        current_low <= ema9_now
        and price > ema9_now
        and bullish
    ):
        buy_score += 10

    # ========================================================
    # SELL SCORE
    # ========================================================

    sell_score = 0

    if ema9_now < ema21_now:
        sell_score += 20

    if price < ema9_now:
        sell_score += 10

    if 32 <= rsi_now <= 48:
        sell_score += 15

    if volume_ratio >= 1.10:
        sell_score += 15

    if (
        bearish
        and body_ratio >= 0.45
    ):
        sell_score += 10

    if price < previous_low:
        sell_score += 20

    if (
        current_high >= ema9_now
        and price < ema9_now
        and bearish
    ):
        sell_score += 10

    # ========================================================
    # SELECT
    # ========================================================

    action = None
    score = 0

    if buy_score >= MIN_SIGNAL_SCORE:

        action = "BUY"
        score = buy_score

    if sell_score >= MIN_SIGNAL_SCORE:

        if sell_score > score:

            action = "SELL"
            score = sell_score

    if not action:
        return None

    # ========================================================
    # DYNAMIC ATR RISK
    # ========================================================

    # Minimum distances are only safety floors.
    # Actual SL is primarily volatility based.

    if asset == "PAXG":

        minimum_sl = 0.40

        recommended_lot = 0.03

        display_asset = "PAXGUSD"

    else:

        minimum_sl = 50.0

        recommended_lot = 0.01

        display_asset = "BTCUSD"

    sl_distance = max(
        atr_now * 0.95,
        minimum_sl
    )

    tp_distance = (
        sl_distance
        * BASE_RR
    )

    if action == "BUY":

        tp = (
            price
            + tp_distance
        )

        sl = (
            price
            - sl_distance
        )

    else:

        tp = (
            price
            - tp_distance
        )

        sl = (
            price
            + sl_distance
        )

    signal = {

        "asset":
            display_asset,

        "asset_key":
            asset,

        "price":
            round(price, 2),

        "action":
            action,

        "score":
            score,

        "rsi":
            round(rsi_now, 2),

        "ema9":
            round(ema9_now, 2),

        "ema21":
            round(ema21_now, 2),

        "atr":
            round(atr_now, 4),

        "volume_ratio":
            round(volume_ratio, 2),

        "tp":
            round(tp, 2),

        "sl":
            round(sl, 2),

        "initial_sl":
            round(sl, 2),

        "risk_distance":
            round(sl_distance, 4),

        "recommended_lot":
            recommended_lot,

        "breakeven_trigger":
            round(
                sl_distance
                * BREAK_EVEN_TRIGGER_R,
                4
            ),

        "trail_trigger":
            round(
                sl_distance
                * TRAIL_TRIGGER_R,
                4
            )
    }

    latest_signal_data = signal

    return signal


# ============================================================
# SIGNAL PROTECTION
# ============================================================

def has_active_signal(asset):

    for signal in active_signals:

        if (
            signal["asset_key"]
            == asset
        ):
            return True

    return False


def cooldown_active(asset):

    last_time = (
        last_signal_time
        .get(asset)
    )

    if not last_time:
        return False

    return (
        datetime.now()
        - last_time
    ) < timedelta(
        minutes=
        SIGNAL_COOLDOWN_MINUTES
    )


# ============================================================
# CREATE SIGNAL
# ============================================================

def analyze_and_trigger(asset):

    if cooldown_active(asset):
        return

    if has_active_signal(asset):
        return

    signal = analyze_asset(asset)

    if not signal:
        return

    now = datetime.now()

    signal_time = datetime.now(
        IST
    ).strftime(
        "%I:%M:%S %p | %d %b %Y"
    )

    if signal["asset"] == "PAXGUSD":

        chart_symbol = "PAXGUSD"

    else:

        chart_symbol = "COINBASE:BTCUSD"

    chart_link = (
        "https://www.tradingview.com/"
        "chart/?symbol="
        + chart_symbol
    )

    reply_markup = {

        "inline_keyboard": [

            [

                {
                    "text":
                        "📈 TradingView Chart",

                    "url":
                        chart_link
                }

            ]

        ]
    }

    emoji = (
        "🟢"
        if signal["action"] == "BUY"
        else "🔴"
    )

    message = (

        "🎯 *5M SCALPING SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"

        f"💹 Asset: *{signal['asset']}*\n"

        f"⏰ Time: `{signal_time}`\n\n"

        f"{emoji} Action: "
        f"*{signal['action']}*\n"

        f"💰 Entry: "
        f"`{signal['price']}`\n"

        f"🎯 TP: "
        f"`{signal['tp']}`\n"

        f"🛑 Initial SL: "
        f"`{signal['sl']}`\n\n"

        f"🧠 Score: "
        f"*{signal['score']}/100*\n"

        f"📊 RSI: "
        f"`{signal['rsi']}`\n"

        f"📈 EMA 9: "
        f"`{signal['ema9']}`\n"

        f"📉 EMA 21: "
        f"`{signal['ema21']}`\n"

        f"🔊 Volume: "
        f"`{signal['volume_ratio']}x`\n"

        f"〽️ ATR: "
        f"`{signal['atr']}`\n\n"

        "🛡️ *Trade Management*\n"

        f"🔒 BE Trigger: "
        f"`{signal['breakeven_trigger']} "
        f"price move`\n"

        f"📈 Trail Trigger: "
        f"`{signal['trail_trigger']} "
        f"price move`\n\n"

        f"📦 Suggested Lot: "
        f"`{signal['recommended_lot']}`\n\n"

        "Status: *ACTIVE ⏳*"
    )

    message_id = send_telegram_alert(
        message,
        reply_markup
    )

    if not message_id:
        return

    with state_lock:

        last_signal_time[
            asset
        ] = now

        active_signals.append({

            "msg_id":
                message_id,

            "asset_key":
                asset,

            "asset":
                signal["asset"],

            "action":
                signal["action"],

            "price":
                signal["price"],

            "tp":
                signal["tp"],

            "sl":
                signal["sl"],

            "initial_sl":
                signal["initial_sl"],

            "risk_distance":
                signal["risk_distance"],

            "score":
                signal["score"],

            "created_at":
                signal_time,

            "created_timestamp":
                now.isoformat(),

            "breakeven_done":
                False,

            "trailing_active":
                False,

            "best_price":
                signal["price"],

            "result_r":
                None
        })


# ============================================================
# TRADE MANAGEMENT
# ============================================================

def update_trade_management(
    signal,
    current_price,
    current_atr
):

    entry = signal["price"]
    initial_risk = signal["risk_distance"]

    is_buy = (
        signal["action"]
        == "BUY"
    )

    if initial_risk <= 0:
        return

    if is_buy:

        favorable_move = (
            current_price
            - entry
        )

    else:

        favorable_move = (
            entry
            - current_price
        )

    current_r = (
        favorable_move
        / initial_risk
    )

    # ========================================================
    # UPDATE BEST PRICE
    # ========================================================

    if is_buy:

        if current_price > signal["best_price"]:

            signal["best_price"] = (
                current_price
            )

    else:

        if current_price < signal["best_price"]:

            signal["best_price"] = (
                current_price
            )

    # ========================================================
    # BREAK-EVEN
    # ========================================================

    if (
        not signal["breakeven_done"]
        and current_r
        >= BREAK_EVEN_TRIGGER_R
    ):

        buffer = (
            initial_risk
            * BREAK_EVEN_BUFFER_R
        )

        if is_buy:

            new_sl = (
                entry
                + buffer
            )

            if new_sl > signal["sl"]:

                signal["sl"] = round(
                    new_sl,
                    2
                )

        else:

            new_sl = (
                entry
                - buffer
            )

            if new_sl < signal["sl"]:

                signal["sl"] = round(
                    new_sl,
                    2
                )

        signal["breakeven_done"] = True

        print(
            f"{signal['asset']} "
            f"BREAK-EVEN activated: "
            f"{signal['sl']}"
        )

    # ========================================================
    # TRAILING STOP
    # ========================================================

    if (
        signal["breakeven_done"]
        and current_r
        >= TRAIL_TRIGGER_R
    ):

        trail_distance = (
            current_atr
            * TRAIL_ATR_MULTIPLIER
        )

        if is_buy:

            new_sl = (
                signal["best_price"]
                - trail_distance
            )

            if new_sl > signal["sl"]:

                signal["sl"] = round(
                    new_sl,
                    2
                )

                signal["trailing_active"] = True

        else:

            new_sl = (
                signal["best_price"]
                + trail_distance
            )

            if new_sl < signal["sl"]:

                signal["sl"] = round(
                    new_sl,
                    2
                )

                signal["trailing_active"] = True


# ============================================================
# RESULT / R CALCULATION
# ============================================================

def calculate_result_r(
    signal,
    exit_price
):

    entry = signal["price"]
    risk = signal["risk_distance"]

    if risk <= 0:
        return 0.0

    if signal["action"] == "BUY":

        return (
            exit_price
            - entry
        ) / risk

    return (
        entry
        - exit_price
    ) / risk


def update_trade_history(
    result,
    result_r
):

    with state_lock:

        if result == "WIN":

            trade_history["wins"] += 1

            trade_history[
                "gross_profit_r"
            ] += max(
                result_r,
                0
            )

        elif result == "LOSS":

            trade_history["losses"] += 1

            trade_history[
                "gross_loss_r"
            ] += abs(
                min(
                    result_r,
                    0
                )
            )

        elif result == "BREAKEVEN":

            trade_history[
                "breakeven"
            ] += 1

        total_closed = (
            trade_history["wins"]
            + trade_history["losses"]
            + trade_history["breakeven"]
        )

        trade_history[
            "total_signals"
        ] = total_closed

        decisive_trades = (
            trade_history["wins"]
            + trade_history["losses"]
        )

        if decisive_trades > 0:

            trade_history[
                "win_rate"
            ] = round(

                (
                    trade_history["wins"]
                    / decisive_trades
                )
                * 100,

                2
            )

        gross_profit = (
            trade_history[
                "gross_profit_r"
            ]
        )

        gross_loss = (
            trade_history[
                "gross_loss_r"
            ]
        )

        if gross_loss > 0:

            trade_history[
                "profit_factor"
            ] = round(
                gross_profit
                / gross_loss,
                2
            )

        else:

            trade_history[
                "profit_factor"
            ] = 0.0


# ============================================================
# MONITOR ACTIVE TRADES
# ============================================================

def monitor_active_trades():

    while True:

        try:

            for signal in list(
                active_signals
            ):

                asset = signal[
                    "asset_key"
                ]

                market = get_market_klines(
                    asset,
                    20
                )

                if not market:
                    continue

                highs = market["highs"]
                lows = market["lows"]
                closes = market["closes"]

                if not closes:
                    continue

                current_price = closes[-1]
                current_high = highs[-1]
                current_low = lows[-1]

                atr_values = calculate_atr(
                    highs,
                    lows,
                    closes,
                    14
                )

                current_atr = (
                    atr_values[-1]
                    if atr_values
                    else signal["risk_distance"]
                )

                # ------------------------------------------------
                # FIRST: UPDATE BE / TRAILING
                # ------------------------------------------------

                update_trade_management(
                    signal,
                    current_price,
                    current_atr
                )

                is_buy = (
                    signal["action"]
                    == "BUY"
                )

                # ------------------------------------------------
                # CHECK TP / SL
                # ------------------------------------------------

                if is_buy:

                    tp_hit = (
                        current_high
                        >= signal["tp"]
                    )

                    sl_hit = (
                        current_low
                        <= signal["sl"]
                    )

                else:

                    tp_hit = (
                        current_low
                        <= signal["tp"]
                    )

                    sl_hit = (
                        current_high
                        >= signal["sl"]
                    )

                # ------------------------------------------------
                # BOTH HIT SAME CANDLE
                # ------------------------------------------------

                if (
                    tp_hit
                    and sl_hit
                ):

                    # Conservative treatment.
                    result = "LOSS"

                    if is_buy:

                        exit_price = (
                            signal["sl"]
                        )

                    else:

                        exit_price = (
                            signal["sl"]
                        )

                    status_text = (
                        "⚠️ TP & SL touched "
                        "in same candle — "
                        "counted conservatively"
                    )

                elif tp_hit:

                    result = "WIN"

                    exit_price = (
                        signal["tp"]
                    )

                    status_text = (
                        "✅ TARGET HIT 🎯"
                    )

                elif sl_hit:

                    exit_price = (
                        signal["sl"]
                    )

                    result_r = (
                        calculate_result_r(
                            signal,
                            exit_price
                        )
                    )

                    if (
                        signal["breakeven_done"]
                        and result_r >= -0.10
                    ):

                        result = "BREAKEVEN"

                        status_text = (
                            "🛡️ BREAK-EVEN / "
                            "RISK-FREE EXIT"
                        )

                    else:

                        result = "LOSS"

                        status_text = (
                            "❌ STOP LOSS HIT 🛑"
                        )

                else:

                    continue

                result_r = (
                    calculate_result_r(
                        signal,
                        exit_price
                    )
                )

                update_trade_history(
                    result,
                    result_r
                )

                # ------------------------------------------------
                # TELEGRAM RESULT
                # ------------------------------------------------

                if (
                    signal["asset"]
                    == "PAXGUSD"
                ):

                    chart_symbol = "PAXGUSD"

                else:

                    chart_symbol = (
                        "COINBASE:BTCUSD"
                    )

                chart_link = (
                    "https://www.tradingview.com/"
                    "chart/?symbol="
                    + chart_symbol
                )

                reply_markup = {

                    "inline_keyboard": [

                        [

                            {
                                "text":
                                    "📈 TradingView Chart",

                                "url":
                                    chart_link
                            }

                        ]

                    ]
                }

                result_emoji = {

                    "WIN":
                        "✅",

                    "LOSS":
                        "❌",

                    "BREAKEVEN":
                        "🛡️"

                }.get(
                    result,
                    "📊"
                )

                updated_message = (

                    "🎯 *5M SCALPING RESULT*\n"
                    "━━━━━━━━━━━━━━━━━━\n"

                    f"💹 Asset: "
                    f"*{signal['asset']}*\n"

                    f"⏰ Signal: "
                    f"`{signal['created_at']}`\n\n"

                    f"{result_emoji} "
                    f"*{status_text}*\n\n"

                    f"Action: "
                    f"*{signal['action']}*\n"

                    f"Entry: "
                    f"`{signal['price']}`\n"

                    f"Exit: "
                    f"`{round(exit_price, 2)}`\n"

                    f"TP: "
                    f"`{signal['tp']}`\n"

                    f"Current SL: "
                    f"`{signal['sl']}`\n\n"

                    f"R Result: "
                    f"`{round(result_r, 2)}R`\n"

                    f"BE Activated: "
                    f"`{'YES' if signal['breakeven_done'] else 'NO'}`\n"

                    f"Trailing: "
                    f"`{'YES' if signal['trailing_active'] else 'NO'}`\n\n"

                    "📊 *Performance*\n"

                    f"Trades: "
                    f"`{trade_history['total_signals']}`\n"

                    f"✅ Wins: "
                    f"`{trade_history['wins']}`\n"

                    f"❌ Losses: "
                    f"`{trade_history['losses']}`\n"

                    f"🛡️ BE: "
                    f"`{trade_history['breakeven']}`\n"

                    f"🏆 Win Rate: "
                    f"*{trade_history['win_rate']}%*\n"

                    f"📈 Profit Factor: "
                    f"`{trade_history['profit_factor']}`"
                )

                edit_telegram_alert(
                    signal["msg_id"],
                    updated_message,
                    reply_markup
                )

                with state_lock:

                    if signal in active_signals:

                        active_signals.remove(
                            signal
                        )

        except Exception as e:

            print(
                "Monitor Error:",
                e
            )

        time.sleep(10)


# ============================================================
# AUTO SCANNER
# ============================================================

def continuous_auto_scanner():

    while True:

        try:

            for asset in [
                "PAXG",
                "BTC"
            ]:

                analyze_and_trigger(
                    asset
                )

                time.sleep(2)

        except Exception as e:

            print(
                "Scanner Error:",
                e
            )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# API
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "status":
            "5M Scalping Bot Active",

        "assets": [
            "PAXGUSD",
            "BTCUSD"
        ],

        "timeframe":
            "5m",

        "signal_score":
            MIN_SIGNAL_SCORE,

        "features": [

            "EMA 9/21",

            "RSI",

            "Volume",

            "Breakout",

            "Pullback",

            "ATR TP/SL",

            "Break Even",

            "Trailing Stop",

            "API Cache",

            "429 Protection"

        ]

    })


@app.route("/api/stats")
def get_stats():

    with state_lock:

        return jsonify({

            "performance":
                trade_history,

            "active_signals_count":
                len(active_signals)

        })


@app.route("/api/latest_signal")
def get_latest_signal():

    with state_lock:

        return jsonify({

            "status":
                "success",

            "latest_signal":
                latest_signal_data,

            "active_signals":
                active_signals

        })


@app.route("/api/health")
def health():

    return jsonify({

        "status":
            "healthy",

        "telegram_configured":
            bool(
                TELEGRAM_BOT_TOKEN
            ),

        "active_signals":
            len(active_signals)

    })


# ============================================================
# START THREADS
# ============================================================

scanner_thread = threading.Thread(
    target=continuous_auto_scanner,
    daemon=True
)

monitor_thread = threading.Thread(
    target=monitor_active_trades,
    daemon=True
)

scanner_thread.start()
monitor_thread.start()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
