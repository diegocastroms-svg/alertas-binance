import asyncio
import aiohttp
import os
import time
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask
import math

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

app = Flask(__name__)

COOLDOWN_TIME = 15 * 60  # 15 minutos
PAIR_LIMIT = 50

last_alert_times = {}
last_volume_check = 0

# =========================
# 📈 Funções auxiliares
# =========================
def binance_pair_link(pair, interval):
    base = pair.replace("USDT", "")
    return f"https://www.binance.com/en/trade/{base}_USDT?type=spot"

def current_time():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M")

async def send_telegram_message(session, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    try:
        async with session.post(url, data=payload) as response:
            if response.status != 200:
                print(f"Erro ao enviar mensagem: {await response.text()}")
    except Exception as e:
        print(f"Erro Telegram: {e}")

# =========================
# 🔍 Coleta de dados Binance
# =========================
async def fetch_json(session, url):
    try:
        async with session.get(url) as response:
            return await response.json()
    except Exception as e:
        print(f"Erro ao buscar dados: {e}")
        return None

async def get_spot_pairs(session):
    url = "https://api.binance.com/api/v3/exchangeInfo"
    data = await fetch_json(session, url)
    if not data:
        return []
    return [s["symbol"] for s in data["symbols"] if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]

async def get_top_volume_pairs(session):
    global last_volume_check
    if time.time() - last_volume_check < 300 and last_alert_times:
        return list(last_alert_times.keys())[:PAIR_LIMIT]
    url = "https://api.binance.com/api/v3/ticker/24hr"
    data = await fetch_json(session, url)
    if not data:
        return []
    usdt_pairs = [d for d in data if d["symbol"].endswith("USDT")]
    sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
    top_pairs = [p["symbol"] for p in sorted_pairs[:PAIR_LIMIT]]
    last_volume_check = time.time()
    return top_pairs

async def get_klines(session, symbol, interval="5m", limit=200):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = await fetch_json(session, url)
    return data if data else []

# =========================
# 📊 Cálculos
# =========================
def calculate_ema(data, period):
    k = 2 / (period + 1)
    ema = []
    for i, price in enumerate(data):
        if i == 0:
            ema.append(price)
        else:
            ema.append(price * k + ema[-1] * (1 - k))
    return ema

def calculate_rsi(data, period=14):
    gains, losses = [], []
    for i in range(1, len(data)):
        change = data[i] - data[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi_values = []
    for i in range(period, len(data)):
        gain = gains[i - 1]
        loss = losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else math.inf
        rsi_values.append(100 - (100 / (1 + rs)))
    return rsi_values[-1] if rsi_values else 50

# =========================
# 🚀 Verificação de sinais
# =========================
async def analyze_pair(session, symbol):
    data = await get_klines(session, symbol, "5m")
    if len(data) < 100:
        return None

    closes = [float(c[4]) for c in data]
    ema9 = calculate_ema(closes, 9)
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema200 = calculate_ema(closes, 200)
    rsi = calculate_rsi(closes, 14)
    price = closes[-1]

    msg = None
    now = current_time()

    # PUMP 5m — PRÉ-CONFIRMAÇÃO
    if ema9[-1] > ema20[-1] > ema50[-1] and price < ema200[-1] and rsi > 55:
        msg = f"🟢 [PUMP 5m — PRÉ-CONFIRMAÇÃO]\n{symbol}\nEMA9>MA20>MA50 com preço abaixo da MA200\nRSI(5m)={rsi:.1f}\n💰 Preço: {price}\n⏰ {now}\n🔗 <a href='{binance_pair_link(symbol, '5m')}'>Ver gráfico 5m no app da Binance</a>"

    # PUMP 5m (normal)
    elif ema9[-1] > ema20[-1] and rsi > 55:
        msg = f"🚀 [PUMP 5m] {symbol}\nEMA9>MA20 • RSI={rsi:.1f}\n💰 Preço: {price}\n🔗 <a href='{binance_pair_link(symbol, '5m')}'>Ver gráfico 5m no app da Binance</a>"

    if msg:
        if symbol not in last_alert_times or time.time() - last_alert_times[symbol] > COOLDOWN_TIME:
            await send_telegram_message(session, msg)
            last_alert_times[symbol] = time.time()

# =========================
# 🔁 Loop principal
# =========================
async def main():
    await asyncio.sleep(5)
    async with aiohttp.ClientSession() as session:
        pairs = await get_top_volume_pairs(session)
        print(f"Pares carregados (TOP {PAIR_LIMIT}):", pairs)
        while True:
            for symbol in pairs:
                try:
                    await analyze_pair(session, symbol)
                except Exception as e:
                    print(f"Erro em {symbol}: {e}")
            await asyncio.sleep(60)

@app.route("/")
def home():
    return "Bot ativo ✅"

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)
