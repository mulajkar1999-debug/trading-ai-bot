import os
import time
import logging
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np

# ============================================================
# CONFIG
# ============================================================

SYMBOL = os.getenv("SYMBOL", "BTCUSDT")

# Timeframes
TF_ENTRY = os.getenv("ENTRY_TIMEFRAME", "5m")
TF_CONFIRM = os.getenv("CONFIRM_TIMEFRAME", "15m")
TF_H1 = "1h"
TF_H4 = "4h"
TF_D1 = "1d"

# Bot timing
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))

# Paper trading
PAPER_BALANCE = float(os.getenv("PAPER_BALANCE", "10000"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1.0"))

# Confidence threshold
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "85"))

# Binance
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("8723192534:AAGz9ViSaVzC1bF2Kmpjxmu37hDI9fO4oYg", "")
TELEGRAM_CHAT_ID = os.getenv("1317739622", "")

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("TradeBrain")


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials missing")
        return False

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
        r = requests.post(url, json=payload, timeout=15)

        if r.status_code == 200:
            return True

        log.error(
            "Telegram error %s: %s",
            r.status_code,
            r.text[:300]
        )

    except Exception as e:
        log.error("Telegram exception: %s", e)

    return False


# ============================================================
# BINANCE
# ============================================================

BINANCE_URL = "https://api.binance.com"


def get_klines(symbol, interval, limit=250):

    url = f"{BINANCE_URL}/api/v3/klines"

    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit
    }

    try:
        r = requests.get(
            url,
            params=params,
            timeout=15
        )

        r.raise_for_status()

        data = r.json()

        if not isinstance(data, list):
            return pd.DataFrame()

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

        df = pd.DataFrame(data, columns=columns)

        numeric_cols = [
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]

        for col in numeric_cols:
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
        log.error(
            "Binance %s %s error: %s",
            symbol,
            interval,
            e
        )

        return pd.DataFrame()


# ============================================================
# BASIC INDICATORS
# ============================================================

def ema(series, period):
    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def atr(df, period=14):

    high_low = df["high"] - df["low"]

    high_close = (
        df["high"] -
        df["close"].shift()
    ).abs()

    low_close = (
        df["low"] -
        df["close"].shift()
    ).abs()

    tr = pd.concat(
        [
            high_low,
            high_close,
            low_close
        ],
        axis=1
    ).max(axis=1)

    return tr.rolling(period).mean()


# ============================================================
# SWING STRUCTURE
# ============================================================

def detect_structure(df, lookback=3):

    if len(df) < lookback * 2 + 10:
        return {
            "trend": "UNKNOWN",
            "last_swing_high": None,
            "last_swing_low": None,
            "previous_swing_high": None,
            "previous_swing_low": None
        }

    highs = []
    lows = []

    for i in range(
        lookback,
        len(df) - lookback
    ):

        high = df["high"].iloc[i]
        low = df["low"].iloc[i]

        left_high = df["high"].iloc[
            i - lookback:i
        ].max()

        right_high = df["high"].iloc[
            i + 1:i + lookback + 1
        ].max()

        left_low = df["low"].iloc[
            i - lookback:i
        ].min()

        right_low = df["low"].iloc[
            i + 1:i + lookback + 1
        ].min()

        if high > left_high and high > right_high:
            highs.append((i, high))

        if low < left_low and low < right_low:
            lows.append((i, low))

    if len(highs) < 2 or len(lows) < 2:
        return {
            "trend": "RANGE",
            "last_swing_high": None,
            "last_swing_low": None,
            "previous_swing_high": None,
            "previous_swing_low": None
        }

    h1 = highs[-1][1]
    h2 = highs[-2][1]

    l1 = lows[-1][1]
    l2 = lows[-2][1]

    if h1 > h2 and l1 > l2:
        trend = "BULLISH"

    elif h1 < h2 and l1 < l2:
        trend = "BEARISH"

    else:
        trend = "RANGE"

    return {
        "trend": trend,
        "last_swing_high": h1,
        "last_swing_low": l1,
        "previous_swing_high": h2,
        "previous_swing_low": l2
    }


# ============================================================
# BOS
# ============================================================

def detect_bos(df):

    structure = detect_structure(df)

    if structure["last_swing_high"] is None:
        return "NONE"

    close = df["close"].iloc[-1]

    if close > structure["last_swing_high"]:
        return "BULLISH_BOS"

    if close < structure["last_swing_low"]:
        return "BEARISH_BOS"

    return "NONE"


# ============================================================
# CHOCH
# ============================================================

def detect_choch(df):

    if len(df) < 50:
        return "NONE"

    structure = detect_structure(df)

    previous_trend = structure["trend"]

    close = df["close"].iloc[-1]

    high = structure["last_swing_high"]
    low = structure["last_swing_low"]

    if high is None or low is None:
        return "NONE"

    # Bearish structure -> bullish shift
    if previous_trend == "BEARISH":
        if close > high:
            return "BULLISH_CHOCH"

    # Bullish structure -> bearish shift
    if previous_trend == "BULLISH":
        if close < low:
            return "BEARISH_CHOCH"

    return "NONE"


# ============================================================
# FVG
# ============================================================

def detect_fvg(df):

    if len(df) < 5:
        return {
            "bullish": False,
            "bearish": False,
            "bullish_zone": None,
            "bearish_zone": None
        }

    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

    bullish = c3["low"] > c1["high"]

    bearish = c3["high"] < c1["low"]

    result = {
        "bullish": bullish,
        "bearish": bearish,
        "bullish_zone": None,
        "bearish_zone": None
    }

    if bullish:
        result["bullish_zone"] = (
            float(c1["high"]),
            float(c3["low"])
        )

    if bearish:
        result["bearish_zone"] = (
            float(c3["high"]),
            float(c1["low"])
        )

    return result


# ============================================================
# DEMAND / SUPPLY
# ============================================================

def detect_zones(df):

    if len(df) < 20:
        return {
            "demand": None,
            "supply": None
        }

    recent = df.tail(20)

    demand_low = float(
        recent["low"].min()
    )

    demand_high = float(
        recent["low"].quantile(0.35)
    )

    supply_high = float(
        recent["high"].max()
    )

    supply_low = float(
        recent["high"].quantile(0.65)
    )

    return {
        "demand": (
            demand_low,
            demand_high
        ),
        "supply": (
            supply_low,
            supply_high
        )
    }


# ============================================================
# TGL APPROXIMATION
# ============================================================

def calculate_tgl(df):

    structure = detect_structure(df)

    high = structure["last_swing_high"]
    low = structure["last_swing_low"]

    if high is None or low is None:
        return {
            "level1": None,
            "level2": None
        }

    rng = abs(high - low)

    trend = structure["trend"]

    if trend == "BULLISH":

        level1 = high
        level2 = high + rng * 0.50

    elif trend == "BEARISH":

        level1 = low
        level2 = low - rng * 0.50

    else:

        level1 = high
        level2 = low

    return {
        "level1": float(level1),
        "level2": float(level2)
    }


# ============================================================
# CANDLE CONFIRMATION
# ============================================================

def confirmation(df):

    if len(df) < 3:
        return "NONE"

    candle = df.iloc[-1]

    body = abs(
        candle["close"] -
        candle["open"]
    )

    candle_range = (
        candle["high"] -
        candle["low"]
    )

    if candle_range <= 0:
        return "NONE"

    body_ratio = body / candle_range

    if (
        candle["close"] > candle["open"]
        and body_ratio >= 0.55
    ):
        return "BULLISH"

    if (
        candle["close"] < candle["open"]
        and body_ratio >= 0.55
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# MTF ANALYSIS
# ============================================================

def analyze_mtf():

    frames = {}

    for tf in [
        TF_D1,
        TF_H4,
        TF_H1,
        TF_CONFIRM,
        TF_ENTRY
    ]:

        df = get_klines(
            SYMBOL,
            tf,
            250
        )

        if df.empty:
            return None

        structure = detect_structure(df)

        frames[tf] = {
            "df": df,
            "trend": structure["trend"],
            "structure": structure,
            "bos": detect_bos(df),
            "choch": detect_choch(df),
            "fvg": detect_fvg(df),
            "zones": detect_zones(df),
            "confirmation": confirmation(df),
            "tgl": calculate_tgl(df)
        }

    return frames


# ============================================================
# DIRECTIONAL BIAS
# ============================================================

def determine_bias(frames):

    d1 = frames[TF_D1]["trend"]
    h4 = frames[TF_H4]["trend"]
    h1 = frames[TF_H1]["trend"]

    bullish = [
        d1,
        h4,
        h1
    ].count("BULLISH")

    bearish = [
        d1,
        h4,
        h1
    ].count("BEARISH")

    # Rulebook priority logic
    if d1 == h4 == h1:
        return d1, "1H_PRIORITY"

    if d1 == h4 and h1 != d1:
        return d1, "4H_PRIORITY"

    if h4 == h1 and h4 != d1:
        return h4, "DAILY_CONFLICT"

    if bullish > bearish:
        return "BULLISH", "MAJORITY"

    if bearish > bullish:
        return "BEARISH", "MAJORITY"

    return "RANGE", "NO_CLEAR_BIAS"


# ============================================================
# ENTRY ANALYSIS
# ============================================================

def generate_signal(frames):

    bias, priority = determine_bias(frames)

    entry = frames[TF_ENTRY]
    confirm = frames[TF_CONFIRM]

    entry_df = entry["df"]

    price = float(
        entry_df["close"].iloc[-1]
    )

    score = 0
    reasons = []

    # ----------------------------------------
    # Higher timeframe bias
    # ----------------------------------------

    if bias == "BULLISH":
        score += 25
        reasons.append("HTF bullish")

    elif bias == "BEARISH":
        score += 25
        reasons.append("HTF bearish")

    else:
        return {
            "signal": "WAIT",
            "confidence": 0,
            "price": price,
            "reason": "No clear HTF bias",
            "bias": bias,
            "priority": priority
        }

    # ----------------------------------------
    # Confirmation timeframe
    # ----------------------------------------

    if bias == "BULLISH":

        if confirm["trend"] == "BULLISH":
            score += 15
            reasons.append("15M bullish")

        if confirm["bos"] == "BULLISH_BOS":
            score += 15
            reasons.append("Bullish BOS")

        if confirm["choch"] == "BULLISH_CHOCH":
            score += 10
            reasons.append("Bullish CHOCH")

        if confirm["fvg"]["bullish"]:
            score += 5
            reasons.append("Bullish FVG")

    elif bias == "BEARISH":

        if confirm["trend"] == "BEARISH":
            score += 15
            reasons.append("15M bearish")

        if confirm["bos"] == "BEARISH_BOS":
            score += 15
            reasons.append("Bearish BOS")

        if confirm["choch"] == "BEARISH_CHOCH":
            score += 10
            reasons.append("Bearish CHOCH")

        if confirm["fvg"]["bearish"]:
            score += 5
            reasons.append("Bearish FVG")

    # ----------------------------------------
    # Candle confirmation
    # ----------------------------------------

    if bias == "BULLISH":
        if entry["confirmation"] == "BULLISH":
            score += 10
            reasons.append("Bullish confirmation")

    if bias == "BEARISH":
        if entry["confirmation"] == "BEARISH":
            score += 10
            reasons.append("Bearish confirmation")

    # ----------------------------------------
    # EMA trend filter
    # ----------------------------------------

    df = entry_df.copy()

    df["ema20"] = ema(
        df["close"],
        20
    )

    df["ema50"] = ema(
        df["close"],
        50
    )

    last = df.iloc[-1]

    if bias == "BULLISH":

        if last["ema20"] > last["ema50"]:
            score += 5
            reasons.append("EMA bullish")

    elif bias == "BEARISH":

        if last["ema20"] < last["ema50"]:
            score += 5
            reasons.append("EMA bearish")

    # ----------------------------------------
    # Confidence
    # ----------------------------------------

    confidence = min(
        100,
        score
    )

    # IMPORTANT:
    # Minimum confidence rule
    if confidence < MIN_CONFIDENCE:
        signal = "WAIT"

    elif bias == "BULLISH":
        signal = "BUY"

    elif bias == "BEARISH":
        signal = "SELL"

    else:
        signal = "WAIT"

    return {
        "signal": signal,
        "confidence": confidence,
        "price": price,
        "reason": ", ".join(reasons),
        "bias": bias,
        "priority": priority
    }


# ============================================================
# PAPER TRADE
# ============================================================

class PaperTrader:

    def __init__(self):

        self.balance = PAPER_BALANCE
        self.open_trade = None

        self.total = 0
        self.wins = 0
        self.losses = 0

    def calculate_trade(
        self,
        signal,
        price,
        frames
    ):

        entry = frames[TF_ENTRY]

        atr_value = atr(
            entry["df"]
        ).iloc[-1]

        if pd.isna(atr_value):
            atr_value = price * 0.002

        atr_value = float(atr_value)

        if signal == "BUY":

            sl = price - atr_value * 1.5

            risk = price - sl

            tp1 = price + risk * 1.5
            tp2 = price + risk * 2.5

        elif signal == "SELL":

            sl = price + atr_value * 1.5

            risk = sl - price

            tp1 = price - risk * 1.5
            tp2 = price - risk * 2.5

        else:
            return None

        return {
            "side": signal,
            "entry": price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "opened_at": datetime.now(
                timezone.utc
            ).isoformat()
        }

    def open(
        self,
        signal,
        price,
        frames
    ):

        if self.open_trade is not None:
            return None

        trade = self.calculate_trade(
            signal,
            price,
            frames
        )

        if trade is None:
            return None

        self.open_trade = trade
        self.total += 1

        return trade

    def monitor(self, price):

        if self.open_trade is None:
            return None

        trade = self.open_trade

        side = trade["side"]

        result = None

        if side == "BUY":

            if price <= trade["sl"]:
                result = "LOSS"

            elif price >= trade["tp2"]:
                result = "WIN"

        elif side == "SELL":

            if price >= trade["sl"]:
                result = "LOSS"

            elif price <= trade["tp2"]:
                result = "WIN"

        if result is None:
            return None

        entry = trade["entry"]

        if side == "BUY":
            pnl = price - entry
        else:
            pnl = entry - price

        self.balance += pnl

        if result == "WIN":
            self.wins += 1
        else:
            self.losses += 1

        trade["exit"] = price
        trade["result"] = result
        trade["pnl"] = pnl

        self.open_trade = None

        return trade

    def winrate(self):

        if self.total == 0:
            return 0

        return (
            self.wins /
            self.total
        ) * 100


# ============================================================
# FORMAT TELEGRAM SIGNAL
# ============================================================

def signal_message(signal_data):

    signal = signal_data["signal"]

    if signal == "BUY":
        icon = "🟢"

    elif signal == "SELL":
        icon = "🔴"

    else:
        icon = "🟡"

    return (
        f"{icon} <b>TradeBrain AI</b>\n\n"
        f"<b>Symbol:</b> {SYMBOL}\n"
        f"<b>Timeframe:</b> {TF_ENTRY}\n"
        f"<b>Signal:</b> {signal}\n"
        f"<b>Confidence:</b> "
        f"{signal_data['confidence']:.0f}%\n"
        f"<b>Price:</b> "
        f"{signal_data['price']:.2f}\n"
        f"<b>Bias:</b> "
        f"{signal_data['bias']}\n"
        f"<b>Priority:</b> "
        f"{signal_data['priority']}\n\n"
        f"<b>Reason:</b>\n"
        f"{signal_data['reason']}"
    )


def trade_message(trade):

    return (
        f"📌 <b>PAPER TRADE OPENED</b>\n\n"
        f"<b>Symbol:</b> {SYMBOL}\n"
        f"<b>Side:</b> {trade['side']}\n"
        f"<b>Entry:</b> {trade['entry']:.2f}\n"
        f"<b>SL:</b> {trade['sl']:.2f}\n"
        f"<b>TP1:</b> {trade['tp1']:.2f}\n"
        f"<b>TP2:</b> {trade['tp2']:.2f}\n"
    )


def result_message(
    trade,
    trader
):

    result_icon = (
        "✅"
        if trade["result"] == "WIN"
        else "❌"
    )

    return (
        f"{result_icon} <b>PAPER TRADE "
        f"{trade['result']}</b>\n\n"
        f"<b>Side:</b> {trade['side']}\n"
        f"<b>Entry:</b> {trade['entry']:.2f}\n"
        f"<b>Exit:</b> {trade['exit']:.2f}\n"
        f"<b>P/L:</b> {trade['pnl']:.4f}\n\n"
        f"<b>Wins:</b> {trader.wins}\n"
        f"<b>Losses:</b> {trader.losses}\n"
        f"<b>Total:</b> {trader.total}\n"
        f"<b>Win Rate:</b> "
        f"{trader.winrate():.2f}%\n"
        f"<b>Paper Balance:</b> "
        f"{trader.balance:.2f}"
    )


# ============================================================
# DUPLICATE SIGNAL PROTECTION
# ============================================================

last_signal = None
last_signal_time = None


def should_send_signal(signal):

    global last_signal
    global last_signal_time

    if signal == "WAIT":
        return False

    now = time.time()

    if (
        last_signal == signal
        and last_signal_time is not None
        and now - last_signal_time < 900
    ):
        return False

    last_signal = signal
    last_signal_time = now

    return True


# ============================================================
# MAIN BOT
# ============================================================

def run_bot():

    log.info("=" * 60)
    log.info("TradeBrain AI Trading Bot Starting")
    log.info("Symbol: %s", SYMBOL)
    log.info("Entry TF: %s", TF_ENTRY)
    log.info("Minimum confidence: %s", MIN_CONFIDENCE)
    log.info("Paper Trading: ENABLED")
    log.info("Live Trading: DISABLED")
    log.info("=" * 60)

    telegram(
        "🚀 <b>TradeBrain AI Started</b>\n\n"
        f"Symbol: {SYMBOL}\n"
        f"Entry TF: {TF_ENTRY}\n"
        "Mode: PAPER TRADING\n"
        "Live trading: OFF"
    )

    trader = PaperTrader()

    while True:

        try:

            frames = analyze_mtf()

            if frames is None:

                log.warning(
                    "Market data unavailable"
                )

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # Current price
            entry_df = frames[
                TF_ENTRY
            ]["df"]

            price = float(
                entry_df["close"].iloc[-1]
            )

            # --------------------------------
            # Monitor existing paper trade
            # --------------------------------

            result = trader.monitor(
                price
            )

            if result:

                log.info(
                    "Trade finished: %s",
                    result["result"]
                )

                telegram(
                    result_message(
                        result,
                        trader
                    )
                )

            # --------------------------------
            # Generate new signal
            # --------------------------------

            signal_data = generate_signal(
                frames
            )

            log.info(
                "%s | Price %.2f | "
                "Confidence %.0f%% | "
                "Bias %s",
                signal_data["signal"],
                price,
                signal_data["confidence"],
                signal_data["bias"]
            )

            # --------------------------------
            # Only high confidence signals
            # --------------------------------

            if (
                signal_data["signal"]
                in ["BUY", "SELL"]
                and
                signal_data["confidence"]
                >= MIN_CONFIDENCE
            ):

                if should_send_signal(
                    signal_data["signal"]
                ):

                    telegram(
                        signal_message(
                            signal_data
                        )
                    )

                    # Paper trade
                    trade = trader.open(
                        signal_data["signal"],
                        price,
                        frames
                    )

                    if trade:

                        telegram(
                            trade_message(
                                trade
                            )
                        )

                        log.info(
                            "Paper trade opened: %s",
                            trade
                        )

            time.sleep(
                SCAN_INTERVAL
            )

        except KeyboardInterrupt:

            log.info(
                "Bot stopped manually"
            )

            break

        except Exception as e:

            log.exception(
                "Main loop error: %s",
                e
            )

            try:

                telegram(
                    "⚠️ <b>Bot Error</b>\n\n"
                    f"<code>{str(e)[:500]}</code>\n\n"
                    "Bot will retry automatically."
                )

            except Exception:
                pass

            time.sleep(30)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    run_bot()
