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
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Alert Error: {e}")

# Strict Real-Data Fetcher (No Fake Fallbacks)
def get_market_klines(asset_name, interval="15m", limit=210):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    # --- 1. BTCUSD Data (Coinbase USD) ---
    if asset_name == "BTC":
        granularity = 900 if interval == "15m" else 3600
        url = f"https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity={granularity}"
        try:
            res = requests.get(url, headers=headers, timeout=6)
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
            print(f"Coinbase BTC Error: {e}")

    # --- 2. XAUUSD Data (PAXG / Real Ounce Gold Spot) ---
    if asset_name == "GOLD":
        urls = [
            f"https://api.binance.us/api/v3/klines?symbol=PAXGUSDT&interval={interval}&limit={limit}",
            f"https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval={interval}&limit={limit}"
        ]
        for url in urls:
            try:
                res = requests.get(url, headers=headers, timeout=6)
                if res.status_code == 200:
                    data = res.json()
                    if isinstance(data, list) and len(data) >= 14:
                        closes = [float(c[4]) for c in data]
                        opens = [float(c[1]) for c in data]
                        highs = [float(c[2]) for c in data]
                        lows = [float(c[3]) for c in data]
                        volumes = [float(c[5]) for c in data]
                        return closes, opens, highs, lows, volumes
            except Exception as e:
                continue

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
        return 2.0
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
    atr_val = sum(tr_list[-period:]) / period
    return round(max(atr_val, 1.0), 2)

def analyze_asset(asset_name):
    try:
        clean_asset = "GOLD" if asset_name in ["GOLD", "XAUUSD"] else "BTC"
        closes_15m, opens_15m, highs_15m, lows_15m, volumes_15m = get_market_klines(clean_asset, interval="15m", limit=210)
        
        # Guard Clause: Strict check to avoid Fake Data Calculations
        if not closes_15m or len(closes_15m) < 14:
            return None

        current_price = closes_15m[-1]
        ema9 = calculate_ema(closes_15m, 9)
        ema21 = calculate_ema(closes_15m, 21)
        ema200_15m = calculate_ema(closes_15m, 200) if len(closes_15m) >= 200 else calculate_ema(closes_15m, len(closes_15m))
        rsi_15m = calculate_rsi(closes_15m, 14)
        atr = calculate_atr(highs_15m, lows_15m, closes_15m, 14)
        
        htf_trend_bullish = current_price >= ema200_15m
        
        score = 0
        action = "WAIT / NO CLEAR ENTRY 🟡"
        
        if current_price >= ema200_15m:
            score += 25
            if htf_trend_bullish: score += 20
            if ema9 > ema21: score += 25
            if 48 <= rsi_15m <= 67: score += 20
            if score >= 85: action = "INSTITUTIONAL BUY 🟢"
        else:
            score += 25
            if not htf_trend_bullish: score += 20
            if ema9 < ema21: score += 25
            if 33 <= rsi_15m <= 52: score += 20
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
        print(f"Error in analyze_asset: {e}")
        return None

# --- ROUTES ---
@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "Trading Bot Engine Active 🚀"}), 200

@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    data = analyze_asset(asset)
    
    if not data:
        return jsonify({"status": "Live data connecting... Please refresh in 5 seconds."}), 200

    if data["score"] >= 85:
        chart_symbol = "OANDA:XAUUSD" if data['asset'] == "XAUUSD" else "COINBASE:BTCUSD"
        chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"
        
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
            f"🧠 *Analytics:* \n"
            f"• Confluence Score: *{data['score']}%*\n"
            f"• 1H Trend: `{data['htf_alignment']}`\n"
            f"• Dynamic Volatility (ATR): `{data['atr']}`\n\n"
            f"🛡️ *Risk Management:* \n"
            f"• Risk per Trade: `1.5% ($15)`\n"
            f"• Recommended Lot: `{data['recommended_lot']}`"
        )
        send_telegram_alert(alert_msg, reply_markup=reply_markup)

    return jsonify(data), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
