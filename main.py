import os
import time
import threading
import requests
from datetime import datetime, timedelta
import pytz
from flask import Flask, jsonify, request

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1317739622")

active_signals = []
latest_signal_data = {}  # For future Auto-Trading webhook/API consumption
last_signal_time = {"GOLD": None, "BTC": None}

trade_history = {
    "total_signals": 0,
    "wins": 0,
    "losses": 0,
    "win_rate": 0.0
}

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
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code == 200:
            return res.json().get("result", {}).get("message_id")
    except Exception as e:
        print(f"Telegram Alert Error: {e}")
    return None

def edit_telegram_alert(message_id, new_message, reply_markup=None):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": new_message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Edit Error: {e}")

def get_market_klines(asset_name, interval="1m", limit=50):
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    if asset_name in ["GOLD", "XAUUSD"]:
        product_id = "PAXG-USD"
    elif asset_name in ["BTC", "BTCUSD"]:
        product_id = "BTC-USD"
    else:
        return [], [], [], [], []

    granularity = 60 if interval == "1m" else (900 if interval == "15m" else 3600)
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?granularity={granularity}"

    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list) and len(data) >= 2:
                data = sorted(data, key=lambda x: x[0])
                closes = [float(c[4]) for c in data[-limit:]]
                opens = [float(c[3]) for c in data[-limit:]]
                highs = [float(c[2]) for c in data[-limit:]]
                lows = [float(c[1]) for c in data[-limit:]]
                volumes = [float(c[5]) for c in data[-limit:]]
                return closes, opens, highs, lows, volumes
    except Exception as e:
        print(f"Coinbase Fetch Error ({product_id}): {e}")

    return [], [], [], [], []

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = (price * k) + (ema * (1 - k))
    return ema

def calculate_rsi(prices, period=7): # Fast Scalping RSI (7 period)
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

def update_win_rate():
    total = trade_history["wins"] + trade_history["losses"]
    trade_history["total_signals"] = total
    if total > 0:
        trade_history["win_rate"] = round((trade_history["wins"] / total) * 100, 2)

def analyze_asset(asset_name):
    global latest_signal_data
    try:
        clean_asset = "GOLD" if asset_name in ["GOLD", "XAUUSD"] else "BTC"

        # Fetch 1-Minute Candles for Scalping
        closes_1m, opens_1m, highs_1m, lows_1m, volumes_1m = get_market_klines(clean_asset, interval="1m", limit=50)
        
        if not closes_1m or len(closes_1m) < 15:
            return None

        current_price = closes_1m[-1]
        ema3 = calculate_ema(closes_1m, 3)
        ema8 = calculate_ema(closes_1m, 8)
        rsi = calculate_rsi(closes_1m, period=7)

        score = 0
        action = "WAIT / NO CLEAR ENTRY 🟡"

        # Fast Scalping Bullish Cross
        if ema3 > ema8 and closes_1m[-1] > opens_1m[-1]:
            score += 40
            if rsi > 50: score += 30
            if closes_1m[-1] > closes_1m[-2]: score += 30
            if score >= 70: action = "SCALPING BUY 🟢"

        # Fast Scalping Bearish Cross
        elif ema3 < ema8 and closes_1m[-1] < opens_1m[-1]:
            score += 40
            if rsi < 50: score += 30
            if closes_1m[-1] < closes_1m[-2]: score += 30
            if score >= 70: action = "SCALPING SELL 🔴"

        # Micro Take Profit / Stop Loss (1-3 Pips)
        if clean_asset == "GOLD":
            tp_dist = 0.30  # 3 Pips Gold
            sl_dist = 0.35  # 3.5 Pips Gold
            recommended_lot = 0.05
            display_pair = "XAUUSD"
        else: # BTC
            tp_dist = 25.0  # ~$25 BTC Micro Move
            sl_dist = 30.0  # ~$30 BTC Micro Stop
            recommended_lot = 0.01
            display_pair = "BTCUSD"

        tp = round(current_price + tp_dist if "BUY" in action else current_price - tp_dist, 2)
        sl = round(current_price - sl_dist if "BUY" in action else current_price + sl_dist, 2)

        signal_obj = {
            "asset": display_pair,
            "price": round(current_price, 2),
            "action": action,
            "score": score,
            "rsi": rsi,
            "tp": tp,
            "sl": sl,
            "recommended_lot": recommended_lot
        }

        if score >= 70:
            latest_signal_data = signal_obj  # Ready for Auto-Trading API

        return signal_obj

    except Exception as e:
        print(f"Error in analyze_asset: {e}")
        return None

def analyze_and_trigger(asset_key):
    # Reduced Cooldown for Scalping (2 min delay per asset)
    now = datetime.now()
    if last_signal_time[asset_key] and (now - last_signal_time[asset_key]) < timedelta(minutes=2):
        return

    data = analyze_asset(asset_key)
    if not data:
        return

    existing = [s for s in active_signals if s['asset'] == data['asset']]
    if existing:
        return

    if data["score"] >= 70:
        tz_ist = pytz.timezone('Asia/Kolkata')
        signal_time = datetime.now(tz_ist).strftime("%I:%M %p | %d %b")

        chart_symbol = "PAXGUSD" if data['asset'] == "XAUUSD" else "COINBASE:BTCUSD"
        chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"
        
        reply_markup = {
            "inline_keyboard": [[{"text": "📈 TradingView Chart", "url": chart_link}]]
        }

        alert_msg = (
            f"⚡ *1M ULTRA SCALPING SIGNAL ({data['asset']})*\n"
            f"⏰ Time: `{signal_time}`\n\n"
            f"Status: *ACTIVE ⏳*\n"
            f"Action: *{data['action']}*\n"
            f"Entry Price: `{data['price']}`\n"
            f"Take Profit (TP): `{data['tp']}` *(Micro Target)*\n"
            f"Stop Loss (SL): `{data['sl']}`\n\n"
            f"🧠 *Score:* *{data['score']}%*\n"
            f"🛡️ *Lot Size:* `{data['recommended_lot']}`"
        )
        
        msg_id = send_telegram_alert(alert_msg, reply_markup=reply_markup)
        
        if msg_id:
            last_signal_time[asset_key] = now
            active_signals.append({
                "msg_id": msg_id,
                "asset": data['asset'],
                "action": data['action'],
                "price": data['price'],
                "tp": data['tp'],
                "sl": data['sl'],
                "score": data['score'],
                "created_at": signal_time,
                "break_even_triggered": False
            })

def continuous_auto_scanner():
    while True:
        try:
            for asset in ["GOLD", "BTC"]:
                analyze_and_trigger(asset)
                time.sleep(2)
        except Exception as e:
            print(f"Auto Scanner Error: {e}")
        # Continuous Fast Scan every 15 seconds
        time.sleep(15)

threading.Thread(target=continuous_auto_scanner, daemon=True).start()

def monitor_active_trades():
    while True:
        try:
            for signal in list(active_signals):
                asset_code = "BTC" if "BTC" in signal["asset"] else "GOLD"
                closes, _, highs, lows, _ = get_market_klines(asset_code, interval="1m", limit=3)
                if not closes:
                    continue
                
                curr_high, curr_low = highs[-1], lows[-1]
                is_buy = "BUY" in signal["action"]
                entry_p, tp_p, sl_p = signal["price"], signal["tp"], signal["sl"]

                tp_hit = curr_high >= tp_p if is_buy else curr_low <= tp_p
                sl_hit = curr_low <= signal["sl"] if is_buy else curr_high >= signal["sl"]

                if tp_hit or sl_hit:
                    if tp_hit:
                        status_text = "✅ MICRO TARGET HIT (WIN) 🎯"
                        trade_history["wins"] += 1
                    else:
                        status_text = "❌ STOP LOSS HIT (LOSS) 🛑"
                        trade_history["losses"] += 1
                    
                    update_win_rate()

                    chart_symbol = "PAXGUSD" if signal['asset'] == "XAUUSD" else "COINBASE:BTCUSD"
                    chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"
                    reply_markup = {"inline_keyboard": [[{"text": "📈 TradingView Chart", "url": chart_link}]]}

                    updated_msg = (
                        f"⚡ *1M SCALPING SIGNAL ({signal['asset']})*\n"
                        f"⏰ Time: `{signal['created_at']}`\n\n"
                        f"Status: *{status_text}*\n"
                        f"Action: *{signal['action']}*\n"
                        f"Entry Price: `{signal['price']}`\n"
                        f"Take Profit: `{signal['tp']}`\n"
                        f"Stop Loss: `{signal['sl']}`\n\n"
                        f"📊 Overall Win Rate: *{trade_history['win_rate']}%* ({trade_history['wins']}W / {trade_history['losses']}L)"
                    )

                    edit_telegram_alert(signal["msg_id"], updated_msg, reply_markup=reply_markup)
                    active_signals.remove(signal)
        except Exception as e:
            print(f"Monitor Loop Error: {e}")
            
        time.sleep(5)  # Fast 5-sec trade check for scalping

threading.Thread(target=monitor_active_trades, daemon=True).start()

# --- ROUTES ---
@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "1M Ultra Scalping Auto-Scanner Active 🚀"}), 200

@app.route('/api/stats', methods=['GET'])
def get_stats():
    return jsonify({
        "performance": trade_history,
        "active_signals_count": len(active_signals)
    }), 200

@app.route('/api/latest_signal', methods=['GET'])
def get_latest_signal():
    # Dedicated endpoint for future MT4/MT5 / Python Auto-Trading bot integration
    return jsonify({
        "status": "success",
        "latest_signal": latest_signal_data,
        "active_signals": active_signals
    }), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
