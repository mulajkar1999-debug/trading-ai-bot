import os
import time
import logging
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np


# ============================================================
# TRADEBRAIN AI - MULTI MARKET PAPER TRADING BOT
# ============================================================
#
# MARKETS:
#   BTCUSD   -> Binance public data (BTCUSDT)
#   ETHUSDT  -> Binance public data
#   XAUUSD   -> Twelve Data (XAU/USD)
#   GBPUSD   -> Twelve Data (GBP/USD)
#
# LIVE TRADING: OFF
# PAPER TRADING: ON
#
# ============================================================


# ============================================================
# ENVIRONMENT VARIABLES
# ============================================================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TWELVE_DATA_API_KEY = os.getenv(
    "TWELVE_DATA_API_KEY",
    ""
)

MARKETS = [
    x.strip().upper()
    for x in os.getenv(
        "MARKETS",
        "BTCUSD,XAUUSD,ETHUSDT,GBPUSD"
    ).split(",")
    if x.strip()
]

SCAN_INTERVAL = int(
    os.getenv("SCAN_INTERVAL", "30")
)

MIN_CONFLUENCE = int(
    os.getenv("MIN_CONFLUENCE", "85")
)

PAPER_BALANCE = float(
    os.getenv("PAPER_BALANCE", "10000")
)

RISK_PERCENT = float(
    os.getenv("RISK_PERCENT", "1")
)


# ============================================================
# API
# ============================================================

BINANCE_URL = "https://api.binance.com"

TWELVE_DATA_URL = (
    "https://api.twelvedata.com/time_series"
)


# ============================================================
# MARKET CONFIGURATION
# ============================================================

MARKET_CONFIG = {

    "BTCUSD": {
        "source": "binance",
        "symbol": "BTCUSDT",
        "decimals": 2,
    },

    "ETHUSDT": {
        "source": "binance",
        "symbol": "ETHUSDT",
        "decimals": 2,
    },

    "XAUUSD": {
        "source": "twelve",
        "symbol": "XAU/USD",
        "decimals": 2,
    },

    "GBPUSD": {
        "source": "twelve",
        "symbol": "GBP/USD",
        "decimals": 5,
    },
}


INTERVALS = {

    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1day",

}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(

    level=logging.INFO,

    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(message)s"
    ),

)

logger = logging.getLogger("TradeBrain")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    if not TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN missing"
        )
        return False

    if not TELEGRAM_CHAT_ID:
        logger.warning(
            "TELEGRAM_CHAT_ID missing"
        )
        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}"
        "/sendMessage"
    )

    try:

        response = requests.post(

            url,

            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },

            timeout=15,

        )

        if response.status_code != 200:

            logger.error(
                "Telegram error: %s",
                response.text[:500]
            )

            return False

        return True

    except Exception as e:

        logger.error(
            "Telegram exception: %s",
            e
        )

        return False


# ============================================================
# EMPTY DATAFRAME
# ============================================================

def empty_df():

    return pd.DataFrame(

        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time",
        ]

    )


# ============================================================
# BINANCE DATA
# ============================================================

def get_binance_klines(
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
                "limit": limit,
            },

            timeout=15,

        )

        response.raise_for_status()

        raw = response.json()

        if not isinstance(raw, list):
            return empty_df()

        if not raw:
            return empty_df()

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
            "ignore",

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
            "volume",

        ]

        for column in numeric_columns:

            df[column] = pd.to_numeric(
                df[column],
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

        df = df[
            [
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
            ]
        ]

        return df.dropna().reset_index(
            drop=True
        )

    except Exception as e:

        logger.error(
            "Binance %s %s: %s",
            symbol,
            interval,
            e
        )

        return empty_df()


# ============================================================
# TWELVE DATA
# ============================================================

def get_twelve_data(
    symbol,
    interval,
    outputsize=300
):

    if not TWELVE_DATA_API_KEY:

        logger.error(
            "TWELVE_DATA_API_KEY missing. "
            "Required for %s.",
            symbol
        )

        return empty_df()

    try:

        params = {

            "symbol": symbol,

            "interval": INTERVALS[interval],

            "outputsize": min(
                outputsize,
                5000
            ),

            "apikey": TWELVE_DATA_API_KEY,

            "timezone": "UTC",

            "order": "ASC",

        }

        response = requests.get(

            TWELVE_DATA_URL,

            params=params,

            timeout=20,

        )

        response.raise_for_status()

        data = response.json()

        if data.get("status") == "error":

            logger.error(

                "Twelve Data %s %s: %s",

                symbol,
                interval,

                data.get(
                    "message",
                    "Unknown API error"
                )

            )

            return empty_df()

        values = data.get("values")

        if not values:

            logger.error(
                "Twelve Data %s %s: "
                "no candles returned.",
                symbol,
                interval
            )

            return empty_df()

        df = pd.DataFrame(values)

        required = [

            "datetime",
            "open",
            "high",
            "low",
            "close",

        ]

        for column in required:

            if column not in df.columns:

                logger.error(
                    "Twelve Data response missing %s",
                    column
                )

                return empty_df()

        df["open_time"] = pd.to_datetime(

            df["datetime"],

            utc=True,

            errors="coerce"

        )

        for column in [

            "open",
            "high",
            "low",
            "close",
            "volume",

        ]:

            if column in df.columns:

                df[column] = pd.to_numeric(

                    df[column],

                    errors="coerce"

                )

        if "volume" not in df.columns:

            df["volume"] = 0.0

        df["close_time"] = df["open_time"]

        df = df[

            [
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_time",
            ]

        ]

        df = df.dropna(

            subset=[
                "open_time",
                "open",
                "high",
                "low",
                "close",
            ]

        )

        return df.sort_values(
            "open_time"
        ).reset_index(drop=True)

    except Exception as e:

        logger.error(

            "Twelve Data %s %s: %s",

            symbol,
            interval,
            e

        )

        return empty_df()


# ============================================================
# UNIVERSAL DATA FUNCTION
# ============================================================

def get_klines(
    market,
    timeframe,
    limit=300
):

    config = MARKET_CONFIG.get(
        market
    )

    if not config:

        logger.error(
            "Unsupported market: %s",
            market
        )

        return empty_df()

    if config["source"] == "binance":

        return get_binance_klines(

            config["symbol"],

            timeframe,

            limit

        )

    return get_twelve_data(

        config["symbol"],

        timeframe,

        limit

    )


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

    previous_close = df["close"].shift(1)

    tr = pd.concat(

        [

            df["high"] - df["low"],

            (
                df["high"]
                - previous_close
            ).abs(),

            (
                df["low"]
                - previous_close
            ).abs(),

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

    highs = []
    lows = []

    if len(df) < 20:

        return highs, lows

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

        left_high = df[
            "high"
        ].iloc[
            i-left:i
        ].max()

        right_high = df[
            "high"
        ].iloc[
            i+1:i+right+1
        ].max()

        left_low = df[
            "low"
        ].iloc[
            i-left:i
        ].min()

        right_low = df[
            "low"
        ].iloc[
            i+1:i+right+1
        ].min()

        if (
            current_high > left_high
            and
            current_high > right_high
        ):

            highs.append({

                "index": i,

                "price": current_high,

            })

        if (
            current_low < left_low
            and
            current_low < right_low
        ):

            lows.append({

                "index": i,

                "price": current_low,

            })

    return highs, lows


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

        "labels": [],

    }

    if len(highs) >= 2:

        previous_high = highs[-2]["price"]

        last_high = highs[-1]["price"]

        result[
            "previous_high"
        ] = previous_high

        result[
            "last_high"
        ] = last_high

        if last_high > previous_high:

            result["labels"].append(
                "HH"
            )

        else:

            result["labels"].append(
                "LH"
            )

    if len(lows) >= 2:

        previous_low = lows[-2]["price"]

        last_low = lows[-1]["price"]

        result[
            "previous_low"
        ] = previous_low

        result[
            "last_low"
        ] = last_low

        if last_low > previous_low:

            result["labels"].append(
                "HL"
            )

        else:

            result["labels"].append(
                "LL"
            )

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
# TWO CLOSES BEYOND LEVEL
# ============================================================

def two_closes_beyond(
    df,
    level,
    direction
):

    if level is None:
        return False

    if len(df) < 3:
        return False

    close1 = float(
        df["close"].iloc[-2]
    )

    close2 = float(
        df["close"].iloc[-1]
    )

    if direction == "BULLISH":

        return (
            close1 > level
            and
            close2 > level
        )

    if direction == "BEARISH":

        return (
            close1 < level
            and
            close2 < level
        )

    return False


# ============================================================
# BOS
# ============================================================

def detect_bos(df):

    structure = get_structure(df)

    if two_closes_beyond(

        df,

        structure["last_high"],

        "BULLISH"

    ):

        return "BULLISH_BOS"

    if two_closes_beyond(

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

    if structure["trend"] == "BEARISH":

        if two_closes_beyond(

            df,

            structure["last_high"],

            "BULLISH"

        ):

            return "BULLISH_CHOCH"

    if structure["trend"] == "BULLISH":

        if two_closes_beyond(

            df,

            structure["last_low"],

            "BEARISH"

        ):

            return "BEARISH_CHOCH"

    return "NONE"


# ============================================================
# CONFIRMATION CANDLE
# ============================================================

def confirmation_candle(df):

    if len(df) < 3:
        return "NONE"

    candle = df.iloc[-1]

    open_price = float(
        candle["open"]
    )

    high = float(
        candle["high"]
    )

    low = float(
        candle["low"]
    )

    close = float(
        candle["close"]
    )

    candle_range = high - low

    if candle_range <= 0:
        return "NONE"

    body_ratio = (
        abs(close - open_price)
        / candle_range
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
# 2 CANDLE RETRACEMENT
# ============================================================

def two_candle_retracement(
    df,
    direction
):

    if len(df) < 5:
        return False

    candle1 = df.iloc[-2]
    candle2 = df.iloc[-1]

    if direction == "BULLISH":

        return (

            float(candle1["close"])
            <
            float(candle1["open"])

            and

            float(candle2["close"])
            >
            float(candle2["open"])

        )

    if direction == "BEARISH":

        return (

            float(candle1["close"])
            >
            float(candle1["open"])

            and

            float(candle2["close"])
            <
            float(candle2["open"])

        )

    return False


# ============================================================
# SUPPLY / DEMAND
# ============================================================

def detect_zones(df):

    structure = get_structure(df)

    current_atr = calculate_atr(
        df
    ).iloc[-1]

    if (
        pd.isna(current_atr)
        or
        current_atr <= 0
    ):

        current_atr = (
            float(
                df["close"].iloc[-1]
            )
            * 0.002
        )

    demand = None
    supply = None

    if structure["last_low"] is not None:

        low = float(
            structure["last_low"]
        )

        demand = (

            low - current_atr * 0.25,

            low + current_atr * 0.25,

        )

    if structure["last_high"] is not None:

        high = float(
            structure["last_high"]
        )

        supply = (

            high - current_atr * 0.25,

            high + current_atr * 0.25,

        )

    return {

        "demand": demand,

        "supply": supply,

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

        }

    candle1 = df.iloc[-3]

    candle3 = df.iloc[-1]

    first_high = float(
        candle1["high"]
    )

    first_low = float(
        candle1["low"]
    )

    third_high = float(
        candle3["high"]
    )

    third_low = float(
        candle3["low"]
    )

    if third_low > first_high:

        return {

            "bullish": True,

            "bearish": False,

            "zone": (
                first_high,
                third_low
            ),

        }

    if third_high < first_low:

        return {

            "bullish": False,

            "bearish": True,

            "zone": (
                third_high,
                first_low
            ),

        }

    return {

        "bullish": False,

        "bearish": False,

        "zone": None,

    }


# ============================================================
# LIQUIDITY
# ============================================================

def detect_liquidity(df):

    if len(df) < 20:

        return {

            "bullish": False,

            "bearish": False,

        }

    structure = get_structure(
        df
    )

    bullish = False
    bearish = False

    if structure["last_low"] is not None:

        if (
            float(
                df["low"].tail(5).min()
            )
            <
            float(
                structure["last_low"]
            )
        ):

            bullish = True

    if structure["last_high"] is not None:

        if (
            float(
                df["high"].tail(5).max()
            )
            >
            float(
                structure["last_high"]
            )
        ):

            bearish = True

    return {

        "bullish": bullish,

        "bearish": bearish,

    }


# ============================================================
# ZONE TAP
# ============================================================

def price_tapped_zone(
    price,
    zone
):

    if zone is None:
        return False

    return (
        min(zone)
        <=
        price
        <=
        max(zone)
    )


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

    lower = min(zone)
    upper = max(zone)

    touches = 0

    for _, candle in df.tail(
        lookback
    ).iterrows():

        if (

            float(candle["high"])
            >= lower

            and

            float(candle["low"])
            <= upper

        ):

            touches += 1

    return touches >= 3


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

    high = float(
        current["high"]
    )

    low = float(
        current["low"]
    )

    open_price = float(
        current["open"]
    )

    close = float(
        current["close"]
    )

    candle_range = high - low

    if candle_range <= 0:
        return True

    body_ratio = (
        abs(close - open_price)
        /
        candle_range
    )

    if body_ratio < 0.30:
        return True

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
# EMA SUPPORT
# ============================================================

def ema_support(
    df,
    direction
):

    if len(df) < 55:
        return False

    ema20 = calculate_ema(
        df,
        20
    ).iloc[-1]

    ema50 = calculate_ema(
        df,
        50
    ).iloc[-1]

    if direction == "BULLISH":

        return ema20 > ema50

    if direction == "BEARISH":

        return ema20 < ema50

    return False


# ============================================================
# TGL
# ============================================================

def calculate_tgl(df):

    """
    IMPORTANT:

    This is a PROVISIONAL TGL calculation.

    The supplied Rulebook material describes:

        TGL Level 1
        TGL Level 2
        latest 1-2-3 structure
        higher timeframe reaction levels

    but the exact mathematical formula was not supplied.

    Therefore this function must NOT be treated as the final
    mathematical Rulebook TGL formula.
    """

    structure = get_structure(
        df
    )

    high = structure[
        "last_high"
    ]

    low = structure[
        "last_low"
    ]

    if (
        high is None
        or
        low is None
    ):

        return {

            "level1": None,

            "level2": None,

        }

    movement = abs(
        high - low
    )

    if structure["trend"] == "BULLISH":

        return {

            "level1": float(high),

            "level2": float(
                high + movement * 0.50
            ),

        }

    if structure["trend"] == "BEARISH":

        return {

            "level1": float(low),

            "level2": float(
                low - movement * 0.50
            ),

        }

    return {

        "level1": float(high),

        "level2": float(low),

    }


# ============================================================
# MTF PRIORITY
# ============================================================

def select_priority(
    daily,
    four_hour,
    one_hour
):

    daily_trend = daily["trend"]

    four_hour_trend = four_hour["trend"]

    one_hour_trend = one_hour["trend"]


    # --------------------------------------------------------
    # RULE 1
    # Daily = 4H = 1H
    # -> 1H priority
    # --------------------------------------------------------

    if (

        daily_trend
        ==
        four_hour_trend
        ==
        one_hour_trend

        and

        daily_trend
        in
        (
            "BULLISH",
            "BEARISH"
        )

    ):

        return {

            "timeframe": "1h",

            "direction": daily_trend,

        }


    # --------------------------------------------------------
    # RULE 2
    # Daily + 4H same
    # 1H opposite
    # -> 4H priority
    # --------------------------------------------------------

    if (

        daily_trend
        ==
        four_hour_trend

        and

        one_hour_trend
        !=
        daily_trend

        and

        daily_trend
        in
        (
            "BULLISH",
            "BEARISH"
        )

    ):

        return {

            "timeframe": "4h",

            "direction": daily_trend,

        }


    # --------------------------------------------------------
    # RULE 3
    # 4H + 1H same
    # Daily opposite
    # -> Daily priority
    # --------------------------------------------------------

    if (

        four_hour_trend
        ==
        one_hour_trend

        and

        four_hour_trend
        !=
        daily_trend

        and

        daily_trend
        in
        (
            "BULLISH",
            "BEARISH"
        )

    ):

        return {

            "timeframe": "1d",

            "direction": daily_trend,

        }

    return None


# ============================================================
# REQUIRED CHOCH
# ============================================================

def required_choch(
    priority_tf,
    data,
    direction
):

    expected = (

        "BULLISH_CHOCH"

        if direction == "BULLISH"

        else

        "BEARISH_CHOCH"

    )


    # 1H -> 1M CHOCH

    if priority_tf == "1h":

        return (
            data["1m"]["choch"]
            ==
            expected
        )


    # 4H -> 5M CHOCH

    if priority_tf == "4h":

        return (
            data["5m"]["choch"]
            ==
            expected
        )


    # Daily -> 1H CHOCH -> 1M confirmation

    if priority_tf == "1d":

        return (

            data["1h"]["choch"]
            ==
            expected

            and

            data["1m"]["choch"]
            ==
            expected

        )

    return False


# ============================================================
# LOAD ALL TIMEFRAMES
# ============================================================

def load_market(
    market
):

    data = {}

    timeframes = [

        "1d",
        "4h",
        "1h",
        "15m",
        "5m",
        "1m",

    ]

    for timeframe in timeframes:

        df = get_klines(

            market,

            timeframe,

            300

        )

        if (

            df.empty

            or

            len(df) < 60

        ):

            logger.warning(

                "%s %s: "
                "insufficient data: %d",

                market,

                timeframe,

                len(df)

            )

            return None

        data[timeframe] = {

            "df": df,

            "structure":
                get_structure(df),

            "bos":
                detect_bos(df),

            "choch":
                detect_choch(df),

            "fvg":
                detect_fvg(df),

            "liquidity":
                detect_liquidity(df),

            "zones":
                detect_zones(df),

            "confirmation":
                confirmation_candle(df),

            "tgl":
                calculate_tgl(df),

        }

    return data


# ============================================================
# RULEBOOK ANALYSIS
# ============================================================

def analyze_rulebook(
    market,
    data
):

    price = float(

        data["1m"]["df"][
            "close"
        ].iloc[-1]

    )

    result = {

        "market": market,

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

    }


    # --------------------------------------------------------
    # MTF DIRECTION
    # --------------------------------------------------------

    priority = select_priority(

        data["1d"]["structure"],

        data["4h"]["structure"],

        data["1h"]["structure"],

    )


    if priority is None:

        return result


    direction = priority[
        "direction"
    ]

    priority_tf = priority[
        "timeframe"
    ]

    result[
        "direction"
    ] = direction

    result[
        "priority_tf"
    ] = priority_tf


    selected = data[
        priority_tf
    ]

    selected_df = selected[
        "df"
    ]

    structure = selected[
        "structure"
    ]


    # --------------------------------------------------------
    # HTF DIRECTION
    # --------------------------------------------------------

    result["checks"][
        "HTF_DIRECTION"
    ] = True


    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    if direction == "BULLISH":

        result["checks"][
            "STRUCTURE"
        ] = (

            "HH"
            in
            structure["labels"]

            and

            "HL"
            in
            structure["labels"]

        )

    else:

        result["checks"][
            "STRUCTURE"
        ] = (

            "LH"
            in
            structure["labels"]

            and

            "LL"
            in
            structure["labels"]

        )


    # --------------------------------------------------------
    # SUPPLY / DEMAND
    # --------------------------------------------------------

    if direction == "BULLISH":

        zone = selected[
            "zones"
        ]["demand"]

    else:

        zone = selected[
            "zones"
        ]["supply"]


    result["checks"][
        "PRICE_TAP"
    ] = price_tapped_zone(

        price,

        zone

    )


    # --------------------------------------------------------
    # ALREADY PLAYED LEVEL
    # --------------------------------------------------------

    result["checks"][
        "LEVEL_NOT_PLAYED"
    ] = not level_already_played(

        selected_df,

        zone

    )


    # --------------------------------------------------------
    # CHOCH
    # --------------------------------------------------------

    result["checks"][
        "LOWER_TF_CHOCH"
    ] = required_choch(

        priority_tf,

        data,

        direction

    )


    # --------------------------------------------------------
    # 2 CANDLE RETRACEMENT
    # --------------------------------------------------------

    result["checks"][
        "TWO_CANDLE_RETRACEMENT"
    ] = two_candle_retracement(

        data["1m"]["df"],

        direction

    )


    # --------------------------------------------------------
    # CONFIRMATION
    # --------------------------------------------------------

    result["checks"][
        "CONFIRMATION"
    ] = (

        data["1m"][
            "confirmation"
        ]

        ==

        direction

    )


    # --------------------------------------------------------
    # FAKEOUT
    # --------------------------------------------------------

    result["checks"][
        "NO_FAKEOUT"
    ] = not is_fakeout(

        data["1m"]["df"],

        direction

    )


    # --------------------------------------------------------
    # FVG
    # --------------------------------------------------------

    fvg = data[
        "5m"
    ]["fvg"]


    result["checks"][
        "FVG"
    ] = (

        fvg["bullish"]

        if direction == "BULLISH"

        else

        fvg["bearish"]

    )


    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    liquidity = data[
        "5m"
    ]["liquidity"]


    result["checks"][
        "LIQUIDITY"
    ] = (

        liquidity["bullish"]

        if direction == "BULLISH"

        else

        liquidity["bearish"]

    )


    # --------------------------------------------------------
    # EMA
    # --------------------------------------------------------

    result["checks"][
        "EMA_SUPPORT"
    ] = ema_support(

        data["15m"]["df"],

        direction

    )


    # ========================================================
    # CONFLUENCE SCORE
    # ========================================================

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

        "EMA_SUPPORT": 1,

    }


    result["confidence"] = sum(

        weight

        for key, weight
        in weights.items()

        if result["checks"].get(
            key,
            False
        )

    )


    # ========================================================
    # MANDATORY RULEBOOK FILTER
    # ========================================================

    mandatory = [

        "HTF_DIRECTION",

        "STRUCTURE",

        "PRICE_TAP",

        "LEVEL_NOT_PLAYED",

        "LOWER_TF_CHOCH",

        "TWO_CANDLE_RETRACEMENT",

        "CONFIRMATION",

        "NO_FAKEOUT",

    ]


    result[
        "mandatory_pass"
    ] = all(

        result["checks"].get(
            key,
            False
        )

        for key in mandatory

    )


    # ========================================================
    # FINAL SIGNAL
    # ========================================================

    if (

        result[
            "mandatory_pass"
        ]

        and

        result[
            "confidence"
        ]

        >=

        MIN_CONFLUENCE

    ):

        if direction == "BULLISH":

            result["signal"] = "BUY"

        else:

            result["signal"] = "SELL"


    # ========================================================
    # ENTRY / SL / TP
    # ========================================================

    if result["signal"] in (

        "BUY",
        "SELL"

    ):

        current_atr = calculate_atr(

            data["1m"]["df"]

        ).iloc[-1]


        if (

            pd.isna(current_atr)

            or

            current_atr <= 0

        ):

            current_atr = (
                price * 0.002
            )


        current_atr = float(
            current_atr
        )


        tgl = selected[
            "tgl"
        ]


        # ----------------------------------------------------
        # BUY
        # ----------------------------------------------------

        if direction == "BULLISH":

            entry = price


            if structure[
                "last_low"
            ] is not None:

                sl = (

                    float(
                        structure[
                            "last_low"
                        ]
                    )

                    -

                    current_atr * 0.20

                )

            else:

                sl = (
                    entry
                    -
                    current_atr * 1.5
                )


            risk = (
                entry - sl
            )


            tp1 = tgl[
                "level1"
            ]

            tp2 = tgl[
                "level2"
            ]


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


        # ----------------------------------------------------
        # SELL
        # ----------------------------------------------------

        else:

            entry = price


            if structure[
                "last_high"
            ] is not None:

                sl = (

                    float(
                        structure[
                            "last_high"
                        ]
                    )

                    +

                    current_atr * 0.20

                )

            else:

                sl = (
                    entry
                    +
                    current_atr * 1.5
                )


            risk = (
                sl - entry
            )


            tp1 = tgl[
                "level1"
            ]

            tp2 = tgl[
                "level2"
            ]


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


        result[
            "entry"
        ] = float(entry)

        result[
            "sl"
        ] = float(sl)

        result[
            "tp1"
        ] = float(tp1)

        result[
            "tp2"
        ] = float(tp2)


    return result


# ============================================================
# PAPER TRADING ENGINE
# ============================================================

class PaperTrader:

    def __init__(self):

        self.balance = PAPER_BALANCE

        self.positions = {}

        self.total = 0

        self.wins = 0

        self.losses = 0

        self.realized_pnl = 0.0


    def risk_amount(self):

        return (
            self.balance
            *
            (
                RISK_PERCENT
                /
                100
            )
        )


    def open(
        self,
        setup
    ):

        market = setup[
            "market"
        ]

        if market in self.positions:

            return False


        if setup[
            "signal"
        ] not in (

            "BUY",
            "SELL"

        ):

            return False


        self.positions[
            market
        ] = {

            "market": market,

            "side": setup[
                "signal"
            ],

            "entry": setup[
                "entry"
            ],

            "sl": setup[
                "sl"
            ],

            "tp1": setup[
                "tp1"
            ],

            "tp2": setup[
                "tp2"
            ],

            "opened": datetime.now(
                timezone.utc
            ).isoformat(),

        }


        self.total += 1

        return True


    def check(
        self,
        market,
        price
    ):

        if market not in self.positions:

            return None


        trade = self.positions[
            market
        ]

        side = trade[
            "side"
        ]


        result = None

        exit_price = None


        if side == "BUY":

            if price <= trade[
                "sl"
            ]:

                result = "LOSS"

                exit_price = trade[
                    "sl"
                ]

            elif price >= trade[
                "tp2"
            ]:

                result = "WIN"

                exit_price = trade[
                    "tp2"
                ]


        elif side == "SELL":

            if price >= trade[
                "sl"
            ]:

                result = "LOSS"

                exit_price = trade[
                    "sl"
                ]

            elif price <= trade[
                "tp2"
            ]:

                result = "WIN"

                exit_price = trade[
                    "tp2"
                ]


        if result is None:

            return None


        if side == "BUY":

            movement = (
                exit_price
                -
                trade["entry"]
            )

        else:

            movement = (
                trade["entry"]
                -
                exit_price
            )


        risk_distance = abs(

            trade["entry"]
            -
            trade["sl"]

        )


        if risk_distance > 0:

            pnl = (

                movement
                /
                risk_distance

            ) * self.risk_amount()

        else:

            pnl = 0.0


        if result == "WIN":

            self.wins += 1

        else:

            self.losses += 1


        self.realized_pnl += pnl

        self.balance += pnl


        completed = {

            **trade,

            "exit": exit_price,

            "result": result,

            "pnl": pnl,

        }


        del self.positions[
            market
        ]


        return completed


    def win_rate(self):

        if self.total <= 0:

            return 0.0

        return (

            self.wins
            /
            self.total

        ) * 100


# ============================================================
# PRICE FORMAT
# ============================================================

def format_price(
    market,
    value
):

    decimals = MARKET_CONFIG[
        market
    ]["decimals"]

    return (
        f"{value:.{decimals}f}"
    )


# ============================================================
# SIGNAL TELEGRAM
# ============================================================

def signal_message(
    setup
):

    market = setup[
        "market"
    ]

    icon = {

        "BUY": "🟢",

        "SELL": "🔴",

        "WAIT": "🟡",

    }.get(

        setup["signal"],

        "🟡"

    )


    check_lines = []


    for key, value in setup[
        "checks"
    ].items():

        symbol = (
            "✓"
            if value
            else
            "✗"
        )

        check_lines.append(

            f"{symbol} {key}"

        )


    message = (

        f"{icon} "
        "<b>TRADEBRAIN AI</b>\n\n"

        f"<b>Market:</b> "
        f"{market}\n"

        f"<b>Signal:</b> "
        f"{setup['signal']}\n"

        f"<b>Price:</b> "
        f"{format_price(market, setup['price'])}\n"

        f"<b>Bias:</b> "
        f"{setup['direction']}\n"

        f"<b>Priority:</b> "
        f"{setup['priority_tf']}\n"

        f"<b>Confluence:</b> "
        f"{setup['confidence']}/100\n\n"

        "<b>Rulebook Checks:</b>\n"

        +
        "\n".join(
            check_lines
        )

    )


    if setup[
        "signal"
    ] in (

        "BUY",
        "SELL"

    ):

        message += (

            "\n\n"

            f"<b>ENTRY:</b> "
            f"{format_price(market, setup['entry'])}\n"

            f"<b>SL:</b> "
            f"{format_price(market, setup['sl'])}\n"

            f"<b>TP1:</b> "
            f"{format_price(market, setup['tp1'])}\n"

            f"<b>TP2:</b> "
            f"{format_price(market, setup['tp2'])}\n"

            "<b>MODE:</b> "
            "PAPER TRADE"

        )


    return message


# ============================================================
# PAPER OPEN MESSAGE
# ============================================================

def opened_message(
    setup,
    trader
):

    market = setup[
        "market"
    ]


    return (

        "📌 "
        "<b>PAPER TRADE OPENED</b>\n\n"

        f"<b>Market:</b> "
        f"{market}\n"

        f"<b>Side:</b> "
        f"{setup['signal']}\n"

        f"<b>Entry:</b> "
        f"{format_price(market, setup['entry'])}\n"

        f"<b>SL:</b> "
        f"{format_price(market, setup['sl'])}\n"

        f"<b>TP1:</b> "
        f"{format_price(market, setup['tp1'])}\n"

        f"<b>TP2:</b> "
        f"{format_price(market, setup['tp2'])}\n"

        f"<b>Risk:</b> "
        f"{RISK_PERCENT:.2f}%\n"

        f"<b>Risk Amount:</b> "
        f"{trader.risk_amount():.2f}\n"

        f"<b>Paper Balance:</b> "
        f"{trader.balance:.2f}"

    )


# ============================================================
# WIN / LOSS MESSAGE
# ============================================================

def result_message(
    result,
    trader
):

    market = result[
        "market"
    ]

    icon = (

        "✅"
        if result[
            "result"
        ] == "WIN"

        else

        "❌"

    )


    return (

        f"{icon} "
        f"<b>PAPER TRADE "
        f"{result['result']}</b>\n\n"

        f"<b>Market:</b> "
        f"{market}\n"

        f"<b>Side:</b> "
        f"{result['side']}\n"

        f"<b>Entry:</b> "
        f"{format_price(market, result['entry'])}\n"

        f"<b>Exit:</b> "
        f"{format_price(market, result['exit'])}\n"

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

        f"<b>Paper Balance:</b> "
        f"{trader.balance:.2f}"

    )


# ============================================================
# STARTUP MESSAGE
# ============================================================

def startup_message():

    return (

        "🚀 "
        "<b>TradeBrain AI Started</b>\n\n"

        f"<b>Markets:</b> "
        f"{', '.join(MARKETS)}\n"

        f"<b>Scan:</b> "
        f"{SCAN_INTERVAL}s\n"

        f"<b>Min Confluence:</b> "
        f"{MIN_CONFLUENCE}%\n"

        f"<b>Paper Balance:</b> "
        f"{PAPER_BALANCE:.2f}\n"

        f"<b>Risk:</b> "
        f"{RISK_PERCENT:.2f}%\n"

        "<b>Mode:</b> "
        "PAPER TRADING ONLY\n"

        "<b>Live Orders:</b> "
        "OFF\n\n"

        "<b>MTF Rulebook:</b> ACTIVE\n"

        "<b>Yahoo Finance:</b> OFF"

    )


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    logger.info(
        "=========================================="
    )

    logger.info(
        "TradeBrain AI STARTED"
    )

    logger.info(
        "Markets: %s",
        ", ".join(MARKETS)
    )

    logger.info(
        "Scan interval: %ss",
        SCAN_INTERVAL
    )

    logger.info(
        "Minimum confluence: %s%%",
        MIN_CONFLUENCE
    )

    logger.info(
        "Paper balance: %.2f",
        PAPER_BALANCE
    )

    logger.info(
        "Risk per trade: %.2f%%",
        RISK_PERCENT
    )

    logger.info(
        "Live trading: OFF"
    )

    logger.info(
        "Yahoo Finance: OFF"
    )

    logger.info(
        "=========================================="
    )


    unsupported = [

        market

        for market in MARKETS

        if market not in MARKET_CONFIG

    ]


    if unsupported:

        logger.error(

            "Unsupported markets: %s",

            ", ".join(
                unsupported
            )

        )


    if (

        "XAUUSD" in MARKETS

        or

        "GBPUSD" in MARKETS

    ):

        if not TWELVE_DATA_API_KEY:

            logger.error(

                "TWELVE_DATA_API_KEY "
                "is missing. "
                "XAUUSD/GBPUSD cannot be scanned."

            )


    send_telegram(
        startup_message()
    )


    trader = PaperTrader()


    # Prevent duplicate entry alerts.

    last_setup_key = {}


    while True:

        try:

            for market in MARKETS:

                if market not in MARKET_CONFIG:

                    continue


                try:

                    # ----------------------------------------
                    # GET DATA
                    # ----------------------------------------

                    data = load_market(
                        market
                    )


                    if data is None:

                        continue


                    price = float(

                        data["1m"]["df"][
                            "close"
                        ].iloc[-1]

                    )


                    # ----------------------------------------
                    # CHECK EXISTING PAPER TRADE
                    # ----------------------------------------

                    closed = trader.check(

                        market,

                        price

                    )


                    if closed:

                        logger.info(

                            "%s | %s | "
                            "P/L %.2f",

                            market,

                            closed[
                                "result"
                            ],

                            closed[
                                "pnl"
                            ]

                        )


                        send_telegram(

                            result_message(

                                closed,

                                trader

                            )

                        )


                    # ----------------------------------------
                    # RULEBOOK ANALYSIS
                    # ----------------------------------------

                    setup = analyze_rulebook(

                        market,

                        data

                    )


                    logger.info(

                        "%s | %s | "
                        "Price %s | "
                        "Confidence %d%% | "
                        "Bias %s | "
                        "Priority %s",

                        market,

                        setup[
                            "signal"
                        ],

                        format_price(
                            market,
                            price
                        ),

                        setup[
                            "confidence"
                        ],

                        setup[
                            "direction"
                        ],

                        setup[
                            "priority_tf"
                        ]

                    )


                    # ----------------------------------------
                    # ONLY VALID BUY / SELL
                    # ----------------------------------------

                    if setup[
                        "signal"
                    ] in (

                        "BUY",
                        "SELL"

                    ):


                        setup_key = (

                            setup[
                                "signal"
                            ],

                            setup[
                                "priority_tf"
                            ],

                            round(
                                setup[
                                    "entry"
                                ],
                                8
                            ),

                            round(
                                setup[
                                    "sl"
                                ],
                                8
                            ),

                            round(
                                setup[
                                    "tp2"
                                ],
                                8
                            ),

                        )


                        # Don't repeatedly alert
                        # same setup.

                        if (

                            last_setup_key.get(
                                market
                            )

                            !=

                            setup_key

                        ):

                            send_telegram(

                                signal_message(
                                    setup
                                )

                            )


                            # --------------------------------
                            # PAPER TRADE
                            # --------------------------------

                            if trader.open(
                                setup
                            ):

                                logger.info(

                                    "%s | "
                                    "PAPER %s OPENED",

                                    market,

                                    setup[
                                        "signal"
                                    ]

                                )


                                send_telegram(

                                    opened_message(

                                        setup,

                                        trader

                                    )

                                )


                            last_setup_key[
                                market
                            ] = setup_key


                except Exception as e:

                    logger.exception(

                        "%s analysis error: %s",

                        market,

                        e

                    )


            # --------------------------------------------
            # WAIT
            # --------------------------------------------

            time.sleep(
                SCAN_INTERVAL
            )


        except KeyboardInterrupt:

            logger.info(
                "Bot stopped."
            )

            break


        except Exception as e:

            logger.exception(

                "Main loop error: %s",

                e

            )

            time.sleep(30)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
