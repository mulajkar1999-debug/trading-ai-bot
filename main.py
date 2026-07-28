import os
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# Telegram Configuration (Render Environment Variables with Fallbacks)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1317739622")

# --- TELEGRAM HELPER FUNCTIONS ---
def send_telegram_alert(message, reply_markup=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Alert Error: {e}")

# --- DATA FETCHING & INDICATORS ---
def get_binance_klines(symbol, interval="15m", limit=210):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            closes = [float(c[4]) for c in data]
            opens = [float(c[1]) for c in data]
            highs = [float(c[2]) for c in data]
            lows = [float(c[3]) for c in data]
            volumes = [float(c[5]) for c in data]
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

# --- CORE ENGINE ANALYSIS ---
def analyze_asset(asset_name):
    symbol = 'PAXGUSDT' if asset_name == 'GOLD' else 'BTCUSDT'
    
    # 15m Timeframe Data
    closes_15m, opens_15m, highs_15m, lows_15m, volumes_15m = get_binance_klines(symbol, interval="15m", limit=210)
    # 1h Higher Timeframe Data
    closes_1h, _, _, _, _ = get_binance_klines(symbol, interval="1h", limit=200)
    
    if not closes_15m or len(closes_15m) < 200 or not closes_1h or len(closes_1h) < 200:
        return None

    current_price = closes_15m[-1]
    
    # 15m Technicals
    ema9 = calculate_ema(closes_15m, 9)
    ema21 = calculate_ema(closes_15m, 21)
    ema200_15m = calculate_ema(closes_15m, 200)
    rsi_15m = calculate_rsi(closes_15m, 14)
    atr = calculate_atr(highs_15m, lows_15m, closes_15m, 14)
    
    # 1H Trend Filter
    ema200_1h = calculate_ema(closes_1h, 200)
    htf_trend_bullish = current_price > ema200_1h
    
    # Candlestick Wicks & Volatility
    body = abs(closes_15m[-1] - opens_15m[-1])
    lower_wick = min(opens_15m[-1], closes_15m[-1]) - lows_15m[-1]
    upper_wick = highs_15m[-1] - max(opens_15m[-1], closes_15m[-1])
    bull_rejection = lower_wick > (body * 1.8) and lower_wick > upper_wick
    bear_rejection = upper_wick > (body * 1.8) and upper_wick > lower_wick
    
    avg_vol = sum(volumes_15m[-20:]) / 20
    volume_surge = volumes_15m[-1] > (avg_vol * 1.3)
    
    score = 0
    action = "WAIT / NO CLEAR ENTRY 🟡"
    
    # BUY SETUP
    if current_price > ema200_15m:
        score += 20
        if htf_trend_bullish:  # HTF Alignment (+15%)
            score += 15
        if ema9 > ema21:
            score += 20
        if 48 <= rsi_15m <= 67:
            score += 20
        if bull_rejection:
            score += 15
        if volume_surge:
            score += 10
            
        if score >= 85:
            action = "INSTITUTIONAL BUY 🟢"

    # SELL SETUP
    elif current_price < ema200_15m:
        score += 20
        if not htf_trend_bullish:  # HTF Alignment (+15%)
            score += 15
        if ema9 < ema21:
            score += 20
        if 33 <= rsi_15m <= 52:
            score += 20
        if bear_rejection:
            score += 15
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

    # Risk Management Calculation ($1000 standard balance, 1.5% max risk)
    risk_amount = 15.0
    recommended_lot = round(risk_amount / (sl_distance if sl_distance > 0 else 1.0), 2)
    if asset_name == "GOLD":
        recommended_lot = max(0.01, min(recommended_lot, 0.10))
    else:
        recommended_lot = max(0.001, min(recommended_lot, 0.05))

    return {
        "asset": asset_name,
        "price": current_price,
        "action": action,
        "score": score,
        "rsi": round(rsi_15m, 2),
        "atr": round(atr, 2),
        "htf_alignment": "BULLISH 📈" if htf_trend_bullish else "BEARISH 📉",
        "volume_surge": "STRONG 📊" if volume_surge else "NORMAL",
        "tp": tp,
        "sl": sl,
        "recommended_lot": recommended_lot
    }

# --- ROUTES ---
@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    data = analyze_asset(asset)
    
    if not data:
        return jsonify({"status": "Scanning Error or Insufficient Data"}), 500

    if data["score"] >= 85:
        chart_symbol = "PAXGUSDT" if asset == "GOLD" else "BTCUSDT"
        chart_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{chart_symbol}"
        
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📈 Open TradingView Chart", "url": chart_link}]
            ]
        }

        alert_msg = (
            f"🚀 *PRO ALGO SIGNAL ({data['asset']})*\n\n"
            f"Action: *{data['action']}*\n"
            f"Entry Price: `{data['price']}`\n"
            f"Take Profit (TP): `{data['tp']}`\n"
            f"Stop Loss (SL): `{data['sl']}`\n\n"
            f"🧠 *Smart Analytics:* \n"
            f"• Confluence Score: *{data['score']}%*\n"
            f"• 1H Higher Trend: `{data['htf_alignment']}`\n"
            f"• Volume Expansion: `{data['volume_surge']}`\n"
            f"• Dynamic Volatility (ATR): `{data['atr']}`\n\n"
            f"🛡️ *Risk Management:* \n"
            f"• Risk per Trade: `1.5% ($15 on $1K)`\n"
            f"• Rec. Lot Size: `{data['recommended_lot']}`"
        )
        send_telegram_alert(alert_msg, reply_markup=reply_markup)

    return jsonify(data)

# Telegram Webhook for Interactive Commands (/status, /gold, /btc)
@app.route('/telegram/webhook', methods=['POST'])
def telegram_webhook():
    req_data = request.get_json()
    if req_data and "message" in req_data:
        text = req_data["message"].get("text", "").strip().lower()
        
        if text in ["/start", "/status"]:
            gold_data = analyze_asset("GOLD")
            btc_data = analyze_asset("BTC")
            msg = (
                "🤖 *BOT LIVE STATUS REPORT*\n\n"
                f"🟡 *GOLD (PAXG):* Score {gold_data['score']}% | Trend {gold_data['htf_alignment']}\n"
                f"🟠 *BTC:* Score {btc_data['score']}% | Trend {btc_data['htf_alignment']}\n\n"
                "✅ _24/7 Engine Active & Scanning Market_"
            )
            send_telegram_alert(msg)
            
        elif text in ["/gold", "/btc"]:
            asset_selected = "GOLD" if "gold" in text else "BTC"
            res = analyze_asset(asset_selected)
            msg = (
                f"📊 *LIVE SNAPSHOT ({res['asset']})*\n\n"
                f"Price: `{res['price']}`\n"
                f"Action: *{res['action']}*\n"
                f"Confluence Score: *{res['score']}%*\n"
                f"RSI (15m): `{res['rsi']}` | HTF: `{res['htf_alignment']}`"
            )
            send_telegram_alert(msg)

    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
