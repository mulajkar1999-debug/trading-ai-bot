import os
import time
import threading
import requests
import pytz
from datetime import datetime
from flask import Flask, jsonify, render_template_string
import yfinance as yf

app = Flask(__name__)

# =========================================================
# ⚙️ CONFIGURATION & TELEGRAM CREDENTIALS
# =========================================================
TELEGRAM_BOT_TOKEN = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"  # <-- Replace with your Bot Token
TELEGRAM_CHAT_ID = "123456789"                           # <-- Replace with your Chat ID

SYMBOLS = {
    "BTCUSD": "BTC-USD",
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "USDCHF": "CHF=X",
    "XAUUSD": "GC=F"
}

ist = pytz.timezone('Asia/Kolkata')
sent_signals = set()
active_trades = {}
market_status_cache = {}  # Web Dashboard ke liye cache memory

# ---------------------------------------------------------
# 1. HELPER & DATA FETCHING
# ---------------------------------------------------------
def format_price(price, ticker):
    if "JPY" in ticker:
        return f"{price:.3f}"
    elif any(f in ticker for f in ["EUR", "GBP", "CHF"]):
        return f"{price:.5f}"
    else:
        return f"{price:.2f}"

def fetch_candles(ticker_symbol, timeframe="1h"):
    try:
        period = "7d"
        if timeframe in ['1m', '5m', '15m']:
            period = "1d"
        elif timeframe == '1d':
            period = "60d"

        df = yf.download(tickers=ticker_symbol, period=period, interval=timeframe, progress=False)
        if df.empty or len(df) < 5:
            return []

        candles = []
        for index, row in df.iterrows():
            candles.append({
                'time': index,
                'open': float(row['Open'].iloc[0] if hasattr(row['Open'], 'iloc') else row['Open']),
                'high': float(row['High'].iloc[0] if hasattr(row['High'], 'iloc') else row['High']),
                'low': float(row['Low'].iloc[0] if hasattr(row['Low'], 'iloc') else row['Low']),
                'close': float(row['Close'].iloc[0] if hasattr(row['Close'], 'iloc') else row['Close']),
            })
        return candles
    except Exception as e:
        return []

# ---------------------------------------------------------
# 2. TRADE LEVEL CALCULATION & CHOCH LOGIC
# ---------------------------------------------------------
def calculate_trade_levels(direction, current_price, lsm, lrm, ticker):
    if direction == 'BULLISH':
        entry = current_price
        sl = lsm * 0.9995
        risk = entry - sl
        tp1 = entry + (risk * 1.5)
        tp2 = entry + (risk * 2.5)
    else:
        entry = current_price
        sl = lrm * 1.0005
        risk = sl - entry
        tp1 = entry - (risk * 1.5)
        tp2 = entry - (risk * 2.5)

    return {
        "raw_entry": entry,
        "raw_sl": sl,
        "raw_tp1": tp1,
        "raw_tp2": tp2,
        "entry": format_price(entry, ticker),
        "sl": format_price(sl, ticker),
        "tp1": format_price(tp1, ticker),
        "tp2": format_price(tp2, ticker)
    }

def check_choch(candles, ref_level, current_trend):
    if len(candles) < 2:
        return {'confirmed': False, 'new_trend': current_trend}

    c1, c2 = candles[-2], candles[-1]

    if current_trend == 'BULLISH':
        if c1['close'] < ref_level and c2['close'] < ref_level:
            return {'confirmed': True, 'new_trend': 'BEARISH'}

    elif current_trend == 'BEARISH':
        if c1['close'] > ref_level and c2['close'] > ref_level:
            return {'confirmed': True, 'new_trend': 'BULLISH'}

    return {'confirmed': False, 'new_trend': current_trend}

def determine_trend_and_levels(candles):
    if len(candles) < 5:
        return {'trend': 'NEUTRAL', 'lsm': None, 'lrm': None}

    highs = [c['high'] for c in candles[-10:]]
    lows = [c['low'] for c in candles[-10:]]

    lrm = max(highs)
    lsm = min(lows)

    last_close = candles[-1]['close']
    mid = (lrm + lsm) / 2
    trend = 'BULLISH' if last_close > mid else 'BEARISH'

    return {'trend': trend, 'lsm': lsm, 'lrm': lrm}

def evaluate_mtf_matrix(daily_trend, h1_trend):
    if daily_trend == h1_trend:
        return {'condition': 'C1', 'confirm_tf': '5m', 'desc': 'Trend Aligned'}
    else:
        return {'condition': 'C2/C3', 'confirm_tf': '15m', 'desc': 'Counter Trend Setup'}

# ---------------------------------------------------------
# 3. TELEGRAM ALERT SENDER
# ---------------------------------------------------------
def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ":
        print("[WARNING] Valid Telegram Token missing!")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[ERROR] Alert error: {e}")

# ---------------------------------------------------------
# 4. WIN / LOSS TRACKER ENGINE
# ---------------------------------------------------------
def track_active_trades(timestamp_str):
    for display_name, trade in list(active_trades.items()):
        ticker = SYMBOLS[display_name]
        m1_candles = fetch_candles(ticker, "1m")
        if not m1_candles:
            continue

        latest_candle = m1_candles[-1]
        high = latest_candle['high']
        low = latest_candle['low']

        direction = trade['direction']
        raw_sl = trade['raw_sl']
        raw_tp1 = trade['raw_tp1']
        raw_tp2 = trade['raw_tp2']

        if direction == 'BULLISH':
            if high >= raw_tp2:
                msg = f"🎯 *TRADE RESULT: WIN 🟢 (TP2 HIT)*\n📍 `{display_name}` (BUY)\n🎯 Entry: `{trade['entry']}` | 🚀 Exit: `{trade['tp2']}`"
                send_telegram_alert(msg)
                del active_trades[display_name]
            elif high >= raw_tp1 and not trade.get('tp1_hit'):
                trade['tp1_hit'] = True
                msg = f"🎯 *TRADE RESULT: WIN 🟢 (TP1 HIT)*\n📍 `{display_name}` (BUY)\n🎯 Entry: `{trade['entry']}` | 🚀 Exit: `{trade['tp1']}`"
                send_telegram_alert(msg)
            elif low <= raw_sl:
                msg = f"🛑 *TRADE RESULT: LOSS 🔴 (SL HIT)*\n📍 `{display_name}` (BUY)\n🎯 Entry: `{trade['entry']}` | 🛑 Exit: `{trade['sl']}`"
                send_telegram_alert(msg)
                del active_trades[display_name]

        elif direction == 'BEARISH':
            if low <= raw_tp2:
                msg = f"🎯 *TRADE RESULT: WIN 🟢 (TP2 HIT)*\n📍 `{display_name}` (SELL)\n🎯 Entry: `{trade['entry']}` | 🚀 Exit: `{trade['tp2']}`"
                send_telegram_alert(msg)
                del active_trades[display_name]
            elif low <= raw_tp1 and not trade.get('tp1_hit'):
                trade['tp1_hit'] = True
                msg = f"🎯 *TRADE RESULT: WIN 🟢 (TP1 HIT)*\n📍 `{display_name}` (SELL)\n🎯 Entry: `{trade['entry']}` | 🚀 Exit: `{trade['tp1']}`"
                send_telegram_alert(msg)
            elif high >= raw_sl:
                msg = f"🛑 *TRADE RESULT: LOSS 🔴 (SL HIT)*\n📍 `{display_name}` (SELL)\n🎯 Entry: `{trade['entry']}` | 🛑 Exit: `{trade['sl']}`"
                send_telegram_alert(msg)
                del active_trades[display_name]

# ---------------------------------------------------------
# 5. SCANNER LOOP ENGINE
# ---------------------------------------------------------
def run_scanner_job():
    print("[SYSTEM] SMC Rulebook Multi-Timeframe Scanner Started...")
    while True:
        try:
            timestamp_str = datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S IST')
            track_active_trades(timestamp_str)

            for display_name, ticker in SYMBOLS.items():
                daily_candles = fetch_candles(ticker, "1d")
                h1_candles = fetch_candles(ticker, "1h")
                
                if not (daily_candles and h1_candles):
                    continue

                daily_info = determine_trend_and_levels(daily_candles)
                h1_info = determine_trend_and_levels(h1_candles)
                mtf = evaluate_mtf_matrix(daily_info['trend'], h1_info['trend'])
                
                confirm_candles = fetch_candles(ticker, mtf['confirm_tf'])
                if not confirm_candles:
                    continue

                ref_level = h1_info['lsm'] if h1_info['trend'] == 'BULLISH' else h1_info['lrm']
                choch = check_choch(confirm_candles, ref_level, h1_info['trend'])

                # Cache data for Web UI
                market_status_cache[display_name] = {
                    "daily": daily_info['trend'],
                    "h1": h1_info['trend'],
                    "condition": mtf['condition'],
                    "price": format_price(confirm_candles[-1]['close'], ticker),
                    "last_update": timestamp_str
                }

                if choch['confirmed']:
                    signal_key = f"{display_name}_{mtf['condition']}_{choch['new_trend']}_{confirm_candles[-1]['time']}"
                    
                    if signal_key not in sent_signals:
                        sent_signals.add(signal_key)
                        direction = "BUY 🟢" if choch['new_trend'] == 'BULLISH' else "SELL 🔴"
                        current_price = confirm_candles[-1]['close']
                        
                        levels = calculate_trade_levels(choch['new_trend'], current_price, h1_info['lsm'], h1_info['lrm'], ticker)
                        
                        active_trades[display_name] = {
                            "direction": choch['new_trend'],
                            "raw_entry": levels['raw_entry'], "raw_sl": levels['raw_sl'],
                            "raw_tp1": levels['raw_tp1'], "raw_tp2": levels['raw_tp2'],
                            "entry": levels['entry'], "sl": levels['sl'],
                            "tp1": levels['tp1'], "tp2": levels['tp2'], "tp1_hit": False
                        }

                        alert_msg = (
                            f"🚀 *SMC RULEBOOK ALERT: {direction}*\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📍 *Asset:* `{display_name}`\n"
                            f"📊 *MTF Condition:* `{mtf['condition']}` ({mtf['desc']})\n\n"
                            f"🎯 *Entry Zone:* `{levels['entry']}`\n"
                            f"🛑 *Stop Loss (SL):* `{levels['sl']}`\n"
                            f"📈 *Take Profit 1 (1:1.5):* `{levels['tp1']}`\n"
                            f"🚀 *Take Profit 2 (1:2.5):* `{levels['tp2']}`\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"⏱️ *TF:* `{mtf['confirm_tf'].upper()}` | ⏰ *Time:* `{timestamp_str}`"
                        )
                        send_telegram_alert(alert_msg)

        except Exception as e:
            print(f"[ERROR] Scanner loop error: {e}")

        time.sleep(60)

scanner_thread = threading.Thread(target=run_scanner_job, daemon=True)
scanner_thread.start()

# ---------------------------------------------------------
# 6. HTML WEB DASHBOARD TEMPLATE
# ---------------------------------------------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="10">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SMC AI Bot Terminal</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background-color: #0b0e14; color: #e1e1e6; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .card-custom { background: #151922; border: 1px solid #232936; border-radius: 12px; }
        .badge-bull { background-color: #00c853; color: #000; font-weight: bold; }
        .badge-bear { background-color: #ff3d00; color: #fff; font-weight: bold; }
        .status-dot { height: 12px; width: 12px; background-color: #00e676; border-radius: 50%; display: inline-block; box-shadow: 0 0 8px #00e676; }
        table { color: #e1e1e6 !important; }
    </style>
</head>
<body class="py-4">
    <div class="container">
        <!-- Header -->
        <div class="d-flex justify-content-between align-items-center mb-4 p-3 card-custom">
            <div>
                <h3 class="m-0 text-warning">🤖 SMC Rulebook Terminal v2.0</h3>
                <small class="text-muted">Auto Refreshes Every 10 Seconds | IST Time: {{ server_time }}</small>
            </div>
            <div>
                <span class="status-dot me-1"></span>
                <span class="fw-bold text-success">ENGINE ACTIVE</span>
            </div>
        </div>

        <!-- Metric Cards -->
        <div class="row g-3 mb-4">
            <div class="col-md-4">
                <div class="card-custom p-3 text-center">
                    <h6 class="text-muted">Total Signals Sent</h6>
                    <h2 class="text-info m-0">{{ total_signals }}</h2>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card-custom p-3 text-center">
                    <h6 class="text-muted">Active Trades Tracked</h6>
                    <h2 class="text-warning m-0">{{ active_count }}</h2>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card-custom p-3 text-center">
                    <h6 class="text-muted">Total Pairs Scanned</h6>
                    <h2 class="text-success m-0">{{ symbols_count }}</h2>
                </div>
            </div>
        </div>

        <!-- Market Structure Matrix Table -->
        <div class="card-custom p-3 mb-4">
            <h5 class="mb-3 text-light">📊 Live Market Structure Matrix</h5>
            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle m-0">
                    <thead>
                        <tr class="text-muted">
                            <th>Asset</th>
                            <th>Current Price</th>
                            <th>Daily Trend</th>
                            <th>1-Hour Trend</th>
                            <th>Setup Matrix</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for symbol, data in market_status.items() %}
                        <tr>
                            <td class="fw-bold text-warning">{{ symbol }}</td>
                            <td>${{ data.price }}</td>
                            <td>
                                <span class="badge {{ 'badge-bull' if data.daily == 'BULLISH' else 'badge-bear' }}">{{ data.daily }}</span>
                            </td>
                            <td>
                                <span class="badge {{ 'badge-bull' if data.h1 == 'BULLISH' else 'badge-bear' }}">{{ data.h1 }}</span>
                            </td>
                            <td><span class="badge bg-secondary">{{ data.condition }}</span></td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</body>
</html>
"""

# ---------------------------------------------------------
# 7. FLASK SERVER ROUTES
# ---------------------------------------------------------
@app.route("/")
def home():
    timestamp_str = datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S IST')
    return render_template_string(
        HTML_TEMPLATE,
        server_time=timestamp_str,
        total_signals=len(sent_signals),
        active_count=len(active_trades),
        symbols_count=len(SYMBOLS),
        market_status=market_status_cache
    )

@app.route("/api/stats")
def api_stats():
    return jsonify({
        "status": "active",
        "system": "SMC Rulebook Engine v2.0",
        "active_trades_monitored": len(active_trades),
        "total_signals_sent": len(sent_signals),
        "server_time": datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S IST')
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
