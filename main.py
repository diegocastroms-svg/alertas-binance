import os, asyncio, aiohttp, time, math
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V11 LIGHT - 1H - DIST 2.5% (EMA200 + Leque + BB)", 200

BINANCE = "https://fapi.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 2_000_000
TOP_N = 180
SCAN_INTERVAL = 30

# NOVO CONTROLE (independente por TF)
last_alert = {}

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    try:
        await s.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print("Erro Telegram:", e)

async def get_oi(session, symbol):
    try:
        async with session.get(f"{BINANCE}/fapi/v1/openInterest?symbol={symbol}") as r:
            data = await r.json()
            return float(data["openInterest"])
    except:
        return 0

def ema(data, period):
    if len(data) < period:
        return [sum(data) / len(data)] * len(data) if data else []
    k = 2 / (period + 1)
    ema_vals = [sum(data[:period]) / period]
    for price in data[period:]:
        ema_vals.append(price * k + ema_vals[-1] * (1 - k))
    return ema_vals

def bollinger_bands(closes, period=20, std=2):
    if len(closes) < period: return [], []
    sma = []
    for i in range(len(closes)):
        if i < period - 1: sma.append(sum(closes[:i+1]) / (i + 1))
        else: sma.append(sum(closes[i - period + 1:i + 1]) / period)
    bb_up, bb_down = [], []
    for i in range(len(sma)):
        if i < period - 1:
            bb_up.append(0); bb_down.append(0)
            continue
        window = closes[i - period + 1:i + 1]
        std_dev = (sum((x - sma[i]) ** 2 for x in window) / period) ** 0.5
        bb_up.append(sma[i] + std_dev * std)
        bb_down.append(sma[i] - std_dev * std)
    return bb_up, bb_down

async def scan(session, sym):
    try:

        now = time.time()

        # ===================== 1H =====================
        async with session.get(f"{BINANCE}/fapi/v1/klines?symbol={sym}&interval=1h&limit=500") as r:
            k = await r.json()

        if len(k) >= 200:

            closes = [float(x[4]) for x in k]
            price = closes[-1]
            p_prev = closes[-2]

            ema200 = ema(closes, 200)
            bb_up, bb_down = bollinger_bands(closes)
            bb_up_prev, bb_down_prev = bb_up[-2], bb_down[-2]

            dist = abs(price - ema200[-1]) / ema200[-1]
            perto = dist <= 0.02
            cruzou = (p_prev < ema200[-1] <= price) or (p_prev > ema200[-1] >= price)

            if (perto or cruzou):

                key = f"{sym}_1h"
                if now - last_alert.get(key, 0) >= 3600:

                    oi_now = await get_oi(session, sym)

                    if price >= bb_up[-1] and bb_up[-1] > bb_up_prev:
                        msg = (
                            f"🟪⏫ <b>ALERTA BINANCE LONG 1H</b>\n\n"
                            f"Moeda: {sym.replace('USDT', '')}\n"
                            f"Preço: {price:.5f}\n"
                            f"Dist. EMA200: {dist*100:.2f}%\n"
                            f"OI: {oi_now:,.0f}\n"
                            f"⏰ {now_br()} BR"
                        )
                        await tg(session, msg)
                        last_alert[key] = now

                    elif price <= bb_down[-1] and bb_down[-1] < bb_down_prev:
                        msg = (
                            f"🟫⏬ <b>ALERTA BINANCE SHORT 1H</b>\n\n"
                            f"Moeda: {sym.replace('USDT', '')}\n"
                            f"Preço: {price:.5f}\n"
                            f"Dist. EMA200: {dist*100:.2f}%\n"
                            f"OI: {oi_now:,.0f}\n"
                            f"⏰ {now_br()} BR"
                        )
                        await tg(session, msg)
                        last_alert[key] = now


        # ===================== 15M =====================
        async with session.get(f"{BINANCE}/fapi/v1/klines?symbol={sym}&interval=15m&limit=500") as r:
            k = await r.json()

        if len(k) >= 200:

            closes = [float(x[4]) for x in k]
            price = closes[-1]
            p_prev = closes[-2]

            ema200 = ema(closes, 200)
            bb_up, bb_down = bollinger_bands(closes)
            bb_up_prev, bb_down_prev = bb_up[-2], bb_down[-2]

            dist = abs(price - ema200[-1]) / ema200[-1]
            perto = dist <= 0.015
            cruzou = (p_prev < ema200[-1] <= price) or (p_prev > ema200[-1] >= price)

            if (perto or cruzou):

                key = f"{sym}_15m"
                if now - last_alert.get(key, 0) >= 900:

                    oi_now = await get_oi(session, sym)

                    if price >= bb_up[-1] and bb_up[-1] > bb_up_prev:
                        msg = (
                            f"👆👆 <b>ALERTA BINANCE LONG 15M</b>\n\n"
                            f"Moeda: {sym.replace('USDT', '')}\n"
                            f"Preço: {price:.5f}\n"
                            f"Dist. EMA200: {dist*100:.2f}%\n"
                            f"OI: {oi_now:,.0f}\n"
                            f"⏰ {now_br()} BR"
                        )
                        await tg(session, msg)
                        last_alert[key] = now

                    elif price <= bb_down[-1] and bb_down[-1] < bb_down_prev:
                        msg = (
                            f"👇👇 <b>ALERTA BINANCE SHORT 15M</b>\n\n"
                            f"Moeda: {sym.replace('USDT', '')}\n"
                            f"Preço: {price:.5f}\n"
                            f"Dist. EMA200: {dist*100:.2f}%\n"
                            f"OI: {oi_now:,.0f}\n"
                            f"⏰ {now_br()} BR"
                        )
                        await tg(session, msg)
                        last_alert[key] = now

    except Exception:
        pass

async def main():
    async with aiohttp.ClientSession() as session:
        await tg(session, "<b>V11 LIGHT ATIVA (1h - 2.5%)</b>\nMonitorando Top 180 moedas.")
        while True:
            try:
                async with session.get(f"{BINANCE}/fapi/v1/ticker/24hr") as r:
                    data = await r.json()
                symbols = [d["symbol"] for d in data if d["symbol"].endswith("USDT") and float(d.get("quoteVolume", 0)) >= MIN_VOL24][:TOP_N]
                await asyncio.gather(*[scan(session, s) for s in symbols])
            except:
                pass
            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=10000), daemon=True).start()
asyncio.run(main())
