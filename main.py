import os
import time
import threading
import requests
from datetime import datetime, timedelta
import pytz
from flask import Flask, jsonify
# ============================================================
# APP / CONFIG
# ============================================================
app = Flask(__name__)
# IMPORTANT:
# Telegram token Render Environment Variables se liya jayega.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1317739622")
TIMEFRAME = "5m"
CANDLE_GRANULARITY = 300
SCAN_INTERVAL_SECONDS = 10
SIGNAL_COOLDOWN_MINUTES = 5
# 75 = more signals
# 80-85 = stricter / fewer signals
MIN_SIGNAL_SCORE = 75
CANDLE_LIMIT = 100
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
    "win_rate": 0.0
}
state_lock = threading.Lock()
# ============================================================
# TELEGRAM
# ============================================================
def send_telegram_alert(message, reply_markup=None):
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is missing.")
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
        print("Telegram Send Error:", response.text)
    except Exception as e:
        print("Telegram Alert Error:", e)
    return None
def edit_telegram_alert(
    message_id,
    new_message,
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
            "text": new_message,
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
            print("Telegram Edit Error:", response.text)
    except Exception as e:
        print("Telegram Edit Error:", e)
# ============================================================
# MARKET DATA
# ============================================================
def get_product_id(asset):
    if asset == "PAXG":
        return "PAXG-USD"
    if asset == "BTC":
        return "BTC-USD"
    return None
def get_market_klines(
    asset,
    limit=CANDLE_LIMIT
):
    product_id = get_product_id(asset)
    if not product_id:
        return None
    url = (
        f"https://api.exchange.coinbase.com/"
        f"products/{product_id}/candles"
        f"?granularity={CANDLE_GRANULARITY}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=10
        )
        if response.status_code != 200:
            print(
                f"Coinbase API Error "
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
        # ----------------------------------------------------
        # ONLY COMPLETED 5-MINUTE CANDLES
        # ----------------------------------------------------
        current_timestamp = int(time.time())
        completed = []
        for candle in data:
            candle_time = int(candle[0])
            if (
                candle_time
                + CANDLE_GRANULARITY
                <= current_timestamp
            ):
                completed.append(candle)
        if len(completed) < 30:
            return None
        completed = completed[-limit:]
        opens = [
            float(x[3])
            for x in completed
        ]
        highs = [
            float(x[2])
            for x in completed
        ]
        lows = [
            float(x[1])
            for x in completed
        ]
        closes = [
            float(x[4])
            for x in completed
        ]
        volumes = [
            float(x[5])
            for x in completed
        ]
        timestamps = [
            int(x[0])
            for x in completed
        ]
        return {
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "closes": closes,
            "volumes": volumes,
            "timestamps": timestamps
        }
    except Exception as e:
        print(
            f"Market Fetch Error "
            f"{product_id}: {e}"
        )
        return None
# ============================================================
# EMA
# ============================================================
def calculate_ema(
    values,
    period
):
    if len(values) < period:
        return []
    multiplier = 2 / (period + 1)
    first_sma = (
        sum(values[:period])
        / period
    )
    ema_values = [
        first_sma
    ]
    previous = first_sma
    for price in values[period:]:
        current = (
            (price - previous)
            * multiplier
            + previous
        )
        ema_values.append(current)
        previous = current
    return ema_values
# ============================================================
# RSI
# ============================================================
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
    average_gain = (
        sum(gains[:period])
        / period
    )
    average_loss = (
        sum(losses[:period])
        / period
    )
    result = []
    if average_loss == 0:
        result.append(100)
    else:
        rs = (
            average_gain
            / average_loss
        )
        result.append(
            100 - (
                100
                / (1 + rs)
            )
        )
    for i in range(
        period,
        len(gains)
    ):
        average_gain = (
            (
                average_gain
                * (period - 1)
            )
            + gains[i]
        ) / period
        average_loss = (
            (
                average_loss
                * (period - 1)
            )
            + losses[i]
        ) / period
        if average_loss == 0:
            result.append(100)
        else:
            rs = (
                average_gain
                / average_loss
            )
            result.append(
                100 - (
                    100
                    / (1 + rs)
                )
            )
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
    for i in range(
        1,
        len(closes)
    ):
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
    first_atr = (
        sum(
            true_ranges[:period]
        )
        / period
    )
    atr_values = [
        first_atr
    ]
    previous_atr = first_atr
    for tr in true_ranges[period:]:
        current_atr = (
            (
                previous_atr
                * (period - 1)
            )
            + tr
        ) / period
        atr_values.append(
            current_atr
        )
        previous_atr = current_atr
    return atr_values
# ============================================================
# SIGNAL ANALYSIS
# ============================================================
def analyze_asset(asset):
    global latest_signal_data
    market = get_market_klines(asset)
    if not market:
        return None
    opens = market["opens"]
    highs = market["highs"]
    lows = market["lows"]
    closes = market["closes"]
    volumes = market["volumes"]
    if len(closes) < 50:
        return None
    current_price = closes[-1]
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
    if not ema9:
        return None
    if not ema21:
        return None
    if not rsi:
        return None
    if not atr:
        return None
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
    average_volume = (
        sum(volumes[-6:-1])
        / 5
    )
    current_volume = volumes[-1]
    if average_volume <= 0:
        return None
    volume_ratio = (
        current_volume
        / average_volume
    )
    bullish_candle = (
        current_price
        > current_open
    )
    bearish_candle = (
        current_price
        < current_open
    )
    candle_range = (
        current_high
        - current_low
    )
    if candle_range <= 0:
        return None
    candle_body = abs(
        current_price
        - current_open
    )
    body_ratio = (
        candle_body
        / candle_range
    )
    # ========================================================
    # BUY SCORE
    # ========================================================
    buy_score = 0
    # Strong bullish trend
    if ema9_now > ema21_now:
        buy_score += 20
    # Price above EMA 9
    if current_price > ema9_now:
        buy_score += 10
    # RSI bullish zone
    if 52 <= rsi_now <= 68:
        buy_score += 15
    # Volume confirmation
    if volume_ratio >= 1.15:
        buy_score += 15
    # Strong bullish candle
    if (
        bullish_candle
        and body_ratio >= 0.50
    ):
        buy_score += 10
    # Breakout
    if current_price > previous_high:
        buy_score += 20
    # Early pullback/recovery
    if (
        current_low <= ema9_now
        and current_price > ema9_now
        and bullish_candle
    ):
        buy_score += 10
    # ========================================================
    # SELL SCORE
    # ========================================================
    sell_score = 0
    # Strong bearish trend
    if ema9_now < ema21_now:
        sell_score += 20
    # Price below EMA 9
    if current_price < ema9_now:
        sell_score += 10
    # RSI bearish zone
    if 32 <= rsi_now <= 48:
        sell_score += 15
    # Volume confirmation
    if volume_ratio >= 1.15:
        sell_score += 15
    # Strong bearish candle
    if (
        bearish_candle
        and body_ratio >= 0.50
    ):
        sell_score += 10
    # Breakdown
    if current_price < previous_low:
        sell_score += 20
    # Early pullback/recovery
    if (
        current_high >= ema9_now
        and current_price < ema9_now
        and bearish_candle
    ):
        sell_score += 10
    # ========================================================
    # SELECT BEST SIGNAL
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
    # ATR TP / SL
    # ========================================================
    if asset == "PAXG":
        sl_distance = max(
            atr_now * 1.15,
            0.80
        )
        tp_distance = (
            sl_distance * 1.45
        )
        recommended_lot = 0.03
        display_asset = "PAXGUSD"
    else:
        sl_distance = max(
            atr_now * 1.15,
            70.0
        )
        tp_distance = (
            sl_distance * 1.45
        )
        recommended_lot = 0.01
        display_asset = "BTCUSD"
    if action == "BUY":
        tp = (
            current_price
            + tp_distance
        )
        sl = (
            current_price
            - sl_distance
        )
    else:
        tp = (
            current_price
            - tp_distance
        )
        sl = (
            current_price
            + sl_distance
        )
    signal = {
        "asset": display_asset,
        "asset_key": asset,
        "price": round(
            current_price,
            2
        ),
        "action": action,
        "score": score,
        "rsi": round(
            rsi_now,
            2
        ),
        "ema9": round(
            ema9_now,
            2
        ),
        "ema21": round(
            ema21_now,
            2
        ),
        "atr": round(
            atr_now,
            4
        ),
        "volume_ratio": round(
            volume_ratio,
            2
        ),
        "tp": round(
            tp,
            2
        ),
        "sl": round(
            sl,
            2
        ),
        "recommended_lot":
            recommended_lot
    }
    latest_signal_data = signal
    return signal
# ============================================================
# SIGNAL PROTECTION
# ============================================================
def has_active_signal(
    asset_key
):
    for signal in active_signals:
        if (
            signal["asset_key"]
            == asset_key
        ):
            return True
    return False
def cooldown_active(
    asset_key
):
    previous_time = (
        last_signal_time
        .get(asset_key)
    )
    if not previous_time:
        return False
    return (
        datetime.now()
        - previous_time
    ) < timedelta(
        minutes=
        SIGNAL_COOLDOWN_MINUTES
    )
# ============================================================
# CREATE SIGNAL
# ============================================================
def analyze_and_trigger(
    asset_key
):
    if cooldown_active(
        asset_key
    ):
        return
    if has_active_signal(
        asset_key
    ):
        return
    signal = analyze_asset(
        asset_key
    )
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
    if signal["action"] == "BUY":
        direction_emoji = "🟢"
    else:
        direction_emoji = "🔴"
    alert_message = (
        "🎯 *5M SCALPING SIGNAL*\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💹 Asset: *{signal['asset']}*\n"
        f"⏰ Time: `{signal_time}`\n\n"
        f"{direction_emoji} "
        f"Action: *{signal['action']}*\n"
        f"💰 Entry: `{signal['price']}`\n"
        f"🎯 Take Profit: `{signal['tp']}`\n"
        f"🛑 Stop Loss: `{signal['sl']}`\n\n"
        f"🧠 Signal Score: "
        f"*{signal['score']}/100*\n"
        f"📊 RSI: `{signal['rsi']}`\n"
        f"📈 EMA 9: `{signal['ema9']}`\n"
        f"📉 EMA 21: `{signal['ema21']}`\n"
        f"🔊 Volume: "
        f"`{signal['volume_ratio']}x`\n"
        f"〽️ ATR: `{signal['atr']}`\n\n"
        f"🛡️ Suggested Lot: "
        f"`{signal['recommended_lot']}`\n\n"
        "Status: *ACTIVE ⏳*"
    )
    message_id = send_telegram_alert(
        alert_message,
        reply_markup
    )
    if not message_id:
        return
    with state_lock:
        last_signal_time[
            asset_key
        ] = now
        active_signals.append({
            "msg_id":
                message_id,
            "asset_key":
                asset_key,
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
            "score":
                signal["score"],
            "created_at":
                signal_time,
            "created_timestamp":
                now.isoformat()
        })
# ============================================================
# TRADE MONITOR
# ============================================================
def monitor_active_trades():
    while True:
        try:
            for signal in list(
                active_signals
            ):
                asset_key = (
                    signal["asset_key"]
                )
                market = (
                    get_market_klines(
                        asset_key,
                        limit=5
                    )
                )
                if not market:
                    continue
                highs = market["highs"]
                lows = market["lows"]
                if not highs or not lows:
                    continue
                current_high = highs[-1]
                current_low = lows[-1]
                is_buy = (
                    signal["action"]
                    == "BUY"
                )
                tp_hit = (
                    current_high
                    >= signal["tp"]
                    if is_buy
                    else
                    current_low
                    <= signal["tp"]
                )
                sl_hit = (
                    current_low
                    <= signal["sl"]
                    if is_buy
                    else
                    current_high
                    >= signal["sl"]
                )
                # ------------------------------------------------
                # BOTH TP AND SL
                # ------------------------------------------------
                if (
                    tp_hit
                    and sl_hit
                ):
                    result = "LOSS"
                    status_text = (
                        "⚠️ BOTH TP & SL "
                        "TOUCHED — "
                        "COUNTED AS LOSS"
                    )
                elif tp_hit:
                    result = "WIN"
                    status_text = (
                        "✅ TARGET HIT "
                        "(WIN) 🎯"
                    )
                elif sl_hit:
                    result = "LOSS"
                    status_text = (
                        "❌ STOP LOSS HIT "
                        "(LOSS) 🛑"
                    )
                else:
                    continue
                # ------------------------------------------------
                # UPDATE HISTORY
                # ------------------------------------------------
                with state_lock:
                    if result == "WIN":
                        trade_history[
                            "wins"
                        ] += 1
                    else:
                        trade_history[
                            "losses"
                        ] += 1
                    total = (
                        trade_history[
                            "wins"
                        ]
                        +
                        trade_history[
                            "losses"
                        ]
                    )
                    trade_history[
                        "total_signals"
                    ] = total
                    if total > 0:
                        trade_history[
                            "win_rate"
                        ] = round(
                            (
                                trade_history[
                                    "wins"
                                ]
                                / total
                            ) * 100,
                            2
                        )
                # ------------------------------------------------
                # TELEGRAM RESULT
                # ------------------------------------------------
                if (
                    signal["asset"]
                    == "PAXGUSD"
                ):
                    chart_symbol = (
                        "PAXGUSD"
                    )
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
                updated_message = (
                    "🎯 *5M SCALPING RESULT*\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"💹 Asset: "
                    f"*{signal['asset']}*\n"
                    f"⏰ Signal: "
                    f"`{signal['created_at']}`\n\n"
                    f"Status: "
                    f"*{status_text}*\n"
                    f"Action: "
                    f"*{signal['action']}*\n"
                    f"Entry: "
                    f"`{signal['price']}`\n"
                    f"TP: "
                    f"`{signal['tp']}`\n"
                    f"SL: "
                    f"`{signal['sl']}`\n"
                    f"Score: "
                    f"`{signal['score']}/100`\n\n"
                    "📊 *Performance*\n"
                    f"Total: "
                    f"`{trade_history['total_signals']}`\n"
                    f"✅ Wins: "
                    f"`{trade_history['wins']}`\n"
                    f"❌ Losses: "
                    f"`{trade_history['losses']}`\n"
                    f"🏆 Win Rate: "
                    f"*{trade_history['win_rate']}%*"
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
                "Monitor Loop Error:",
                e
            )
        time.sleep(5)
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
            SCAN_INTERVAL_SECONDS
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
            TIMEFRAME,
        "min_signal_score":
            MIN_SIGNAL_SCORE
    }), 200
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
        }), 200
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
        }), 200
# ============================================================
# START THREADS
# ============================================================
scanner_thread = threading.Thread(
    target=
        continuous_auto_scanner,
    daemon=True
)
monitor_thread = threading.Thread(
    target=
        monitor_active_trades,
    daemon=True
)
scanner_thread.start()
monitor_thread.start()
# ============================================================
# RUN SERVER
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
