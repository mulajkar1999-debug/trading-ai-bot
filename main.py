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

# Render Environment Variables:
#
# TELEGRAM_BOT_TOKEN = <your Telegram bot token>
# TELEGRAM_CHAT_ID   = 1317739622

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

# Strict quality filter.
MIN_SIGNAL_SCORE = 85

# Required difference between BUY and SELL score.
MIN_DIRECTION_EDGE = 10

# Scan every 30 seconds.
SCAN_INTERVAL = 30

# Coinbase data cache.
MARKET_CACHE_SECONDS = 20

# Same asset cooldown.
SIGNAL_COOLDOWN_MINUTES = 5

# Only one active trade per asset.
ONE_TRADE_PER_ASSET = True


# ============================================================
# RISK MANAGEMENT
# ============================================================

ATR_SL_MULTIPLIER = 0.95

BASE_RR = 1.40

# Move SL to BE after +0.65R.
BREAK_EVEN_TRIGGER_R = 0.65

# Small positive buffer around entry.
BREAK_EVEN_BUFFER_R = 0.03

# Start trailing after +1R.
TRAIL_TRIGGER_R = 1.0

TRAIL_ATR_MULTIPLIER = 0.75


# ============================================================
# ASSETS
# ============================================================

ASSETS = {

    "PAXG": {
        "product": "PAXG-USD",
        "display": "PAXGUSD",
        "lot": 0.03,
        "minimum_sl": 0.40
    },

    "BTC": {
        "product": "BTC-USD",
        "display": "BTCUSD",
        "lot": 0.01,
        "minimum_sl": 50.0
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
# TELEGRAM SEND
# ============================================================

def send_telegram_alert(
    message,
    reply_markup=None
):

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

            payload[
                "reply_markup"
            ] = reply_markup

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

        print(
            "Telegram send error:",
            e
        )

    return None


# ============================================================
# TELEGRAM EDIT
# ============================================================

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

            payload[
                "reply_markup"
            ] = reply_markup

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

        print(
            "Telegram edit exception:",
            e
        )


# ============================================================
# COINBASE CANDLES
# ============================================================

def fetch_coinbase_candles(
    asset,
    limit=100
):

    if asset not in ASSETS:
        return None

    product_id = ASSETS[
        asset
    ]["product"]

    url = (
        "https://api.exchange.coinbase.com/"
        f"products/{product_id}/candles"
        f"?granularity={GRANULARITY}"
    )

    headers = {
        "User-Agent":
            "Quality5MScalper/1.0"
    }

    retry_delay = 2

    for _ in range(4):

        try:

            response = requests.get(
                url,
                headers=headers,
                timeout=10
            )

            if response.status_code == 429:

                print(
                    f"Coinbase 429 for {asset}. "
                    f"Waiting {retry_delay}s."
                )

                time.sleep(
                    retry_delay
                )

                retry_delay *= 2

                continue

            if response.status_code != 200:

                print(
                    f"Coinbase error {asset}: "
                    f"{response.status_code}"
                )

                return None

            raw = response.json()

            if not isinstance(
                raw,
                list
            ):

                return None

            raw = sorted(
                raw,
                key=lambda x: x[0]
            )

            # Ignore currently forming candle.
            now_ts = int(
                time.time()
            )

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

            time.sleep(
                retry_delay
            )

            retry_delay *= 2

        except Exception as e:

            print(
                f"Coinbase data error "
                f"{asset}: {e}"
            )

            return None

    return None


def get_market_data(
    asset,
    limit=100,
    force_refresh=False
):

    now = time.time()

    with cache_lock:

        cached = market_cache.get(
            asset
        )

        if (
            cached
            and not force_refresh
            and
            (
                now
                - cached["time"]
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

            market_cache[
                asset
            ] = {

                "time":
                    time.time(),

                "data":
                    data
            }

    return data


# ============================================================
# EMA
# ============================================================

def ema(
    values,
    period
):

    if len(values) < period:

        return []

    multiplier = (
        2
        /
        (period + 1)
    )

    current = (
        sum(values[:period])
        /
        period
    )

    result = [
        current
    ]

    for value in values[period:]:

        current = (
            (
                value
                - current
            )
            *
            multiplier
            +
            current
        )

        result.append(
            current
        )

    return result


# ============================================================
# RSI
# ============================================================

def rsi(
    values,
    period=14
):

    if len(values) <= period:

        return []

    gains = []
    losses = []

    for i in range(
        1,
        len(values)
    ):

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

    def current_rsi():

        if avg_loss == 0:

            return 100.0

        rs = (
            avg_gain
            /
            avg_loss
        )

        return (
            100
            -
            (
                100
                /
                (1 + rs)
            )
        )

    result.append(
        current_rsi()
    )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            (
                avg_gain
                *
                (period - 1)
            )
            +
            gains[i]
        ) / period

        avg_loss = (
            (
                avg_loss
                *
                (period - 1)
            )
            +
            losses[i]
        ) / period

        result.append(
            current_rsi()
        )

    return result


# ============================================================
# ATR
# ============================================================

def atr(
    highs,
    lows,
    closes,
    period=14
):

    if len(closes) <= period:

        return []

    true_ranges = []

    for i in range(
        1,
        len(closes)
    ):

        tr = max(

            highs[i]
            -
            lows[i],

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

        true_ranges.append(
            tr
        )

    if len(true_ranges) < period:

        return []

    current = (
        sum(
            true_ranges[:period]
        )
        /
        period
    )

    result = [
        current
    ]

    for tr in true_ranges[period:]:

        current = (
            (
                current
                *
                (period - 1)
            )
            +
            tr
        ) / period

        result.append(
            current
        )

    return result


# ============================================================
# SWING HIGHS
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

        left = highs[
            i - lookback:i
        ]

        right = highs[
            i + 1:
            i + lookback + 1
        ]

        if (
            highs[i] > max(left)
            and
            highs[i] >= max(right)
        ):

            points.append(
                (
                    i,
                    highs[i]
                )
            )

    return points


# ============================================================
# SWING LOWS
# ============================================================

def find_swing_lows(
    lows,
    lookback=2
):

    points = []

    for i in range(
        lookback,
        len(lows) - lookback
    ):

        left = lows[
            i - lookback:i
        ]

        right = lows[
            i + 1:
            i + lookback + 1
        ]

        if (
            lows[i] < min(left)
            and
            lows[i] <= min(right)
        ):

            points.append(
                (
                    i,
                    lows[i]
                )
            )

    return points


# ============================================================
# PATTERN DETECTION
# ============================================================

def detect_patterns(
    highs,
    lows,
    closes
):

    patterns = []

    swing_highs = find_swing_highs(
        highs
    )

    swing_lows = find_swing_lows(
        lows
    )

    recent_highs = swing_highs[-5:]
    recent_lows = swing_lows[-5:]


    # --------------------------------------------------------
    # DOUBLE TOP
    # --------------------------------------------------------

    if len(recent_highs) >= 2:

        h1 = recent_highs[-2][1]
        h2 = recent_highs[-1][1]

        tolerance = (
            abs(h1)
            * 0.0025
        )

        if (
            abs(h1 - h2)
            <= tolerance
            and
            closes[-1] < h2
        ):

            patterns.append(
                (
                    "DOUBLE_TOP",
                    "SELL",
                    16
                )
            )


    # --------------------------------------------------------
    # DOUBLE BOTTOM
    # --------------------------------------------------------

    if len(recent_lows) >= 2:

        l1 = recent_lows[-2][1]
        l2 = recent_lows[-1][1]

        tolerance = (
            abs(l1)
            * 0.0025
        )

        if (
            abs(l1 - l2)
            <= tolerance
            and
            closes[-1] > l2
        ):

            patterns.append(
                (
                    "DOUBLE_BOTTOM",
                    "BUY",
                    16
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
            average * 0.003
            and
            closes[-1] < average
        ):

            patterns.append(
                (
                    "TRIPLE_TOP",
                    "SELL",
                    20
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
            average * 0.003
            and
            closes[-1] > average
        ):

            patterns.append(
                (
                    "TRIPLE_BOTTOM",
                    "BUY",
                    20
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

        tolerance = (
            abs(left)
            * 0.008
        )

        if (
            head > left
            and
            head > right
            and
            abs(left - right)
            <= tolerance
            and
            closes[-1] < right
        ):

            patterns.append(
                (
                    "HEAD_SHOULDERS",
                    "SELL",
                    22
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

        tolerance = (
            abs(left)
            * 0.008
        )

        if (
            head < left
            and
            head < right
            and
            abs(left - right)
            <= tolerance
            and
            closes[-1] > right
        ):

            patterns.append(
                (
                    "INVERSE_HEAD_SHOULDERS",
                    "BUY",
                    22
                )
            )


    # --------------------------------------------------------
    # TREND STRUCTURE
    # --------------------------------------------------------

    if (
        len(recent_highs) >= 3
        and
        len(recent_lows) >= 3
    ):

        h = [
            x[1]
            for x in recent_highs[-3:]
        ]

        l = [
            x[1]
            for x in recent_lows[-3:]
        ]

        higher_highs = (
            h[0]
            <
            h[1]
            <
            h[2]
        )

        higher_lows = (
            l[0]
            <
            l[1]
            <
            l[2]
        )

        lower_highs = (
            h[0]
            >
            h[1]
            >
            h[2]
        )

        lower_lows = (
            l[0]
            >
            l[1]
            >
            l[2]
        )

        if (
            higher_highs
            and
            higher_lows
        ):

            patterns.append(
                (
                    "RISING_STRUCTURE",
                    "BUY",
                    10
                )
            )

        if (
            lower_highs
            and
            lower_lows
        ):

            patterns.append(
                (
                    "FALLING_STRUCTURE",
                    "SELL",
                    10
                )
            )


    return patterns


# ============================================================
# MARKET REGIME
# ============================================================

def market_regime(
    closes,
    ema9_values,
    ema21_values,
    atr_value
):

    if (
        not ema9_values
        or
        not ema21_values
    ):

        return "UNKNOWN"

    e9 = ema9_values[-1]
    e21 = ema21_values[-1]

    price = closes[-1]

    separation = abs(
        e9 - e21
    )

    minimum_separation = (
        atr_value
        * 0.10
    )

    if (
        separation
        <
        minimum_separation
    ):

        return "CHOPPY"

    if (
        e9 > e21
        and
        price > e9
    ):

        return "BULLISH"

    if (
        e9 < e21
        and
        price < e9
    ):

        return "BEARISH"

    return "TRANSITION"


# ============================================================
# BREAKOUT CONFIRMATION
# ============================================================

def breakout_confirmation(
    highs,
    lows,
    closes,
    opens,
    volumes,
    direction,
    atr_value
):

    if len(closes) < 10:

        return False

    previous_high = max(
        highs[-8:-1]
    )

    previous_low = min(
        lows[-8:-1]
    )

    average_volume = (
        sum(volumes[-8:-1])
        /
        7
    )

    if average_volume <= 0:

        return False

    volume_ratio = (
        volumes[-1]
        /
        average_volume
    )

    candle_body = abs(
        closes[-1]
        -
        opens[-1]
    )

    candle_range = (
        highs[-1]
        -
        lows[-1]
    )

    if candle_range <= 0:

        return False

    body_ratio = (
        candle_body
        /
        candle_range
    )

    if direction == "BUY":

        breakout = (
            closes[-1]
            >
            previous_high
            +
            atr_value * 0.08
        )

        strong_candle = (
            closes[-1]
            >
            opens[-1]
            and
            body_ratio >= 0.50
        )

        strong_volume = (
            volume_ratio >= 1.20
        )

        return (
            breakout
            and
            strong_candle
            and
            strong_volume
        )

    if direction == "SELL":

        breakout = (
            closes[-1]
            <
            previous_low
            -
            atr_value * 0.08
        )

        strong_candle = (
            closes[-1]
            <
            opens[-1]
            and
            body_ratio >= 0.50
        )

        strong_volume = (
            volume_ratio >= 1.20
        )

        return (
            breakout
            and
            strong_candle
            and
            strong_volume
        )

    return False


# ============================================================
# ANALYSIS
# ============================================================

def analyze_asset(
    asset
):

    global latest_signal_data

    market = get_market_data(
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

    if len(closes) < 60:

        return None

    ema9_values = ema(
        closes,
        9
    )

    ema21_values = ema(
        closes,
        21
    )

    rsi_values = rsi(
        closes,
        14
    )

    atr_values = atr(
        highs,
        lows,
        closes,
        14
    )

    if not (
        ema9_values
        and
        ema21_values
        and
        rsi_values
        and
        atr_values
    ):

        return None

    price = closes[-1]

    ema9_now = ema9_values[-1]
    ema21_now = ema21_values[-1]
    rsi_now = rsi_values[-1]
    atr_now = atr_values[-1]

    if atr_now <= 0:

        return None

    regime = market_regime(
        closes,
        ema9_values,
        ema21_values,
        atr_now
    )

    # Avoid sideways market.
    if regime in (
        "CHOPPY",
        "UNKNOWN",
        "TRANSITION"
    ):

        return None

    patterns = detect_patterns(
        highs,
        lows,
        closes
    )

    volume_average = (
        sum(volumes[-8:-1])
        /
        7
    )

    volume_ratio = (
        volumes[-1]
        /
        volume_average
        if volume_average > 0
        else 0
    )

    bullish_candle = (
        closes[-1]
        >
        opens[-1]
    )

    bearish_candle = (
        closes[-1]
        <
        opens[-1]
    )

    buy_score = 0
    sell_score = 0

    buy_reasons = []
    sell_reasons = []

    # EMA TREND
    if (
        ema9_now > ema21_now
        and
        regime == "BULLISH"
    ):

        buy_score += 20

        buy_reasons.append(
            "EMA9>EMA21"
        )

    if (
        ema9_now < ema21_now
        and
        regime == "BEARISH"
    ):

        sell_score += 20

        sell_reasons.append(
            "EMA9<EMA21"
        )

    # PRICE LOCATION
    if (
        price > ema9_now
        and
        buy_score > 0
    ):

        buy_score += 10

        buy_reasons.append(
            "Price>EMA9"
        )

    if (
        price < ema9_now
        and
        sell_score > 0
    ):

        sell_score += 10

        sell_reasons.append(
            "Price<EMA9"
        )

    # RSI
    if 52 <= rsi_now <= 67:

        buy_score += 15

        buy_reasons.append(
            "RSI bullish"
        )

    if 33 <= rsi_now <= 48:

        sell_score += 15

        sell_reasons.append(
            "RSI bearish"
        )

    # VOLUME
    if volume_ratio >= 1.20:

        buy_score += 10
        sell_score += 10

    if volume_ratio >= 1.50:

        buy_score += 5
        sell_score += 5

    # CANDLE
    if bullish_candle:

        buy_score += 5

    if bearish_candle:

        sell_score += 5

    # BREAKOUT
    buy_breakout = breakout_confirmation(
        highs,
        lows,
        closes,
        opens,
        volumes,
        "BUY",
        atr_now
    )

    sell_breakout = breakout_confirmation(
        highs,
        lows,
        closes,
        opens,
        volumes,
        "SELL",
        atr_now
    )

    if buy_breakout:

        buy_score += 15

        buy_reasons.append(
            "Volume breakout"
        )

    if sell_breakout:

        sell_score += 15

        sell_reasons.append(
            "Volume breakdown"
        )

    # PATTERNS
    detected_pattern = None

    for (
        pattern_name,
        direction,
        pattern_score
    ) in patterns:

        if direction == "BUY":

            buy_score += pattern_score

            buy_reasons.append(
                pattern_name
            )

            if not detected_pattern:

                detected_pattern = (
                    pattern_name
                )

        elif direction == "SELL":

            sell_score += pattern_score

            sell_reasons.append(
                pattern_name
            )

            if not detected_pattern:

                detected_pattern = (
                    pattern_name
                )

    buy_confirmation = (
        buy_breakout
        or
        any(
            p[1] == "BUY"
            for p in patterns
        )
    )

    sell_confirmation = (
        sell_breakout
        or
        any(
            p[1] == "SELL"
            for p in patterns
        )
    )

    action = None
    score = 0
    reasons = []

    # BUY
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

    # SELL
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

    # ========================================================
    # TP / SL
    # ========================================================

    config = ASSETS[
        asset
    ]

    minimum_sl = config[
        "minimum_sl"
    ]

    sl_distance = max(
        atr_now
        *
        ATR_SL_MULTIPLIER,
        minimum_sl
    )

    tp_distance = (
        sl_distance
        *
        BASE_RR
    )

    if action == "BUY":

        tp = (
            price
            +
            tp_distance
        )

        sl = (
            price
            -
            sl_distance
        )

    else:

        tp = (
            price
            -
            tp_distance
        )

        sl = (
            price
            +
            sl_distance
        )

    signal = {

        "asset_key":
            asset,

        "asset":
            config["display"],

        "action":
            action,

        "price":
            round(
                price,
                2
            ),

        "tp":
            round(
                tp,
                2
            ),

        "sl":
            round(
                sl,
                2
            ),

        "initial_sl":
            round(
                sl,
                2
            ),

        "risk_distance":
            round(
                sl_distance,
                4
            ),

        "score":
            min(
                score,
                100
            ),

        "ema9":
            round(
                ema9_now,
                2
            ),

        "ema21":
            round(
                ema21_now,
                2
            ),

        "rsi":
            round(
                rsi_now,
                2
            ),

        "atr":
            round(
                atr_now,
                4
            ),

        "volume_ratio":
            round(
                volume_ratio,
                2
            ),

        "regime":
            regime,

        "pattern":
            detected_pattern
            or
            "BREAKOUT",

        "reasons":
            reasons[:8],

        "recommended_lot":
            config["lot"]
    }

    latest_signal_data = signal

    return signal


# ============================================================
# SIGNAL CONTROL
# ============================================================

def has_active_signal(
    asset
):

    with state_lock:

        return any(
            signal["asset_key"] == asset
            for signal in active_signals
        )


def cooldown_active(
    asset
):

    last_time = (
        last_signal_time[
            asset
        ]
    )

    if not last_time:

        return False

    return (
        datetime.now()
        -
        last_time
    ) < timedelta(
        minutes=
        SIGNAL_COOLDOWN_MINUTES
    )


# ============================================================
# CREATE SIGNAL
# ============================================================

def analyze_and_trigger(
    asset
):

    if cooldown_active(
        asset
    ):

        return

    if (
        ONE_TRADE_PER_ASSET
        and
        has_active_signal(
            asset
        )
    ):

        return

    signal = analyze_asset(
        asset
    )

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

    direction_emoji = (
        "🟢"
        if signal["action"] == "BUY"
        else
        "🔴"
    )

    reasons = ", ".join(
        signal["reasons"]
    )

    message = (

        "🎯 *HIGH QUALITY 5M SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"

        f"💹 *{signal['asset']}*\n"
        f"⏰ `{signal_time}`\n\n"

        f"{direction_emoji} Action: "
        f"*{signal['action']}*\n"

        f"💰 Entry: "
        f"`{signal['price']}`\n"

        f"🎯 TP: "
        f"`{signal['tp']}`\n"

        f"🛑 Initial SL: "
        f"`{signal['sl']}`\n\n"

        f"⭐ Quality Score: "
        f"*{signal['score']}/100*\n"

        f"📊 Pattern: "
        f"`{signal['pattern']}`\n"

        f"📈 EMA9: "
        f"`{signal['ema9']}`\n"

        f"📉 EMA21: "
        f"`{signal['ema21']}`\n"

        f"📊 RSI: "
        f"`{signal['rsi']}`\n"

        f"🔊 Volume: "
        f"`{signal['volume_ratio']}x`\n"

        f"〽️ ATR: "
        f"`{signal['atr']}`\n"

        f"🌐 Regime: "
        f"`{signal['regime']}`\n\n"

        f"🧠 Confirmation:\n"
        f"`{reasons}`\n\n"

        f"📦 Suggested Lot: "
        f"`{signal['recommended_lot']}`\n\n"

        "🛡️ Management:\n"
        f"BE at `+{BREAK_EVEN_TRIGGER_R}R`\n"
        f"Trailing from `+{TRAIL_TRIGGER_R}R`\n\n"

        "Status: *ACTIVE ⏳*"
    )

    message_id = send_telegram_alert(
        message,
        reply_markup
    )

    if not message_id:

        return

    now = datetime.now()

    trade = {

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

        active_signals.append(
            trade
        )

        last_signal_time[
            asset
        ] = now


# ============================================================
# TRADE MANAGEMENT
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
        signal["action"]
        ==
        "BUY"
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

    # ========================================================
    # BREAK EVEN
    # ========================================================

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

        signal[
            "breakeven_done"
        ] = True

    # ========================================================
    # TRAILING STOP
    # ========================================================

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

def result_r(
    signal,
    exit_price
):

    entry = signal["price"]
    risk = signal["risk_distance"]

    if risk <= 0:

        return 0

    if signal["action"] == "BUY":

        return (
            exit_price
            -
            entry
        ) / risk

    return (
        entry
        -
        exit_price
    ) / risk


# ============================================================
# PERFORMANCE
# ============================================================

def update_history(
    result,
    r_value
):

    with state_lock:

        if result == "WIN":

            trade_history[
                "wins"
            ] += 1

            trade_history[
                "gross_profit_r"
            ] += max(
                r_value,
                0
            )

        elif result == "LOSS":

            trade_history[
                "losses"
            ] += 1

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
                *
                100,

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
# MONITOR TRADES
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
                    30
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

                atr_values = atr(
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
                    signal["action"]
                    ==
                    "BUY"
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

                # Conservative handling:
                # if both TP and SL are touched
                # in same candle, count as LOSS.
                if (
                    tp_hit
                    and
                    sl_hit
                ):

                    exit_price = signal["sl"]

                    result = "LOSS"

                    status = (
                        "⚠️ TP & SL same candle "
                        "— counted as LOSS"
                    )

                elif tp_hit:

                    exit_price = signal["tp"]

                    result = "WIN"

                    status = (
                        "✅ TARGET HIT 🎯"
                    )

                else:

                    exit_price = signal["sl"]

                    r_value = result_r(
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
                            "🛡️ RISK-FREE "
                            "BREAK-EVEN"
                        )

                    else:

                        result = "LOSS"

                        status = (
                            "❌ STOP LOSS HIT"
                        )

                r_value = result_r(
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

                    "🎯 *5M TRADE RESULT*\n"
                    "━━━━━━━━━━━━━━━━━━\n"

                    f"💹 Asset: "
                    f"*{signal['asset']}*\n"

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
# AUTO SCANNER
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
            "Quality 5M Scalping Bot Active",

        "assets": [
            "PAXGUSD",
            "BTCUSD"
        ],

        "timeframe":
            "5M",

        "minimum_score":
            MIN_SIGNAL_SCORE,

        "features": [

            "EMA 9/21",
            "RSI",
            "ATR",
            "Volume",
            "Breakout",
            "Double Top",
            "Double Bottom",
            "Triple Top",
            "Triple Bottom",
            "Head & Shoulders",
            "Inverse Head & Shoulders",
            "Trend Structure",
            "Market Regime",
            "Break Even",
            "Trailing Stop",
            "Coinbase Cache",
            "429 Protection",
            "Telegram Alerts"
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

        "active_signals":
            len(active_signals),

        "assets": [
            "PAXGUSD",
            "BTCUSD"
        ],

        "timeframe":
            "5M"
    })


# ============================================================
# BACKGROUND THREADS
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
