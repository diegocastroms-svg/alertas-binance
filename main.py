import asyncio
import aiohttp
import time
import threading
import requests
import statistics
from flask import Flask
from dotenv import load_dotenv
import os

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.binance.com/api/v3"
TIMEFRAMES = ["5m", "15m"]
TOP_PAIRS = []
LAST_ALERTS = {}
COOLDOWN = 15 * 60  # 15 minutos

app = Flask(__name__)

def send_message(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("Telegram não configurado.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": text})
    except Exception as e:
        print("Erro ao enviar mensagem:", e)

async def get_top_pairs():
    global TOP_PAIRS
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/ticker/24hr") as resp:
            data = await resp.json()
    data = [d for d in data if d["symbol"].endswith("USDT")]
    data.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    TOP_PAIRS = [d["symbol"] for d in data[:50]]
    print("Top 50 pares atualizados.")

async def get_klines(symbol, interval, limit=100):
    url = f"{BASE_URL}/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()

def ema(values, period):
    if len(values) < period:
        return []
    result = []
    k = 2 / (period + 1)
    result.append(statistics.mean(values[:period]))
    for price in values[period:]:
        result.append(price * k + result[-1] * (1 - k))
    return result

async def analyze(symbol, tf):
    try:
        candles = await get_klines(symbol, tf, 200)
        closes = [float(c[4]) for c in candles]
        ema9 = ema(closes, 9)
        ma20 = ema(closes, 20)
        ma50 = ema(closes, 50)
        ma200 = ema(closes, 200)
        rsi = statistics.mean(closes[-14:]) / statistics.mean(closes[-28:-14]) * 50  # simplificado
        price = closes[-1]

        text_alert = None

        if ema9[-1] > ma20[-1] > ma50[-1] and price < ma200[-1]:
            text_alert = f"{symbol} ({tf}) - Pré-confirmação de alta"
        elif ema9[-1] > ma200[-1] and ema9[-2] < ma200[-2]:
            text_alert = f"{symbol} ({tf}) - Tendência iniciando"
        elif price < ma20[-1] and price > ma200[-1]:
            text_alert = f"{symbol} ({tf}) - Pullback em andamento"

        elif price < ma200[-1] and ema9[-1] < ma20[-1]:
            text_alert = f"{symbol} ({tf}) - Queda e lateralização detectadas"

        if text_alert:
            now = time.time()
            key = f"{symbol}_{tf}"
            if key not in LAST_ALERTS or now - LAST_ALERTS[key] > COOLDOWN:
                send_message(text_alert)
                LAST_ALERTS[key] = now

    except Exception as e:
        print(f"Erro analisando {symbol}: {e}")

async def main_loop():
    await get_top_pairs()
    while True:
        tasks = []
        for tf in TIMEFRAMES:
            for s in TOP_PAIRS:
                tasks.append(analyze(s, tf))
        await asyncio.gather(*tasks)
        await asyncio.sleep(300)  # 5 minutos entre ciclos

def start_flask():
    @app.route('/')
    def index():
        return "Bot de monitoramento ativo"
    app.run(host="0.0.0.0", port=10000)

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    asyncio.run(main_loop())
