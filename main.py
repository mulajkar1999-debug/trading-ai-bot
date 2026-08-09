import os
import time
import threading
import requests
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

IST = pytz.timezone("Asia/Kolkata")


# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    "1317739622"
)


# ============================================================
# SETTINGS
# ============================================================

TIMEFRAME = "5M"
GRANULARITY = 300

SCAN_INTERVAL = 30
MARKET_CACHE_SECONDS = 20

SIGNAL_COOLDOWN_MINUTES = 5

# High-quality filter.
MIN_SIGNAL_SCORE = 80

# Direction must clearly beat opposite direction.
MIN_DIRECTION_EDGE = 12

# Risk/reward.
BASE_RR = 1.40

# Break-even.
BREAK_EVEN_TRIGGER_R = 0.70
BREAK_EVEN_BUFFER_R = 0.03

# Trailing.
TRAIL_TRIGGER_R = 1.10
TRAIL_ATR_MULTIPLIER = 0.75

# ATR SL.
ATR_SL_MULTIPLIER = 1.00


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
# STATE
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
        print(
            "Telegram token missing. "
            "Set TELEGRAM_BOT_TOKEN in Render."
        )
        return None

    try:

        url = (
            "https://api.telegram.org/"
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
            return (
                response
                .json()
                .get("result", {})
                .get("message_id")
            )

        print(
            "Telegram error:",
            response.status_code,
            response.text
        )

    except Exception as e:
        print("Telegram send error:", e)

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
            "https://api.telegram.org/"
            f"bot{TELEGRAM_BOT_TOKEN}/"
            "editMessageText"
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
                "Telegram edit error:",
                response.text
            )

    except Exception as e:
        print("Telegram edit error:", e)


# ============================================================
# COINBASE DATA
# ============================================================

def fetch_coinbase_candles(asset, limit=120):

    if asset not in ASSETS:
        return None

    product_id = ASSETS[asset]["product"]

    url = (
        "https://api.exchange.coinbase.com/"
        f"products/{product_id}/candles"
        f"?granularity={GRANULARITY}"
    )

    headers = {
        "User-Agent": "5M-Scalping-Bot/1.0"
    }

    delay = 2

    for attempt in range(4):

        try:

            response = requests.get(
                url,
                headers=headers,
                timeout=10
            )

            if response.status_code == 429:

                print(
                    f"Coinbase rate limit for {asset}. "
                    f"Retrying in {delay}s."
                )

                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code != 200:

                print(
                    f"Coinbase error {asset}: "
                    f"{response.status_code}"
                )

                return None

            raw = response.json()

            if not isinstance(raw, list):
                return None

            raw = sorted(
                raw,
                key=lambda x: x[0]
            )

            # Only COMPLETED candles.
            now_ts = int(time.time())

            completed = [
                candle
                for candle in raw
                if (
                    int(candle[0])
                    + GRANULARITY
                    <= now_ts
                )
            ]

            if len(completed) < 60:
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
                f"{asset}: {e}"
            )

            time.sleep(delay)
            delay *= 2

        except Exception as e:

            print(
                f"Coinbase parsing error "
                f"{asset}: {e}"
            )

            return None

    return None


def get_market_data(
    asset,
    limit=120,
    force_refresh=False
):

    now = time.time()

    with cache_lock:

        cached = market_cache.get(asset)

        if (
            cached
            and not force_refresh
            and
            (
                now - cached["time"]
                <
                MARKET_CACHE_SECONDS
            )
        ):
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
# EMA
# ============================================================

def calculate_ema(values, period):

    if len(values) < period:
        return []

    multiplier = 2 / (period + 1)

    current = (
        sum(values[:period])
        / period
    )

    result = [current]

    for value in values[period:]:

        current = (
            (
                value - current
            )
            * multiplier
            + current
        )

        result.append(current)

    return result


# ============================================================
# RSI
# ============================================================

def calculate_rsi(values, period=14):

    if len(values) <= period:
        return []

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = (
            values[i]
            -
            values[i - 1]
        )

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = (
        sum(gains[:period])
        /
        period
    )

    avg_loss = (
        sum(losses[:period])
        /
        period
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
                +
                gains[i]
            ) / period

            avg_loss = (
                (
                    avg_loss
                    * (period - 1)
                )
                +
                losses[i]
            ) / period

        if avg_loss == 0:
            value = 100.0
        else:

            rs = (
                avg_gain
                /
                avg_loss
            )

            value = (
                100
                -
                (
                    100
                    /
                    (1 + rs)
                )
            )

        result.append(value)

    return result


# ============================================================
# ATR
# ============================================================

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
                -
                closes[i - 1]
            ),

            abs(
                lows[i]
                -
                closes[i - 1]
            )
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return []

    current = (
        sum(
            true_ranges[:period]
        )
        /
        period
    )

    result = [current]

    for tr in true_ranges[period:]:

        current = (
            (
                current
                * (period - 1)
            )
            +
            tr
        ) / period

        result.append(current)

    return result


# ============================================================
# ADX
# ============================================================

def calculate_adx(
    highs,
    lows,
    closes,
    period=14
):

    if len(closes) < period * 2 + 5:
        return []

    tr_list = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(closes)):

        high_diff = (
            highs[i]
            -
            highs[i - 1]
        )

        low_diff = (
            lows[i - 1]
            -
            lows[i]
        )

        tr = max(
            highs[i] - lows[i],
            abs(
                highs[i]
                -
                closes[i - 1]
            ),
            abs(
                lows[i]
                -
                closes[i - 1]
            )
        )

        tr_list.append(tr)

        plus_dm.append(
            high_diff
            if (
                high_diff > low_diff
                and high_diff > 0
            )
            else 0
        )

        minus_dm.append(
            low_diff
            if (
                low_diff > high_diff
                and low_diff > 0
            )
            else 0
        )

    if len(tr_list) < period:
        return []

    atr_value = (
        sum(tr_list[:period])
        /
        period
    )

    plus_value = (
        sum(plus_dm[:period])
        /
        period
    )

    minus_value = (
        sum(minus_dm[:period])
        /
        period
    )

    dx_values = []

    for i in range(
        period,
        len(tr_list)
    ):

        atr_value = (
            (
                atr_value
                * (period - 1)
            )
            +
            tr_list[i]
        ) / period

        plus_value = (
            (
                plus_value
                * (period - 1)
            )
            +
            plus_dm[i]
        ) / period

        minus_value = (
            (
                minus_value
                * (period - 1)
            )
            +
            minus_dm[i]
        ) / period

        if atr_value == 0:
            continue

        plus_di = (
            100
            *
            plus_value
            /
            atr_value
        )

        minus_di = (
            100
            *
            minus_value
            /
            atr_value
        )

        denominator = (
            plus_di
            +
            minus_di
        )

        if denominator == 0:
            dx = 0
        else:

            dx = (
                100
                *
                abs(
                    plus_di
                    -
                    minus_di
                )
                /
                denominator
            )

        dx_values.append(dx)

    if len(dx_values) < period:
        return []

    adx = (
        sum(dx_values[:period])
        /
        period
    )

    result = [adx]

    for dx in dx_values[period:]:

        adx = (
            (
                adx
                * (period - 1)
            )
            +
            dx
        ) / period

        result.append(adx)

    return result


# ============================================================
# SWING POINTS
# ============================================================

def find_swing_highs(
    highs,
    lookback=2
):

    points = []

    for i in range(
        lookback,
        len(highs) - lookback
    ):

        if (
            highs[i]
            >
            max(
                highs[
                    i - lookback:i
                ]
            )
            and
            highs[i]
            >=
            max(
                highs[
                    i + 1:
                    i + lookback + 1
                ]
            )
        ):

            points.append(
                (i, highs[i])
            )

    return points


def find_swing_lows(
    lows,
    lookback=2
):

    points = []

    for i in range(
        lookback,
        len(lows) - lookback
    ):

        if (
            lows[i]
            <
            min(
                lows[
                    i - lookback:i
                ]
            )
            and
            lows[i]
            <=
            min(
                lows[
                    i + 1:
                    i + lookback + 1
                ]
            )
        ):

            points.append(
                (i, lows[i])
            )

    return points


# ============================================================
# CHART PATTERNS
# ============================================================

def detect_patterns(
    highs,
    lows,
    closes
):

    patterns = []

    swing_highs = find_swing_highs(highs)
    swing_lows = find_swing_lows(lows)

    recent_highs = swing_highs[-5:]
    recent_lows = swing_lows[-5:]

    # --------------------------------------------------------
    # DOUBLE TOP
    # --------------------------------------------------------

    if len(recent_highs) >= 2:

        h1 = recent_highs[-2][1]
        h2 = recent_highs[-1][1]

        tolerance = abs(h1) * 0.003

        if (
            abs(h1 - h2)
            <= tolerance
            and closes[-1] < h2
        ):

            patterns.append(
                (
                    "DOUBLE_TOP",
                    "SELL",
                    12
                )
            )

    # --------------------------------------------------------
    # DOUBLE BOTTOM
    # --------------------------------------------------------

    if len(recent_lows) >= 2:

        l1 = recent_lows[-2][1]
        l2 = recent_lows[-1][1]

        tolerance = abs(l1) * 0.003

        if (
            abs(l1 - l2)
            <= tolerance
            and closes[-1] > l2
        ):

            patterns.append(
                (
                    "DOUBLE_BOTTOM",
                    "BUY",
                    12
                )
            )

    # --------------------------------------------------------
    # TRIPLE TOP
    # --------------------------------------------------------

    if len(recent_highs) >= 3:

        values = [
            x[1]
            for x in recent_highs[-3:]
        ]

        average = (
            sum(values)
            / 3
        )

        if (
            max(values)
            -
            min(values)
            <=
            average * 0.004
            and
            closes[-1] < average
        ):

            patterns.append(
                (
                    "TRIPLE_TOP",
                    "SELL",
                    15
                )
            )

    # --------------------------------------------------------
    # TRIPLE BOTTOM
    # --------------------------------------------------------

    if len(recent_lows) >= 3:

        values = [
            x[1]
            for x in recent_lows[-3:]
        ]

        average = (
            sum(values)
            / 3
        )

        if (
            max(values)
            -
            min(values)
            <=
            average * 0.004
            and
            closes[-1] > average
        ):

            patterns.append(
                (
                    "TRIPLE_BOTTOM",
                    "BUY",
                    15
                )
            )

    # --------------------------------------------------------
    # HEAD AND SHOULDERS
    # --------------------------------------------------------

    if len(recent_highs) >= 3:

        left, head, right = [
            x[1]
            for x in recent_highs[-3:]
        ]

        shoulder_tolerance = (
            abs(left) * 0.01
        )

        if (
            head > left
            and
            head > right
            and
            abs(left - right)
            <=
            shoulder_tolerance
            and
            closes[-1] < right
        ):

            patterns.append(
                (
                    "HEAD_SHOULDERS",
                    "SELL",
                    16
                )
            )

    # --------------------------------------------------------
    # INVERSE HEAD AND SHOULDERS
    # --------------------------------------------------------

    if len(recent_lows) >= 3:

        left, head, right = [
            x[1]
            for x in recent_lows[-3:]
        ]

        shoulder_tolerance = (
            abs(left) * 0.01
        )

        if (
            head < left
            and
            head < right
            and
            abs(left - right)
            <=
            shoulder_tolerance
            and
            closes[-1] > right
        ):

            patterns.append(
                (
                    "INVERSE_HEAD_SHOULDERS",
                    "BUY",
                    16
                )
            )

    return patterns


# ============================================================
# CANDLE CONFIRMATION
# ============================================================

def candle_confirmation(
    opens,
    highs,
    lows,
    closes,
    direction
):

    candle_range = (
        highs[-1]
        -
        lows[-1]
    )

    if candle_range <= 0:
        return False

    body = abs(
        closes[-1]
        -
        opens[-1]
    )

    body_ratio = (
        body
        /
        candle_range
    )

    upper_wick = (
        highs[-1]
        -
        max(
            opens[-1],
            closes[-1]
        )
    )

    lower_wick = (
        min(
            opens[-1],
            closes[-1]
        )
        -
        lows[-1]
    )

    if direction == "BUY":

        return (
            closes[-1]
            >
            opens[-1]
            and
            body_ratio >= 0.50
            and
            upper_wick
            <
            body * 0.75
        )

    if direction == "SELL":

        return (
            closes[-1]
            <
            opens[-1]
            and
            body_ratio >= 0.50
            and
            lower_wick
            <
            body * 0.75
        )

    return False


# ============================================================
# BREAKOUT
# ============================================================

def breakout_confirmation(
    highs,
    lows,
    closes,
    volumes,
    atr_value,
    direction
):

    if len(closes) < 10:
        return False

    resistance = max(
        highs[-9:-1]
    )

    support = min(
        lows[-9:-1]
    )

    avg_volume = (
        sum(volumes[-9:-1])
        /
        8
    )

    if avg_volume <= 0:
        return False

    volume_ratio = (
        volumes[-1]
        /
        avg_volume
    )

    if direction == "BUY":

        return (
            closes[-1]
            >
            resistance
            +
            atr_value * 0.08
            and
            volume_ratio >= 1.20
        )

    if direction == "SELL":

        return (
            closes[-1]
            <
            support
            -
            atr_value * 0.08
            and
            volume_ratio >= 1.20
        )

    return False


# ============================================================
# RETEST CONFIRMATION
# ============================================================

def retest_confirmation(
    highs,
    lows,
    closes,
    direction
):

    if len(closes) < 8:
        return False

    level_high = max(
        highs[-8:-2]
    )

    level_low = min(
        lows[-8:-2]
    )

    if direction == "BUY":

        previous_breakout = (
            closes[-2]
            >
            level_high
        )

        current_holds_level = (
            lows[-1]
            >=
            level_high * 0.999
        )

        return (
            previous_breakout
            and
            current_holds_level
            and
            closes[-1]
            >
            closes[-2]
        )

    if direction == "SELL":

        previous_breakout = (
            closes[-2]
            <
            level_low
        )

        current_holds_level = (
            highs[-1]
            <=
            level_low * 1.001
        )

        return (
            previous_breakout
            and
            current_holds_level
            and
            closes[-1]
            <
            closes[-2]
        )

    return False


# ============================================================
# MARKET REGIME
# ============================================================

def get_market_regime(
    price,
    ema9,
    ema21,
    adx
):

    if adx < 18:
        return "CHOPPY"

    if (
        ema9 > ema21
        and
        price > ema9
    ):
        return "BULLISH"

    if (
        ema9 < ema21
        and
        price < ema9
    ):
        return "BEARISH"

    return "TRANSITION"


# ============================================================
# ANALYZE ASSET
# ============================================================

def analyze_asset(asset):

    global latest_signal_data

    market = get_market_data(
        asset,
        120
    )

    if not market:
        return None

    opens = market["opens"]
    highs = market["highs"]
    lows = market["lows"]
    closes = market["closes"]
    volumes = market["volumes"]

    if len(closes) < 70:
        return None

    ema9_values = calculate_ema(
        closes,
        9
    )

    ema21_values = calculate_ema(
        closes,
        21
    )

    rsi_values = calculate_rsi(
        closes,
        14
    )

    atr_values = calculate_atr(
        highs,
        lows,
        closes,
        14
    )

    adx_values = calculate_adx(
        highs,
        lows,
        closes,
        14
    )

    if not all([
        ema9_values,
        ema21_values,
        rsi_values,
        atr_values,
        adx_values
    ]):
        return None

    price = closes[-1]

    ema9 = ema9_values[-1]
    ema21 = ema21_values[-1]
    rsi = rsi_values[-1]
    atr = atr_values[-1]
    adx = adx_values[-1]

    if atr <= 0:
        return None

    regime = get_market_regime(
        price,
        ema9,
        ema21,
        adx
    )

    # No trade in sideways/transition market.
    if regime in (
        "CHOPPY",
        "TRANSITION"
    ):
        return None

    avg_volume = (
        sum(volumes[-9:-1])
        /
        8
    )

    if avg_volume <= 0:
        return None

    volume_ratio = (
        volumes[-1]
        /
        avg_volume
    )

    patterns = detect_patterns(
        highs,
        lows,
        closes
    )

    buy_score = 0
    sell_score = 0

    buy_reasons = []
    sell_reasons = []

    # --------------------------------------------------------
    # EMA
    # --------------------------------------------------------

    if (
        ema9 > ema21
        and
        regime == "BULLISH"
    ):

        buy_score += 20
        buy_reasons.append(
            "EMA9>EMA21"
        )

    if (
        ema9 < ema21
        and
        regime == "BEARISH"
    ):

        sell_score += 20
        sell_reasons.append(
            "EMA9<EMA21"
        )

    # --------------------------------------------------------
    # PRICE
    # --------------------------------------------------------

    if (
        price > ema9
        and
        regime == "BULLISH"
    ):

        buy_score += 10
        buy_reasons.append(
            "Price above EMA9"
        )

    if (
        price < ema9
        and
        regime == "BEARISH"
    ):

        sell_score += 10
        sell_reasons.append(
            "Price below EMA9"
        )

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    # Avoid buying extreme RSI.
    if (
        52 <= rsi <= 68
        and
        regime == "BULLISH"
    ):

        buy_score += 12
        buy_reasons.append(
            "RSI momentum"
        )

    # Avoid selling extreme RSI.
    if (
        32 <= rsi <= 48
        and
        regime == "BEARISH"
    ):

        sell_score += 12
        sell_reasons.append(
            "RSI momentum"
        )

    # --------------------------------------------------------
    # ADX
    # --------------------------------------------------------

    if adx >= 20:

        if regime == "BULLISH":

            buy_score += 12
            buy_reasons.append(
                "ADX trend"
            )

        elif regime == "BEARISH":

            sell_score += 12
            sell_reasons.append(
                "ADX trend"
            )

    if adx >= 25:

        if regime == "BULLISH":

            buy_score += 5

        elif regime == "BEARISH":

            sell_score += 5

    # --------------------------------------------------------
    # VOLUME
    # --------------------------------------------------------

    if volume_ratio >= 1.20:

        if regime == "BULLISH":

            buy_score += 10
            buy_reasons.append(
                f"Volume {volume_ratio:.2f}x"
            )

        elif regime == "BEARISH":

            sell_score += 10
            sell_reasons.append(
                f"Volume {volume_ratio:.2f}x"
            )

    if volume_ratio >= 1.50:

        if regime == "BULLISH":
            buy_score += 5

        elif regime == "BEARISH":
            sell_score += 5

    # --------------------------------------------------------
    # CANDLE
    # --------------------------------------------------------

    buy_candle = candle_confirmation(
        opens,
        highs,
        lows,
        closes,
        "BUY"
    )

    sell_candle = candle_confirmation(
        opens,
        highs,
        lows,
        closes,
        "SELL"
    )

    if buy_candle:

        buy_score += 8
        buy_reasons.append(
            "Strong candle"
        )

    if sell_candle:

        sell_score += 8
        sell_reasons.append(
            "Strong candle"
        )

    # --------------------------------------------------------
    # BREAKOUT
    # --------------------------------------------------------

    buy_breakout = breakout_confirmation(
        highs,
        lows,
        closes,
        volumes,
        atr,
        "BUY"
    )

    sell_breakout = breakout_confirmation(
        highs,
        lows,
        closes,
        volumes,
        atr,
        "SELL"
    )

    if buy_breakout:

        buy_score += 15
        buy_reasons.append(
            "Breakout"
        )

    if sell_breakout:

        sell_score += 15
        sell_reasons.append(
            "Breakdown"
        )

    # --------------------------------------------------------
    # RETEST
    # --------------------------------------------------------

    buy_retest = retest_confirmation(
        highs,
        lows,
        closes,
        "BUY"
    )

    sell_retest = retest_confirmation(
        highs,
        lows,
        closes,
        "SELL"
    )

    if buy_retest:

        buy_score += 12
        buy_reasons.append(
            "Retest confirmed"
        )

    if sell_retest:

        sell_score += 12
        sell_reasons.append(
            "Retest confirmed"
        )

    # --------------------------------------------------------
    # PATTERNS
    # --------------------------------------------------------

    detected_pattern = None

    for (
        pattern,
        direction,
        points
    ) in patterns:

        if direction == "BUY":

            buy_score += points

            buy_reasons.append(
                pattern
            )

            if detected_pattern is None:
                detected_pattern = pattern

        elif direction == "SELL":

            sell_score += points

            sell_reasons.append(
                pattern
            )

            if detected_pattern is None:
                detected_pattern = pattern

    # --------------------------------------------------------
    # MANDATORY CONFIRMATION
    # --------------------------------------------------------

    buy_confirmation = (
        (
            buy_breakout
            and
            buy_candle
        )
        or
        buy_retest
        or
        any(
            p[1] == "BUY"
            for p in patterns
        )
    )

    sell_confirmation = (
        (
            sell_breakout
            and
            sell_candle
        )
        or
        sell_retest
        or
        any(
            p[1] == "SELL"
            for p in patterns
        )
    )

    action = None
    score = 0
    reasons = []

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    if (
        buy_score >= MIN_SIGNAL_SCORE
        and
        buy_confirmation
        and
        buy_score
        >=
        sell_score
        +
        MIN_DIRECTION_EDGE
    ):

        action = "BUY"
        score = buy_score
        reasons = buy_reasons

    # --------------------------------------------------------
    # SELL
    # --------------------------------------------------------

    elif (
        sell_score >= MIN_SIGNAL_SCORE
        and
        sell_confirmation
        and
        sell_score
        >=
        buy_score
        +
        MIN_DIRECTION_EDGE
    ):

        action = "SELL"
        score = sell_score
        reasons = sell_reasons

    if not action:
        return None

    # --------------------------------------------------------
    # SL / TP
    # --------------------------------------------------------

    minimum_sl = ASSETS[
        asset
    ]["minimum_sl"]

    sl_distance = max(
        atr * ATR_SL_MULTIPLIER,
        minimum_sl
    )

    tp_distance = (
        sl_distance
        *
        BASE_RR
    )

    if action == "BUY":

        sl = price - sl_distance
        tp = price + tp_distance

    else:

        sl = price + sl_distance
        tp = price - tp_distance

    signal = {

        "asset_key": asset,

        "asset":
            ASSETS[asset]["display"],

        "action": action,

        "price":
            round(price, 2),

        "tp":
            round(tp, 2),

        "sl":
            round(sl, 2),

        "initial_sl":
            round(sl, 2),

        "risk_distance":
            round(sl_distance, 4),

        "score":
            min(score, 100),

        "ema9":
            round(ema9, 2),

        "ema21":
            round(ema21, 2),

        "rsi":
            round(rsi, 2),

        "atr":
            round(atr, 4),

        "adx":
            round(adx, 2),

        "volume_ratio":
            round(volume_ratio, 2),

        "regime":
            regime,

        "pattern":
            detected_pattern or "BREAKOUT",

        "reasons":
            reasons[:10],

        "recommended_lot":
            ASSETS[asset]["lot"]
    }

    latest_signal_data = signal

    return signal


# ============================================================
# SIGNAL CONTROL
# ============================================================

def has_active_signal(asset):

    with state_lock:

        return any(
            s["asset_key"] == asset
            for s in active_signals
        )


def cooldown_active(asset):

    last = last_signal_time.get(asset)

    if not last:
        return False

    return (
        datetime.now()
        -
        last
    ) < timedelta(
        minutes=SIGNAL_COOLDOWN_MINUTES
    )


# ============================================================
# SEND NEW SIGNAL
# ============================================================

def analyze_and_trigger(asset):

    if cooldown_active(asset):
        return

    if has_active_signal(asset):
        return

    signal = analyze_asset(asset)

    if not signal:
        return

    signal_time = datetime.now(
        IST
    ).strftime(
        "%I:%M:%S %p | %d %b"
    )

    if signal["asset"] == "PAXGUSD":

        chart_symbol = "PAXGUSD"

    else:

        chart_symbol = "COINBASE:BTCUSD"

    chart_link = (
        "https://www.tradingview.com/"
        "chart/?symbol="
        +
        chart_symbol
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
        else
        "🔴"
    )

    reasons = ", ".join(
        signal["reasons"]
    )

    message = (

        "🎯 *5M SCALPING SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"

        f"💹 *{signal['asset']}*\n"
        f"⏰ `{signal_time}`\n\n"

        f"{emoji} Action: "
        f"*{signal['action']}*\n"

        f"💰 Entry: "
        f"`{signal['price']}`\n"

        f"🎯 TP: "
        f"`{signal['tp']}`\n"

        f"🛑 SL: "
        f"`{signal['sl']}`\n\n"

        f"⭐ Score: "
        f"*{signal['score']}/100*\n"

        f"📊 Pattern: "
        f"`{signal['pattern']}`\n"

        f"📈 EMA9: "
        f"`{signal['ema9']}`\n"

        f"📉 EMA21: "
        f"`{signal['ema21']}`\n"

        f"RSI: "
        f"`{signal['rsi']}`\n"

        f"ADX: "
        f"`{signal['adx']}`\n"

        f"ATR: "
        f"`{signal['atr']}`\n"

        f"Volume: "
        f"`{signal['volume_ratio']}x`\n"

        f"Regime: "
        f"`{signal['regime']}`\n\n"

        f"🧠 *Confirmation:*\n"
        f"`{reasons}`\n\n"

        f"📦 Lot: "
        f"`{signal['recommended_lot']}`\n\n"

        "🛡️ BE: "
        f"`+{BREAK_EVEN_TRIGGER_R}R`\n"

        "📈 Trail: "
        f"`+{TRAIL_TRIGGER_R}R`\n\n"

        "Status: *ACTIVE ⏳*"
    )

    msg_id = send_telegram_alert(
        message,
        reply_markup
    )

    if not msg_id:
        return

    now = datetime.now()

    trade = {

        "msg_id":
            msg_id,

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

        "pattern":
            signal["pattern"],

        "created_at":
            signal_time,

        "created_timestamp":
            now.isoformat(),

        "best_price":
            signal["price"],

        "breakeven_done":
            False,

        "trailing_active":
            False
    }

    with state_lock:

        active_signals.append(trade)

        last_signal_time[asset] = now


# ============================================================
# BREAK-EVEN + TRAILING
# ============================================================

def update_trade_management(
    signal,
    current_price,
    current_atr
):

    entry = signal["price"]
    risk = signal["risk_distance"]

    if risk <= 0:
        return

    is_buy = (
        signal["action"] == "BUY"
    )

    if is_buy:

        favorable_move = (
            current_price
            -
            entry
        )

    else:

        favorable_move = (
            entry
            -
            current_price
        )

    current_r = (
        favorable_move
        /
        risk
    )

    if is_buy:

        signal["best_price"] = max(
            signal["best_price"],
            current_price
        )

    else:

        signal["best_price"] = min(
            signal["best_price"],
            current_price
        )

    # --------------------------------------------------------
    # BREAK EVEN
    # --------------------------------------------------------

    if (
        not signal["breakeven_done"]
        and
        current_r
        >=
        BREAK_EVEN_TRIGGER_R
    ):

        buffer = (
            risk
            *
            BREAK_EVEN_BUFFER_R
        )

        if is_buy:

            new_sl = (
                entry
                +
                buffer
            )

            if new_sl > signal["sl"]:

                signal["sl"] = round(
                    new_sl,
                    2
                )

        else:

            new_sl = (
                entry
                -
                buffer
            )

            if new_sl < signal["sl"]:

                signal["sl"] = round(
                    new_sl,
                    2
                )

        signal["breakeven_done"] = True

    # --------------------------------------------------------
    # TRAILING
    # --------------------------------------------------------

    if (
        signal["breakeven_done"]
        and
        current_r
        >=
        TRAIL_TRIGGER_R
    ):

        trail_distance = (
            current_atr
            *
            TRAIL_ATR_MULTIPLIER
        )

        if is_buy:

            new_sl = (
                signal["best_price"]
                -
                trail_distance
            )

            if new_sl > signal["sl"]:

                signal["sl"] = round(
                    new_sl,
                    2
                )

                signal[
                    "trailing_active"
                ] = True

        else:

            new_sl = (
                signal["best_price"]
                +
                trail_distance
            )

            if new_sl < signal["sl"]:

                signal["sl"] = round(
                    new_sl,
                    2
                )

                signal[
                    "trailing_active"
                ] = True


# ============================================================
# RESULT R
# ============================================================

def calculate_result_r(
    signal,
    exit_price
):

    risk = signal["risk_distance"]

    if risk <= 0:
        return 0.0

    if signal["action"] == "BUY":

        return (
            exit_price
            -
            signal["price"]
        ) / risk

    return (
        signal["price"]
        -
        exit_price
    ) / risk


# ============================================================
# UPDATE PERFORMANCE
# ============================================================

def update_history(
    result,
    r_value
):

    with state_lock:

        if result == "WIN":

            trade_history["wins"] += 1

            trade_history[
                "gross_profit_r"
            ] += max(
                r_value,
                0
            )

        elif result == "LOSS":

            trade_history["losses"] += 1

            trade_history[
                "gross_loss_r"
            ] += abs(
                min(
                    r_value,
                    0
                )
            )

        else:

            trade_history[
                "breakeven"
            ] += 1

        total = (
            trade_history["wins"]
            +
            trade_history["losses"]
            +
            trade_history["breakeven"]
        )

        trade_history[
            "total_signals"
        ] = total

        decisive = (
            trade_history["wins"]
            +
            trade_history["losses"]
        )

        if decisive > 0:

            trade_history[
                "win_rate"
            ] = round(
                (
                    trade_history["wins"]
                    /
                    decisive
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
                /
                gross_loss,
                2
            )


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

                market = get_market_data(
                    asset,
                    40
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

                if atr_values:

                    current_atr = (
                        atr_values[-1]
                    )

                else:

                    current_atr = (
                        signal[
                            "risk_distance"
                        ]
                    )

                update_trade_management(
                    signal,
                    current_price,
                    current_atr
                )

                is_buy = (
                    signal["action"] == "BUY"
                )

                if is_buy:

                    tp_hit = (
                        current_high
                        >=
                        signal["tp"]
                    )

                    sl_hit = (
                        current_low
                        <=
                        signal["sl"]
                    )

                else:

                    tp_hit = (
                        current_low
                        <=
                        signal["tp"]
                    )

                    sl_hit = (
                        current_high
                        >=
                        signal["sl"]
                    )

                if not (
                    tp_hit
                    or
                    sl_hit
                ):
                    continue

                # Conservative:
                # same candle touches TP + SL
                # = LOSS.
                if (
                    tp_hit
                    and
                    sl_hit
                ):

                    exit_price = (
                        signal["sl"]
                    )

                    result = "LOSS"

                    status = (
                        "⚠️ TP & SL same candle "
                        "— counted as LOSS"
                    )

                elif tp_hit:

                    exit_price = (
                        signal["tp"]
                    )

                    result = "WIN"

                    status = (
                        "✅ TARGET HIT 🎯"
                    )

                else:

                    exit_price = (
                        signal["sl"]
                    )

                    r_value = calculate_result_r(
                        signal,
                        exit_price
                    )

                    if (
                        signal[
                            "breakeven_done"
                        ]
                        and
                        r_value >= -0.10
                    ):

                        result = "BREAKEVEN"

                        status = (
                            "🛡️ BREAK-EVEN"
                        )

                    else:

                        result = "LOSS"

                        status = (
                            "❌ STOP LOSS"
                        )

                r_value = calculate_result_r(
                    signal,
                    exit_price
                )

                update_history(
                    result,
                    r_value
                )

                if signal["asset"] == "PAXGUSD":

                    chart_symbol = "PAXGUSD"

                else:

                    chart_symbol = (
                        "COINBASE:BTCUSD"
                    )

                chart_link = (
                    "https://www.tradingview.com/"
                    "chart/?symbol="
                    +
                    chart_symbol
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

                message = (

                    "🎯 *5M SCALPING RESULT*\n"
                    "━━━━━━━━━━━━━━━━━━\n"

                    f"💹 *{signal['asset']}*\n"
                    f"📊 Pattern: "
                    f"`{signal['pattern']}`\n\n"

                    f"{status}\n\n"

                    f"Action: "
                    f"*{signal['action']}*\n"

                    f"Entry: "
                    f"`{signal['price']}`\n"

                    f"Exit: "
                    f"`{round(exit_price, 2)}`\n"

                    f"TP: "
                    f"`{signal['tp']}`\n"

                    f"Final SL: "
                    f"`{signal['sl']}`\n\n"

                    f"Result: "
                    f"`{round(r_value, 2)}R`\n"

                    f"BE: "
                    f"`{'YES' if signal['breakeven_done'] else 'NO'}`\n"

                    f"Trailing: "
                    f"`{'YES' if signal['trailing_active'] else 'NO'}`\n\n"

                    "📊 *Performance*\n"

                    f"Trades: "
                    f"`{trade_history['total_signals']}`\n"

                    f"Wins: "
                    f"`{trade_history['wins']}`\n"

                    f"Losses: "
                    f"`{trade_history['losses']}`\n"

                    f"BE: "
                    f"`{trade_history['breakeven']}`\n"

                    f"Win Rate: "
                    f"*{trade_history['win_rate']}%*\n"

                    f"Profit Factor: "
                    f"`{trade_history['profit_factor']}`"
                )

                edit_telegram_alert(
                    signal["msg_id"],
                    message,
                    reply_markup
                )

                with state_lock:

                    if signal in active_signals:

                        active_signals.remove(
                            signal
                        )

        except Exception as e:

            print(
                "Monitor error:",
                e
            )

        time.sleep(10)


# ============================================================
# SCANNER
# ============================================================

def continuous_auto_scanner():

    while True:

        try:

            for asset in (
                "PAXG",
                "BTC"
            ):

                analyze_and_trigger(
                    asset
                )

                time.sleep(2)

        except Exception as e:

            print(
                "Scanner error:",
                e
            )

        time.sleep(
            SCAN_INTERVAL
        )


# ============================================================
# API
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify({

        "status":
            "5M Scalping Bot Active",

        "assets": [
            "PAXGUSD",
            "BTCUSD"
        ],

        "timeframe":
            "5M",

        "filters": [

            "EMA 9/21",
            "RSI",
            "ADX",
            "ATR",
            "Relative Volume",
            "Breakout",
            "Retest",
            "Candle Confirmation",
            "Chart Patterns",
            "Choppy Market Filter",
            "Break Even",
            "Trailing Stop"
        ]
    })


@app.route(
    "/api/stats",
    methods=["GET"]
)
def get_stats():

    with state_lock:

        return jsonify({

            "performance":
                trade_history,

            "active_signals_count":
                len(active_signals)
        })


@app.route(
    "/api/latest_signal",
    methods=["GET"]
)
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


@app.route(
    "/api/health",
    methods=["GET"]
)
def health():

    return jsonify({

        "status":
            "healthy",

        "telegram_configured":
            bool(
                TELEGRAM_BOT_TOKEN
            ),

        "assets": [
            "PAXGUSD",
            "BTCUSD"
        ],

        "timeframe":
            "5M",

        "active_signals":
            len(active_signals)
    })


# ============================================================
# START THREADS
# ============================================================

threading.Thread(
    target=continuous_auto_scanner,
    daemon=True
).start()

threading.Thread(
    target=monitor_active_trades,
    daemon=True
).start()


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
