import os
import time
import threading
import requests
import pytz
from datetime import datetime
from flask import Flask, jsonify
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
active_trades = {}  # Active trade tracker for Win/Loss calculation

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
    """ Checks active trades against current market high/low for Win/Loss """
    for display_name, trade in list(active_trades.items()):
        ticker = SYMBOLS[display_name]
        m1_candles = fetch_candles(ticker, "1m")
        if not m1_candles:
            continue

        latest_candle = m1_candles[-1]
        high = latest_candle['high']
        low = latest_candle['low']
        close = latest_candle['close']

        direction = trade['direction']
        raw_sl = trade['raw_sl']
        raw_tp1 = trade['raw_tp1']
        raw_tp2 = trade['raw_tp2']

        # BUY Trade Evaluation
        if direction == 'BULLISH':
            if high >= raw_tp2:
                msg = (
                    f"🎯 *TRADE RESULT: WIN 🟢 (TP2 HIT)*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📍 *Asset:* `{display_name}` (BUY)\n"
                    f"🎯 *Entry Price:* `{trade['entry']}`\n"
                    f"🚀 *Exit Price:* `{trade['tp2']}`\n"
                    f"📈 *Reward Ratio:* `1:2.5 RR`\n"
                    f"⏰ *Time:* `{timestamp_str}`\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_telegram_alert(msg)
                del active_trades[display_name]

            elif high >= raw_tp1 and not trade.get('tp1_hit'):
                trade['tp1_hit'] = True
                msg = (
                    f"🎯 *TRADE RESULT: WIN 🟢 (TP1 HIT)*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📍 *Asset:* `{display_name}` (BUY)\n"
                    f"🎯 *Entry Price:* `{trade['entry']}`\n"
                    f"🚀 *Exit Price:* `{trade['tp1']}`\n"
                    f"📈 *Reward Ratio:* `1:1.5 RR`\n"
                    f"⏰ *Time:* `{timestamp_str}`\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_telegram_alert(msg)

            elif low <= raw_sl:
                msg = (
                    f"🛑 *TRADE RESULT: LOSS 🔴 (SL HIT)*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📍 *Asset:* `{display_name}` (BUY)\n"
                    f"🎯 *Entry Price:* `{trade['entry']}`\n"
                    f"🛑 *Exit Price:* `{trade['sl']}`\n"
                    f"⏰ *Time:* `{timestamp_str}`\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_telegram_alert(msg)
                del active_trades[display_name]

        # SELL Trade Evaluation
        elif direction == 'BEARISH':
            if low <= raw_tp2:
                msg = (
                    f"🎯 *TRADE RESULT: WIN 🟢 (TP2 HIT)*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📍 *Asset:* `{display_name}` (SELL)\n"
                    f"🎯 *Entry Price:* `{trade['entry']}`\n"
                    f"🚀 *Exit Price:* `{trade['tp2']}`\n"
                    f"📈 *Reward Ratio:* `1:2.5 RR`\n"
                    f"⏰ *Time:* `{timestamp_str}`\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_telegram_alert(msg)
                del active_trades[display_name]

            elif low <= raw_tp1 and not trade.get('tp1_hit'):
                trade['tp1_hit'] = True
                msg = (
                    f"🎯 *TRADE RESULT: WIN 🟢 (TP1 HIT)*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📍 *Asset:* `{display_name}` (SELL)\n"
                    f"🎯 *Entry Price:* `{trade['entry']}`\n"
                    f"🚀 *Exit Price:* `{trade['tp1']}`\n"
                    f"📈 *Reward Ratio:* `1:1.5 RR`\n"
                    f"⏰ *Time:* `{timestamp_str}`\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_telegram_alert(msg)

            elif high >= raw_sl:
                msg = (
                    f"🛑 *TRADE RESULT: LOSS 🔴 (SL HIT)*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"📍 *Asset:* `{display_name}` (SELL)\n"
                    f"🎯 *Entry Price:* `{trade['entry']}`\n"
                    f"🛑 *Exit Price:* `{trade['sl']}`\n"
                    f"⏰ *Time:* `{timestamp_str}`\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                send_telegram_alert(msg)
                del active_trades[display_name]

# ---------------------------------------------------------
# 5. SCANNER LOOP & DISPATCHER
# ---------------------------------------------------------
def run_scanner_job():
    print("[SYSTEM] SMC Rulebook Multi-Timeframe Scanner Started...")
    while True:
        try:
            timestamp_str = datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S IST')
            
            # First, track active trades for Win/Loss
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

                if choch['confirmed']:
                    signal_key = f"{display_name}_{mtf['condition']}_{choch['new_trend']}_{confirm_candles[-1]['time']}"
                    
                    if signal_key not in sent_signals:
                        sent_signals.add(signal_key)
                        
                        direction = "BUY 🟢" if choch['new_trend'] == 'BULLISH' else "SELL 🔴"
                        current_price = confirm_candles[-1]['close']
                        
                        levels = calculate_trade_levels(
                            choch['new_trend'], 
                            current_price, 
                            h1_info['lsm'], 
                            h1_info['lrm'], 
                            ticker
                        )
                        
                        # Save trade to Active Trades memory for Win/Loss tracking
                        active_trades[display_name] = {
                            "direction": choch['new_trend'],
                            "raw_entry": levels['raw_entry'],
                            "raw_sl": levels['raw_sl'],
                            "raw_tp1": levels['raw_tp1'],
                            "raw_tp2": levels['raw_tp2'],
                            "entry": levels['entry'],
                            "sl": levels['sl'],
                            "tp1": levels['tp1'],
                            "tp2": levels['tp2'],
                            "tp1_hit": False
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
# 6. FLASK SERVER ROUTES
# ---------------------------------------------------------
@app.route("/")
def home():
    return "SMC Multi-Asset Engine with Win/Loss Tracker Active!"

@app.route("/api/stats")
def api_stats():
    return jsonify({
        "status": "online",
        "active_trades_monitored": len(active_trades),
        "total_signals_sent": len(sent_signals),
        "server_time": datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S IST')
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
