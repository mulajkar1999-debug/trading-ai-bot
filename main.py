from flask import Flask, jsonify, request
import requests
import os

app = Flask(__name__)

# Telegram Configuration
TELEGRAM_BOT_TOKEN = "7963384242:AAEg3L2d_g8w-vR8fInS4YtS2NqE3Y3L-S8"
TELEGRAM_CHAT_ID = "1317739622"

def send_telegram_alert(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Send Error: {e}")

def get_binance_data(symbol, interval="15m", limit=50):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            closes = [float(candle[4]) for candle in data]
            return closes
        else:
            print(f"Binance API status code: {response.status_code}")
            return []
    except Exception as e:
        print(f"Error fetching data: {e}")
        return []

def calculate_sma(data, period):
    if not data or len(data) < period:
        return 0
    return sum(data[-period:]) / period

@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    
    symbol_map = {
        'GOLD': 'PAXGUSDT',
        'BTC': 'BTCUSDT'
    }
    
    symbol = symbol_map.get(asset, 'PAXGUSDT')
    prices = get_binance_data(symbol, interval="15m", limit=50)
    
    # Error fallback if Binance fails temporarily
    if not prices or len(prices) < 21:
        return jsonify({
            "asset": asset,
            "status": "Fetching data... Please refresh in 10 seconds",
            "action": "WAIT / CONSOLIDATION 🟡",
            "confidence": 50
        }), 200
    
    current_price = prices[-1]
    sma_fast = calculate_sma(prices, 9)
    sma_slow = calculate_sma(prices, 21)
    
    pips_margin = 1.5 if asset == 'GOLD' else 100
    
    action = "WAIT / CONSOLIDATION 🟡"
    confidence = 50
    tp = 0
    sl = 0
    
    if sma_fast > sma_slow and current_price > sma_fast:
        action = "BUY NOW 🟢"
        confidence = 65
        tp = round(current_price + pips_margin, 2)
        sl = round(current_price - (pips_margin * 0.8), 2)
        
    elif sma_fast < sma_slow and current_price < sma_fast:
        action = "SELL NOW 🔴"
        confidence = 65
        tp = round(current_price - pips_margin, 2)
        sl = round(current_price + (pips_margin * 0.8), 2)
        
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
