from flask import Flask, jsonify, request
import requests

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

def get_binance_data(symbol, interval="15m", limit=210):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            closes = [float(candle[4]) for candle in data]
            return closes
        return []
    except Exception as e:
        print(f"Error fetching data: {e}")
        return []

def calculate_ema(prices, period):
    if len(prices) < period:
        return 0
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(change if change > 0 else 0)
        losses.append(abs(change) if change < 0 else 0)
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    
    symbol_map = {
        'GOLD': 'PAXGUSDT',
        'BTC': 'BTCUSDT'
    }
    
    symbol = symbol_map.get(asset, 'PAXGUSDT')
    prices = get_binance_data(symbol, interval="15m", limit=210)
    
    if not prices or len(prices) < 200:
        return jsonify({
            "asset": asset,
            "status": "Analyzing market data... Please refresh",
            "action": "WAIT 🟡",
            "confidence": 50
        }), 200
    
    current_price = prices[-1]
    
    # Technical Indicators
    ema_fast = calculate_ema(prices, 9)      # Short term trend
    ema_slow = calculate_ema(prices, 21)     # Medium term trend
    ema_trend = calculate_ema(prices, 200)   # Major overall trend (Filter)
    rsi = calculate_rsi(prices, 14)           # Momentum indicator
    
    confidence = 50
    action = "WAIT / CONSOLIDATION 🟡"
    
    # Advanced Multi-Indicator Logic (Targeting 80%+ Accuracy)
    # BUY SETUP
    if current_price > ema_trend:  # Rule 1: Above 200 EMA (Uptrend)
        if ema_fast > ema_slow:    # Rule 2: Bullish Crossover
            if 45 <= rsi <= 68:     # Rule 3: Perfect Momentum Zone
                confidence = 85
                action = "BUY NOW 🟢"
            elif rsi < 45:
                confidence = 75
                action = "STRONG BUY WATCH 🟢"
                
    # SELL SETUP
    elif current_price < ema_trend: # Rule 1: Below 200 EMA (Downtrend)
        if ema_fast < ema_slow:     # Rule 2: Bearish Crossover
            if 32 <= rsi <= 55:      # Rule 3: Perfect Momentum Zone
                confidence = 85
                action = "SELL NOW 🔴"
            elif rsi > 55:
                confidence = 75
                action = "STRONG SELL WATCH 🔴"

    # Pip margins calculation
    pips_margin = 1.8 if asset == 'GOLD' else 120
    tp = round(current_price + pips_margin, 2) if "BUY" in action else round(current_price - pips_margin, 2)
    sl = round(current_price - (pips_margin * 0.7), 2) if "BUY" in action else round(current_price + (pips_margin * 0.7), 2)

    # High Confidence Alert Trigger (80%+ Only)
    if confidence >= 80:
        alert_msg = (
            f"🔥 *HIGH PRECISION SIGNAL ({asset})*\n\n"
            f"Action: *{action}*\n"
            f"Current Price: `{current_price}`\n"
            f"Target (TP): `{tp}`\n"
            f"Stop Loss (SL): `{sl}`\n"
            f"RSI Value: `{round(rsi, 2)}`\n"
            f"Confidence: *{confidence}%*\n\n"
            f"🎯 Multi-Indicator High Win-Rate Setup!"
        )
        send_telegram_alert(alert_msg)

    return jsonify({
        "asset": asset,
        "price": current_price,
        "action": action,
        "confidence": confidence,
        "rsi": round(rsi, 2),
        "tp": tp if confidence >= 80 else 0,
        "sl": sl if confidence >= 80 else 0
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
