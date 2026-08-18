import os
import time
import logging
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np


# ============================================================
# TRADEBRAIN AI
# RULEBOOK-BASED PAPER TRADING ENGINE
# ============================================================

SYMBOL = os.getenv("SYMBOL", "BTCUSDT").upper()

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))

MIN_CONFLUENCE = int(
    os.getenv("MIN_CONFLUENCE", "85")
)

PAPER_BALANCE = float(
    os.getenv("PAPER_BALANCE", "10000")
)

RISK_PERCENT = float(
    os.getenv("RISK_PERCENT", "1")
)

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", ""
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID", ""
)

BINANCE_URL = "https://api.binance.com"

# Rulebook timeframes
DAILY = "1d"
H4 = "4h"
H1 = "1h"
M15 = "15m"
M5 = "5m"
M1 = "1m"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("TradeBrain")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN:
        logger.warning("Telegram token missing")
        return

    if not TELEGRAM_CHAT_ID:
        logger.warning("Telegram chat ID missing")
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        r = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if r.status_code != 200:
            logger.error(
                "Telegram error: %s",
                r.text[:500]
            )

    except Exception as e:
        logger.error(
            "Telegram exception: %s",
            e
        )


# ============================================================
# BINANCE PUBLIC DATA
# API KEY NOT REQUIRED
# ============================================================

def get_klines(
    symbol,
    interval,
    limit=300
):

    try:

        response = requests.get(
            f"{BINANCE_URL}/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "limit": limit
            },
            timeout=15
        )

        response.raise_for_status()

        raw = response.json()

        columns = [
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
            "quote_volume",
            "trades",
            "taker_base",
            "taker_quote",
            "ignore"
        ]

        df = pd.DataFrame(
            raw,
            columns=columns
        )

        numeric_columns = [
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]

        for col in numeric_columns:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        df["open_time"] = pd.to_datetime(
            df["open_time"],
            unit="ms",
            utc=True
        )

        df["close_time"] = pd.to_datetime(
            df["close_time"],
            unit="ms",
            utc=True
        )

        return df

    except Exception as e:

        logger.error(
            "Binance data error %s %s: %s",
            symbol,
            interval,
            e
        )

        return pd.DataFrame()


# ============================================================
# INDICATORS
# ============================================================

def calculate_atr(
    df,
    period=14
):

    prev_close = df["close"].shift(1)

    tr1 = df["high"] - df["low"]

    tr2 = (
        df["high"] - prev_close
    ).abs()

    tr3 = (
        df["low"] - prev_close
    ).abs()

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    return tr.rolling(
        period
    ).mean()


def calculate_ema(
    df,
    period
):

    return df["close"].ewm(
        span=period,
        adjust=False
    ).mean()


# ============================================================
# SWING DETECTION
# ============================================================

def find_swings(
    df,
    left=2,
    right=2
):

    swing_highs = []
    swing_lows = []

    if len(df) < 20:
        return swing_highs, swing_lows

    for i in range(
        left,
        len(df) - right
    ):

        current_high = float(
            df["high"].iloc[i]
        )

        current_low = float(
            df["low"].iloc[i]
        )

        left_high = float(
            df["high"]
            .iloc[i-left:i]
            .max()
        )

        right_high = float(
            df["high"]
            .iloc[i+1:i+right+1]
            .max()
        )

        left_low = float(
            df["low"]
            .iloc[i-left:i]
            .min()
        )

        right_low = float(
            df["low"]
            .iloc[i+1:i+right+1]
            .min()
        )

        if (
            current_high > left_high
            and
            current_high > right_high
        ):
            swing_highs.append(
                {
                    "index": i,
                    "price": current_high
                }
            )

        if (
            current_low < left_low
            and
            current_low < right_low
        ):
            swing_lows.append(
                {
                    "index": i,
                    "price": current_low
                }
            )

    return swing_highs, swing_lows


# ============================================================
# MARKET STRUCTURE
# HH / HL / LH / LL
# ============================================================

def get_structure(df):

    highs, lows = find_swings(df)

    result = {
        "trend": "UNKNOWN",
        "last_high": None,
        "previous_high": None,
        "last_low": None,
        "previous_low": None,
        "labels": []
    }

    if len(highs) >= 2:

        previous_high = highs[-2]["price"]
        last_high = highs[-1]["price"]

        result["previous_high"] = previous_high
        result["last_high"] = last_high

        if last_high > previous_high:
            result["labels"].append("HH")
        else:
            result["labels"].append("LH")

    if len(lows) >= 2:

        previous_low = lows[-2]["price"]
        last_low = lows[-1]["price"]

        result["previous_low"] = previous_low
        result["last_low"] = last_low

        if last_low > previous_low:
            result["labels"].append("HL")
        else:
            result["labels"].append("LL")

    labels = result["labels"]

    if (
        "HH" in labels
        and "HL" in labels
    ):
        result["trend"] = "BULLISH"

    elif (
        "LH" in labels
        and "LL" in labels
    ):
        result["trend"] = "BEARISH"

    elif labels:
        result["trend"] = "RANGE"

    return result


# ============================================================
# TWO CONSECUTIVE CLOSES
# ============================================================

def two_consecutive_closes(
    df,
    level,
    direction
):

    if level is None:
        return False

    if len(df) < 3:
        return False

    close_1 = float(
        df["close"].iloc[-2]
    )

    close_2 = float(
        df["close"].iloc[-1]
    )

    if direction == "BULLISH":

        return (
            close_1 > level
            and
            close_2 > level
        )

    if direction == "BEARISH":

        return (
            close_1 < level
            and
            close_2 < level
        )

    return False


# ============================================================
# BOS
# ============================================================

def detect_bos(df):

    structure = get_structure(df)

    if (
        structure["last_high"] is None
        or
        structure["last_low"] is None
    ):
        return "NONE"

    if two_consecutive_closes(
        df,
        structure["last_high"],
        "BULLISH"
    ):
        return "BULLISH_BOS"

    if two_consecutive_closes(
        df,
        structure["last_low"],
        "BEARISH"
    ):
        return "BEARISH_BOS"

    return "NONE"


# ============================================================
# CHOCH
#
# Simplified programmable interpretation:
# previous directional structure + opposite structural break
# + two consecutive closes.
# ============================================================

def detect_choch(df):

    if len(df) < 40:
        return "NONE"

    structure = get_structure(df)

    if (
        structure["last_high"] is None
        or
        structure["last_low"] is None
    ):
        return "NONE"

    trend = structure["trend"]

    if trend == "BEARISH":

        if two_consecutive_closes(
            df,
            structure["last_high"],
            "BULLISH"
        ):
            return "BULLISH_CHOCH"

    if trend == "BULLISH":

        if two_consecutive_closes(
            df,
            structure["last_low"],
            "BEARISH"
        ):
            return "BEARISH_CHOCH"

    return "NONE"


# ============================================================
# CANDLE CONFIRMATION
# ============================================================

def confirmation_candle(df):

    if len(df) < 3:
        return "NONE"

    candle = df.iloc[-1]

    body = abs(
        float(candle["close"])
        -
        float(candle["open"])
    )

    candle_range = (
        float(candle["high"])
        -
        float(candle["low"])
    )

    if candle_range <= 0:
        return "NONE"

    body_ratio = (
        body / candle_range
    )

    if (
        candle["close"] >
        candle["open"]
        and
        body_ratio >= 0.55
    ):
        return "BULLISH"

    if (
        candle["close"] <
        candle["open"]
        and
        body_ratio >= 0.55
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# 2-CANDLE RETRACEMENT
# ============================================================

def detect_two_candle_retracement(
    df,
    direction
):

    if len(df) < 5:
        return False

    c1 = df.iloc[-2]
    c2 = df.iloc[-1]

    if direction == "BULLISH":

        return (
            c1["close"] < c1["open"]
            and
            c2["close"] > c2["open"]
        )

    if direction == "BEARISH":

        return (
            c1["close"] > c1["open"]
            and
            c2["close"] < c2["open"]
        )

    return False


# ============================================================
# SUPPLY / DEMAND ZONE
# ============================================================

def detect_zones(df):

    structure = get_structure(df)

    atr_series = calculate_atr(df)

    atr = atr_series.iloc[-1]

    if pd.isna(atr):
        atr = float(
            df["close"].iloc[-1]
        ) * 0.002

    atr = float(atr)

    demand = None
    supply = None

    if structure["last_low"] is not None:

        low = structure["last_low"]

        demand = (
            low - atr * 0.25,
            low + atr * 0.25
        )

    if structure["last_high"] is not None:

        high = structure["last_high"]

        supply = (
            high - atr * 0.25,
            high + atr * 0.25
        )

    return {
        "demand": demand,
        "supply": supply
    }


# ============================================================
# FVG
# ============================================================

def detect_fvg(df):

    if len(df) < 4:

        return {
            "bullish": False,
            "bearish": False,
            "zone": None
        }

    first = df.iloc[-3]
    third = df.iloc[-1]

    # Bullish FVG
    if (
        float(third["low"])
        >
        float(first["high"])
    ):

        return {
            "bullish": True,
            "bearish": False,
            "zone": (
                float(first["high"]),
                float(third["low"])
            )
        }

    # Bearish FVG
    if (
        float(third["high"])
        <
        float(first["low"])
    ):

        return {
            "bullish": False,
            "bearish": True,
            "zone": (
                float(third["high"]),
                float(first["low"])
            )
        }

    return {
        "bullish": False,
        "bearish": False,
        "zone": None
    }


# ============================================================
# PRICE TAP
# ============================================================

def price_tapped_zone(
    price,
    zone
):

    if zone is None:
        return False

    low = min(
        zone[0],
        zone[1]
    )

    high = max(
        zone[0],
        zone[1]
    )

    return (
        low <= price <= high
    )


# ============================================================
# LIQUIDITY
# ============================================================

def detect_liquidity(df):

    if len(df) < 20:

        return {
            "bullish": False,
            "bearish": False
        }

    structure = get_structure(df)

    last_high = structure["last_high"]
    last_low = structure["last_low"]

    price = float(
        df["close"].iloc[-1]
    )

    bullish = False
    bearish = False

    if last_low is not None:

        # Price recently swept below a low
        recent_low = float(
            df["low"].tail(5).min()
        )

        if recent_low < last_low:
            bullish = True

    if last_high is not None:

        recent_high = float(
            df["high"].tail(5).max()
        )

        if recent_high > last_high:
            bearish = True

    return {
        "bullish": bullish,
        "bearish": bearish
    }


# ============================================================
# TGL
#
# IMPORTANT:
# The transcript available to us does not contain a complete
# mathematical TGL formula.
#
# Therefore this function deliberately stays isolated.
# When the exact Rulebook formula is available, ONLY this
# function needs replacement.
# ============================================================

def calculate_tgl(df):

    structure = get_structure(df)

    high = structure["last_high"]
    low = structure["last_low"]

    if high is None or low is None:

        return {
            "level1": None,
            "level2": None
        }

    movement = abs(
        high - low
    )

    if structure["trend"] == "BULLISH":

        level1 = high

        level2 = (
            high
            +
            movement * 0.50
        )

    elif structure["trend"] == "BEARISH":

        level1 = low

        level2 = (
            low
            -
            movement * 0.50
        )

    else:

        level1 = high
        level2 = low

    return {
        "level1": float(level1),
        "level2": float(level2)
    }


# ============================================================
# HTF PRIORITY
#
# Rulebook:
#
# Daily = 4H = 1H
#       -> 1H priority
#
# 1H opposite Daily + 4H
#       -> 4H priority
#
# 4H + 1H opposite Daily
#       -> Daily priority
# ============================================================

def select_priority(
    daily,
    h4,
    h1
):

    d = daily["trend"]
    four = h4["trend"]
    one = h1["trend"]

    if (
        d == four
        and
        four == one
        and
        d in ["BULLISH", "BEARISH"]
    ):

        return {
            "timeframe": H1,
            "data": h1,
            "direction": d
        }

    if (
        d == four
        and
        one != d
        and
        d in ["BULLISH", "BEARISH"]
    ):

        return {
            "timeframe": H4,
            "data": h4,
            "direction": d
        }

    if (
        four == one
        and
        four != d
        and
        four in ["BULLISH", "BEARISH"]
    ):

        return {
            "timeframe": DAILY,
            "data": daily,
            "direction": d
        }

    return None


# ============================================================
# REQUIRED LOWER TF
#
# 1H -> 1M
# 4H -> 5M
# Daily -> 1H + 1M
# ============================================================

def required_choch(
    priority_tf,
    data,
    direction
):

    expected = (
        "BULLISH_CHOCH"
        if direction == "BULLISH"
        else "BEARISH_CHOCH"
    )

    if priority_tf == H1:

        return (
            data[M1]["choch"] == expected
        )

    if priority_tf == H4:

        return (
            data[M5]["choch"] == expected
        )

    if priority_tf == DAILY:

        return (
            data[H1]["choch"] == expected
            and
            data[M1]["choch"] == expected
        )

    return False


# ============================================================
# FAKEOUT FILTER
# ============================================================

def is_fakeout(
    df,
    direction
):

    if len(df) < 5:
        return True

    current = df.iloc[-1]
    previous = df.iloc[-2]

    high = float(current["high"])
    low = float(current["low"])
    close = float(current["close"])
    open_price = float(current["open"])

    candle_range = high - low

    if candle_range <= 0:
        return True

    body = abs(
        close - open_price
    )

    body_ratio = (
        body / candle_range
    )

    # Weak candle
    if body_ratio < 0.30:
        return True

    previous_high = float(
        previous["high"]
    )

    previous_low = float(
        previous["low"]
    )

    # Bullish breakout rejected
    if direction == "BULLISH":

        if (
            high > previous_high
            and
            close < previous_high
        ):
            return True

    # Bearish breakout rejected
    if direction == "BEARISH":

        if (
            low < previous_low
            and
            close > previous_low
        ):
            return True

    return False


# ============================================================
# ALREADY PLAYED LEVEL
# ============================================================

def level_already_played(
    df,
    zone,
    lookback=40
):

    if zone is None:
        return False

    low = min(
        zone[0],
        zone[1]
    )

    high = max(
        zone[0],
        zone[1]
    )

    recent = df.tail(
        lookback
    )

    touches = 0

    for _, candle in recent.iterrows():

        candle_high = float(
            candle["high"]
        )

        candle_low = float(
            candle["low"]
        )

        if (
            candle_high >= low
            and
            candle_low <= high
        ):
            touches += 1

    # Conservative filter.
    # Exact Rulebook retest count should replace this
    # when the transcript gives an explicit number.
    return touches >= 3


# ============================================================
# EMA DIRECTION
# ============================================================

def ema_confirmation(
    df,
    direction
):

    if len(df) < 50:
        return False

    ema20 = calculate_ema(
        df,
        20
    )

    ema50 = calculate_ema(
        df,
        50
    )

    if direction == "BULLISH":

        return (
            ema20.iloc[-1]
            >
            ema50.iloc[-1]
        )

    if direction == "BEARISH":

        return (
            ema20.iloc[-1]
            <
            ema50.iloc[-1]
        )

    return False


# ============================================================
# LOAD ALL TIMEFRAMES
# ============================================================

def load_market():

    timeframes = [
        DAILY,
        H4,
        H1,
        M15,
        M5,
        M1
    ]

    market = {}

    for tf in timeframes:

        df = get_klines(
            SYMBOL,
            tf,
            300
        )

        if df.empty:
            return None

        market[tf] = {
            "df": df,
            "structure":
                get_structure(df),
            "bos":
                detect_bos(df),
            "choch":
                detect_choch(df),
            "fvg":
                detect_fvg(df),
            "zones":
                detect_zones(df),
            "liquidity":
                detect_liquidity(df),
            "confirmation":
                confirmation_candle(df),
            "tgl":
                calculate_tgl(df)
        }

    return market


# ============================================================
# RULEBOOK ANALYSIS
# ============================================================

def analyze_rulebook(
    market
):

    price = float(
        market[M1]["df"]
        ["close"]
        .iloc[-1]
    )

    daily = market[DAILY]["structure"]
    h4 = market[H4]["structure"]
    h1 = market[H1]["structure"]

    result = {

        "signal": "WAIT",

        "price": price,

        "direction": "NONE",

        "priority_tf": "NONE",

        "confidence": 0,

        "mandatory_pass": False,

        "checks": {},

        "entry": None,

        "sl": None,

        "tp1": None,

        "tp2": None,

        "reasons": []

    }

    # --------------------------------------------------------
    # HTF PRIORITY
    # --------------------------------------------------------

    priority = select_priority(
        daily,
        h4,
        h1
    )

    if priority is None:

        result["reasons"].append(
            "Daily/4H/1H do not satisfy "
            "the Rulebook priority condition"
        )

        return result

    direction = priority["direction"]
    priority_tf = priority["timeframe"]

    result["direction"] = direction
    result["priority_tf"] = priority_tf

    result["checks"][
        "HTF_DIRECTION"
    ] = True

    result["reasons"].append(
        f"{priority_tf} priority selected "
        f"with {direction} direction"
    )

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    selected_structure = priority[
        "data"
    ]

    if direction == "BULLISH":

        structure_ok = (
            "HH"
            in selected_structure["labels"]
            and
            "HL"
            in selected_structure["labels"]
        )

    else:

        structure_ok = (
            "LH"
            in selected_structure["labels"]
            and
            "LL"
            in selected_structure["labels"]
        )

    result["checks"][
        "STRUCTURE"
    ] = structure_ok

    if structure_ok:

        result["reasons"].append(
            "Valid directional structure"
        )

    else:

        result["reasons"].append(
            "Directional structure not confirmed"
        )

    # --------------------------------------------------------
    # PRICE TAP
    # --------------------------------------------------------

    if direction == "BULLISH":

        zone = selected_structure[
            "data"
            if False else "zones"
        ]["demand"]

    else:

        zone = selected_structure[
            "data"
            if False else "zones"
        ]["supply"]

    # Above expression resolves to selected_structure["zones"]
    # and keeps the logic explicit.

    tapped = price_tapped_zone(
        price,
        zone
    )

    result["checks"][
        "PRICE_TAP"
    ] = tapped

    if tapped:

        result["reasons"].append(
            "Price tapped selected HTF zone"
        )

    else:

        result["reasons"].append(
            "Waiting for price to tap selected HTF zone"
        )

    # --------------------------------------------------------
    # ALREADY PLAYED
    # --------------------------------------------------------

    already_played = level_already_played(
        selected_structure["data"]
        if "data" in selected_structure
        else market[priority_tf]["df"],
        zone
    )

    # Correct dataframe:
    selected_df = market[
        priority_tf
    ]["df"]

    already_played = level_already_played(
        selected_df,
        zone
    )

    result["checks"][
        "LEVEL_NOT_PLAYED"
    ] = not already_played

    if already_played:

        result["reasons"].append(
            "Selected level appears already played/retested"
        )

    else:

        result["reasons"].append(
            "Level-retouch filter passed"
        )

    # --------------------------------------------------------
    # LOWER TF CHOCH
    # --------------------------------------------------------

    choch_ok = required_choch(
        priority_tf,
        market,
        direction
    )

    result["checks"][
        "LOWER_TF_CHOCH"
    ] = choch_ok

    if choch_ok:

        result["reasons"].append(
            "Required lower-TF CHOCH confirmed"
        )

    else:

        if priority_tf == H1:

            required_tf = "1M"

        elif priority_tf == H4:

            required_tf = "5M"

        else:

            required_tf = "1H + 1M"

        result["reasons"].append(
            f"Waiting for {required_tf} CHOCH"
        )

    # --------------------------------------------------------
    # 2 CANDLE RETRACEMENT
    # --------------------------------------------------------

    retracement_ok = detect_two_candle_retracement(
        market[M1]["df"],
        direction
    )

    result["checks"][
        "TWO_CANDLE_RETRACEMENT"
    ] = retracement_ok

    if retracement_ok:

        result["reasons"].append(
            "2-candle retracement detected"
        )

    else:

        result["reasons"].append(
            "2-candle retracement not confirmed"
        )

    # --------------------------------------------------------
    # CONFIRMATION CANDLE
    # --------------------------------------------------------

    confirmation = market[M1][
        "confirmation"
    ]

    confirmation_ok = (
        confirmation == direction
    )

    result["checks"][
        "CONFIRMATION"
    ] = confirmation_ok

    if confirmation_ok:

        result["reasons"].append(
            "Entry confirmation candle valid"
        )

    else:

        result["reasons"].append(
            "Waiting for directional confirmation candle"
        )

    # --------------------------------------------------------
    # FAKEOUT
    # --------------------------------------------------------

    fakeout = is_fakeout(
        market[M1]["df"],
        direction
    )

    fakeout_ok = not fakeout

    result["checks"][
        "NO_FAKEOUT"
    ] = fakeout_ok

    if fakeout_ok:

        result["reasons"].append(
            "Fakeout filter passed"
        )

    else:

        result["reasons"].append(
            "Possible fakeout detected"
        )

    # --------------------------------------------------------
    # FVG
    # --------------------------------------------------------

    fvg = market[M5]["fvg"]

    if direction == "BULLISH":

        fvg_ok = fvg["bullish"]

    else:

        fvg_ok = fvg["bearish"]

    result["checks"][
        "FVG"
    ] = fvg_ok

    if fvg_ok:

        result["reasons"].append(
            "Directional FVG present"
        )

    else:

        result["reasons"].append(
            "No directional FVG"
        )

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    liquidity = market[M5][
        "liquidity"
    ]

    if direction == "BULLISH":

        liquidity_ok = liquidity[
            "bullish"
        ]

    else:

        liquidity_ok = liquidity[
            "bearish"
        ]

    result["checks"][
        "LIQUIDITY"
    ] = liquidity_ok

    if liquidity_ok:

        result["reasons"].append(
            "Liquidity interaction detected"
        )

    else:

        result["reasons"].append(
            "Liquidity confirmation not detected"
        )

    # --------------------------------------------------------
    # EMA = SUPPORTING CONFLUENCE ONLY
    # --------------------------------------------------------

    ema_ok = ema_confirmation(
        market[M15]["df"],
        direction
    )

    result["checks"][
        "EMA_SUPPORT"
    ] = ema_ok

    # --------------------------------------------------------
    # CONFLUENCE SCORE
    #
    # This is NOT win probability.
    # It measures technical confluence.
    # --------------------------------------------------------

    weights = {

        "HTF_DIRECTION": 20,

        "STRUCTURE": 15,

        "PRICE_TAP": 15,

        "LOWER_TF_CHOCH": 20,

        "TWO_CANDLE_RETRACEMENT": 10,

        "CONFIRMATION": 10,

        "NO_FAKEOUT": 5,

        "FVG": 2,

        "LIQUIDITY": 2,

        "EMA_SUPPORT": 1

    }

    score = 0

    for key, weight in weights.items():

        if result["checks"].get(
            key,
            False
        ):

            score += weight

    result["confidence"] = score

    # --------------------------------------------------------
    # MANDATORY CONDITIONS
    #
    # Direct BUY/SELL is forbidden unless these pass.
    # --------------------------------------------------------

    mandatory = [

        result["checks"].get(
            "HTF_DIRECTION",
            False
        ),

        result["checks"].get(
            "STRUCTURE",
            False
        ),

        result["checks"].get(
            "PRICE_TAP",
            False
        ),

        result["checks"].get(
            "LEVEL_NOT_PLAYED",
            False
        ),

        result["checks"].get(
            "LOWER_TF_CHOCH",
            False
        ),

        result["checks"].get(
            "TWO_CANDLE_RETRACEMENT",
            False
        ),

        result["checks"].get(
            "CONFIRMATION",
            False
        ),

        result["checks"].get(
            "NO_FAKEOUT",
            False
        )

    ]

    all_mandatory = all(
        mandatory
    )

    result["mandatory_pass"] = (
        all_mandatory
    )

    # --------------------------------------------------------
    # FINAL DECISION
    # --------------------------------------------------------

    if (
        all_mandatory
        and
        score >= MIN_CONFLUENCE
    ):

        result["signal"] = (
            "BUY"
            if direction == "BULLISH"
            else "SELL"
        )

    else:

        result["signal"] = "WAIT"

    # --------------------------------------------------------
    # ENTRY / SL / TP
    # --------------------------------------------------------

    if result["signal"] in [
        "BUY",
        "SELL"
    ]:

        df = market[M1]["df"]

        atr = calculate_atr(
            df
        ).iloc[-1]

        if pd.isna(atr):

            atr = (
                price * 0.002
            )

        atr = float(atr)

        selected = market[
            priority_tf
        ]

        structure = selected[
            "structure"
        ]

        tgl = selected[
            "tgl"
        ]

        if direction == "BULLISH":

            entry = price

            swing_low = (
                structure["last_low"]
            )

            if swing_low is not None:

                sl = (
                    swing_low
                    - atr * 0.20
                )

            else:

                sl = (
                    entry
                    - atr * 1.5
                )

            risk = entry - sl

            tp1 = tgl["level1"]
            tp2 = tgl["level2"]

            if (
                tp1 is None
                or
                tp1 <= entry
            ):

                tp1 = (
                    entry
                    + risk * 1.5
                )

            if (
                tp2 is None
                or
                tp2 <= tp1
            ):

                tp2 = (
                    entry
                    + risk * 2.5
                )

        else:

            entry = price

            swing_high = (
                structure["last_high"]
            )

            if swing_high is not None:

                sl = (
                    swing_high
                    + atr * 0.20
                )

            else:

                sl = (
                    entry
                    + atr * 1.5
                )

            risk = sl - entry

            tp1 = tgl["level1"]
            tp2 = tgl["level2"]

            if (
                tp1 is None
                or
                tp1 >= entry
            ):

                tp1 = (
                    entry
                    - risk * 1.5
                )

            if (
                tp2 is None
                or
                tp2 >= tp1
            ):

                tp2 = (
                    entry
                    - risk * 2.5
                )

        result["entry"] = float(entry)
        result["sl"] = float(sl)
        result["tp1"] = float(tp1)
        result["tp2"] = float(tp2)

    return result


# ============================================================
# PAPER TRADER
# ============================================================

class PaperTrader:

    def __init__(self):

        self.balance = PAPER_BALANCE

        self.trade = None

        self.total = 0
        self.wins = 0
        self.losses = 0

    def open(
        self,
        setup
    ):

        if self.trade is not None:
            return False

        if setup["signal"] not in [
            "BUY",
            "SELL"
        ]:
            return False

        self.trade = {

            "side":
                setup["signal"],

            "entry":
                setup["entry"],

            "sl":
                setup["sl"],

            "tp1":
                setup["tp1"],

            "tp2":
                setup["tp2"],

            "opened":
                datetime.now(
                    timezone.utc
                ).isoformat()

        }

        self.total += 1

        return True

    def check(
        self,
        price
    ):

        if self.trade is None:
            return None

        trade = self.trade

        side = trade["side"]

        result = None
        exit_price = None

        if side == "BUY":

            if price <= trade["sl"]:

                result = "LOSS"
                exit_price = trade["sl"]

            elif price >= trade["tp2"]:

                result = "WIN"
                exit_price = trade["tp2"]

        elif side == "SELL":

            if price >= trade["sl"]:

                result = "LOSS"
                exit_price = trade["sl"]

            elif price <= trade["tp2"]:

                result = "WIN"
                exit_price = trade["tp2"]

        if result is None:
            return None

        if side == "BUY":

            pnl = (
                exit_price
                -
                trade["entry"]
            )

        else:

            pnl = (
                trade["entry"]
                -
                exit_price
            )

        if result == "WIN":
            self.wins += 1
        else:
            self.losses += 1

        self.balance += pnl

        completed = {
            **trade,
            "exit": exit_price,
            "result": result,
            "pnl": pnl
        }

        self.trade = None

        return completed

    def win_rate(self):

        if self.total == 0:
            return 0

        return (
            self.wins
            /
            self.total
        ) * 100


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def setup_message(
    setup
):

    if setup["signal"] == "BUY":
        icon = "🟢"
    elif setup["signal"] == "SELL":
        icon = "🔴"
    else:
        icon = "🟡"

    lines = []

    for key, value in setup[
        "checks"
    ].items():

        symbol = (
            "✓"
            if value
            else "✗"
        )

        lines.append(
            f"{symbol} {key}"
        )

    checks_text = "\n".join(
        lines
    )

    reasons_text = "\n".join(
        f"• {x}"
        for x in setup["reasons"]
    )

    text = (

        f"{icon} <b>TRADEBRAIN AI</b>\n\n"

        f"<b>Symbol:</b> {SYMBOL}\n"

        f"<b>Signal:</b> "
        f"{setup['signal']}\n"

        f"<b>Price:</b> "
        f"{setup['price']:.2f}\n"

        f"<b>Direction:</b> "
        f"{setup['direction']}\n"

        f"<b>Priority:</b> "
        f"{setup['priority_tf']}\n"

        f"<b>Confluence:</b> "
        f"{setup['confidence']}/100\n\n"

        f"<b>Rulebook Checks</b>\n"
        f"{checks_text}\n\n"

        f"<b>Analysis</b>\n"
        f"{reasons_text}"
    )

    if setup["signal"] in [
        "BUY",
        "SELL"
    ]:

        text += (

            "\n\n"
            f"<b>ENTRY:</b> "
            f"{setup['entry']:.2f}\n"

            f"<b>SL:</b> "
            f"{setup['sl']:.2f}\n"

            f"<b>TP1:</b> "
            f"{setup['tp1']:.2f}\n"

            f"<b>TP2:</b> "
            f"{setup['tp2']:.2f}\n"

            "\n<b>MODE:</b> PAPER TRADE"
        )

    return text


def result_message(
    result,
    trader
):

    icon = (
        "✅"
        if result["result"] == "WIN"
        else "❌"
    )

    return (

        f"{icon} <b>PAPER TRADE "
        f"{result['result']}</b>\n\n"

        f"<b>Symbol:</b> {SYMBOL}\n"

        f"<b>Side:</b> "
        f"{result['side']}\n"

        f"<b>Entry:</b> "
        f"{result['entry']:.2f}\n"

        f"<b>Exit:</b> "
        f"{result['exit']:.2f}\n"

        f"<b>P/L:</b> "
        f"{result['pnl']:.4f}\n\n"

        f"<b>Total Trades:</b> "
        f"{trader.total}\n"

        f"<b>Wins:</b> "
        f"{trader.wins}\n"

        f"<b>Losses:</b> "
        f"{trader.losses}\n"

        f"<b>Win Rate:</b> "
        f"{trader.win_rate():.2f}%\n"

        f"<b>Balance:</b> "
        f"{trader.balance:.2f}"
    )


# ============================================================
# DUPLICATE SIGNAL PROTECTION
# ============================================================

last_trade_signal = None


def signal_is_new(
    setup
):

    global last_trade_signal

    if setup["signal"] not in [
        "BUY",
        "SELL"
    ]:
        return False

    candle_time = (
        setup["price"],
        setup["priority_tf"],
        setup["direction"]
    )

    if candle_time == last_trade_signal:

        return False

    last_trade_signal = candle_time

    return True


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "=============================================="
    )

    logger.info(
        "TradeBrain Rulebook Engine STARTED"
    )

    logger.info(
        "Symbol: %s",
        SYMBOL
    )

    logger.info(
        "Paper Trading: ON"
    )

    logger.info(
        "Live Trading: OFF"
    )

    logger.info(
        "Minimum Confluence: %s",
        MIN_CONFLUENCE
    )

    logger.info(
        "=============================================="
    )

    send_telegram(

        "🚀 <b>TradeBrain AI Started</b>\n\n"

        f"Symbol: {SYMBOL}\n"

        "Mode: PAPER TRADING\n"

        "Live trading: OFF\n\n"

        "Rulebook engine: ACTIVE"
    )

    trader = PaperTrader()

    while True:

        try:

            market = load_market()

            if market is None:

                logger.warning(
                    "Market data unavailable"
                )

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            price = float(
                market[M1]["df"]
                ["close"]
                .iloc[-1]
            )

            # ------------------------------------------------
            # Existing paper trade
            # ------------------------------------------------

            completed = trader.check(
                price
            )

            if completed:

                logger.info(
                    "PAPER %s | P/L %.4f",
                    completed["result"],
                    completed["pnl"]
                )

                send_telegram(
                    result_message(
                        completed,
                        trader
                    )
                )

            # ------------------------------------------------
            # Rulebook analysis
            # ------------------------------------------------

            setup = analyze_rulebook(
                market
            )

            logger.info(

                "%s | Price %.2f | "
                "Confidence %d/100 | "
                "Direction %s | "
                "Priority %s",

                setup["signal"],

                price,

                setup["confidence"],

                setup["direction"],

                setup["priority_tf"]
            )

            # ------------------------------------------------
            # Send BUY / SELL only
            # ------------------------------------------------

            if (
                setup["signal"]
                in ["BUY", "SELL"]
                and
                signal_is_new(setup)
            ):

                send_telegram(
                    setup_message(
                        setup
                    )
                )

                opened = trader.open(
                    setup
                )

                if opened:

                    send_telegram(

                        "📌 <b>PAPER TRADE OPENED</b>\n\n"

                        f"<b>Side:</b> "
                        f"{setup['signal']}\n"

                        f"<b>Entry:</b> "
                        f"{setup['entry']:.2f}\n"

                        f"<b>SL:</b> "
                        f"{setup['sl']:.2f}\n"

                        f"<b>TP1:</b> "
                        f"{setup['tp1']:.2f}\n"

                        f"<b>TP2:</b> "
                        f"{setup['tp2']:.2f}"
                    )

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            logger.info(
                "Bot stopped manually"
            )

            break

        except Exception as e:

            logger.exception(
                "Main loop error"
            )

            try:

                send_telegram(

                    "⚠️ <b>TradeBrain Error</b>\n\n"

                    f"<code>{str(e)[:500]}</code>\n\n"

                    "Bot will retry."
                )

            except Exception:
                pass

            time.sleep(30)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
