import os
import requests
import pytz
from datetime import datetime
from flask import Flask, jsonify

app = Flask(__name__)

# =========================================================
# ⚙️ CONFIGURATION & TELEGRAM CREDENTIALS
# =========================================================
# Yahan apna REAL Telegram Bot Token aur Chat ID daalein:
TELEGRAM_BOT_TOKEN = "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"  # <-- Replace with your Bot Token
TELEGRAM_CHAT_ID = "123456789"                           # <-- Replace with your Chat ID

# Trading Symbol (Coinbase)
SYMBOL = "BTC-USD"

ist = pytz.timezone('Asia/Kolkata')

# ---------------------------------------------------------
# 1. CANDLE DATA FETCHING (COINBASE API)
# ---------------------------------------------------------
def fetch_coinbase_candles(symbol, granularity=3600):
    """
    Coinbase se candles fetch karta hai.
    Granularity (Seconds): 60 (1M), 300 (5M), 3600 (1H), 14400 (4H), 86400 (1D)
    """
    url = f"https://api.exchange.coinbase.com/products/{symbol}/candles?granularity={granularity}"
    headers = {"Accept": "application/json", "User-Agent": "SMC-Bot/1.0"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            candles = []
            for item in reversed(data):
                candles.append({
                    'time': item[0],
                    'low': float(item[1]),
                    'high': float(item[2]),
                    'open': float(item[3]),
                    'close': float(item[4])
                })
            return candles
    except Exception as e:
        print(f"Error fetching candles ({granularity}s): {e}")
    return []

# ---------------------------------------------------------
# 2. SMC CORE LOGIC (2CR, CHOCH, MTF)
# ---------------------------------------------------------
def check_2cr_retracement(candles, trend):
    """
    Two-Candle Retracement (2CR) Logic
    - Bullish: 2 consecutive red candles (2nd close < 1st low)
    - Bearish: 2 consecutive green candles (2nd close > 1st high)
    """
    if len(candles) < 2:
        return False

    c1, c2 = candles[-2], candles[-1]

    if trend == 'BULLISH':
        if c1['close'] < c1['open'] and c2['close'] < c2['open']:
            if c2['close'] < c1['low']:
                return True
    elif trend == 'BEARISH':
        if c1['close'] > c1['open'] and c2['close'] > c2['open']:
            if c2['close'] > c1['high']:
                return True
    return False

def check_choch(candles, ref_level, current_trend):
    """
    Change of Character (CHOCH) & Fakeout Filter
    2 consecutive candle closes beyond LSM/LRM is required.
    """
    if len(candles) < 2:
        return {'confirmed': False, 'fakeout': False, 'new_trend': current_trend}

    c1, c2 = candles[-2], candles[-1]

    if current_trend == 'BULLISH':  # Check Bearish CHOCH
        c1_below = c1['close'] < ref_level
        c2_below = c2['close'] < ref_level

        if c1_below and c2_below:
            return {'confirmed': True, 'fakeout': False, 'new_trend': 'BEARISH'}
        elif c1_below and not c2_below:
            return {'confirmed': False, 'fakeout': True, 'new_trend': 'BULLISH'}

    elif current_trend == 'BEARISH':  # Check Bullish CHOCH
        c1_above = c1['close'] > ref_level
        c2_above = c2['close'] > ref_level

        if c1_above and c2_above:
            return {'confirmed': True, 'fakeout': False, 'new_trend': 'BULLISH'}
        elif c1_above and not c2_above:
            return {'confirmed': False, 'fakeout': True, 'new_trend': 'BEARISH'}

    return {'confirmed': False, 'fakeout': False, 'new_trend': current_trend}

def determine_structure_and_trend(candles):
    """
    Calculates LSM, LRM, and Trend for a given timeframe.
    """
    if len(candles) < 10:
        return {'trend': 'NEUTRAL', 'lsm': None, 'lrm': None}

    highs = [c['high'] for c in candles[-10:]]
    lows = [c['low'] for c in candles[-10:]]

    lrm = max(highs)  # Last Resistance in Market
    lsm = min(lows)   # Last Support in Market

    last_close = candles[-1]['close']
    mid = (lrm + lsm) / 2

    trend = 'BULLISH' if last_close > mid else 'BEARISH'

    return {'trend': trend, 'lsm': lsm, 'lrm': lrm}

def evaluate_mtf_condition(daily_trend, h4_trend, h1_trend):
    """
    Multi-Timeframe Decision Matrix (C1, C2, C3)
    """
    if daily_trend == h4_trend == h1_trend:
        return {
            'condition': 'C1',
            'trade_tf': '1H',
            'confirm_tf': '1M',
            'description': 'All Timeframes Aligned'
        }
    elif daily_trend == h4_trend and h1_trend != daily_trend:
        return {
            'condition': 'C2',
            'trade_tf': '4H',
            'confirm_tf': '5M',
            'description': '1H Counter-Trend to Daily & 4H'
        }
    elif h4_trend == h1_trend and h4_trend != daily_trend:
        return {
            'condition': 'C3',
            'trade_tf': 'Daily',
            'confirm_tf': '1H',
            'description': '4H & 1H Counter-Trend to Daily'
        }
    return {
        'condition': 'MIXED',
        'trade_tf': None,
        'confirm_tf': None,
        'description': 'Mixed Market Structure'
    }

# ---------------------------------------------------------
# 3. TELEGRAM ALERT SENDER
# ---------------------------------------------------------
def send_telegram_alert(message):
    """
    Telegram par Alert Message bhejta hai.
    """
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ":
        print("[WARNING] Valid Telegram Bot Token missing! Skipping alert.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 200:
            print("[INFO] Telegram alert successfully sent.")
        else:
            print(f"[ERROR] Telegram API error: {res.status_code} - {res.text}")
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram alert: {e}")

# ---------------------------------------------------------
# 4. MAIN ENGINE & ANALYSIS ROUTINE
# ---------------------------------------------------------
def analyze_asset():
    """
    Full Analysis Execution Loop
    """
    daily_candles = fetch_coinbase_candles(SYMBOL, 86400)
    h4_candles = fetch_coinbase_candles(SYMBOL, 14400)
    h1_candles = fetch_coinbase_candles(SYMBOL, 3600)
    m1_candles = fetch_coinbase_candles(SYMBOL, 60)

    if not (daily_candles and h4_candles and h1_candles):
        return {"status": "error", "message": "Failed to fetch market candles"}

    # Timeframe Structure Analysis
    daily_struct = determine_structure_and_trend(daily_candles)
    h4_struct = determine_structure_and_trend(h4_candles)
    h1_struct = determine_structure_and_trend(h1_candles)

    # MTF Condition Evaluation
    mtf = evaluate_mtf_condition(
        daily_struct['trend'],
        h4_struct['trend'],
        h1_struct['trend']
    )

    # CHOCH Check on LTF Confirmation Candle
    choch_status = {'confirmed': False, 'fakeout': False}
    if mtf['condition'] == 'C1' and m1_candles:
        ref_level = h1_struct['lsm'] if h1_struct['trend'] == 'BULLISH' else h1_struct['lrm']
        choch_status = check_choch(m1_candles, ref_level, h1_struct['trend'])

    timestamp_str = datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S IST')

    result = {
        "timestamp": timestamp_str,
        "symbol": SYMBOL,
        "daily_trend": daily_struct['trend'],
        "h4_trend": h4_struct['trend'],
        "h1_trend": h1_struct['trend'],
        "mtf_condition": mtf['condition'],
        "trade_level_tf": mtf['trade_tf'],
        "confirmation_tf": mtf['confirm_tf'],
        "choch_confirmed": choch_status.get('confirmed', False),
        "is_fakeout": choch_status.get('fakeout', False)
    }

    # Signal Broadcast Trigger
    if choch_status.get('confirmed'):
        direction = "BUY 🟢" if choch_status.get('new_trend') == 'BULLISH' else "SELL 🔴"
        alert_msg = (
            f"🚀 *SMC RULEBOOK SIGNAL: {direction}*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📍 *Symbol:* `{SYMBOL}`\n"
            f"📊 *MTF Condition:* `{mtf['condition']}` ({mtf['description']})\n"
            f"📈 *Daily:* `{daily_struct['trend']}` | *4H:* `{h4_struct['trend']}` | *1H:* `{h1_struct['trend']}`\n"
            f"⚡ *CHOCH Status:* 2-candle close confirmed on `{mtf['confirm_tf']}`\n"
            f"⏰ *Time:* `{timestamp_str}`\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ *Note:* Confirmation complete. Enter as per risk management!"
        )
        send_telegram_alert(alert_msg)

    return result

# ---------------------------------------------------------
# 5. FLASK SERVER ROUTES
# ---------------------------------------------------------
@app.route("/")
def home():
    return "SMC Trading Rulebook Server is Live!"

@app.route("/api/stats")
def api_stats():
    return jsonify({
        "status": "active",
        "system": "SMC Rulebook Engine v2.0",
        "server_time": datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S IST')
    })

@app.route("/api/latest_signal")
def api_signal():
    analysis = analyze_asset()
    return jsonify(analysis)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
