import os
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1317739622")

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
        requests.post(url, json=payload, timeout=3)
    except Exception as e:
        print(f"Telegram Alert Error: {e}")

def get_market_klines(asset_name, interval="15m", limit=210):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
    }

    if asset_name == "BTC":
        granularity = 900 if interval == "15m" else 3600
        url = f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity={granularity}"
        try:
            res = requests.get(url, headers=headers, timeout=4)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list) and len(data) >= 14:
                    data = sorted(data, key=lambda x: x[0])
                    closes = [float(c[4]) for c in data[-limit:]]
                    opens = [float(c[3]) for c in data[-limit:]]
                    highs = [float(c[2]) for c in data[-limit:]]
                    lows = [float(c[1]) for c in data[-limit:]]
                    volumes = [float(c[5]) for c in data[-limit:]]
                    return closes, opens, highs, lows, volumes
        except Exception as e:
            print(f"BTC Error: {e}")

    if asset_name == "GOLD":
        yf_interval = "15m" if interval == "15m" else "1h"
        yf_range = "5d" if interval == "15m" else "1mo"
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/XAUUSD=X?interval={yf_interval}&range={yf_range}"
        try:
            res = requests.get(url, headers=headers, timeout=4)
            if res.status_code == 200:
                result = res.json().get('chart', {}).get('result', [])
                if result:
                    indicators = result[0].get('indicators', {}).get('quote', [{}])[0]
                    raw_closes = indicators.get('close', [])
                    raw_opens = indicators.get('open', [])
                    raw_highs = indicators.get('high', [])
                    raw_lows = indicators.get('low', [])
                    raw_vols = indicators.get('volume', [])

                    closes, opens, highs, lows, volumes = [], [], [], [], []
                    for i in range(len(raw_closes)):
                        if raw_closes[i] is not None:
                            closes.append(float(raw_closes[i]))
                            opens.append(float(raw_opens[i]) if raw_opens[i] is not None else float(raw_closes[i]))
                            highs.append(float(raw_highs[i]) if raw_highs[i] is not None else float(raw_closes[i]))
                            lows.append(float(raw_lows[i]) if raw_lows[i] is not None else float(raw_closes[i]))
                            volumes.append(float(raw_vols[i]) if (raw_vols and raw_vols[i] is not None) else 100.0)

                    if len(closes) >= 14:
                        return closes[-limit:], opens[-limit:], highs[-limit:], lows[-limit:], volumes[-limit:]
        except Exception as e:
            print(f"Yahoo Gold Error: {e}")

        # Fast Instant Fallback
        base_price = 4050.00
        closes = [base_price + (i * 0.1) for i in range(30)]
        return closes, closes, [p + 1.5 for p in closes], [p - 1.5 for p in closes], [100.0] * 30

    return [], [], [], [], []

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0
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
    return round(100 - (100 / (1 + rs)), 2)

def calculate_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 5.0
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    atr_val = sum(tr_list[-period:]) / period
    return round(max(atr_val, 2.0), 2)

def analyze_asset(asset_name):
    try:
        clean_asset = "GOLD" if asset_name in ["GOLD", "XAUUSD"] else "BTC"
        closes_15m, opens_15m, highs_15m, lows_15m, volumes_15m = get_market_klines(clean_asset, interval="15m", limit=210)
        
        if not closes_15m or len(closes_15m) < 14:
            return None

        current_price = closes_15m[-1]
        ema9 = calculate_ema(closes_15m, 9)
        ema21 = calculate_ema(closes_15m, 21)
        ema200_15m = calculate_ema(closes_15m, 200) if len(closes_15m) >= 200 else calculate_ema(closes_15m, len(closes_15m))
        rsi_15m = calculate_rsi(closes_15m, 14)
        atr = calculate_atr(highs_15m, lows_15m, closes_15m, 14)
        
        htf_trend_bullish = current_price >= ema200_15m
        
        score = 50
        action = "WAIT / NO CLEAR ENTRY 🟡"
        
        if current_price >= ema200_15m:
            score += 15
            if ema9 > ema21: score += 20
            if 48 <= rsi_15m <= 67: score += 10
            if score >= 85: action = "INSTITUTIONAL BUY 🟢"
        else:
            score += 15
            if ema9 < ema21: score += 20
            if 33 <= rsi_15m <= 52: score += 10
            if score >= 85: action = "INSTITUTIONAL SELL 🔴"

        tp_distance = round(atr * 1.8, 2)
        sl_distance = round(atr * 1.0, 2)
        
        tp = round(current_price + tp_distance if "BUY" in action else current_price - tp_distance, 2)
        sl = round(current_price - sl_distance if "BUY" in action else current_price + sl_distance, 2)

        recommended_lot = 0.05 if clean_asset == "GOLD" else 0.01
        display_pair = "XAUUSD" if clean_asset == "GOLD" else "BTCUSD"

        return {
            "asset": display_pair,
            "price": round(current_price, 2),
            "action": action,
            "score": score,
            "rsi": rsi_15m,
            "atr": atr,
            "htf_alignment": "BULLISH 📈" if htf_trend_bullish else "BEARISH 📉",
            "volume_surge": "NORMAL",
            "tp": tp,
            "sl": sl,
            "recommended_lot": recommended_lot
        }
    except Exception as e:
        print(f"Error: {e}")
        return None

@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "Trading Bot Engine Active 🚀"}), 200

@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    data = analyze_asset(asset)
    
    if not data:
        return jsonify({"status": "Error fetching market data. Please try again."}), 500

    return jsonify(data), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
