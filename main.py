import os
import time
import threading
import requests
from datetime import datetime, timedelta
import pytz
from flask import Flask, jsonify

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1317739622")

active_signals = []
latest_signal_data = {}
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

def get_market_klines(asset_name, interval="5m", limit=30):
    headers = {'User-Agent': 'Mozilla/5.0'}
    product_id = "PAXG-USD" if asset_name in ["GOLD", "XAUUSD"] else "BTC-USD"
    granularity = 300 if interval == "5m" else 900
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?granularity={granularity}"

    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, list) and len(data) >= 10:
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

def update_win_rate():
    total = trade_history["wins"] + trade_history["losses"]
    trade_history["total_signals"] = total
    if total > 0:
        trade_history["win_rate"] = round((trade_history["wins"] / total) * 100, 2)

def analyze_asset(asset_name):
    global latest_signal_data
    try:
        clean_asset = "GOLD" if asset_name in ["GOLD", "XAUUSD"] else "BTC"

        closes, opens, highs, lows, volumes = get_market_klines(clean_asset, interval="5m", limit=20)
        if not closes or len(closes) < 10:
            return None

        current_price = closes[-1]
        prev_high = max(highs[-6:-1])
        prev_low = min(lows[-6:-1])
        
        avg_volume = sum(volumes[-6:-1]) / 5
        curr_volume = volumes[-1]

        action = "NONE"
        score = 0

        # Strict Breakout Logic (Higher Volume Multiplier to Stop False Signals)
        if current_price > prev_high and closes[-1] > opens[-1] and curr_volume > avg_volume * 1.3:
            action = "HIGH PROBABILITY SCALP BUY 🟢"
            score = 88

        elif current_price < prev_low and closes[-1] < opens[-1] and curr_volume > avg_volume * 1.3:
            action = "HIGH PROBABILITY SCALP SELL 🔴"
            score = 88

        if score < 88:
            return None

        # BALANCED VOLATILITY TARGETS (Spread-Safe)
        if clean_asset == "GOLD":
            tp_dist = 1.50  # $1.50 Target (15 Pips)
            sl_dist = 0.80  # $0.80 SL (8 Pips - Spread Safe)
            recommended_lot = 0.03
            display_pair = "XAUUSD"
        else:
            tp_dist = 120.0  # $120 Target
            sl_dist = 70.0   # $70 SL
            recommended_lot = 0.01
            display_pair = "BTCUSD"

        tp = round(current_price + tp_dist if "BUY" in action else current_price - tp_dist, 2)
        sl = round(current_price - sl_dist if "BUY" in action else current_price + sl_dist, 2)

        signal_obj = {
            "asset": display_pair,
            "price": round(current_price, 2),
            "action": action,
            "score": score,
            "tp": tp,
            "sl": sl,
            "recommended_lot": recommended_lot
        }

        latest_signal_data = signal_obj
        return signal_obj

    except Exception as e:
        print(f"Error in analyze_asset: {e}")
        return None

def analyze_and_trigger(asset_key):
    now = datetime.now()
    if last_signal_time[asset_key] and (now - last_signal_time[asset_key]) < timedelta(minutes=5):
        return

    data = analyze_asset(asset_key)
    if not data:
        return

    existing = [s for s in active_signals if s['asset'] == data['asset']]
    if existing:
        return

    tz_ist = pytz.timezone('Asia/Kolkata')
    signal_time = datetime.now(tz_ist).strftime("%I:%M %p | %d %b")

    chart_symbol = "PAXGUSD" if data['asset'] == "XAUUSD" else "COINBASE:BTCUSD"
    chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"
    
    reply_markup = {"inline_keyboard": [[{"text": "📈 TradingView Chart", "url": chart_link}]]}

    alert_msg = (
        f"🎯 *ACCURATE 5M BREAKOUT ({data['asset']})*\n"
        f"⏰ Time: `{signal_time}`\n\n"
        f"Status: *ACTIVE ⏳*\n"
        f"Action: *{data['action']}*\n"
        f"Entry Price: `{data['price']}`\n"
        f"Take Profit (TP): `{data['tp']}`\n"
        f"Stop Loss (SL): `{data['sl']}` *(Spread Safe)*\n\n"
        f"🧠 *Signal Score:* *{data['score']}%*\n"
        f"🛡️ *Recommended Lot:* `{data['recommended_lot']}`"
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
            "created_at": signal_time
        })

def continuous_auto_scanner():
    while True:
        try:
            for asset in ["GOLD", "BTC"]:
                analyze_and_trigger(asset)
                time.sleep(2)
        except Exception as e:
            print(f"Auto Scanner Error: {e}")
        time.sleep(10)

threading.Thread(target=continuous_auto_scanner, daemon=True).start()

def monitor_active_trades():
    while True:
        try:
            for signal in list(active_signals):
                asset_code = "BTC" if "BTC" in signal["asset"] else "GOLD"
                closes, _, highs, lows, _ = get_market_klines(asset_code, interval="5m", limit=3)
                if not closes:
                    continue
                
                curr_high, curr_low = highs[-1], lows[-1]
                is_buy = "BUY" in signal["action"]

                tp_hit = curr_high >= signal["tp"] if is_buy else curr_low <= signal["tp"]
                sl_hit = curr_low <= signal["sl"] if is_buy else curr_high >= signal["sl"]

                if tp_hit or sl_hit:
                    if tp_hit:
                        status_text = "✅ TARGET HIT (WIN) 🎯"
                        trade_history["wins"] += 1
                    else:
                        status_text = "❌ STOP LOSS HIT (LOSS) 🛑"
                        trade_history["losses"] += 1
                    
                    update_win_rate()

                    chart_symbol = "PAXGUSD" if signal['asset'] == "XAUUSD" else "COINBASE:BTCUSD"
                    chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"
                    reply_markup = {"inline_keyboard": [[{"text": "📈 TradingView Chart", "url": chart_link}]]}

                    updated_msg = (
                        f"🎯 *RESULT UPDATE ({signal['asset']})*\n"
                        f"⏰ Time: `{signal['created_at']}`\n\n"
                        f"Status: *{status_text}*\n"
                        f"Action: *{signal['action']}*\n"
                        f"Entry Price: `{signal['price']}`\n"
                        f"Take Profit: `{signal['tp']}`\n"
                        f"Stop Loss: `{signal['sl']}`\n\n"
                        f"📊 Win Rate: *{trade_history['win_rate']}%* ({trade_history['wins']}W / {trade_history['losses']}L)"
                    )

                    edit_telegram_alert(signal["msg_id"], updated_msg, reply_markup=reply_markup)
                    active_signals.remove(signal)
        except Exception as e:
            print(f"Monitor Loop Error: {e}")
            
        time.sleep(3)

threading.Thread(target=monitor_active_trades, daemon=True).start()

@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "Spread Safe Scalper Active 🚀"}), 200

@app.route('/api/stats', methods=['GET'])
def get_stats():
    return jsonify({"performance": trade_history, "active_signals_count": len(active_signals)}), 200

@app.route('/api/latest_signal', methods=['GET'])
def get_latest_signal():
    return jsonify({"status": "success", "latest_signal": latest_signal_data, "active_signals": active_signals}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
