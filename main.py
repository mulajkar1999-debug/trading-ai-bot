from flask import Flask, jsonify, request
import requests

app = Flask(__name__)

# Telegram Configuration
TELEGRAM_BOT_TOKEN = "7963384242:AAEg3L2d_g8w-vR8fInS4YtS2NqE3Y3L-S8"
TELEGRAM_CHAT_ID = "1317739622"

def send_telegram_alert(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID, 
            "text": message, 
            "parse_mode": "Markdown",
            "disable_web_page_preview": False
        }
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Send Error: {e}")

def get_binance_klines(symbol, interval="15m", limit=210):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            closes = [float(candle[4]) for candle in data]
            volumes = [float(candle[5]) for candle in data]
            highs = [float(candle[2]) for candle in data]
            lows = [float(candle[3]) for candle in data]
            return closes, volumes, highs, lows
        return [], [], [], []
    except Exception as e:
        print(f"Error fetching data: {e}")
        return [], [], [], []

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

# 📊 Support and Resistance Calculator (Pivot Points)
def calculate_support_resistance(highs, lows, closes):
    prev_high = highs[-2]
    prev_low = lows[-2]
    prev_close = closes[-2]
    
    pivot = (prev_high + prev_low + prev_close) / 3
    r1 = (2 * pivot) - prev_low
    s1 = (2 * pivot) - prev_high
    return s1, r1, pivot

# 🔊 Volume Analysis Check
def is_volume_strong(volumes, period=20):
    if len(volumes) < period:
        return False
    avg_vol = sum(volumes[-period:]) / period
    current_vol = volumes[-1]
    return current_vol > (avg_vol * 1.2)  # 20% higher than average volume

@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    
    symbol_map = {
        'GOLD': 'PAXGUSDT',
        'BTC': 'BTCUSDT'
    }
    
    symbol = symbol_map.get(asset, 'PAXGUSDT')
    prices, volumes, highs, lows = get_binance_klines(symbol, interval="15m", limit=210)
    
    if not prices or len(prices) < 200:
        return jsonify({
            "asset": asset,
            "status": "Scanning Technicals, Volume & Levels...",
            "action": "WAIT 🟡",
            "confidence": 50
        }), 200
    
    current_price = prices[-1]
    
    # Indicators & Price Action Data
    ema_fast = calculate_ema(prices, 9)
    ema_slow = calculate_ema(prices, 21)
    ema_trend = calculate_ema(prices, 200)
    rsi = calculate_rsi(prices, 14)
    support, resistance, pivot = calculate_support_resistance(highs, lows, prices)
    has_volume = is_volume_strong(volumes)
    
    confidence = 50
    action = "WAIT / CONSOLIDATION 🟡"
    
    # 🎯 ADVANCED CONFLUENCE LOGIC
    # BUY SETUP: Uptrend + EMA Cross + RSI Zone + Above Support + Strong Volume
    if current_price > ema_trend and ema_fast > ema_slow:
        if 48 <= rsi <= 68 and current_price > support:
            if has_volume:
                confidence = 88  # Volume confirmed
                action = "STRONG BUY NOW 🟢"
            else:
                confidence = 75  # Low volume buy
                action = "BUY WATCH 🟢"
                
    # SELL SETUP: Downtrend + EMA Cross + RSI Zone + Below Resistance + Strong Volume
    elif current_price < ema_trend and ema_fast < ema_slow:
        if 32 <= rsi <= 52 and current_price < resistance:
            if has_volume:
                confidence = 88  # Volume confirmed
                action = "STRONG SELL NOW 🔴"
            else:
                confidence = 75  # Low volume sell
                action = "SELL WATCH 🔴"

    pips_margin = 2.0 if asset == 'GOLD' else 150
    
    if "BUY" in action:
        tp = round(current_price + pips_margin, 2)
        sl = round(current_price - (pips_margin * 0.65), 2)
    else:
        tp = round(current_price - pips_margin, 2)
        sl = round(current_price + (pips_margin * 0.65), 2)

    tv_symbol = "PAXGUSDT" if asset == "GOLD" else "BTCUSDT"
    chart_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{tv_symbol}"

    # Telegram Notification Trigger (High Precision Only)
    if confidence >= 85:
        alert_msg = (
            f"🔥 *ULTIMATE HIGH PRECISION SIGNAL ({asset})*\n\n"
            f"Action: *{action}*\n"
            f"Current Price: `{current_price}`\n"
            f"Target (TP): `{tp}`\n"
            f"Stop Loss (SL): `{sl}`\n\n"
            f"📊 *Market Analysis Data:*\n"
            f"• Support Level: `{round(support, 2)}`\n"
            f"• Resistance Level: `{round(resistance, 2)}`\n"
            f"• Volume Status: `{'STRONG 🚀' if has_volume else 'NORMAL'}`\n"
            f"• RSI Momentum: `{round(rsi, 2)}`\n"
            f"• Confidence: *{confidence}%*\n\n"
            f"📈 [Live Chart View]({chart_link})"
        )
        send_telegram_alert(alert_msg)

    return jsonify({
        "asset": asset,
        "price": current_price,
        "action": action,
        "confidence": confidence,
        "rsi": round(rsi, 2),
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "volume_spike": has_volume,
        "tp": tp if confidence >= 85 else 0,
        "sl": sl if confidence >= 85 else 0
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
