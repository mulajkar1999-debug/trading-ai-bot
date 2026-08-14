import os
import time
import threading
from flask import Flask, jsonify, render_template_string
import requests
import ccxt

app = Flask(__name__)

# ==========================================
# 🔑 CONFIGURATION (TELEGRAM CREDENTIALS)
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1317739622")

# ==========================================
# 📈 EXCHANGE SETUP (BYBIT FOR NO REGION BLOCK)
# ==========================================
# ccxt.bybit US/Render servers par 451 Location Restricted Error nahi deta.
exchange = ccxt.bybit({
    'enableRateLimit': True,
})

# Scanning Assets & Global Memory
SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
active_trades = []
trade_logs = []

# ==========================================
# 📲 TELEGRAM NOTIFIER FUNCTION
# ==========================================
def send_telegram(message):
    """Telegram par text alert bhejne ke liye function"""
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or TELEGRAM_CHAT_ID == "YOUR_CHAT_ID_HERE":
        print(f"[Telegram Skip] Token/Chat ID set nahi hai. Message: {message}")
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Notification Error: {e}")

# ==========================================
# 🔍 SMC STRATEGY LOGIC & SCANNER
# ==========================================
def fetch_candles(symbol, timeframe, limit=30):
    """Bybit se Candlestick Data Fetch Karne Ka Function"""
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return ohlcv
    except Exception as e:
        print(f"Error fetching candles for {symbol} ({timeframe}): {e}")
        return None

def get_4h_trend(symbol):
    """4-Hour Trend Direction Check Logic"""
    candles = fetch_candles(symbol, timeframe='4h', limit=20)
    if not candles or len(candles) < 20:
        return 'NEUTRAL'
    
    closes = [c[4] for c in candles]
    avg_price = sum(closes) / len(closes)
    current_price = closes[-1]
    
    if current_price > avg_price:
        return 'BULLISH'
    elif current_price < avg_price:
        return 'BEARISH'
    return 'NEUTRAL'

def check_15m_choch(symbol, trend_4h):
    """15-Minute Change of Character (CHOCH) Confirmation Logic"""
    candles = fetch_candles(symbol, timeframe='15m', limit=5)
    if not candles or len(candles) < 3:
        return None
    
    last_candle = candles[-2]  # Recently closed candle
    prev_candle = candles[-3]
    
    # Bullish CHOCH (BUY)
    if trend_4h == 'BULLISH' and last_candle[4] > prev_candle[2]: # Close > Prev High
        return 'BUY'
    # Bearish CHOCH (SELL)
    elif trend_4h == 'BEARISH' and last_candle[4] < prev_candle[3]: # Close < Prev Low
        return 'SELL'
        
    return None

def background_trading_scanner():
    """Continuous Background Scanner Thread (Runs every 30s)"""
    global active_trades, trade_logs
    print("🚀 SMC Scanner Engine Started with Bybit Feed...")
    
    while True:
        try:
            for symbol in SYMBOLS:
                # 1. Active Trade Management & Outcome Tracking
                ticker = exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                for trade in active_trades[:]:
                    if trade['symbol'] == symbol:
                        # Check BUY Trade Targets
                        if trade['type'] == 'BUY':
                            # Check TP2 (Full Win)
                            if current_price >= trade['tp2']:
                                msg = f"🎉 *TRADE RESULT: TP2 HIT (JACKPOT) 🟢*\n━━━━━━━━━━━━━━━━━━\n📍 *Asset:* {symbol}\n🚀 *Exit Price:* {current_price:.2f}"
                                send_telegram(msg)
                                trade_logs.append(f"[WIN TP2] {symbol} @ {current_price:.2f}")
                                active_trades.remove(trade)
                            # Check TP1 (Auto Break-Even)
                            elif current_price >= trade['tp1'] and not trade.get('tp1_hit'):
                                trade['tp1_hit'] = True
                                trade['sl'] = trade['entry'] # Shift Stoploss to Entry
                                msg = f"🎯 *TRADE RESULT: TP1 HIT 🟢*\n━━━━━━━━━━━━━━━━━━\n📍 *Asset:* {symbol}\n🎯 *Exit:* {current_price:.2f}\n🛡️ *Action:* SL Shifted to Entry (Break-Even)"
                                send_telegram(msg)
                                trade_logs.append(f"[TP1 HIT] {symbol} - SL Trailed to Break-Even")
                            # Check SL
                            elif current_price <= trade['sl']:
                                msg = f"🛑 *TRADE RESULT: {'BREAK-EVEN EXIT' if trade.get('tp1_hit') else 'SL HIT'} 🔴*\n━━━━━━━━━━━━━━━━━━\n📍 *Asset:* {symbol}\n🛑 *Exit Price:* {current_price:.2f}"
                                send_telegram(msg)
                                trade_logs.append(f"[{'BREAK-EVEN' if trade.get('tp1_hit') else 'LOSS'}] {symbol} @ {current_price:.2f}")
                                active_trades.remove(trade)

                        # Check SELL Trade Targets
                        elif trade['type'] == 'SELL':
                            if current_price <= trade['tp2']:
                                msg = f"🎉 *TRADE RESULT: TP2 HIT (JACKPOT) 🟢*\n━━━━━━━━━━━━━━━━━━\n📍 *Asset:* {symbol}\n🚀 *Exit Price:* {current_price:.2f}"
                                send_telegram(msg)
                                trade_logs.append(f"[WIN TP2] {symbol} @ {current_price:.2f}")
                                active_trades.remove(trade)
                            elif current_price <= trade['tp1'] and not trade.get('tp1_hit'):
                                trade['tp1_hit'] = True
                                trade['sl'] = trade['entry']
                                msg = f"🎯 *TRADE RESULT: TP1 HIT 🟢*\n━━━━━━━━━━━━━━━━━━\n📍 *Asset:* {symbol}\n🎯 *Exit:* {current_price:.2f}\n🛡️ *Action:* SL Shifted to Entry (Break-Even)"
                                send_telegram(msg)
                                trade_logs.append(f"[TP1 HIT] {symbol} - SL Trailed to Break-Even")
                            elif current_price >= trade['sl']:
                                msg = f"🛑 *TRADE RESULT: {'BREAK-EVEN EXIT' if trade.get('tp1_hit') else 'SL HIT'} 🔴*\n━━━━━━━━━━━━━━━━━━\n📍 *Asset:* {symbol}\n🛑 *Exit Price:* {current_price:.2f}"
                                send_telegram(msg)
                                trade_logs.append(f"[{'BREAK-EVEN' if trade.get('tp1_hit') else 'LOSS'}] {symbol} @ {current_price:.2f}")
                                active_trades.remove(trade)

                # 2. Check New Trade Signals (If no open trade for this symbol)
                has_active = any(t['symbol'] == symbol for t in active_trades)
                if not has_active:
                    trend_4h = get_4h_trend(symbol)
                    signal = check_15m_choch(symbol, trend_4h)
                    
                    if signal:
                        entry = current_price
                        if signal == 'BUY':
                            sl = entry * 0.992    # 0.8% SL
                            tp1 = entry * 1.012   # 1:1.5 RR
                            tp2 = entry * 1.024   # 1:3 RR
                        else: # SELL
                            sl = entry * 1.008
                            tp1 = entry * 0.988
                            tp2 = entry * 0.976

                        # Save to memory
                        new_trade = {
                            'symbol': symbol,
                            'type': signal,
                            'entry': entry,
                            'sl': sl,
                            'tp1': tp1,
                            'tp2': tp2,
                            'tp1_hit': False
                        }
                        active_trades.append(new_trade)

                        # Telegram Notification
                        alert_msg = (
                            f"⚡ *NEW SMC TRADE SIGNAL ({signal}) {'🟢' if signal == 'BUY' else '🔴'}*\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📍 *Asset:* {symbol}\n"
                            f"⌛ *Timeframe:* 15M CHOCH Confirmation\n"
                            f"🎯 *Entry:* {entry:.2f}\n"
                            f"🛑 *Stop Loss:* {sl:.2f}\n"
                            f"🎯 *Target 1 (TP1):* {tp1:.2f}\n"
                            f"🎯 *Target 2 (TP2):* {tp2:.2f}\n"
                            f"━━━━━━━━━━━━━━━━━━"
                        )
                        send_telegram(alert_msg)
                        trade_logs.append(f"[NEW SIGNAL] {signal} {symbol} Entry: {entry:.2f}")

        except Exception as e:
            print(f"Scanner Loop Error: {e}")

        time.sleep(30) # Scan loop runs every 30 seconds

# Start Background Scanner in a Thread
scanner_thread = threading.Thread(target=background_trading_scanner, daemon=True)
scanner_thread.start()

# ==========================================
# 🌐 FLASK WEB DASHBOARD (BROWSER UI)
# ==========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>SMC AI Trading Bot Dashboard</title>
    <meta http-equiv="refresh" content="15">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0f172a; color: #f8fafc; margin: 0; padding: 20px; }
        .container { max-width: 1000px; margin: auto; }
        .header { text-align: center; border-bottom: 2px solid #334155; padding-bottom: 15px; margin-bottom: 25px; }
        .card-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-bottom: 25px; }
        .card { background: #1e293b; padding: 15px; border-radius: 10px; border: 1px solid #334155; text-align: center; }
        .card h3 { margin: 0; color: #94a3b8; font-size: 14px; }
        .card p { margin: 10px 0 0 0; font-size: 22px; font-weight: bold; color: #38bdf8; }
        table { width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 10px; overflow: hidden; margin-bottom: 25px; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #334155; }
        th { background: #0f172a; color: #94a3b8; }
        .logs-box { background: #1e293b; padding: 15px; border-radius: 10px; border: 1px solid #334155; height: 200px; overflow-y: auto; font-family: monospace; color: #a7f3d0; }
        .badge { padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }
        .badge-buy { background: #166534; color: #4ade80; }
        .badge-sell { background: #991b1b; color: #fca5a5; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 SMC AI Trading Bot Dashboard</h1>
            <p>Live Multi-Timeframe Scanner & Rule-Book Execution Engine</p>
        </div>

        <div class="card-grid">
            <div class="card">
                <h3>Bot Status</h3>
                <p style="color: #4ade80;">🟢 ONLINE (Bybit)</p>
            </div>
            <div class="card">
                <h3>Active Trades</h3>
                <p>{{ active_trades|length }}</p>
            </div>
            <div class="card">
                <h3>Total Logged Events</h3>
                <p>{{ trade_logs|length }}</p>
            </div>
        </div>

        <h2>📊 Active Positions</h2>
        <table>
            <thead>
                <tr>
                    <th>Symbol</th>
                    <th>Type</th>
                    <th>Entry Price</th>
                    <th>Stop Loss (SL)</th>
                    <th>Target 1 (TP1)</th>
                    <th>Target 2 (TP2)</th>
                </tr>
            </thead>
            <tbody>
                {% for trade in active_trades %}
                <tr>
                    <td><b>{{ trade.symbol }}</b></td>
                    <td><span class="badge {{ 'badge-buy' if trade.type == 'BUY' else 'badge-sell' }}">{{ trade.type }}</span></td>
                    <td>{{ "%.2f"|format(trade.entry) }}</td>
                    <td>{{ "%.2f"|format(trade.sl) }}</td>
                    <td>{{ "%.2f"|format(trade.tp1) }}</td>
                    <td>{{ "%.2f"|format(trade.tp2) }}</td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="6" style="text-align: center; color: #94a3b8;">No Active Trade Signals Right Now. Scanning 15M CHOCH...</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>📱 Telegram Signals & Activity Logs</h2>
        <div class="logs-box">
            {% for log in trade_logs|reverse %}
                <div>> {{ log }}</div>
            {% else %}
                <div>> System Started. Waiting for live signals...</div>
            {% endfor %}
        </div>
    </div>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE, active_trades=active_trades, trade_logs=trade_logs)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
