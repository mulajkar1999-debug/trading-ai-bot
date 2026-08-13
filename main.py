import os
import threading
import time
from datetime import datetime
import ccxt
from flask import Flask, jsonify, render_template_string
import requests

# ==========================================
# 1. CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1317739622")

# Pairs to scan
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# Initialize Binance via CCXT
exchange = ccxt.binance({"enableRateLimit": True})

# ==========================================
# 2. GLOBAL MEMORY & STORES
# ==========================================
recent_telegram_messages = []
MAX_MESSAGE_HISTORY = 30
active_trades = []  # Active Paper Trades tracking

bot_stats = {
    "status": "Online",
    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
    "wins": 0,
    "losses": 0,
    "last_scan": "Initializing...",
}

market_matrix = {}

# ==========================================
# 3. TELEGRAM ALERT SYSTEM
# ==========================================
def send_telegram_message(message_text, signal_type=None):
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "HTML",
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

    msg_entry = {"timestamp": timestamp_str, "message": message_text}
    recent_telegram_messages.insert(0, msg_entry)
    if len(recent_telegram_messages) > MAX_MESSAGE_HISTORY:
        recent_telegram_messages.pop()

    if signal_type == "WIN":
        bot_stats["wins"] += 1
    elif signal_type == "LOSS":
        bot_stats["losses"] += 1


# ==========================================
# 4. SMC ENGINE & MARKET ANALYSIS
# ==========================================
def fetch_candles(symbol, timeframe, limit=50):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return ohlcv
    except Exception as e:
        print(f"Error fetching candles for {symbol} {timeframe}: {e}")
        return None


def detect_trend_4h(ohlcv):
    if not ohlcv or len(ohlcv) < 20:
        return "NEUTRAL"
    closes = [c[4] for c in ohlcv]
    # Simple Trend Logic: Compare Current Close with 20-period Average
    avg_price = sum(closes[-20:]) / 20
    if closes[-1] > avg_price:
        return "BULLISH"
    else:
        return "BEARISH"


def check_15m_choch_and_signal(symbol, trend_4h):
    candles_15m = fetch_candles(symbol, "15m", limit=10)
    if not candles_15m or len(candles_15m) < 5:
        return None

    last_candle = candles_15m[-1]
    prev_candle = candles_15m[-2]
    current_price = last_candle[4]

    # Simple CHOCH Trigger Detection Logic
    if trend_4h == "BULLISH":
        # Bullish CHOCH: Previous high broken upward
        if last_candle[4] > prev_candle[2]:  # Close > Prev High
            sl = current_price * 0.992  # 0.8% Stop Loss
            tp1 = current_price * 1.012  # 1.2% Target 1 (1:1.5 RR)
            tp2 = current_price * 1.024  # 2.4% Target 2
            return {
                "symbol": symbol,
                "direction": "BUY",
                "entry": current_price,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "tp1_hit": False,
            }

    elif trend_4h == "BEARISH":
        # Bearish CHOCH: Previous low broken downward
        if last_candle[4] < prev_candle[3]:  # Close < Prev Low
            sl = current_price * 1.008  # 0.8% Stop Loss
            tp1 = current_price * 0.988  # 1.2% Target 1
            tp2 = current_price * 0.976  # 2.4% Target 2
            return {
                "symbol": symbol,
                "direction": "SELL",
                "entry": current_price,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "tp1_hit": False,
            }

    return None


def track_active_trades():
    global active_trades
    for trade in active_trades[:]:
        ticker = exchange.fetch_ticker(trade["symbol"])
        current_price = ticker["last"]

        # BUY Trade Tracking
        if trade["direction"] == "BUY":
            if not trade["tp1_hit"] and current_price >= trade["tp1"]:
                trade["tp1_hit"] = True
                trade["sl"] = trade["entry"]  # Move SL to Break-Even
                msg = (
                    f"🎯 <b>TRADE RESULT: TP1 HIT 🟢</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📍 <b>Asset:</b> {trade['symbol']} (BUY)\n"
                    f"🎯 <b>Entry:</b> {trade['entry']:.2f}\n"
                    f"🚀 <b>TP1 Exit:</b> {trade['tp1']:.2f}\n"
                    f"🛡️ <b>Action:</b> SL Shifted to Entry (Break-Even)\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_telegram_message(msg, signal_type="WIN")

            elif trade["tp1_hit"] and current_price >= trade["tp2"]:
                msg = f"🎉 <b>TRADE RESULT: TP2 HIT (JACKPOT) 🟢</b>\nAsset: {trade['symbol']}"
                send_telegram_message(msg)
                active_trades.remove(trade)

            elif current_price <= trade["sl"]:
                if trade["tp1_hit"]:
                    msg = f"🛡️ <b>TRADE UPDATE: BREAK-EVEN EXIT 🟡</b>\nAsset: {trade['symbol']} Exit at Entry Price."
                    send_telegram_message(msg)
                else:
                    msg = (
                        f"🛑 <b>TRADE RESULT: LOSS 🔴</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📍 <b>Asset:</b> {trade['symbol']} (BUY)\n"
                        f"🎯 <b>Entry:</b> {trade['entry']:.2f}\n"
                        f"🛑 <b>SL Exit:</b> {trade['sl']:.2f}\n"
                        f"━━━━━━━━━━━━━━━━━━"
                    )
                    send_telegram_message(msg, signal_type="LOSS")
                active_trades.remove(trade)

        # SELL Trade Tracking
        elif trade["direction"] == "SELL":
            if not trade["tp1_hit"] and current_price <= trade["tp1"]:
                trade["tp1_hit"] = True
                trade["sl"] = trade["entry"]  # Move SL to Break-Even
                msg = f"🎯 <b>TRADE RESULT: TP1 HIT 🟢</b>\nAsset: {trade['symbol']} (SELL)\nSL shifted to Break-Even!"
                send_telegram_message(msg, signal_type="WIN")

            elif trade["tp1_hit"] and current_price <= trade["tp2"]:
                msg = f"🎉 <b>TRADE RESULT: TP2 HIT 🟢</b>\nAsset: {trade['symbol']}"
                send_telegram_message(msg)
                active_trades.remove(trade)

            elif current_price >= trade["sl"]:
                if trade["tp1_hit"]:
                    msg = f"🛡️ <b>TRADE UPDATE: BREAK-EVEN EXIT 🟡</b>\nAsset: {trade['symbol']}"
                    send_telegram_message(msg)
                else:
                    msg = f"🛑 <b>TRADE RESULT: LOSS 🔴</b>\nAsset: {trade['symbol']}"
                    send_telegram_message(msg, signal_type="LOSS")
                active_trades.remove(trade)


# ==========================================
# 5. BACKGROUND ENGINE LOOP
# ==========================================
def background_trading_scanner():
    print("🚀 Real Multi-Timeframe SMC Scanner Engine Running...")
    send_telegram_message(
        "🤖 <b>SMC AI REAL SCANNER ONLINE</b> 🟢\nMulti-Timeframe Engine Active!"
    )

    while True:
        try:
            bot_stats["last_scan"] = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S IST"
            )

            for sym in SYMBOLS:
                # 1. Fetch Real Prices & 4H Trend
                candles_4h = fetch_candles(sym, "4h", limit=30)
                trend_4h = detect_trend_4h(candles_4h)
                ticker = exchange.fetch_ticker(sym)
                current_price = ticker["last"]

                # Update Live Matrix UI
                market_matrix[sym] = {
                    "price": f"{current_price:,.2f}",
                    "trend_daily": trend_4h,
                    "trend_1h": trend_4h,
                    "structure": "Scanning 15M CHOCH...",
                }

                # 2. Check 15M/5M CHOCH Signals
                signal = check_15m_choch_and_signal(sym, trend_4h)
                if signal:
                    # Check if symbol already has active trade
                    if not any(t["symbol"] == sym for t in active_trades):
                        active_trades.append(signal)
                        alert_msg = (
                            f"⚡ <b>NEW SMC TRADE SIGNAL</b> ({signal['direction']}) 🟢\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📍 <b>Asset:</b> {signal['symbol']}\n"
                            f"⌛ <b>Timeframe:</b> 15M CHOCH Confirmation\n"
                            f"🎯 <b>Entry:</b> {signal['entry']:.2f}\n"
                            f"🛑 <b>Stop Loss:</b> {signal['sl']:.2f}\n"
                            f"🎯 <b>Target 1 (TP1):</b> {signal['tp1']:.2f}\n"
                            f"🎯 <b>Target 2 (TP2):</b> {signal['tp2']:.2f}\n"
                            f"━━━━━━━━━━━━━━━━━━"
                        )
                        send_telegram_message(alert_msg)

            # 3. Track Active Paper Trades against Live Price
            track_active_trades()

            time.sleep(30)  # Scan every 30 seconds

        except Exception as e:
            print(f"Scanner Loop Error: {e}")
            time.sleep(10)


# Start Background Scanner
scanner_thread = threading.Thread(
    target=background_trading_scanner, daemon=True
)
scanner_thread.start()

# ==========================================
# 6. FLASK WEB APP & DASHBOARD
# ==========================================
app = Flask(__name__)

DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="15">
    <title>SMC Real Trading Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background-color: #0d1117; color: #c9d1d9; font-family: sans-serif; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #30363d; padding-bottom: 15px; margin-bottom: 25px; }
        .badge { background: #238636; color: white; padding: 5px 12px; border-radius: 20px; font-size: 14px; font-weight: bold; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 15px; margin-bottom: 30px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 18px; }
        .card h3 { font-size: 12px; color: #8b949e; text-transform: uppercase; margin-bottom: 8px; }
        .card .value { font-size: 22px; font-weight: bold; color: #58a6ff; }
        table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; margin-bottom: 30px; }
        th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #30363d; }
        th { background: #21262d; color: #8b949e; font-size: 13px; text-transform: uppercase; }
        .bull { color: #3fb950; font-weight: bold; }
        .bear { color: #f85149; font-weight: bold; }
        .log-box { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; max-height: 400px; overflow-y: auto; }
        .log-item { padding: 10px; border-bottom: 1px solid #21262d; font-family: monospace; font-size: 13px; white-space: pre-wrap; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>⚡ SMC AI Real Trading Dashboard</h1>
            <div><span class="badge">● REAL SCANNER ACTIVE</span></div>
        </div>

        <div class="grid">
            <div class="card">
                <h3>Bot Status</h3>
                <div class="value" style="color:#3fb950;">Active</div>
            </div>
            <div class="card">
                <h3>Total Trades</h3>
                <div class="value" style="color:#58a6ff;">{{ stats.wins + stats.losses }}</div>
            </div>
            <div class="card">
                <h3>Total Wins</h3>
                <div class="value" style="color:#3fb950;">{{ stats.wins }}</div>
            </div>
            <div class="card">
                <h3>Total Losses</h3>
                <div class="value" style="color:#f85149;">{{ stats.losses }}</div>
            </div>
            <div class="card">
                <h3>Win Rate</h3>
                <div class="value" style="color:#e3b341;">
                    {% if (stats.wins + stats.losses) > 0 %}
                        {{ "%.1f"|format((stats.wins / (stats.wins + stats.losses)) * 100) }}%
                    {% else %}
                        0%
                    {% endif %}
                </div>
            </div>
            <div class="card">
                <h3>Last Scan</h3>
                <div class="value" style="font-size:13px; margin-top:5px; color:#c9d1d9;">{{ stats.last_scan }}</div>
            </div>
        </div>

        <h2>📊 Live Market Matrix (Binance Live Data)</h2>
        <table style="margin-top: 15px;">
            <thead>
                <tr>
                    <th>Symbol</th>
                    <th>Live Price</th>
                    <th>4H Trend</th>
                    <th>15M Status</th>
                </tr>
            </thead>
            <tbody>
                {% for symbol, data in pairs.items() %}
                <tr>
                    <td><strong>{{ symbol }}</strong></td>
                    <td>${{ data.price }}</td>
                    <td class="{{ 'bull' if data.trend_daily == 'BULLISH' else 'bear' }}">{{ data.trend_daily }}</td>
                    <td><span style="background: #21262d; padding: 3px 8px; border-radius: 4px; font-size: 12px;">{{ data.structure }}</span></td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>📱 Telegram Signals & Logs</h2>
        <div class="log-box" style="margin-top: 15px;">
            {% for msg in messages %}
            <div class="log-item">
                <div style="color: #8b949e; font-size:11px;">⏰ {{ msg.timestamp }}</div>
                <div>{{ msg.message | safe }}</div>
            </div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
"""


@app.route("/")
def home_dashboard():
    return render_template_string(
        DASHBOARD_HTML,
        stats=bot_stats,
        pairs=market_matrix,
        messages=recent_telegram_messages,
    )


@app.route("/api/stats")
def api_stats():
    total_trades = bot_stats["wins"] + bot_stats["losses"]
    win_rate = (
        f"{(bot_stats['wins'] / total_trades * 100):.1f}%"
        if total_trades > 0
        else "0%"
    )
    return jsonify({
        "status": bot_stats["status"],
        "last_scan_time": bot_stats["last_scan"],
        "total_trades": total_trades,
        "wins": bot_stats["wins"],
        "losses": bot_stats["losses"],
        "win_rate": win_rate,
        "active_trades_count": len(active_trades),
        "recent_telegram_messages": recent_telegram_messages,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
