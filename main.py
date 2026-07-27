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
            opens = [float(candle[1]) for candle in data]
            highs = [float(candle[2]) for candle in data]
            lows = [float(candle[3]) for candle in data]
            volumes = [float(candle[5]) for candle in data]
            return closes, opens, highs, lows, volumes
        return [], [], [], [], []
    except Exception as e:
        print(f"Error fetching data: {e}")
        return [], [], [], [], []

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

# 🧠 PRICE ACTION FUNCTION 1: Market Structure (BOS - Break of Structure)
def check_market_structure(highs, lows, closes):
    recent_high = max(highs[-20:-1])  # Last 20 candles highest peak
    recent_low = min(lows[-20:-1])    # Last 20 candles lowest trough
    current_close = closes[-1]
    
    bos_bullish = current_close > recent_high  # High broken = Bullish BOS
    bos_bearish = current_close < recent_low   # Low broken = Bearish BOS
    return bos_bullish, bos_bearish, recent_high, recent_low

# 🧠 PRICE ACTION FUNCTION 2: Wick Rejection (Liquidity Rejection Test)
def check_wick_rejection(opens, closes, highs, lows):
    curr_open = opens[-1]
    curr_close = closes[-1]
    curr_high = highs[-1]
    curr_low = lows[-1]
    
    body_size = abs(curr_close - curr_open)
    lower_wick = min(curr_open, curr_close) - curr_low
    upper_wick = curr_high - max(curr_open, curr_close)
    
    # Strong rejection from bottom (Bullish)
    bullish_rejection = lower_wick > (body_size * 1.5)
    # Strong rejection from top (Bearish)
    bearish_rejection = upper_wick > (body_size * 1.5)
    
    return bullish_rejection, bearish_rejection

@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    
    symbol_map = {
        'GOLD': 'PAXGUSDT',
        'BTC': 'BTCUSDT'
    }
    
    symbol = symbol_map.get(asset, 'PAXGUSDT')
    closes, opens, highs, lows, volumes = get_binance_klines(symbol, interval="15m", limit=210)
    
    if not closes or len(closes) < 200:
        return jsonify({
            "asset": asset,
            "status": "Scanning Technicals & Market Structure...",
            "action": "WAIT 🟡",
            "confidence": 50
        }), 200
    
    current_price = closes[-1]
    
    # Technical Indicators
    ema_fast = calculate_ema(closes, 9)
    ema_slow = calculate_ema(closes, 21)
    ema_trend = calculate_ema(closes, 200)
    rsi = calculate_rsi(closes, 14)
    
    # Price Action Logic Checks
    bos_bullish, bos_bearish, demand_level, supply_level = check_market_structure(highs, lows, closes)
    bullish_wick, bearish_wick = check_wick_rejection(opens, closes, highs, lows)
    
    confidence = 50
    action = "WAIT / CONSOLIDATION 🟡"
    
    # 🎯 HYBRID CONFLUENCE LOGIC (Price Action + Indicators)
    # BUY SETUP
    if current_price > ema_trend and ema_fast > ema_slow:
        if 48 <= rsi <= 68:
            if bos_bullish or bullish_wick:  # Price Action Confirmation
                confidence = 88
                action = "STRONG BUY (BOS CONFIRMED) 🟢"
            else:
                confidence = 75
                action = "BUY WATCH 🟢"
                
    # SELL SETUP
    elif current_price < ema_trend and ema_fast < ema_slow:
        if 32 <= rsi <= 52:
            if bos_bearish or bearish_wick:  # Price Action Confirmation
                confidence = 88
                action = "STRONG SELL (BOS CONFIRMED) 🔴"
            else:
                confidence = 75
                action = "SELL WATCH 🔴"

    pips_margin = 2.0 if asset == 'GOLD' else 150
    
    if "BUY" in action:
        tp = round(current_price + pips_margin, 2)
        sl = round(current_price - (pips_margin * 0.65), 2)
    else:
        tp = round(current_price - pips_margin, 2)
        sl = round(current_price + pips_margin, 2)

    tv_symbol = "PAXGUSDT" if asset == "GOLD" else "BTCUSDT"
    chart_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{tv_symbol}"

    # Telegram Notification Trigger (85%+ High Precision Only)
    if confidence >= 85:
        alert_msg = (
            f"🔥 *PRO HYBRID SIGNAL ({asset})*\n\n"
            f"Action: *{action}*\n"
            f"Current Price: `{current_price}`\n"
            f"Target (TP): `{tp}`\n"
            f"Stop Loss (SL): `{sl}`\n\n"
            f"🧠 *Price Action Data:*\n"
            f"• Demand/Low Level: `{round(supply_level, 2)}`\n"
            f"• Supply/High Level: `{round(demand_level, 2)}`\n"
            f"• Structure Break (BOS): `{'YES ⚡' if (bos_bullish or bos_bearish) else 'NO'}`\n"
            f"• Rejection Wick: `{'DETECTED 🕯️' if (bullish_wick or bearish_wick) else 'NONE'}`\n"
            f"• Confidence: *{confidence}%*\n\n"
            f"📈 [TradingView Chart Link]({chart_link})"
        )
        send_telegram_alert(alert_msg)

    return jsonify({
        "asset": asset,
        "price": current_price,
        "action": action,
        "confidence": confidence,
        "rsi": round(rsi, 2),
        "bos_break": bos_bullish or bos_bearish,
        "wick_rejection": bullish_wick or bearish_wick,
        "tp": tp if confidence >= 85 else 0,
        "sl": sl if confidence >= 85 else 0
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
