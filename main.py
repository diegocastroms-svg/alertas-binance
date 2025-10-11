import asyncio
import aiohttp
import os
import time
import math
from datetime import datetime
from threading import Thread
from flask import Flask
from dotenv import load_dotenv

# ======================================
# CONFIGURAÇÕES INICIAIS
# ======================================
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
COOLDOWN = 900  # 15 minutos
TOP_LIMIT = 50  # top 50 por volume
UPDATE_INTERVAL = 3600  # 1 hora para atualizar a lista
BASE_URL = "https://api.binance.com/api/v3"
app = Flask(__name__)

# Controle de tempo entre alertas
last_alert_time = {}
top_pairs = []

# ======================================
# FUNÇÃO: ENVIAR MENSAGEM TELEGRAM
# ======================================
async def send_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=payload) as resp:
                return await resp.text()
    except Exception as e:
        print("Erro ao enviar mensagem:", e)

# ======================================
# FUNÇÃO: OBTER PARES SPOT USDT
# ======================================
async def get_spot_pairs():
    global top_pairs
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BASE_URL}/ticker/24hr") as resp:
                data = await resp.json()
                usdt_pairs = [
                    item for item in data
                    if item["symbol"].endswith("USDT")
                    and not any(x in item["symbol"] for x in ["UP", "DOWN", "BULL", "BEAR", "PERP", "2L", "3L", "4L"])
                ]
                sorted_pairs = sorted(usdt_pairs, key=lambda x: float(x["quoteVolume"]), reverse=True)
                top_pairs = [p["symbol"] for p in sorted_pairs[:TOP_LIMIT]]
                print(f"✅ Atualizada lista de {len(top_pairs)} pares SPOT.")
    except Exception as e:
        print("Erro ao obter pares:", e)

# ======================================
# FUNÇÃO: OBTER DADOS DE VELA
# ======================================
async def get_klines(session, symbol, interval="5m", limit=100):
    try:
        url = f"{BASE_URL}/klines?symbol={symbol}&interval={interval}&limit={limit}"
        async with session.get(url) as resp:
            return await resp.json()
    except:
        return []

# ======================================
# CÁLCULO DE MÉDIAS MÓVEIS
# ======================================
def moving_average(data, period):
    if len(data) < period:
        return []
    return [sum(data[i-period:i]) / period for i in range(period, len(data)+1)]

def exponential_moving_average(data, period):
    ema = []
    k = 2 / (period + 1)
    for i, price in enumerate(data):
        if i == 0:
            ema.append(price)
        else:
            ema.append(price * k + ema[-1] * (1 - k))
    return ema

# ======================================
# FUNÇÃO: MONITORAR MERCADO
# ======================================
async def analyze_market(session, symbol):
    try:
        klines = await get_klines(session, symbol, "5m", 200)
        closes = [float(x[4]) for x in klines]
        if len(closes) < 200:
            return

        ema9 = exponential_moving_average(closes, 9)
        ma20 = moving_average(closes, 20)
        ma50 = moving_average(closes, 50)
        ma200 = moving_average(closes, 200)

        rsi = calc_rsi(closes)
        price = closes[-1]

        # ===============================
        # ALERTA 1 — TENDÊNCIA INICIANDO (5m)
        # ===============================
        if (
            ema9[-1] > ma20[-1] > ma50[-1]
            and price > ma200[-1]
            and rsi > 50
        ):
            await trigger_alert(symbol, "🚀 TENDÊNCIA INICIANDO (5m)", ema9, ma20, ma50, ma200, price, rsi)

        # ===============================
        # ALERTA 2 — PRÉ-CONFIRMAÇÃO (5m)
        # ===============================
        if (
            ema9[-1] > ma20[-1] > ma50[-1]
            and price < ma200[-1]
            and rsi > 55
        ):
            await trigger_alert(symbol, "🟢 PRÉ-CONFIRMAÇÃO (5m)", ema9, ma20, ma50, ma200, price, rsi)

        # ===============================
        # ALERTA 3 — PULLBACK (15m)
        # ===============================
        klines_15m = await get_klines(session, symbol, "15m", 200)
        closes_15m = [float(x[4]) for x in klines_15m]
        ema9_15m = exponential_moving_average(closes_15m, 9)
        ma20_15m = moving_average(closes_15m, 20)
        ma50_15m = moving_average(closes_15m, 50)
        ma200_15m = moving_average(closes_15m, 200)
        rsi_15m = calc_rsi(closes_15m)

        if (
            ma20_15m[-1] > ma50_15m[-1]
            and ema9_15m[-1] > ma20_15m[-1]
            and closes_15m[-1] > ma200_15m[-1]
        ):
            await trigger_alert(symbol, "📈 PULLBACK (15m)", ema9_15m, ma20_15m, ma50_15m, ma200_15m, closes_15m[-1], rsi_15m)

    except Exception as e:
        print(f"Erro ao analisar {symbol}: {e}")

# ======================================
# RSI
# ======================================
def calc_rsi(data, period=14):
    if len(data) < period + 1:
        return 0
    gains, losses = [], []
    for i in range(1, len(data)):
        delta = data[i] - data[i - 1]
        gains.append(max(delta, 0))
        losses.append(abs(min(delta, 0)))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ======================================
# ALERTA GERAL
# ======================================
async def trigger_alert(symbol, alert_type, ema9, ma20, ma50, ma200, price, rsi):
    now = time.time()
    if symbol in last_alert_time and now - last_alert_time[symbol] < COOLDOWN:
        return
    last_alert_time[symbol] = now

    hora_brasil = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    text = (
        f"{alert_type}\n\n"
        f"<b>{symbol}</b>\n"
        f"📊 EMA9: {ema9[-1]:.5f} | MA20: {ma20[-1]:.5f} | MA50: {ma50[-1]:.5f}\n"
        f"💫 MA200: {ma200[-1]:.5f}\n"
        f"💰 Preço: {price:.6f}\n"
        f"📈 RSI: {rsi:.1f}\n"
        f"🇧🇷 {hora_brasil}\n"
        f"<a href='https://www.binance.com/en/trade?symbol={symbol}&type=spot'>📎 Ver gráfico 5m no app da Binance</a>"
    )
    await send_message(text)

# ======================================
# LOOP PRINCIPAL
# ======================================
async def run_bot():
    await get_spot_pairs()
    await send_message("✅ BOT ATIVO NO RENDER\n🟢 Monitorando pares SPOT da Binance\n🇧🇷 Cooldown: 15min por par\n")
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                tasks = [analyze_market(session, s) for s in top_pairs]
                await asyncio.gather(*tasks)
            await asyncio.sleep(300)
        except Exception as e:
            print("Erro no loop principal:", e)

# ======================================
# FLASK + THREAD + RENDER PORT FIX
# ======================================
if __name__ == "__main__":
    send_message("♻️ Reiniciando bot no Render... 🇧🇷")

    port = int(os.environ.get("PORT", 5000))
    Thread(target=lambda: app.run(host="0.0.0.0", port=port)).start()

    asyncio.run(run_bot())
