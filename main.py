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

def get_market_klines(asset_name, interval="15m", limit=210):
    headers = {'User-Agent': 'Mozilla/5.0'}
    product_id = "BTC-USD" if asset_name == "BTC" else "PAXG-USD"
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
    return round(max(atr_val, 1.0), 2)

def is_high_impact_news_active():
    try:
        now_utc = datetime.now(pytz.utc)
        current_minute = now_utc.hour * 60 + now_utc.minute
        news_windows = [(740, 820), (800, 880), (1070, 1150)]
        
        if now_utc.weekday() in [2, 3, 4]:
            for start, end in news_windows:
                if start <= current_minute <= end:
                    return True, "HIGH IMPACT NEWS PAUSE ⚠️"
    except Exception as e:
        print(f"News Filter Error: {e}")
    return False, "NO NEWS 🟢"

def detect_smc_liquidity_grab(highs, lows, closes):
    if len(closes) < 20:
        return False, "BALANCED"
    recent_high = max(highs[-20:-2])
    recent_low = min(lows[-20:-2])
    current_high, current_low, current_close = highs[-1], lows[-1], closes[-1]
    
    if current_high > recent_high and current_close < recent_high:
        return True, "BEARISH SWEEP 🔴"
    if current_low < recent_low and current_close > recent_low:
        return True, "BULLISH SWEEP 🟢"
    return False, "BALANCED"

def get_current_session_info():
    tz_ist = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(tz_ist)
    hour = now_ist.hour
    if 13 <= hour or hour < 3:
        return "LONDON/NY SESSION 🔥", 85
    return "ASIAN SESSION 🟡", 90

def update_win_rate():
    total = trade_history["wins"] + trade_history["losses"]
    trade_history["total_signals"] = total
    if total > 0:
        trade_history["win_rate"] = round((trade_history["wins"] / total) * 100, 2)

# --- Background Worker: Price Monitor ---
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
                half_tp = entry_p + (tp_dist * 0.5) if is_buy else entry_p - (tp_dist * 0.5)

                # Break-Even Logic
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

                # TP or SL Hit
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
        
        is_news, news_reason = is_high_impact_news_active()
        if is_news:
            return {"status": "PAUSED", "reason": news_reason}

        closes_1h, _, _, _, _ = get_market_klines(clean_asset, interval="1h", limit=210)
        closes_15m, opens_15m, highs_15m, lows_15m, volumes_15m = get_market_klines(clean_asset, interval="15m", limit=210)
        
        if not closes_15m or len(closes_15m) < 14 or not closes_1h or len(closes_1h) < 14:
            return None

        current_price = closes_15m[-1]
        session_name, required_score_threshold = get_current_session_info()

        ema200_1h = calculate_ema(closes_1h, 200) if len(closes_1h) >= 200 else calculate_ema(closes_1h, len(closes_1h))
        macro_bullish_1h = current_price >= ema200_1h
        smc_grab, smc_detail = detect_smc_liquidity_grab(highs_15m, lows_15m, closes_15m)

        if clean_asset == "BTC":
            ema_fast = calculate_ema(closes_15m, 12)
            ema_slow = calculate_ema(closes_15m, 26)
            rsi = calculate_rsi(closes_15m, 14)
            atr = calculate_atr(highs_15m, lows_15m, closes_15m, 14)
            
            score = 0
            action = "WAIT / NO CLEAR ENTRY 🟡"
            
            if macro_bullish_1h:
                score += 30
                if ema_fast > ema_slow: score += 25
                if 50 <= rsi <= 72: score += 20
                if smc_grab and "BULLISH" in smc_detail: score += 20
                if score >= required_score_threshold: action = "INSTITUTIONAL BUY 🟢"
            else:
                score += 30
                if ema_fast < ema_slow: score += 25
                if 28 <= rsi <= 50: score += 20
                if smc_grab and "BEARISH" in smc_detail: score += 20
                if score >= required_score_threshold: action = "INSTITUTIONAL SELL 🔴"

        else:
            ema9 = calculate_ema(closes_15m, 9)
            ema21 = calculate_ema(closes_15m, 21)
            rsi = calculate_rsi(closes_15m, 14)
            atr = calculate_atr(highs_15m, lows_15m, closes_15m, 14)
            
            score = 0
            action = "WAIT / NO CLEAR ENTRY 🟡"
            
            if macro_bullish_1h:
                score += 30
                if ema9 > ema21: score += 25
                if 48 <= rsi <= 67: score += 20
                if smc_grab and "BULLISH" in smc_detail: score += 20
                if score >= required_score_threshold: action = "INSTITUTIONAL BUY 🟢"
            else:
                score += 30
                if ema9 < ema21: score += 25
                if 33 <= rsi <= 52: score += 20
                if smc_grab and "BEARISH" in smc_detail: score += 20
                if score >= required_score_threshold: action = "INSTITUTIONAL SELL 🔴"

        sl_distance = round(atr * 1.5, 2)
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
            "required_threshold": required_score_threshold,
            "session": session_name,
            "smc_liquidity": smc_detail,
            "rsi": rsi,
            "atr": atr,
            "htf_alignment": "BULLISH (1H) 📈" if macro_bullish_1h else "BEARISH (1H) 📉",
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
    return jsonify({"status": "Institutional Trading Engine Active 🚀"}), 200

@app.route('/api/stats', methods=['GET'])
def get_stats():
    return jsonify({
        "performance": trade_history,
        "active_signals_count": len(active_signals)
    }), 200

@app.route('/api/signal', methods=['GET'])
def get_signal():
    asset = request.args.get('asset', 'GOLD').upper()
    data = analyze_asset(asset)
    
    if not data:
        return jsonify({"status": "Connecting Live feed... Please refresh."}), 200

    if data.get("status") == "PAUSED":
        return jsonify(data), 200

    # --- FIX 1: DUPLICATE GUARD (Only 1 active trade per asset) ---
    existing = [s for s in active_signals if s['asset'] == data['asset']]
    if existing:
        return jsonify({"status": f"Signal already active for {data['asset']}. Duplicate blocked."}), 200

    if data["score"] >= data["required_threshold"]:
        tz_ist = pytz.timezone('Asia/Kolkata')
        signal_time = datetime.now(tz_ist).strftime("%I:%M %p | %d %b")

        chart_symbol = "OANDA:XAUUSD" if data['asset'] == "XAUUSD" else "COINBASE:BTCUSD"
        chart_link = f"https://www.tradingview.com/chart/?symbol={chart_symbol}"
        
        reply_markup = {
            "inline_keyboard": [
                [{"text": "📈 Open TradingView Chart", "url": chart_link}]
            ]
        }

        # --- FIX 2 & 3: CLEAN CARD WITH TIME STAMP ---
        alert_msg = (
            f"🚀 *PRO ALGO SIGNAL ({data['asset']})*\n"
            f"⏰ Time: `{signal_time}`\n\n"
            f"Status: *ACTIVE ⏳*\n"
            f"Action: *{data['action']}*\n"
            f"Entry Price: `{data['price']}`\n"
            f"Take Profit (TP): `{data['tp']}` (R:R 1:2 🎯)\n"
            f"Stop Loss (SL): `{data['sl']}` (Dynamic ATR 🛡️)\n\n"
            f"🧠 *Score:* *{data['score']}%* | Session: `{data['session']}`\n"
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

    return jsonify(data), 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
