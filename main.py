# ===========================
# 📁 novo_main_v1.1.py
# ===========================
# Autor: Diego Castro Oliveira
# Projeto: Bot de Monitoramento SPOT Binance (com Flask keep-alive)
# ===========================

import os
import asyncio
import threading
from datetime import datetime
from statistics import mean
import aiohttp
from flask import Flask

# -----------------------------
# 🔧 Variáveis de ambiente
# -----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# -----------------------------
# ⚙️ Funções auxiliares
# -----------------------------
async def send_telegram(msg: str):
    """Envia mensagem formatada para o Telegram"""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ Variáveis de ambiente ausentes.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    async with aiohttp.ClientSession() as session:
        await session.post(url, data=payload)

async def get_klines(symbol: str, interval="5m", limit=100):
    """Obtém candles recentes do par"""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.json()

def ma(values, period):
    """Cálculo simples de média móvel"""
    if len(values) < period:
        return None
    return mean(values[-period:])

def rsi(values, period=14):
    """Cálculo simplificado de RSI"""
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[-i] - values[-i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))
    avg_gain = mean(gains) if gains else 0
    avg_loss = mean(losses) if losses else 1e-6
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# -----------------------------
# 📊 Lógica de análise principal
# -----------------------------
async def analyze_pair(symbol):
    try:
        # ----- 5 MINUTOS -----
        data_5m = await get_klines(symbol, "5m", 120)
        closes_5m = [float(c[4]) for c in data_5m]

        ema9 = ma(closes_5m, 9)
        ma20 = ma(closes_5m, 20)
        ma50 = ma(closes_5m, 50)
        ma200 = ma(closes_5m, 200)
        rsi_5m = rsi(closes_5m)

        last_price = closes_5m[-1]
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # 1️⃣ Queda + Lateralização
        if closes_5m[-3] > closes_5m[-2] > closes_5m[-1] and abs(closes_5m[-1] - closes_5m[-3]) < 0.002 * closes_5m[-1]:
            msg = f"📉 *MERCADO EM QUEDA — {symbol} (5m)*\n⏸️ Lateralizando após queda\n📊 Em queda, monitorando possível alta\n💰 Preço atual: {last_price}\n🕒 {now}"
            await send_telegram(msg)

        # 2️⃣ EMA9 cruza MA20/MA50 → tendência de alta
        if ema9 and ma20 and ma50 and ema9 > ma20 > ma50:
            msg = f"🚀 *TENDÊNCIA DE ALTA INICIADA — {symbol} (5m)*\n📈 EMA9 cruzou acima das MA20 e MA50\n💰 Preço atual: {last_price}\n🕒 {now}"
            await send_telegram(msg)

        # 3️⃣ EMA9 + MA20 + MA50 acima da MA200 → pré-confirmada
        if ema9 and ma20 and ma50 and ma200 and ema9 > ma200 and ma20 > ma200 and ma50 > ma200:
            msg = f"⚡ *TENDÊNCIA PRÉ-CONFIRMADA — {symbol} (5m)*\n📈 EMA9, MA20 e MA50 cruzaram acima da MA200\n💰 Preço atual: {last_price}\n🕒 {now}"
            await send_telegram(msg)

        # ----- 15 MINUTOS -----
        data_15m = await get_klines(symbol, "15m", 120)
        closes_15m = [float(c[4]) for c in data_15m]

        ema9_15 = ma(closes_15m, 9)
        ma20_15 = ma(closes_15m, 20)
        ma50_15 = ma(closes_15m, 50)
        ma200_15 = ma(closes_15m, 200)
        rsi_15 = rsi(closes_15m)

        last_price_15 = closes_15m[-1]

        # 4️⃣ EMA9 cruza MA200 → pré-confirmação
        if ema9_15 and ma200_15 and ema9_15 > ma200_15:
            msg = f"⚡ *TENDÊNCIA PRÉ-CONFIRMADA — {symbol} (15m)*\n📈 EMA9 cruzou acima da MA200\n💰 Preço atual: {last_price_15}\n🕒 {now}"
            await send_telegram(msg)

        # 5️⃣ MA20 + MA50 cruzam MA200 → tendência confirmada
        if ma20_15 and ma50_15 and ma200_15 and ma20_15 > ma200_15 and ma50_15 > ma200_15:
            msg = f"🔥 *TENDÊNCIA CONFIRMADA — {symbol} (15m)*\n📈 MA20 e MA50 cruzaram acima da MA200\n💰 Preço atual: {last_price_15}\n🕒 {now}"
            await send_telegram(msg)

        # 6️⃣ Reteste EMA9/MA20 e reverte com força
        if (abs(last_price_15 - ema9_15) / last_price_15 < 0.003 or abs(last_price_15 - ma20_15) / last_price_15 < 0.003) and rsi_15 > 50:
            msg = f"🔁 *RETESTE CONFIRMADO — {symbol} (15m)*\n📊 Preço testou a EMA9 ou MA20 e reverteu com confirmação dos indicadores\n💬 Continuação de alta\n💰 Preço atual: {last_price_15}\n🕒 {now}"
            await send_telegram(msg)

        # 7️⃣ Reteste fraco — possível queda
        if (abs(last_price_15 - ema9_15) / last_price_15 < 0.003 or abs(last_price_15 - ma20_15) / last_price_15 < 0.003) and rsi_15 < 45:
            msg = f"⚠️ *RETESTE FRACO — {symbol} (15m)*\n📊 Preço testou EMA9 ou MA20 e perdeu força com confirmação dos indicadores\n💬 Possível queda\n💰 Preço atual: {last_price_15}\n🕒 {now}"
            await send_telegram(msg)

        # 8️⃣ Reteste MA200 — confirmação de força
        if abs(last_price_15 - ma200_15) / last_price_15 < 0.003 and rsi_15 > 50:
            msg = f"🔁 *RETESTE MA200 — {symbol} (15m)*\n📊 Preço testou a MA200 e confirmou força pelos indicadores\n💬 Reteste MA200 — tendência de continuação de alta\n💰 Preço atual: {last_price_15}\n🕒 {now}"
            await send_telegram(msg)

    except Exception as e:
        print(f"Erro ao analisar {symbol}: {e}")

# -----------------------------
# 🚀 Loop principal
# -----------------------------
async def main_loop():
    print("🚀 Iniciando monitoramento SPOT USDT...")
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.binance.com/api/v3/exchangeInfo") as resp:
            data = await resp.json()
            pairs = [s["symbol"] for s in data["symbols"] if s["symbol"].endswith("USDT") and s["status"] == "TRADING"]

    while True:
        tasks = [analyze_pair(symbol) for symbol in pairs]
        await asyncio.gather(*tasks)
        await asyncio.sleep(60)

# -----------------------------
# 🌐 Flask (keep-alive)
# -----------------------------
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health():
    return "OK", 200

def run_bot():
    asyncio.run(main_loop())

# Executa o monitoramento em thread paralela
threading.Thread(target=run_bot, daemon=True).start()

# Inicia o Flask (Render requer uma porta HTTP)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
