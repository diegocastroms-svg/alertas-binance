import os
import time
import datetime
import threading
import requests
from flask import Flask

# ========== CONFIGURAÇÕES ==========
TELEGRAM_TOKEN = "SEU_TOKEN_DO_BOT"
TELEGRAM_CHAT_ID = "SEU_CHAT_ID"  # ex: -1001234567890
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"]
INTERVAL = 120  # segundos entre verificações

app = Flask(__name__)

# ========== FUNÇÕES ==========

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        r = requests.post(url, data=data, timeout=10)
        print(f"[TG] {r.status_code} {r.text[:60]}")
    except Exception as e:
        print(f"[TG] erro: {e}")

def get_price(symbol):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=10,
        )
        data = r.json()
        return float(data["price"])
    except Exception as e:
        print(f"[BINANCE] erro {symbol}: {e}")
        return None

def monitor():
    print("🚀 Monitor iniciado.")
    send_telegram("🤖 Monitor Binance iniciado e rodando no Render!")
    last_prices = {}

    while True:
        for s in SYMBOLS:
            price = get_price(s)
            if not price:
                continue

            last = last_prices.get(s)
            if last:
                diff = ((price - last) / last) * 100
                if abs(diff) >= 0.5:
                    msg = f"{datetime.datetime.now():%H:%M:%S} | {s}: {price:.2f} USD ({diff:+.2f}%)"
                    print(msg)
                    send_telegram(msg)
            last_prices[s] = price

        time.sleep(INTERVAL)

# ========== FLASK ==========
@app.route("/")
def home():
    return "✅ Monitor Binance ativo no Render."

if __name__ == "__main__":
    threading.Thread(target=monitor, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
