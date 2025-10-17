# main_v4.0.py
# ✅ Sistema de alertas 100% revisado — apenas cruzamentos de médias móveis
# 🚀 Aurora OURO — versão estável e funcional

import os, asyncio, math
from datetime import datetime, timezone
import aiohttp

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

SYMBOLS = ["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","MATICUSDT","AVAXUSDT","ADAUSDT","LINKUSDT"]
INTERVALS = {"5m":300, "15m":900}
LIMIT = 200

# ---------------- FUNÇÕES ----------------
async def get_klines(symbol, interval):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={LIMIT}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
            return [
                {
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "time": int(k[0])
                } for k in data
            ]

def moving_average(values, period):
    if len(values) < period:
        return []
    return [sum(values[i - period:i]) / period for i in range(period, len(values) + 1)]

def crossed_above(a_prev, a_now, b_prev, b_now):
    return a_prev < b_prev and a_now > b_now

async def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as session:
        await session.post(url, data=payload)

# ---------------- ALERTAS ----------------
async def check_crosses(symbol):
    for tf, secs in INTERVALS.items():
        klines = await get_klines(symbol, tf)
        closes = [k["close"] for k in klines]

        ma9 = moving_average(closes, 9)
        ma20 = moving_average(closes, 20)
        ma50 = moving_average(closes, 50)
        ma200 = moving_average(closes, 200)

        if len(ma200) < 3:  # garante dados suficientes
            continue

        # valores recentes
        ema9_prev, ema9_now = ma9[-2], ma9[-1]
        ma20_prev, ma20_now = ma20[-2], ma20[-1]
        ma50_prev, ma50_now = ma50[-2], ma50[-1]
        ma200_prev, ma200_now = ma200[-2], ma200[-1]

        price_now = closes[-1]
        price_prev = closes[-2]

        # 🟢 Tendência Iniciando (5m)
        if tf == "5m":
            queda_anterior = price_now < price_prev and ((price_prev - price_now) / price_prev) > 0.002
            if queda_anterior and crossed_above(ema9_prev, ema9_now, ma20_prev, ma20_now) and crossed_above(ema9_prev, ema9_now, ma50_prev, ma50_now):
                msg = f"🟢 {symbol} — Tendência iniciando (5m)\nEMA9 cruzou MA20 e MA50 após queda\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 🇧🇷"
                await send_telegram_message(msg)

        # 🌕 Tendência Pré-confirmada (5m)
        if tf == "5m":
            if crossed_above(ma20_prev, ma20_now, ma200_prev, ma200_now) and crossed_above(ma50_prev, ma50_now, ma200_prev, ma200_now) and ema9_now > ma200_now:
                msg = f"🌕 {symbol} — Tendência pré-confirmada (5m)\nMédias 20 e 50 acima da 200\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 🇧🇷"
                await send_telegram_message(msg)

        # 🌕 Tendência Pré-confirmada (15m)
        if tf == "15m":
            if crossed_above(ema9_prev, ema9_now, ma200_prev, ma200_now):
                msg = f"🌕 {symbol} — Tendência pré-confirmada (15m)\nEMA9 cruzou acima da MA200\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 🇧🇷"
                await send_telegram_message(msg)

        # 🚀 Tendência Confirmada (15m)
        if tf == "15m":
            if crossed_above(ma20_prev, ma20_now, ma200_prev, ma200_now) and crossed_above(ma50_prev, ma50_now, ma200_prev, ma200_now) and ema9_now > ma200_now:
                msg = f"🚀 {symbol} — Tendência confirmada (15m)\nMédias 20 e 50 cruzaram acima da 200\n⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 🇧🇷"
                await send_telegram_message(msg)

# ---------------- LOOP PRINCIPAL ----------------
async def main():
    print("🚀 Bot de cruzamentos iniciado — versão estável v4.0")
    while True:
        try:
            for s in SYMBOLS:
                await check_crosses(s)
            await asyncio.sleep(60)
        except Exception as e:
            print("Erro:", e)
            await asyncio.sleep(30)

if __name__ == "__main__":
    asyncio.run(main())
