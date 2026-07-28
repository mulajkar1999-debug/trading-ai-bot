import os
from flask import Flask, jsonify, request
import requests

app = Flask(__name__)

# Secure Token Reading from Render Environment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1317739622")

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
        print(f"Telegram Alert Error: {e}")

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
        print(f"Data Fetch Error: {e}")
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

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 1.0
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def analyze_smc_structure(highs, lows, closes):
    recent_high = max(highs[-25:-1])
    recent_low = min(lows[-25:-1])
    curr_close = closes[-1]
    
    bos_bullish = curr_close > recent_high
    bos_bearish = curr_close < recent_low
    return bos_bullish, bos_bearish, recent_high, recent_low

def analyze_candle_traps(opens, closes, highs, lows):
    c_open, c_close, c_high, c_low = opens[-1], closes[-1], highs[-1], lows[-1]
    body = abs(c_close - c_open)
    upper_wick = c_high - max(c_open, c_close)
    lower_wick = min(c_open, c_close) - c_low
    
    bullish_rejection = lower_wick > (body * 1.8) and lower_wick > upper_wick
    bearish_rejection = upper_wick > (body * 1.8) and upper_wick > lower_wick
    return bullish_rejection, bearish_rejection

@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    symbol = 'PAXGUSDT' if asset == 'GOLD' else 'BTCUSDT'
    
    closes, opens, highs, lows, volumes = get_binance_klines(symbol, interval="15m", limit=210)
    
    if not closes or len(closes) < 200:
        return jsonify({
            "asset": asset,
            "status": "PRO ENGINE SCANNING MARKET...",
            "action": "WAIT 🟡",
            "confidence": 50
        }), 200

    current_price = closes[-1]
    
    ema_fast = calculate_ema(closes, 9)
    ema_slow = calculate_ema(closes, 21)
    ema_trend = calculate_ema(closes, 200)
    rsi = calculate_rsi(closes, 14)
    atr = calculate_atr(highs, lows, closes, 14)
    
    bos_bull, bos_bear, supply_zone, demand_zone = analyze_smc_structure(highs, lows, closes)
    bull_reject, bear_reject = analyze_candle_traps(opens, closes, highs, lows)
    
    avg_vol = sum(volumes[-20:]) / 20
    volume_surge = volumes[-1] > (avg_vol * 1.3)
    
    score = 0
    action = "WAIT / NO CLEAR ENTRY 🟡"
    
    # BUY SETUP
    if current_price > ema_trend:
        score += 25
        if ema_fast > ema_slow:
            score += 20
        if 48 <= rsi <= 67:
            score += 20
        if bos_bull or bull_reject:
            score += 20
        if volume_surge:
            score += 10
            
        if score >= 85:
            action = "INSTITUTIONAL BUY 🟢"

    # SELL SETUP
    elif current_price < ema_trend:
        score += 25
        if ema_fast < ema_slow:
            score += 20
        if 33 <= rsi <= 52:
            score += 20
        if bos_bear or bear_reject:
            score += 20
        if volume_surge:
            score += 10
            
        if score >= 85:
            action = "INSTITUTIONAL SELL 🔴"

    tp_distance = round(atr * 1.8, 2)
    sl_distance = round(atr * 1.0, 2)
    
    if "BUY" in action:
        tp = round(current_price + tp_distance, 2)
        sl = round(current_price - sl_distance, 2)
    else:
        tp = round(current_price - tp_distance, 2)
        sl = round(current_price + sl_distance, 2)

    chart_symbol = "PAXGUSDT" if asset == "GOLD" else "BTCUSDT"
    chart_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{chart_symbol}"

    if score >= 85:
        alert_msg = (
            f"🚀 *PRO ALGO SIGNAL ({asset})*\n\n"
            f"Action: *{action}*\n"
            f"Entry Price: `{current_price}`\n"
            f"Take Profit (TP): `{tp}`\n"
            f"Stop Loss (SL): `{sl}`\n\n"
            f"🔬 *Institutional Analysis:* \n"
            f"• Confluence Score: *{score}%*\n"
            f"• Structure Break: `{'YES ⚡' if (bos_bull or bos_bear) else 'NORMAL'}`\n"
            f"• Volume Expansion: `{'STRONG 📊' if volume_surge else 'MODERATE'}`\n"
            f"• Dynamic Volatility (ATR): `{round(atr, 2)}`\n\n"
            f"📈 [Open Live Chart]({chart_link})"
        )
        send_telegram_alert(alert_msg)

    return jsonify({
        "asset": asset,
        "price": current_price,
        "action": action,
        "confluence_score": score,
        "rsi": round(rsi, 2),
        "tp": tp if score >= 85 else 0,
        "sl": sl if score >= 85 else 0
    })

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
