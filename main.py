import os
import time
import json
import logging
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np


# ============================================================
# TRADEBRAIN AI
# BTCUSDT RULEBOOK-BASED PAPER TRADING ENGINE
# ============================================================

SYMBOL = "BTCUSDT"

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
    "TELEGRAM_BOT_TOKEN",
    ""
)

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
)

BINANCE_URL = "https://api.binance.com"

STATE_FILE = "tradebrain_btcusdt_state.json"

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
        return

    if not TELEGRAM_CHAT_ID:
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

        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.status_code != 200:

            logger.error(
                "Telegram error: %s",
                response.text[:300]
            )

    except Exception as e:

        logger.error(
            "Telegram exception: %s",
            e
        )


# ============================================================
# BINANCE DATA
# ============================================================

def get_klines(
    symbol,
    interval,
    limit=300,
    retries=3
):

    for attempt in range(retries):

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

            if not isinstance(raw, list) or len(raw) < 20:
                raise ValueError("Invalid Binance response")

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

            df = df.dropna(
                subset=[
                    "open",
                    "high",
                    "low",
                    "close"
                ]
            )

            # ------------------------------------------------
            # IMPORTANT:
            # Remove current unfinished candle.
            # ------------------------------------------------

            now = pd.Timestamp.now(
                tz="UTC"
            )

            df = df[
                df["close_time"] <= now
            ].copy()

            return df.reset_index(
                drop=True
            )

        except Exception as e:

            logger.warning(
                "Binance %s %s attempt %d/%d: %s",
                symbol,
                interval,
                attempt + 1,
                retries,
                e
            )

            if attempt < retries - 1:
                time.sleep(2)

    return pd.DataFrame()


# ============================================================
# ATR
# ============================================================

def calculate_atr(
    df,
    period=14
):

    if len(df) < period + 2:
        return pd.Series(
            np.nan,
            index=df.index
        )

    prev_close = df["close"].shift(1)

    tr1 = (
        df["high"]
        -
        df["low"]
    )

    tr2 = (
        df["high"]
        -
        prev_close
    ).abs()

    tr3 = (
        df["low"]
        -
        prev_close
    ).abs()

    tr = pd.concat(
        [
            tr1,
            tr2,
            tr3
        ],
        axis=1
    ).max(axis=1)

    return tr.rolling(
        period
    ).mean()


# ============================================================
# EMA
# ============================================================

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

    if len(df) < left + right + 5:
        return swing_highs, swing_lows

    for i in range(
        left,
        len(df) - right
    ):

        high = float(
            df["high"].iloc[i]
        )

        low = float(
            df["low"].iloc[i]
        )

        left_high = float(
            df["high"]
            .iloc[
                i-left:i
            ]
            .max()
        )

        right_high = float(
            df["high"]
            .iloc[
                i+1:i+right+1
            ]
            .max()
        )

        left_low = float(
            df["low"]
            .iloc[
                i-left:i
            ]
            .min()
        )

        right_low = float(
            df["low"]
            .iloc[
                i+1:i+right+1
            ]
            .min()
        )

        if (
            high > left_high
            and
            high > right_high
        ):

            swing_highs.append(
                {
                    "index": i,
                    "price": high,
                    "time": str(
                        df["close_time"].iloc[i]
                    )
                }
            )

        if (
            low < left_low
            and
            low < right_low
        ):

            swing_lows.append(
                {
                    "index": i,
                    "price": low,
                    "time": str(
                        df["close_time"].iloc[i]
                    )
                }
            )

    return swing_highs, swing_lows


# ============================================================
# MARKET STRUCTURE
# ============================================================

def get_structure(df):

    highs, lows = find_swings(df)

    result = {

        "trend": "UNKNOWN",

        "last_high": None,
        "previous_high": None,

        "last_low": None,
        "previous_low": None,

        "last_high_index": None,
        "last_low_index": None,

        "labels": []
    }

    if len(highs) >= 2:

        previous = highs[-2]
        last = highs[-1]

        result["previous_high"] = previous["price"]
        result["last_high"] = last["price"]
        result["last_high_index"] = last["index"]

        if last["price"] > previous["price"]:

            result["labels"].append("HH")

        else:

            result["labels"].append("LH")

    if len(lows) >= 2:

        previous = lows[-2]
        last = lows[-1]

        result["previous_low"] = previous["price"]
        result["last_low"] = last["price"]
        result["last_low_index"] = last["index"]

        if last["price"] > previous["price"]:

            result["labels"].append("HL")

        else:

            result["labels"].append("LL")

    labels = result["labels"]

    if (
        "HH" in labels
        and
        "HL" in labels
    ):

        result["trend"] = "BULLISH"

    elif (
        "LH" in labels
        and
        "LL" in labels
    ):

        result["trend"] = "BEARISH"

    elif labels:

        result["trend"] = "RANGE"

    return result


# ============================================================
# TWO CLOSES
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

    c1 = float(
        df["close"].iloc[-2]
    )

    c2 = float(
        df["close"].iloc[-1]
    )

    if direction == "BULLISH":

        return (
            c1 > level
            and
            c2 > level
        )

    if direction == "BEARISH":

        return (
            c1 < level
            and
            c2 < level
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
# ============================================================

def detect_choch(df):

    if len(df) < 40:
        return "NONE"

    structure = get_structure(df)

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

    open_price = float(
        candle["open"]
    )

    close = float(
        candle["close"]
    )

    high = float(
        candle["high"]
    )

    low = float(
        candle["low"]
    )

    candle_range = high - low

    if candle_range <= 0:
        return "NONE"

    body = abs(
        close - open_price
    )

    body_ratio = (
        body / candle_range
    )

    if (
        close > open_price
        and
        body_ratio >= 0.55
    ):

        return "BULLISH"

    if (
        close < open_price
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
            float(c1["close"])
            <
            float(c1["open"])
            and
            float(c2["close"])
            >
            float(c2["open"])
        )

    if direction == "BEARISH":

        return (
            float(c1["close"])
            >
            float(c1["open"])
            and
            float(c2["close"])
            <
            float(c2["open"])
        )

    return False


# ============================================================
# DEMAND / SUPPLY
# ============================================================

def detect_zones(df):

    structure = get_structure(df)

    atr_series = calculate_atr(df)

    atr = atr_series.iloc[-1]

    if pd.isna(atr):

        atr = (
            float(df["close"].iloc[-1])
            * 0.002
        )

    atr = float(atr)

    demand = None
    supply = None

    if structure["last_low"] is not None:

        low = float(
            structure["last_low"]
        )

        demand = (
            low - atr * 0.30,
            low + atr * 0.30
        )

    if structure["last_high"] is not None:

        high = float(
            structure["last_high"]
        )

        supply = (
            high - atr * 0.30,
            high + atr * 0.30
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
            "zone": None,
            "type": "NONE"
        }

    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

    c1_high = float(c1["high"])
    c1_low = float(c1["low"])

    c2_open = float(c2["open"])
    c2_close = float(c2["close"])

    c3_high = float(c3["high"])
    c3_low = float(c3["low"])

    # Bullish FVG
    if (
        c3_low > c1_high
        and
        c2_close > c2_open
    ):

        return {
            "bullish": True,
            "bearish": False,
            "zone": (
                c1_high,
                c3_low
            ),
            "type": "BULLISH"
        }

    # Bearish FVG
    if (
        c3_high < c1_low
        and
        c2_close < c2_open
    ):

        return {
            "bullish": False,
            "bearish": True,
            "zone": (
                c3_high,
                c1_low
            ),
            "type": "BEARISH"
        }

    return {
        "bullish": False,
        "bearish": False,
        "zone": None,
        "type": "NONE"
    }


# ============================================================
# PRICE IN ZONE
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
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity(df):

    if len(df) < 30:

        return {
            "bullish": False,
            "bearish": False
        }

    structure = get_structure(df)

    last_high = structure["last_high"]
    last_low = structure["last_low"]

    if last_high is None or last_low is None:

        return {
            "bullish": False,
            "bearish": False
        }

    recent = df.tail(5)

    recent_low = float(
        recent["low"].min()
    )

    recent_high = float(
        recent["high"].max()
    )

    close = float(
        df["close"].iloc[-1]
    )

    bullish = (
        recent_low < last_low
        and
        close > last_low
    )

    bearish = (
        recent_high > last_high
        and
        close < last_high
    )

    return {
        "bullish": bullish,
        "bearish": bearish
    }


# ============================================================
# TGL
#
# NOTE:
# Exact mathematical Rulebook TGL formula has not been
# supplied. This remains an isolated approximation.
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

    if movement <= 0:

        return {
            "level1": None,
            "level2": None
        }

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
# ============================================================

def select_priority(
    daily,
    h4,
    h1
):

    d = daily["trend"]
    four = h4["trend"]
    one = h1["trend"]

    valid = [
        "BULLISH",
        "BEARISH"
    ]

    # Daily = 4H = 1H
    if (
        d == four
        and
        four == one
        and
        d in valid
    ):

        return {
            "timeframe": H1,
            "direction": d
        }

    # Daily = 4H, 1H opposite
    if (
        d == four
        and
        d in valid
        and
        one != d
    ):

        return {
            "timeframe": H4,
            "direction": d
        }

    # 4H = 1H, both opposite Daily
    if (
        four == one
        and
        four in valid
        and
        d != four
    ):

        return {
            "timeframe": DAILY,
            "direction": four
        }

    return None


# ============================================================
# REQUIRED CHOCH
# ============================================================

def required_choch(
    priority_tf,
    market,
    direction
):

    expected = (
        "BULLISH_CHOCH"
        if direction == "BULLISH"
        else "BEARISH_CHOCH"
    )

    if priority_tf == H1:

        return (
            market[M1]["choch"]
            ==
            expected
        )

    if priority_tf == H4:

        return (
            market[M5]["choch"]
            ==
            expected
        )

    if priority_tf == DAILY:

        return (
            market[H1]["choch"]
            ==
            expected
            and
            market[M1]["choch"]
            ==
            expected
        )

    return False


# ============================================================
# FAKEOUT
# ============================================================

def is_fakeout(
    df,
    direction
):

    if len(df) < 5:
        return True

    candle = df.iloc[-1]

    high = float(candle["high"])
    low = float(candle["low"])
    close = float(candle["close"])
    open_price = float(candle["open"])

    candle_range = high - low

    if candle_range <= 0:
        return True

    body = abs(
        close - open_price
    )

    body_ratio = (
        body / candle_range
    )

    if body_ratio < 0.30:
        return True

    previous = df.iloc[-2]

    previous_high = float(
        previous["high"]
    )

    previous_low = float(
        previous["low"]
    )

    if direction == "BULLISH":

        if (
            high > previous_high
            and
            close < previous_high
        ):

            return True

    if direction == "BEARISH":

        if (
            low < previous_low
            and
            close > previous_low
        ):

            return True

    return False


# ============================================================
# LEVEL ALREADY PLAYED
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

    return touches >= 3


# ============================================================
# EMA SUPPORT
# ============================================================

def ema_confirmation(
    df,
    direction
):

    if len(df) < 60:
        return False

    ema20 = calculate_ema(
        df,
        20
    )

    ema50 = calculate_ema(
        df,
        50
    )

    price = float(
        df["close"].iloc[-1]
    )

    if direction == "BULLISH":

        return (
            ema20.iloc[-1]
            >
            ema50.iloc[-1]
            and
            price > ema20.iloc[-1]
        )

    if direction == "BEARISH":

        return (
            ema20.iloc[-1]
            <
            ema50.iloc[-1]
            and
            price < ema20.iloc[-1]
        )

    return False


# ============================================================
# LOAD MARKET
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

            logger.warning(
                "No BTCUSDT data for %s",
                tf
            )

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

        "rr1": None,

        "rr2": None,

        "reasons": []
    }

    # --------------------------------------------------------
    # HTF STATUS
    # --------------------------------------------------------

    result["reasons"].append(
        f"HTF: Daily={daily['trend']} | "
        f"4H={h4['trend']} | "
        f"1H={h1['trend']}"
    )

    priority = select_priority(
        daily,
        h4,
        h1
    )

    # --------------------------------------------------------
    # No priority
    # --------------------------------------------------------

    if priority is None:

        result["checks"][
            "HTF_DIRECTION"
        ] = False

        result["checks"][
            "STRUCTURE"
        ] = False

        result["reasons"].append(
            "No Rulebook HTF priority condition currently active"
        )

        # Diagnostic confluence.
        #
        # This is deliberately NOT treated as a trade score.
        # It prevents the misleading impression that nothing
        # was analysed.

        direction_votes = []

        for structure in [
            daily,
            h4,
            h1
        ]:

            if structure["trend"] in [
                "BULLISH",
                "BEARISH"
            ]:

                direction_votes.append(
                    structure["trend"]
                )

        if direction_votes:

            bullish = direction_votes.count(
                "BULLISH"
            )

            bearish = direction_votes.count(
                "BEARISH"
            )

            if bullish > bearish:

                result["direction"] = "BULLISH"

            elif bearish > bullish:

                result["direction"] = "BEARISH"

            else:

                result["direction"] = "MIXED"

        result["confidence"] = 0

        return result

    # --------------------------------------------------------
    # Priority
    # --------------------------------------------------------

    direction = priority["direction"]
    priority_tf = priority["timeframe"]

    result["direction"] = direction
    result["priority_tf"] = priority_tf

    result["checks"][
        "HTF_DIRECTION"
    ] = True

    result["reasons"].append(
        f"{priority_tf} priority selected: {direction}"
    )

    selected = market[
        priority_tf
    ]

    structure = selected[
        "structure"
    ]

    selected_df = selected[
        "df"
    ]

    # --------------------------------------------------------
    # Structure
    # --------------------------------------------------------

    if direction == "BULLISH":

        structure_ok = (
            "HH" in structure["labels"]
            and
            "HL" in structure["labels"]
        )

    else:

        structure_ok = (
            "LH" in structure["labels"]
            and
            "LL" in structure["labels"]
        )

    result["checks"][
        "STRUCTURE"
    ] = structure_ok

    result["reasons"].append(
        "Structure: "
        +
        (
            "PASS"
            if structure_ok
            else "FAIL"
        )
    )

    # --------------------------------------------------------
    # Zone
    # --------------------------------------------------------

    if direction == "BULLISH":

        zone = selected[
            "zones"
        ]["demand"]

    else:

        zone = selected[
            "zones"
        ]["supply"]

    tapped = price_tapped_zone(
        price,
        zone
    )

    result["checks"][
        "PRICE_TAP"
    ] = tapped

    if tapped:

        result["reasons"].append(
            "Price tapped HTF zone"
        )

    else:

        result["reasons"].append(
            "Waiting for HTF zone tap"
        )

    # --------------------------------------------------------
    # Already played
    # --------------------------------------------------------

    already_played = level_already_played(
        selected_df,
        zone
    )

    level_ok = not already_played

    result["checks"][
        "LEVEL_NOT_PLAYED"
    ] = level_ok

    result["reasons"].append(
        "Level retest filter: "
        +
        (
            "PASS"
            if level_ok
            else "ALREADY PLAYED"
        )
    )

    # --------------------------------------------------------
    # Lower TF CHOCH
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
    # 2 candle retracement
    # --------------------------------------------------------

    retracement_ok = detect_two_candle_retracement(
        market[M1]["df"],
        direction
    )

    result["checks"][
        "TWO_CANDLE_RETRACEMENT"
    ] = retracement_ok

    # --------------------------------------------------------
    # Confirmation
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

    # --------------------------------------------------------
    # Fakeout
    # --------------------------------------------------------

    fakeout = is_fakeout(
        market[M1]["df"],
        direction
    )

    fakeout_ok = not fakeout

    result["checks"][
        "NO_FAKEOUT"
    ] = fakeout_ok

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

    # --------------------------------------------------------
    # Liquidity
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

    # --------------------------------------------------------
    # EMA
    # --------------------------------------------------------

    ema_ok = ema_confirmation(
        market[M15]["df"],
        direction
    )

    result["checks"][
        "EMA_SUPPORT"
    ] = ema_ok

    # --------------------------------------------------------
    # Reasons
    # --------------------------------------------------------

    result["reasons"].append(
        "2-candle retracement: "
        +
        (
            "PASS"
            if retracement_ok
            else "WAIT"
        )
    )

    result["reasons"].append(
        "Confirmation candle: "
        +
        (
            "PASS"
            if confirmation_ok
            else "WAIT"
        )
    )

    result["reasons"].append(
        "Fakeout filter: "
        +
        (
            "PASS"
            if fakeout_ok
            else "FAIL"
        )
    )

    result["reasons"].append(
        "FVG: "
        +
        (
            "PASS"
            if fvg_ok
            else "NONE"
        )
    )

    result["reasons"].append(
        "Liquidity: "
        +
        (
            "PASS"
            if liquidity_ok
            else "NONE"
        )
    )

    result["reasons"].append(
        "EMA support: "
        +
        (
            "PASS"
            if ema_ok
            else "NONE"
        )
    )

    # --------------------------------------------------------
    # Confluence
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
    # Mandatory
    # --------------------------------------------------------

    mandatory_keys = [

        "HTF_DIRECTION",
        "STRUCTURE",
        "PRICE_TAP",
        "LEVEL_NOT_PLAYED",
        "LOWER_TF_CHOCH",
        "TWO_CANDLE_RETRACEMENT",
        "CONFIRMATION",
        "NO_FAKEOUT"
    ]

    all_mandatory = all(
        result["checks"].get(
            key,
            False
        )
        for key in mandatory_keys
    )

    result["mandatory_pass"] = (
        all_mandatory
    )

    # --------------------------------------------------------
    # Final signal
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
    # Entry / SL / TP
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

            atr = price * 0.002

        atr = float(atr)

        tgl = selected[
            "tgl"
        ]

        if direction == "BULLISH":

            entry = price

            swing_low = structure[
                "last_low"
            ]

            if swing_low is not None:

                sl = (
                    swing_low
                    -
                    atr * 0.20
                )

            else:

                sl = (
                    entry
                    -
                    atr * 1.5
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
                    +
                    risk * 1.5
                )

            if (
                tp2 is None
                or
                tp2 <= tp1
            ):

                tp2 = (
                    entry
                    +
                    risk * 2.5
                )

        else:

            entry = price

            swing_high = structure[
                "last_high"
            ]

            if swing_high is not None:

                sl = (
                    swing_high
                    +
                    atr * 0.20
                )

            else:

                sl = (
                    entry
                    +
                    atr * 1.5
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
                    -
                    risk * 1.5
                )

            if (
                tp2 is None
                or
                tp2 >= tp1
            ):

                tp2 = (
                    entry
                    -
                    risk * 2.5
                )

        if risk > 0:

            result["rr1"] = abs(
                tp1 - entry
            ) / risk

            result["rr2"] = abs(
                tp2 - entry
            ) / risk

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

        self.tp1_hits = 0

        self.load_state()

    # --------------------------------------------------------
    # LOAD
    # --------------------------------------------------------

    def load_state(self):

        if not os.path.exists(
            STATE_FILE
        ):

            return

        try:

            with open(
                STATE_FILE,
                "r"
            ) as f:

                data = json.load(f)

            self.balance = float(
                data.get(
                    "balance",
                    PAPER_BALANCE
                )
            )

            self.total = int(
                data.get(
                    "total",
                    0
                )
            )

            self.wins = int(
                data.get(
                    "wins",
                    0
                )
            )

            self.losses = int(
                data.get(
                    "losses",
                    0
                )
            )

            self.tp1_hits = int(
                data.get(
                    "tp1_hits",
                    0
                )
            )

            self.trade = data.get(
                "trade"
            )

            logger.info(
                "Paper state restored | "
                "Trades=%d | Wins=%d | Losses=%d",
                self.total,
                self.wins,
                self.losses
            )

        except Exception as e:

            logger.warning(
                "Could not load paper state: %s",
                e
            )

    # --------------------------------------------------------
    # SAVE
    # --------------------------------------------------------

    def save_state(self):

        data = {

            "balance": self.balance,

            "total": self.total,

            "wins": self.wins,

            "losses": self.losses,

            "tp1_hits": self.tp1_hits,

            "trade": self.trade
        }

        try:

            with open(
                STATE_FILE,
                "w"
            ) as f:

                json.dump(
                    data,
                    f,
                    indent=2
                )

        except Exception as e:

            logger.warning(
                "Could not save paper state: %s",
                e
            )

    # --------------------------------------------------------
    # OPEN
    # --------------------------------------------------------

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

        entry = float(
            setup["entry"]
        )

        sl = float(
            setup["sl"]
        )

        risk_distance = abs(
            entry - sl
        )

        risk_money = (
            self.balance
            *
            RISK_PERCENT
            /
            100
        )

        quantity = (
            risk_money
            /
            risk_distance
            if risk_distance > 0
            else 0
        )

        self.trade = {

            "side":
                setup["signal"],

            "entry":
                entry,

            "sl":
                sl,

            "tp1":
                float(setup["tp1"]),

            "tp2":
                float(setup["tp2"]),

            "quantity":
                float(quantity),

            "risk_money":
                float(risk_money),

            "tp1_hit":
                False,

            "opened":
                datetime.now(
                    timezone.utc
                ).isoformat()
        }

        self.total += 1

        self.save_state()

        return True

    # --------------------------------------------------------
    # CHECK
    # --------------------------------------------------------

    def check(
        self,
        price
    ):

        if self.trade is None:
            return None

        trade = self.trade

        side = trade["side"]

        price = float(price)

        # ----------------------------------------------------
        # BUY
        # ----------------------------------------------------

        if side == "BUY":

            # SL first
            if price <= trade["sl"]:

                result = self.close(
                    "LOSS",
                    trade["sl"]
                )

                return result

            # TP2
            if price >= trade["tp2"]:

                result = self.close(
                    "WIN",
                    trade["tp2"]
                )

                return result

            # TP1
            if (
                not trade["tp1_hit"]
                and
                price >= trade["tp1"]
            ):

                trade["tp1_hit"] = True

                self.tp1_hits += 1

                # Move SL to breakeven
                trade["sl"] = trade[
                    "entry"
                ]

                self.save_state()

                return {
                    "type": "TP1",
                    **trade,
                    "price": price
                }

        # ----------------------------------------------------
        # SELL
        # ----------------------------------------------------

        elif side == "SELL":

            if price >= trade["sl"]:

                result = self.close(
                    "LOSS",
                    trade["sl"]
                )

                return result

            if price <= trade["tp2"]:

                result = self.close(
                    "WIN",
                    trade["tp2"]
                )

                return result

            if (
                not trade["tp1_hit"]
                and
                price <= trade["tp1"]
            ):

                trade["tp1_hit"] = True

                self.tp1_hits += 1

                trade["sl"] = trade[
                    "entry"
                ]

                self.save_state()

                return {
                    "type": "TP1",
                    **trade,
                    "price": price
                }

        return None

    # --------------------------------------------------------
    # CLOSE
    # --------------------------------------------------------

    def close(
        self,
        result,
        exit_price
    ):

        trade = self.trade

        side = trade["side"]

        entry = trade["entry"]

        quantity = trade[
            "quantity"
        ]

        if side == "BUY":

            pnl = (
                exit_price
                -
                entry
            ) * quantity

        else:

            pnl = (
                entry
                -
                exit_price
            ) * quantity

        self.balance += pnl

        if result == "WIN":

            self.wins += 1

        else:

            self.losses += 1

        completed = {

            **trade,

            "exit":
                float(exit_price),

            "result":
                result,

            "pnl":
                float(pnl),

            "balance":
                float(self.balance)
        }

        self.trade = None

        self.save_state()

        return completed

    # --------------------------------------------------------
    # WIN RATE
    # --------------------------------------------------------

    def win_rate(self):

        if self.total <= 0:
            return 0.0

        return (
            self.wins
            /
            self.total
        ) * 100


# ============================================================
# TELEGRAM SETUP MESSAGE
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

    checks = []

    for key, value in setup[
        "checks"
    ].items():

        symbol = (
            "✓"
            if value
            else "✗"
        )

        checks.append(
            f"{symbol} {key}"
        )

    checks_text = "\n".join(
        checks
    )

    reasons = "\n".join(
        f"• {x}"
        for x in setup["reasons"]
    )

    text = (

        f"{icon} <b>TRADEBRAIN AI</b>\n\n"

        f"<b>Symbol:</b> BTCUSDT\n"

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
        f"{reasons}"
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

            f"<b>RR1:</b> "
            f"{setup['rr1']:.2f}\n"

            f"<b>RR2:</b> "
            f"{setup['rr2']:.2f}\n\n"

            "<b>MODE:</b> PAPER TRADE"
        )

    return text


# ============================================================
# WAIT MESSAGE
# ============================================================

def wait_message(
    setup
):

    reasons = "\n".join(
        f"• {x}"
        for x in setup["reasons"]
    )

    return (

        "🟡 <b>BTCUSDT WAIT</b>\n\n"

        f"<b>Price:</b> "
        f"{setup['price']:.2f}\n"

        f"<b>Direction:</b> "
        f"{setup['direction']}\n"

        f"<b>Priority:</b> "
        f"{setup['priority_tf']}\n"

        f"<b>Confluence:</b> "
        f"{setup['confidence']}/100\n\n"

        f"<b>Why WAIT?</b>\n"
        f"{reasons}"
    )


# ============================================================
# RESULT MESSAGE
# ============================================================

def result_message(
    result,
    trader
):

    if result["result"] == "WIN":

        icon = "✅"

    else:

        icon = "❌"

    return (

        f"{icon} <b>PAPER TRADE "
        f"{result['result']}</b>\n\n"

        f"<b>Symbol:</b> BTCUSDT\n"

        f"<b>Side:</b> "
        f"{result['side']}\n"

        f"<b>Entry:</b> "
        f"{result['entry']:.2f}\n"

        f"<b>Exit:</b> "
        f"{result['exit']:.2f}\n"

        f"<b>P/L:</b> "
        f"{result['pnl']:.2f}\n\n"

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
# TP1 MESSAGE
# ============================================================

def tp1_message(
    trade
):

    return (

        "🎯 <b>BTCUSDT TP1 HIT</b>\n\n"

        f"<b>Side:</b> "
        f"{trade['side']}\n"

        f"<b>Entry:</b> "
        f"{trade['entry']:.2f}\n"

        f"<b>TP1:</b> "
        f"{trade['tp1']:.2f}\n\n"

        "SL moved to <b>BREAK-EVEN</b>\n"

        "Paper trade remains active."
    )


# ============================================================
# SIGNAL DUPLICATION PROTECTION
# ============================================================

last_signal_key = None


def signal_is_new(
    setup,
    market
):

    global last_signal_key

    if setup["signal"] not in [
        "BUY",
        "SELL"
    ]:

        return False

    tf = setup[
        "priority_tf"
    ]

    if tf not in market:
        return False

    df = market[tf]["df"]

    candle_time = str(
        df["close_time"].iloc[-1]
    )

    key = (
        SYMBOL,
        setup["signal"],
        tf,
        candle_time
    )

    if key == last_signal_key:

        return False

    last_signal_key = key

    return True


# ============================================================
# STATUS LOG
# ============================================================

def log_market_status(
    market,
    setup,
    trader
):

    d = market[
        DAILY
    ]["structure"]["trend"]

    h4 = market[
        H4
    ]["structure"]["trend"]

    h1 = market[
        H1
    ]["structure"]["trend"]

    logger.info(
        "BTCUSDT | %s | Price %.2f | "
        "Confidence %d/100 | "
        "Bias %s | Priority %s | "
        "D=%s H4=%s H1=%s | "
        "Trades=%d W=%d L=%d WR=%.2f%%",
        setup["signal"],
        setup["price"],
        setup["confidence"],
        setup["direction"],
        setup["priority_tf"],
        d,
        h4,
        h1,
        trader.total,
        trader.wins,
        trader.losses,
        trader.win_rate()
    )


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info(
        "=============================================="
    )

    logger.info(
        "TradeBrain BTCUSDT Rulebook Engine STARTED"
    )

    logger.info(
        "Symbol: BTCUSDT"
    )

    logger.info(
        "Data source: Binance Public API"
    )

    logger.info(
        "Paper Trading: ON"
    )

    logger.info(
        "Live Trading: OFF"
    )

    logger.info(
        "Minimum Confluence: %d",
        MIN_CONFLUENCE
    )

    logger.info(
        "Scan Interval: %d sec",
        SCAN_INTERVAL
    )

    logger.info(
        "=============================================="
    )

    send_telegram(

        "🚀 <b>TradeBrain AI Started</b>\n\n"

        "<b>Symbol:</b> BTCUSDT\n"

        "<b>Data:</b> Binance\n"

        "<b>Mode:</b> PAPER TRADING\n"

        "<b>Live:</b> OFF\n\n"

        "Rulebook engine: ACTIVE"
    )

    trader = PaperTrader()

    last_wait_log = 0

    while True:

        try:

            market = load_market()

            if market is None:

                logger.warning(
                    "BTCUSDT market data unavailable"
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
            # Check existing paper trade
            # ------------------------------------------------

            completed = trader.check(
                price
            )

            if completed:

                if completed.get(
                    "type"
                ) == "TP1":

                    logger.info(
                        "BTCUSDT PAPER TP1 HIT"
                    )

                    send_telegram(
                        tp1_message(
                            completed
                        )
                    )

                else:

                    logger.info(
                        "BTCUSDT PAPER %s | "
                        "P/L %.2f | "
                        "Balance %.2f",
                        completed["result"],
                        completed["pnl"],
                        trader.balance
                    )

                    send_telegram(
                        result_message(
                            completed,
                            trader
                        )
                    )

            # ------------------------------------------------
            # Analyze
            # ------------------------------------------------

            setup = analyze_rulebook(
                market
            )

            log_market_status(
                market,
                setup,
                trader
            )

            # ------------------------------------------------
            # BUY / SELL
            # ------------------------------------------------

            if (
                setup["signal"]
                in [
                    "BUY",
                    "SELL"
                ]
                and
                signal_is_new(
                    setup,
                    market
                )
            ):

                send_telegram(
                    setup_message(
                        setup
                    )
                )

                if trader.trade is None:

                    opened = trader.open(
                        setup
                    )

                    if opened:

                        send_telegram(

                            "📌 <b>PAPER TRADE OPENED</b>\n\n"

                            "<b>Symbol:</b> BTCUSDT\n"

                            f"<b>Side:</b> "
                            f"{setup['signal']}\n"

                            f"<b>Entry:</b> "
                            f"{setup['entry']:.2f}\n"

                            f"<b>SL:</b> "
                            f"{setup['sl']:.2f}\n"

                            f"<b>TP1:</b> "
                            f"{setup['tp1']:.2f}\n"

                            f"<b>TP2:</b> "
                            f"{setup['tp2']:.2f}\n"

                            f"<b>RR2:</b> "
                            f"{setup['rr2']:.2f}"
                        )

            # ------------------------------------------------
            # WAIT logging
            # ------------------------------------------------

            else:

                now = time.time()

                # Avoid Telegram spam.
                # WAIT is logged locally every scan,
                # Telegram WAIT only every 10 minutes.

                if (
                    now - last_wait_log
                    >= 600
                ):

                    send_telegram(
                        wait_message(
                            setup
                        )
                    )

                    last_wait_log = now

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            logger.info(
                "TradeBrain stopped manually"
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

                    "BTCUSDT engine will retry."
                )

            except Exception:
                pass

            time.sleep(30)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
