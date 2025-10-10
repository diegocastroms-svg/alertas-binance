# ===========================
# 📁 novo_main_v1.4_base.py
# ===========================
# Autor: Diego Castro Oliveira
# Projeto: Bot Binance SPOT (Flask + EMA/MA + Volume + RSI + Candle)
# Estrutura: idêntica à v1.3.3 — com 3 filtros inteligentes sem alterar disparos
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
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ Variáveis de ambiente ausentes.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        await session.post(url, data=payload)

async def get_klines(symbol: str, interval="5m", limit=100):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            valid = [c for c in data if c and len(c) >= 6 and c[4] not in (None, "null")]
            return valid

def ma(values, period):
    values = [v for v in values if v is not None]
    if len(values) < period:
        return None
    return mean(values[-period:])

def rsi(values, period=14):
    values = [v for v in values if v is not None]
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = values[-i] - values[-i - 1]
        if diff > 0:
            gains.append(diff)
        else:
            losses.append(abs(diff))
    if not gains and not losses:
        return None
    avg_gain = mean(gains) if gains else 0
    avg_loss = mean(losses) if losses else 1e-6
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# -----------------------------
# 📊 Lógica principal
# -----------------------------
async def analyze_pair(symbol):
    try:
        candles_5m = await get_klines(symbol, "5m", 120)
        candles_15m = await get_klines(symbol, "15m", 120)
        if not candles_5m or not candles_15m:
            return

        closes_5m = [float(c[4]) for c in candles_5m]
        closes_15m = [float(c[4]) for c in candles_15m]

        ema9 = ma(closes_5m, 9)
        ma20 = ma(closes_5m, 20)
        ma50 = ma(closes_5m, 50)
        ma200 = ma(closes_5m, 200)
        rsi_5m = rsi(closes_5m)
        last_price = closes_5m[-1]

        ema9_15 = ma(closes_15m, 9)
        ma20_15 = ma(closes_15m, 20)
        ma50_15 = ma(closes_15m, 50)
        ma200_15 = ma(closes_15m, 200)
        rsi_15 = rsi(closes_15m)
        last_price_15 = closes_15m[-1]
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        separator = "\n━━━━━━━━━━━━━━━━━━━━━━━"

        # =============================
        # 🔍 FILTROS INTELIGENTES
        # =============================
        # Volume inteligente
        volumes_15m = [float(c[5]) for c in candles_15m if float(c[5]) > 0]
        if len(volumes_15m) > 20:
            avg_vol_15m = mean(volumes_15m[-20:])
            volume_ratio = volumes_15m[-1] / avg_vol_15m
        else:
            volume_ratio = 1
        volume_text = "📈 Volume forte" if volume_ratio > 1.5 else "💤 Volume fraco"

        # RSI cruzado (7 e 14)
        def rsi_period(values, period=14):
            values = [v for v in values if v is not None]
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

        rsi7_15 = rsi_period(closes_15m, 7)
        rsi14_15 = rsi_period(closes_15m, 14)
        if rsi7_15 and rsi14_15:
            if rsi7_15 > rsi14_15:
                rsi_status = "💪 RSI confirmando força de alta"
            elif rsi7_15 < rsi14_15:
                rsi_status = "⚠️ RSI sinalizando fraqueza"
            else:
                rsi_status = "〰️ RSI neutro"
        else:
            rsi_status = "ℹ️ RSI insuficiente"

        # Candle de reversão
        last_open = float(candles_15m[-2][1])
        last_close = float(candles_15m[-2][4])
        if last_close > last_open:
            candle_status = "🟢 Candle de alta confirmado"
        else:
            candle_status = "🔴 Sem candle de confirmação"

        # =============================
        # ⚙️ LÓGICA DE ALERTAS
        # =============================

        # 5m - Lateralizando após queda
        if len(closes_5m) >= 3 and closes_5m[-3] > closes_5m[-2] > closes_5m[-1]:
            msg = f"🔴 <b>{symbol}</b>\n📉 <b>MERCADO EM QUEDA (5m)</b>\n⏸️ Lateralizando após queda\n📊 Em queda, monitorando possível alta\n💰 Preço atual: {last_price}\n{volume_text}\n{rsi_status}\n{candle_status}\n🕒 {now}{separator}"
            await send_telegram(msg)

        # 5m - EMA9 cruzou acima de MA20 e MA50
        if ema9 and ma20 and ma50 and ema9 > ma20 > ma50:
            msg = f"🟢 <b>{symbol}</b>\n🚀 <b>TENDÊNCIA DE ALTA INICIADA (5m)</b>\n📈 EMA9 cruzou acima das MA20 e MA50\n💰 Preço atual: {last_price}\n{volume_text}\n{rsi_status}\n{candle_status}\n🕒 {now}{separator}"
            await send_telegram(msg)

        # 5m - Tendência pré-confirmada
        if ema9 and ma20 and ma50 and ma200 and ema9 > ma200 and ma20 > ma200 and ma50 > ma200:
            msg = f"🟢 <b>{symbol}</b>\n⚡ <b>TENDÊNCIA PRÉ-CONFIRMADA (5m)</b>\n📈 EMA9, MA20 e MA50 cruzaram acima da MA200\n💰 Preço atual: {last_price}\n{volume_text}\n{rsi_status}\n{candle_status}\n🕒 {now}{separator}"
            await send_telegram(msg)

        # 15m - Tendência pré-confirmada
        if ema9_15 and ma200_15 and ema9_15 > ma200_15:
            msg = f"🟢 <b>{symbol}</b>\n⚡ <b>TENDÊNCIA PRÉ-CONFIRMADA (15m)</b>\n📈 EMA9 cruzou acima da MA200\n💰 Preço atual: {last_price_15}\n{volume_text}\n{rsi_status}\n{candle_status}\n🕒 {now}{separator}"
            await send_telegram(msg)

        # 15m - Tendência confirmada
        if ma20_15 and ma50_15 and ma200_15 and ma20_15 > ma200_15 and ma50_15 > ma200_15:
            msg = f"🟢 <b>{symbol}</b>\n🔥 <b>TENDÊNCIA CONFIRMADA (15m)</b>\n📈 MA20 e MA50 cruzaram acima da MA200\n💰 Preço atual: {last_price_15}\n{volume_text}\n{rsi_status}\n{candle_status}\n🕒 {now}{separator}"
            await send_telegram(msg)

        # 15m - Retestes
        if (abs(last_price_15 - ema9_15) / last_price_15 < 0.003 or abs(last_price_15 - ma20_15) / last_price_15 < 0.003) and rsi_15 and rsi_15 > 50:
            msg = f"🟢 <b>{symbol}</b>\n🔁 <b>RETESTE CONFIRMADO (15m)</b>\n📊 Preço testou EMA9 ou MA20 e reverteu com confirmação dos indicadores\n💬 Continuação de alta\n💰 Preço atual: {last_price_15}\n{volume_text}\n{rsi_status}\n{candle_status}\n🕒 {now}{separator}"
            await send_telegram(msg)

        if (abs(last_price_15 - ema9_15) / last_price_15 < 0.003 or abs(last_price_15 - ma20_15) / last_price_15 < 0.003) and rsi_15 and rsi_15 < 45:
            msg = f"🟠 <b>{symbol}</b>\n⚠️ <b>RETESTE FRACO (15m)</b>\n📊 Preço testou EMA9 ou MA20 e perdeu força\n💬 Possível queda\n💰 Preço atual: {last_price_15}\n{volume_text}\n{rsi_status}\n{candle_status}\n🕒 {now}{separator}"
            await send_telegram(msg)

    except Exception as e:
        print(f"⚠️ Erro ao analisar {symbol}: {e}")

# -----------------------------
# 🚀 Loop principal
# -----------------------------
async def main_loop():
    print("🚀 Iniciando monitoramento SPOT com filtros inteligentes...")
    EXCLUDED_KEYWORDS = ["USDC", "TUSD", "FDUSD", "BUSD", "DAI", "AEUR", "EURT", "EURS", "PYUSD", "USDP"]
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.binance.com/api/v3/exchangeInfo") as resp:
            info = await resp.json()
            valid_spot = [
                s["symbol"] for s in info["symbols"]
                if s.get("isSpotTradingAllowed")
                and s["status"] == "TRADING"
                and s["symbol"].endswith("USDT")
                and not any(x in s["symbol"] for x in EXCLUDED_KEYWORDS)
            ]

        async with session.get("https://api.binance.com/api/v3/ticker/24hr") as resp:
            ticker_data = await resp.json()
            spot_pairs = [t for t in ticker_data if t["symbol"] in valid_spot]
            sorted_pairs = sorted(spot_pairs, key=lambda x: float(x["quoteVolume"]), reverse=True)
            top_pairs = [p["symbol"] for p in sorted_pairs[:50]]
            other_pairs = [p["symbol"] for p in sorted_pairs[50:]]

    print(f"✅ {len(valid_spot)} pares SPOT válidos carregados.")
    print(f"🔝 Top 10 por volume: {[p for p in top_pairs[:10]]}")

    while True:
        await asyncio.gather(*[analyze_pair(s) for s in top_pairs])
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

threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
