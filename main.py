import os
import time
import threading
import requests
from datetime import datetime
import pytz
from flask import Flask, jsonify, request

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "1317739622")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "154c31601d3b499b847e0dae6efa14fa")

active_signals = []
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

def get_market_klines(asset_name, interval="15m", limit=100):
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    if asset_name in ["GOLD", "XAUUSD"]:
        twelve_interval = "15min" if interval == "15m" else "1h"
        url = f"https://api.twelvedata.com/time_series?symbol=XAU/USD&interval={twelve_interval}&outputsize={limit}&apikey={TWELVEDATA_API_KEY}"
        try:
            res = requests.get(url, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                if "values" in data and len(data["values"]) >= 14:
                    raw_candles = list(reversed(data["values"]))
                    closes = [float(c["close"]) for c in raw_candles]
                    opens = [float(c["open"]) for c in raw_candles]
                    highs = [float(c["high"]) for c in raw_candles]
                    lows = [float(c["low"]) for c in raw_candles]
                    volumes = [float(c.get("volume", 0)) for c in raw_candles]
                    return closes, opens, highs, lows, volumes
        except Exception as e:
            print(f"TwelveData Gold Fetch Error: {e}")

    # Fetch BTC-USD via Coinbase
    product_id = "BTC-USD"
    granularity = 900 if interval == "15m" else 3600
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles?granularity={granularity}"

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
    return round(max(atr_val, 0.5), 2)

def update_win_rate():
    total = trade_history["wins"] + trade_history["losses"]
    trade_history["total_signals"] = total
    if total > 0:
        trade_history["win_rate"] = round((trade_history["wins"] / total) * 100, 2)

def analyze_and_trigger(asset_key):
    data = analyze_asset(asset_key)
    if not data:
        return

    existing = [s for s in active_signals if s['asset'] == data['asset']]
    if existing:
        return

    if data["score"] >= 75:  # Dynamic threshold
        tz_ist = pytz.timezone('Asia/Kolkata')
        signal_time = datetime.now(tz_ist).strftime("%I:%M %p | %d %b")

        chart_symbol = "OANDA:XAUUSD" if data['asset'] == "XAUUSD" else "COINBASE:BTCUSD"
        chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"
        
        reply_markup = {
            "inline_keyboard": [[{"text": "📈 TradingView Chart", "url": chart_link}]]
        }

        alert_msg = (
            f"🚀 *PRO ALGO SIGNAL ({data['asset']})*\n"
            f"⏰ Time: `{signal_time}`\n\n"
            f"Status: *ACTIVE ⏳*\n"
            f"Action: *{data['action']}*\n"
            f"Entry Price: `{data['price']}`\n"
            f"Take Profit (TP): `{data['tp']}` (R:R 1:1.5 🎯)\n"
            f"Stop Loss (SL): `{data['sl']}` (Wide ATR 🛡️)\n\n"
            f"🧠 *Score:* *{data['score']}%*\n"
            f"🛡️ *Lot Size:* `{data['recommended_lot']}`"
        )
        
        msg_id = send_telegram_alert(alert_msg, reply_markup=reply_markup)
        
        if msg_id:
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
    """Background loop that automatically scans both assets every 60s"""
    while True:
        try:
            for asset in ["GOLD", "BTC"]:
                analyze_and_trigger(asset)
                time.sleep(5)
        except Exception as e:
            print(f"Auto Scanner Error: {e}")
        time.sleep(60)

threading.Thread(target=continuous_auto_scanner, daemon=True).start()

def monitor_active_trades():
    while True:
        try:
            for signal in list(active_signals):
                asset_code = "BTC" if "BTC" in signal["asset"] else "GOLD"
                closes, _, highs, lows, _ = get_market_klines(asset_code, interval="15m", limit=5)
                if not closes:
                    continue
                
                curr_high, curr_low = highs[-1], lows[-1]
                is_buy = "BUY" in signal["action"]
                entry_p, tp_p, sl_p = signal["price"], signal["tp"], signal["sl"]
                
                tp_dist = abs(tp_p - entry_p)
                half_tp = entry_p + (tp_dist * 0.6) if is_buy else entry_p - (tp_dist * 0.6)

                if not signal.get("break_even_triggered", False):
                    be_hit = curr_high >= half_tp if is_buy else curr_low <= half_tp
                    if be_hit:
                        signal["break_even_triggered"] = True
                        signal["sl"] = entry_p
                        
                        chart_symbol = "OANDA:XAUUSD" if signal['asset'] == "XAUUSD" else "COINBASE:BTCUSD"
                        chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"
                        reply_markup = {"inline_keyboard": [[{"text": "📈 TradingView Chart", "url": chart_link}]]}

                        be_msg = (
                            f"🚀 *ALGO SIGNAL ({signal['asset']})*\n"
                            f"⏰ Time: `{signal['created_at']}`\n\n"
                            f"Status: *🛡️ BREAK-EVEN (SL SHIFTED TO ENTRY)*\n"
                            f"Action: *{signal['action']}*\n"
                            f"Entry Price: `{signal['price']}`\n"
                            f"Take Profit: `{signal['tp']}`\n"
                            f"Stop Loss: `{entry_p}` *(RISK 0%)* 🔒\n"
                        )
                        edit_telegram_alert(signal["msg_id"], be_msg, reply_markup=reply_markup)

                tp_hit = curr_high >= tp_p if is_buy else curr_low <= tp_p
                sl_hit = curr_low <= signal["sl"] if is_buy else curr_high >= signal["sl"]

                if tp_hit or sl_hit:
                    if tp_hit:
                        status_text = "✅ TARGET HIT (WIN) 🎯"
                        trade_history["wins"] += 1
                    else:
                        if signal.get("break_even_triggered", False):
                            status_text = "🛡️ CLOSED AT BREAK-EVEN (NO LOSS) 🔒"
                        else:
                            status_text = "❌ STOP LOSS HIT (LOSS) 🛑"
                            trade_history["losses"] += 1
                    
                    update_win_rate()

                    chart_symbol = "OANDA:XAUUSD" if signal['asset'] == "XAUUSD" else "COINBASE:BTCUSD"
                    chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"
                    reply_markup = {"inline_keyboard": [[{"text": "📈 TradingView Chart", "url": chart_link}]]}

                    updated_msg = (
                        f"🚀 *ALGO SIGNAL ({signal['asset']})*\n"
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
            
        time.sleep(15)

threading.Thread(target=monitor_active_trades, daemon=True).start()

def analyze_asset(asset_name):
    try:
        clean_asset = "GOLD" if asset_name in ["GOLD", "XAUUSD"] else "BTC"

        closes_15m, opens_15m, highs_15m, lows_15m, volumes_15m = get_market_klines(clean_asset, interval="15m", limit=100)
        
        if not closes_15m or len(closes_15m) < 25:
            return None

        current_price = closes_15m[-1]
        ema9 = calculate_ema(closes_15m, 9)
        ema21 = calculate_ema(closes_15m, 21)
        ema50 = calculate_ema(closes_15m, 50)
        rsi = calculate_rsi(closes_15m, 14)
        atr = calculate_atr(highs_15m, lows_15m, closes_15m, 14)

        score = 0
        action = "WAIT / NO CLEAR ENTRY 🟡"

        # Bullish Conditions
        if current_price > ema50:
            score += 30
            if ema9 > ema21: score += 30
            if 45 <= rsi <= 68: score += 20
            if closes_15m[-1] > opens_15m[-1]: score += 10
            if score >= 75: action = "INSTITUTIONAL BUY 🟢"

        # Bearish Conditions
        elif current_price < ema50:
            score += 30
            if ema9 < ema21: score += 30
            if 32 <= rsi <= 55: score += 20
            if closes_15m[-1] < opens_15m[-1]: score += 10
            if score >= 75: action = "INSTITUTIONAL SELL 🔴"

        # Adjusted Risk-Reward for Wicks
        sl_distance = round(atr * 2.0, 2)
        tp_distance = round(atr * 3.0, 2)

        tp = round(current_price + tp_distance if "BUY" in action else current_price - tp_distance, 2)
        sl = round(current_price - sl_distance if "BUY" in action else current_price + sl_distance, 2)

        recommended_lot = 0.01 if clean_asset == "BTC" else 0.05
        display_pair = "BTCUSD" if clean_asset == "BTC" else "XAUUSD"

        return {
            "asset": display_pair,
            "price": round(current_price, 2),
            "action": action,
            "score": score,
            "rsi": rsi,
            "atr": atr,
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
    return jsonify({"status": "Auto-Scanner Active 🚀"}), 200

@app.route('/api/stats', methods=['GET'])
def get_stats():
    return jsonify({
        "performance": trade_history,
        "active_signals_count": len(active_signals)
    }), 200

@app.route('/api/signal', methods=['GET'])
def get_signal():
    return jsonify({"status": "Auto-Scanner running continuously in background"}), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
