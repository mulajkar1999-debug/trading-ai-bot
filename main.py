from datetime import datetime
import os
import threading
import time
from flask import Flask, jsonify, render_template_string
import requests

# ==========================================
# 1. CONFIGURATION (CREDENTIALS ADDED)
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "8723192534:AAFqkexJpF-yu38dPI0cEUT6H0nooN_sjdM"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "1317739622")

# ==========================================
# 2. GLOBAL MEMORY & DATA STORES
# ==========================================
recent_telegram_messages = (
    []
)  # Telegram messages ko browser/API par dikhane ke liye
MAX_MESSAGE_HISTORY = 30  # Maximum 30 messages history me rahenge

bot_stats = {
    "status": "Online",
    "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
    "total_signals": 0,
    "wins": 0,
    "losses": 0,
    "last_scan": "Initializing...",
}

# Live pairs tracking for Dashboard
market_pairs = {
    "BTCUSDT": {
        "price": "65,200",
        "trend_daily": "BULLISH",
        "trend_1h": "BULLISH",
        "structure": "SMC CHOCH High",
    },
    "ETHUSDT": {
        "price": "3,450",
        "trend_daily": "BULLISH",
        "trend_1h": "BEARISH",
        "structure": "Liquidity Sweep",
    },
    "SOLUSDT": {
        "price": "145.50",
        "trend_daily": "BEARISH",
        "trend_1h": "BEARISH",
        "structure": "OB Mitigation",
    },
}

# ==========================================
# 3. TELEGRAM SENDER WITH MESSAGE LOGGER
# ==========================================


def send_telegram_message(message_text):
    """Telegram par message bhejta hai aur /api/stats ke liye memory me save karta hai."""
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

    # 1. Telegram API Request
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "HTML",
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

    # 2. Memory me save karein (Web Dashboard aur /api/stats ke liye)
    msg_entry = {"timestamp": timestamp_str, "message": message_text}

    recent_telegram_messages.insert(0, msg_entry)  # Latest message top par

    if len(recent_telegram_messages) > MAX_MESSAGE_HISTORY:
        recent_telegram_messages.pop()

    bot_stats["total_signals"] += 1


# ==========================================
# 4. BACKGROUND TRADING BOT ENGINE
# ==========================================


def background_trading_scanner():
    """Ye thread background me bina ruke market scan karta rehta hai."""
    print("🚀 Background Trading Scanner Started...")

    # Startup Notification Message
    startup_msg = (
        "<b>🟢 Trading AI Bot Started Successfully!</b>\n\n"
        "Scanning markets for SMC CHOCH & Liquidity setups...\n"
        "Live logs now available at /api/stats"
    )
    send_telegram_message(startup_msg)

    while True:
        try:
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")
            bot_stats["last_scan"] = current_time

            # Aapka Market Scanning Logic yahan chalega

            time.sleep(60)  # Har 60 second me scan

        except Exception as e:
            print(f"Scanner Loop Error: {e}")
            time.sleep(10)


# Background Thread Start
scanner_thread = threading.Thread(
    target=background_trading_scanner, daemon=True
)
scanner_thread.start()

# ==========================================
# 5. FLASK WEB APP & API ENDPOINTS
# ==========================================
app = Flask(__name__)

# Dark Themed HTML Dashboard
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="15">
    <title>SMC AI Trading Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #30363d; padding-bottom: 15px; margin-bottom: 25px; }
        .badge { background: #238636; color: white; padding: 5px 12px; border-radius: 20px; font-size: 14px; font-weight: bold; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 30px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 18px; }
        .card h3 { font-size: 13px; color: #8b949e; text-transform: uppercase; margin-bottom: 8px; }
        .card .value { font-size: 24px; font-weight: bold; color: #58a6ff; }
        table { width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; margin-bottom: 30px; }
        th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #30363d; }
        th { background: #21262d; color: #8b949e; font-size: 13px; text-transform: uppercase; }
        .bull { color: #3fb950; font-weight: bold; }
        .bear { color: #f85149; font-weight: bold; }
        .log-box { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 15px; max-height: 400px; overflow-y: auto; }
        .log-item { padding: 10px; border-bottom: 1px solid #21262d; font-family: monospace; font-size: 13px; white-space: pre-wrap; }
        .log-item:last-child { border-bottom: none; }
        .time { color: #8b949e; font-size: 11px; margin-bottom: 4px; }
        a.api-link { color: #58a6ff; text-decoration: none; font-size: 14px; }
        a.api-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>⚡ SMC AI Trading Dashboard</h1>
            <div>
                <span class="badge">● ONLINE</span>
                <a href="/api/stats" class="api-link" style="margin-left: 15px;" target="_blank">View Raw JSON API ↗</a>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <h3>Bot Status</h3>
                <div class="value" style="color:#3fb950;">Active</div>
            </div>
            <div class="card">
                <h3>Total Signals Sent</h3>
                <div class="value">{{ stats.total_signals }}</div>
            </div>
            <div class="card">
                <h3>Last Scan Time</h3>
                <div class="value" style="font-size:16px; margin-top:5px;">{{ stats.last_scan }}</div>
            </div>
            <div class="card">
                <h3>Messages Cached</h3>
                <div class="value">{{ messages|length }}</div>
            </div>
        </div>

        <h2>📊 Live Market Matrix</h2>
        <table style="margin-top: 15px;">
            <thead>
                <tr>
                    <th>Symbol</th>
                    <th>Price</th>
                    <th>Daily Trend</th>
                    <th>1H Trend</th>
                    <th>SMC Structure</th>
                </tr>
            </thead>
            <tbody>
                {% for symbol, data in pairs.items() %}
                <tr>
                    <td><strong>{{ symbol }}</strong></td>
                    <td>${{ data.price }}</td>
                    <td class="{{ 'bull' if data.trend_daily == 'BULLISH' else 'bear' }}">{{ data.trend_daily }}</td>
                    <td class="{{ 'bull' if data.trend_1h == 'BULLISH' else 'bear' }}">{{ data.trend_1h }}</td>
                    <td><span style="background: #21262d; padding: 3px 8px; border-radius: 4px; font-size: 12px;">{{ data.structure }}</span></td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <h2>📱 Recent Telegram Messages Log</h2>
        <div class="log-box" style="margin-top: 15px;">
            {% if messages %}
                {% for msg in messages %}
                <div class="log-item">
                    <div class="time">⏰ {{ msg.timestamp }}</div>
                    <div>{{ msg.message | safe }}</div>
                </div>
                {% endfor %}
            {% else %}
                <div class="log-item" style="color: #8b949e;">No Telegram messages logged yet...</div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""


# ------------------------------------------
# ROUTE 1: Home Dashboard Page (HTML)
# ------------------------------------------
@app.route("/")
def home_dashboard():
    return render_template_string(
        DASHBOARD_HTML,
        stats=bot_stats,
        pairs=market_pairs,
        messages=recent_telegram_messages,
    )


# ------------------------------------------
# ROUTE 2: API Stats Endpoint (JSON)
# ------------------------------------------
@app.route("/api/stats")
def api_stats():
    return jsonify({
        "status": bot_stats["status"],
        "bot_started_at": bot_stats["started_at"],
        "last_scan_time": bot_stats["last_scan"],
        "total_telegram_signals_sent": bot_stats["total_signals"],
        "total_messages_stored": len(recent_telegram_messages),
        "recent_telegram_messages": recent_telegram_messages,
    })


# ==========================================
# 6. SERVER STARTUP
# ==========================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
