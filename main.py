from flask import Flask, jsonify, request
import requests
import os

app = Flask(__name__)

# Telegram Configuration
TELEGRAM_BOT_TOKEN = "7963384242:AAEg3L2d_g8w-vR8fInS4YtS2NqE3Y3L-S8"  # Aapka Bot Token
TELEGRAM_CHAT_ID = "1317739622"      # Aapki Chat ID

def send_telegram_alert(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Send Error: {e}")

def get_binance_data(symbol, interval="15m", limit=50):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, timeout=5)
        data = response.json()
        closes = [float(candle[4]) for candle in data]
        return closes
    except Exception as e:
        print(f"Error fetching data: {e}")
        return []

def calculate_sma(data, period):
    if len(data) < period:
        return 0
    return sum(data[-period:]) / period

@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    
    # Binance Pair Mapping
    symbol_map = {
        'GOLD': 'PAXGUSDT',
        'BTC': 'BTCUSDT'
    }
    
    symbol = symbol_map.get(asset, 'PAXGUSDT')
    prices = get_binance_data(symbol, interval="15m", limit=50)
    
    if not prices:
        return jsonify({"status": "Error fetching data"}), 500
    
    current_price = prices[-1]
    sma_fast = calculate_sma(prices, 9)   # Short-term EMA/SMA for 10-20 pips
    sma_slow = calculate_sma(prices, 21)  # Trend filter
    
    # Fast Scalping Logic (10 - 20 Pips Setup)
    pips_margin = 1.5 if asset == 'GOLD' else 100  # Target ~15 pips approx
    
    action = "WAIT / CONSOLIDATION 🟡"
    confidence = 50
    tp = 0
    sl = 0
    
    if sma_fast > sma_slow and current_price > sma_fast:
        action = "BUY NOW 🟢"
        confidence = 65  # Lowered threshold for daily 10-20 pips signals
        tp = round(current_price + pips_margin, 2)
        sl = round(current_price - (pips_margin * 0.8), 2)
        
    elif sma_fast < sma_slow and current_price < sma_fast:
        action = "SELL NOW 🔴"
        confidence = 65
        tp = round(current_price - pips_margin, 2)
        sl = round(current_price + (pips_margin * 0.8), 2)
        
    # Trigger Telegram Alert on Scalp Signal
    if confidence >= 60:
        alert_msg = (
            f"🎯 *FAST SCALP SIGNAL ({asset})*\n\n"
            f"Action: *{action}*\n"
            f"Current Price: `{current_price}`\n"
            f"Target (TP ~15 Pips): `{tp}`\n"
            f"Stop Loss (SL): `{sl}`\n"
            f"Confidence: `{confidence}%`\n\n"
            f"⚡ Quick 10-20 pips trade!"
        )
        send_telegram_alert(alert_msg)
        
    return jsonify({
        "asset": asset,
        "price": current_price,
        "action": action,
        "confidence": confidence,
        "tp": tp,
        "sl": sl
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
