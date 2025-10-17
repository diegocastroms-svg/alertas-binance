# main_curto_v3.3.py
# ✅ Somente ajuste: limitar a 50 moedas com maior volume
# ✅ Mantido: intrabar ativo, alertas 5m/15m, sem outras mudanças

import os, asyncio, aiohttp, math, time
from datetime import datetime, timezone
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVALS = ["5m", "15m"]
MIN_PCT = 0.0
MIN_QV = 10000.0
SHORTLIST_N = 50  # 🔹 Limite: 50 moedas com maior volume
COOLDOWN = 15 * 60

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

app = Flask(__name__)

# ---------------- UTILS ----------------
async def send_msg(session, text):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
        await session.post(url, data=payload)
    except Exception as e:
        print("Erro send_msg:", e)

def fmt(num): 
    return f"{num:.6f}".rstrip("0").rstrip(".")

# ---------------- FETCH DATA ----------------
async def fetch_json(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=10) as r:
            return await r.json()
    except:
        return None

async def get_klines(session, symbol, interval, limit=100):
    url = f"{BINANCE_HTTP}/api/v3/klines"
    return await fetch_json(session, url, {"symbol": symbol, "interval": interval, "limit": limit})

# ---------------- FILTER ----------------
async def filter_tickers(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    data = await fetch_json(session, url)
    if not data:
        return []
    tickers = [t for t in data if t["symbol"].endswith("USDT") and not any(x in t["symbol"] for x in ["UP", "DOWN", "BUSD", "TUSD", "USDC", "FDUSD", "DAI", "EUR", "TRY", "BRL", "GBP"])]
    tickers = [t for t in tickers if float(t["quoteVolume"]) >= MIN_QV]
    tickers.sort(key=lambda t: float(t["quoteVolume"]), reverse=True)
    return [t["symbol"] for t in tickers[:SHORTLIST_N]]  # 🔹 Apenas top 50 por volume

# ---------------- MOVING AVERAGES ----------------
def ma(values, period):
    if len(values) < period: return None
    return sum(values[-period:]) / period

def ema(values, period):
    if len(values) < period: return None
    k = 2 / (period + 1)
    ema_val = values[0]
    for price in values[1:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

# ---------------- ALERTS ----------------
async def check_alerts(session, symbol, interval):
    klines = await get_klines(session, symbol, interval)
    if not klines or len(klines) < 100:
        return
    closes = [float(k[4]) for k in klines]
    last_close = closes[-1]

    ema9 = ema(closes, 9)
    ma20 = ma(closes, 20)
    ma50 = ma(closes, 50)
    ma200 = ma(closes, 200)

    if None in [ema9, ma20, ma50, ma200]:
        return

    # 🔸 Tendência iniciando (5m): EMA9 cruza MA20 e MA50
    if interval == "5m" and ema9 > ma20 > ma50:
        text = f"🟢 {symbol} ⬆️ Tendência iniciando ({interval})\n💰 {fmt(last_close)}\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        await send_msg(session, text)

    # 🔸 Tendência pré-confirmada (5m): MA20 e MA50 cruzam acima da MA200
    if interval == "5m" and ma20 > ma200 and ma50 > ma200:
        text = f"🟡 {symbol} ⬆️ Tendência pré-confirmada ({interval})\n💰 {fmt(last_close)}\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        await send_msg(session, text)

    # 🔸 Tendência pré-confirmada (15m): EMA9 cruza acima da MA200
    if interval == "15m" and ema9 > ma200:
        text = f"🟡 {symbol} ⬆️ Tendência pré-confirmada ({interval})\n💰 {fmt(last_close)}\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        await send_msg(session, text)

    # 🔸 Tendência confirmada (15m): MA20 e MA50 cruzam acima da MA200
    if interval == "15m" and ma20 > ma200 and ma50 > ma200:
        text = f"🚀 {symbol} ⬆️ Tendência confirmada ({interval})\n💰 {fmt(last_close)}\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        await send_msg(session, text)

# ---------------- MAIN LOOP ----------------
async def monitor():
    async with aiohttp.ClientSession() as session:
        tickers = await filter_tickers(session)
        print(f"✅ v3.3 intrabar ativo | {len(tickers)} pares SPOT | cooldown 15m | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        await send_msg(session, f"✅ v3.3 intrabar ativo | {len(tickers)} pares SPOT | cooldown 15m | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 🇧🇷")

        while True:
            for interval in INTERVALS:
                tasks = [check_alerts(session, symbol, interval) for symbol in tickers]
                await asyncio.gather(*tasks)
            await asyncio.sleep(COOLDOWN)

# ---------------- FLASK APP ----------------
@app.route('/')
def home():
    return "Bot de Alertas ativo! 🚀"

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(monitor())
    app.run(host="0.0.0.0", port=10000)
