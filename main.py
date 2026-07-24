import os
import time
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# 📱 TELEGRAM CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = "8723192534:AAHSR0Ysj2LdxWQGwbHOImIoNx0DgnZ7uNs"
TELEGRAM_CHAT_ID = "1317739622"

last_sent_signal = {"GOLD": "", "BTC": ""}

def send_telegram_alert(asset, action, price, sl, tp, confidence):
    global last_sent_signal
    if not TELEGRAM_BOT_TOKEN:
        return
        
    signal_key = f"{action}_{price}"
    if ("BUY" in action or "SELL" in action) and last_sent_signal.get(asset) != signal_key:
        message = (
            f"🚀 **HIGH PRECISION TRADE ALERT** 🚀\n\n"
            f"📊 **Asset:** {asset}\n"
            f"⚡ **Signal:** {action}\n"
            f"💵 **Price:** ${price}\n"
            f"🛑 **Stop Loss (SL):** ${sl}\n"
            f"🎯 **Take Profit (TP):** ${tp}\n"
            f"🔥 **AI Confidence:** {confidence}%\n\n"
            f"📱 Execute trade now!"
        )
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        try:
            requests.post(url, json=payload, timeout=5)
            last_sent_signal[asset] = signal_key
            print(f"[ALERT] Telegram sent for {asset}")
        except Exception as e:
            print("[ALERT ERROR]", e)

# ==========================================
# 📊 DATA FETCHERS
# ==========================================
def get_btc_data():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=15m&limit=50"
        res = requests.get(url, timeout=5).json()
        closes = [float(k[4]) for k in res]
        return closes[-1], closes
    except:
        return 65000.0, [65000.0]*50

def get_gold_data():
    try:
        url = "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=15m&limit=50"
        res = requests.get(url, timeout=5).json()
        closes = [float(k[4]) for k in res]
        return closes[-1], closes
    except:
        return 2350.0, [2350.0]*50

def calculate_sma(data, period):
    if len(data) < period: return data[-1]
    return sum(data[-period:]) / period

def analyze_market(price, history, is_gold=False):
    sma20 = calculate_sma(history, 20)
    sma50 = calculate_sma(history, 50)
    
    sl_offset = 8 if is_gold else 400
    tp_offset = 16 if is_gold else 800
    
    if price > sma20 and sma20 > sma50:
        action = "BUY NOW 🟢"
        color = "GREEN"
        confidence = 85
        sl = round(price - sl_offset, 2)
        tp = round(price + tp_offset, 2)
        note = "Strong Uptrend Detected (SMA20 > SMA50)"
    elif price < sma20 and sma20 < sma50:
        action = "SELL NOW 🔴"
        color = "RED"
        confidence = 85
        sl = round(price + sl_offset, 2)
        tp = round(price - tp_offset, 2)
        note = "Strong Downtrend Detected (SMA20 < SMA50)"
    else:
        action = "WAIT / CONSOLIDATION 🟡"
        color = "GRAY"
        confidence = 50
        sl = 0
        tp = 0
        note = "Market in Range. Waiting for breakout."
        
    return {
        "action": action,
        "color": color,
        "confidence": confidence,
        "sl": sl,
        "tp": tp,
        "status_note": note
    }

# ==========================================
# 🌐 API ENDPOINT
# ==========================================
@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    if asset == 'BTC':
        price, history = get_btc_data()
        analysis = analyze_market(price, history, is_gold=False)
    else:
        price, history = get_gold_data()
        analysis = analyze_market(price, history, is_gold=True)
        
    # Trigger Alert if valid signal
    send_telegram_alert(asset, analysis["action"], price, analysis["sl"], analysis["tp"], analysis["confidence"])
    
    return jsonify({
        "price": price,
        "action": analysis["action"],
        "color": analysis["color"],
        "confidence": analysis["confidence"],
        "sl": analysis["sl"],
        "tp": analysis["tp"],
        "status_note": analysis["status_note"]
    })

@app.route('/')
def home():
    return "Trading AI Engine is Live & Running 24/7!"

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
