import os
import requests
from datetime import datetime
import pytz
from flask import Flask, request, jsonify

app = Flask(__name__)

# TwelveData API Details
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "154c31601d3b499b847e0dae6efa14fa")
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"


def fetch_gold_data(symbol="XAU/USD", interval="15min", outputsize=50):
    """
    Fetch market data for Gold (XAU/USD) using TwelveData API
    """
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY
    }
    
    try:
        response = requests.get(TWELVEDATA_URL, params=params, timeout=10)
        data = response.json()
        
        if response.status_code == 200 and "values" in data:
            return data["values"]
        else:
            print(f"API Error: {data.get('message', 'Unknown error')}")
            return None
    except Exception as e:
        print(f"Exception during fetching gold data: {e}")
        return None


def calculate_simple_signal(values):
    """
    Generate basic trading signal based on latest candle price comparison
    """
    if not values or len(values) < 2:
        return {"action": "HOLD", "reason": "Insufficient data"}
        
    latest = float(values[0]["close"])
    previous = float(values[1]["close"])
    
    diff = latest - previous
    
    if diff > 0.5:
        return {"action": "BUY", "current_price": latest, "change": round(diff, 2)}
    elif diff < -0.5:
        return {"action": "SELL", "current_price": latest, "change": round(diff, 2)}
    else:
        return {"action": "HOLD", "current_price": latest, "change": round(diff, 2)}


@app.route("/", methods=["GET"])
def home():
    # IST Time Formatting
    ist_tz = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist_tz).strftime("%Y-%m-%d %H:%M:%S IST")
    
    return jsonify({
        "status": "Online",
        "service": "XAU/USD Gold Trading AI Bot",
        "time": now_ist
    })


@app.route("/analyze", methods=["GET"])
def analyze_market():
    values = fetch_gold_data()
    if not values:
        return jsonify({"status": "Error", "message": "Failed to fetch market data from TwelveData"}), 500
        
    signal = calculate_simple_signal(values)
    
    ist_tz = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.now(ist_tz).strftime("%Y-%m-%d %H:%M:%S IST")
    
    return jsonify({
        "timestamp": now_ist,
        "symbol": "XAU/USD",
        "signal": signal
    })


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}
    print(f"Received webhook payload: {data}")
    return jsonify({"status": "Success", "received": data}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
