# ===========================
# 📁 novo_main_v1.4.2.py
# ===========================
# Autor: Diego Castro Oliveira
# Projeto: Bot Binance SPOT (Flask + EMA/MA + Filtro anti-USD + Cooldown 15min)
# ===========================

import os
import asyncio
import threading
from datetime import datetime, timedelta
from statistics import mean
import aiohttp
from flask import Flask

# -----------------------------
# 🔧 Variáveis de ambiente
# -----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# -----------------------------
# 🕒 Controle de cooldown
# -----------------------------
cooldowns = {}
COOLDOWN_TIME = timedelta(minutes=15)

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
        # 🔒 Cooldown por símbolo
        now_time = datetime.now()
        if symbol in cooldowns and now_time - cooldowns[symbol] < COOLDOWN_TIME:
            return  # ainda em cooldown

        candles_5m = await get_klines(symbol, "5m", 120)
        candles_15m = await get_klines(symbol, "15m", 120)
        if not candles_5m or not candles_15m:
            return

        closes_5m = [float(c[4]) for c in candles_5m]
        closes_15m = [float(c[4]) for c in candles_15m]

        ema9_15 = ma(closes_15m, 9)
        ma20_15 = ma(closes_15m, 20)
        ma50_15 = ma(closes_15m, 50)
        ma200_15 = ma(closes_15m, 200)
        rsi_15 = rsi(closes_15m)
        last_price_15 = closes_15m[-1]
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        separator = "\n━━━━━━━━━━━━━━━━━━━━━━━"

        # =============================
        # ⚙️ ALERTAS PRINCIPAIS
        # =============================

        # 15m - Tendência pré-confirmada
        if ema9_15 and ma200_15 and ema9_15 > ma200_15:
            msg = f"🟢 <b>{symbol}</b>\n⚡ <b>TENDÊNCIA PRÉ-CONFIRMADA (15m)</b>\n📈 EMA9 cruzou acima da MA200\n💰 Preço atual: {last_price_15}\n🕒 {now}{separator}"
            await send_telegram(msg)
            cooldowns[symbol] = datetime.now()

        # 15m - Tendência confirmada
        if ma20_15 and ma50_15 and ma200_15 and ma20_15 > ma200_15 and ma50_15 > ma200_15:
            msg = f"🟢 <b>{symbol}</b>\n🔥 <b>TENDÊNCIA CONFIRMADA (15m)</b>\n📈 MA20 e MA50 cruzaram acima da MA200\n💰 Preço atual: {last_price_15}\n🕒 {now}{separator}"
            await send_telegram(msg)
            cooldowns[symbol] = datetime.now()

        # 15m - Reteste fraco
        if (abs(last_price_15 - ema9_15) / last_price_15 < 0.003 or abs(last_price_15 - ma20_15) / last_price_15 < 0.003) and rsi_15 and rsi_15 < 45:
            msg = f"🟠 <b>{symbol}</b>\n⚠️ <b>RETESTE FRACO (15m)</b>\n📊 Preço testou EMA9 ou MA20 e perdeu força\n💬 Possível queda\n💰 Preço atual: {last_price_15}\n🕒 {now}{separator}"
            await send_telegram(msg)
            cooldowns[symbol] = datetime.now()

    except Exception as e:
        print(f"⚠️ Erro ao analisar {symbol}: {e}")

# -----------------------------
# 🚀 Loop principal
# -----------------------------
async def main_loop():
    print("🚀 Iniciando monitoramento SPOT com cooldown de 15 minutos...")

    async with aiohttp.ClientSession() as session:
        # 🔎 Filtro anti-USD universal
        async with session.get("https://api.binance.com/api/v3/exchangeInfo") as resp:
            info = await resp.json()
            valid_spot = []
            for s in info["symbols"]:
                sym = s["symbol"]
                base = sym.replace("USDT", "")
                if (
                    s.get("isSpotTradingAllowed")
                    and s["status"] == "TRADING"
                    and sym.endswith("USDT")
                    and not any(x in base for x in ["USD", "FDUSD", "BUSD", "TUSD", "USDC", "DAI", "AEUR", "EUR", "PYUSD"])
                ):
                    valid_spot.append(sym)

        # 🔹 Volume e ordenação
        async with session.get("https://api.binance.com/api/v3/ticker/24hr") as resp:
            ticker_data = await resp.json()
            spot_pairs = [t for t in ticker_data if t["symbol"] in valid_spot]
            sorted_pairs = sorted(spot_pairs, key=lambda x: float(x["quoteVolume"]), reverse=True)
            top_pairs = [p["symbol"] for p in sorted_pairs[:50]]

    # ✅ Notificação inicial no Telegram
    start_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = (
        f"✅ <b>BOT ATIVO NO RENDER</b>\n"
        f"🕒 {start_time}\n"
        f"💹 {len(valid_spot)} pares SPOT válidos carregados (anti-USD ativo)\n"
        f"⏱️ Cooldown ativo: 15 minutos por par\n"
        f"🔝 Top 5 por volume: {', '.join(top_pairs[:5])}"
    )
    await send_telegram(msg)

    print(f"✅ {len(valid_spot)} pares SPOT válidos carregados (anti-USD ativo).")
    print(f"🔝 Top 10 por volume: {[p for p in top_pairs[:10]]}")

    # 🔁 Loop contínuo
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
