import os, asyncio, aiohttp, time, math
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V10 - ZONA DE PRESSÃO (EMA200 + Leque + BB)", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://fapi.binance.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 10_000_000
TOP_N = 180
SCAN_INTERVAL = 30

COOLDOWN_SECONDS = 14400  # 4 horas

cooldown = {}

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

def now_ts():
    return int(time.time())

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

def can_alert(sym):
    t = cooldown.get(sym, 0)
    if now_ts() - t >= COOLDOWN_SECONDS:
        cooldown[sym] = now_ts()
        return True
    return False

async def get_oi(session, symbol):
    try:
        async with session.get(f"{BINANCE}/fapi/v1/openInterest?symbol={symbol}") as r:
            data = await r.json()
            return float(data["openInterest"])
    except:
        return 0

# ====================== FUNÇÕES AUXILIARES ======================
def ema(data, period):
    if len(data) < period:
        return [sum(data)/len(data)] * len(data) if data else []
    k = 2 / (period + 1)
    ema_vals = [sum(data[:period]) / period]
    for price in data[period:]:
        ema_vals.append(price * k + ema_vals[-1] * (1 - k))
    return ema_vals

def bollinger_bands(closes, period=20, std=2):
    if len(closes) < period:
        return [], []
    sma = []
    for i in range(len(closes)):
        if i < period - 1:
            sma.append(sum(closes[:i+1]) / (i+1))
        else:
            sma.append(sum(closes[i-period+1:i+1]) / period)
    
    bb_up = []
    bb_down = []
    for i in range(len(sma)):
        if i < period - 1:
            bb_up.append(0)
            bb_down.append(0)
            continue
        window = closes[i-period+1:i+1]
        std_dev = (sum((x - sma[i]) ** 2 for x in window) / period) ** 0.5
        bb_up.append(sma[i] + std_dev * std)
        bb_down.append(sma[i] - std_dev * std)
    return bb_up, bb_down

# ====================== SCAN ======================
async def scan(session, sym):
    try:
        async with session.get(f"{BINANCE}/fapi/v1/klines?symbol={sym}&interval=15m&limit=150") as r:
            k = await r.json()

        if len(k) < 100:
            return

        closes = [float(x[4]) for x in k]

        price = closes[-1]

        # Cálculo das EMAs
        ema9  = ema(closes, 9)
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        ema200 = ema(closes, 200)

        # Bandas de Bollinger
        bb_up, bb_down = bollinger_bands(closes)

        oi_now = await get_oi(session, sym)

        # ====================== ZONA DE PRESSÃO ======================
        margem = 0.015  # 1.5%

        if len(ema200) == 0 or ema200[-1] == 0:
            return

        distancia_percentual = abs(price - ema200[-1]) / ema200[-1]
        na_zona_200 = distancia_percentual <= margem

        # Leque de médias (alinhamento)
        long_alinhado = (ema9[-1] > ema20[-1]) and (ema20[-1] > ema50[-1])
        short_alinhado = (ema9[-1] < ema20[-1]) and (ema20[-1] < ema50[-1])

        # Bandas abrindo (expansão de volatilidade)
        bb_expandindo = (len(bb_up) > 1 and len(bb_down) > 1 and
                        bb_up[-1] > bb_up[-2] and bb_down[-1] < bb_down[-2])

        # ====================== GATILHOS ======================
        if (long_alinhado and na_zona_200 and bb_expandindo and 
            can_alert(sym + "_LONG")):   # cooldown separado por direção se quiser

            tipo = "ROMPIMENTO" if price > ema200[-1] else "PULLBACK"
            dist = distancia_percentual * 100
            nome = sym.replace("USDT", "")

            msg = (
                f"🚀 <b>ALERTAS BINANCE LONG</b>\n\n"
                f"{nome}\n"
                f"Preço: {price:.4f}\n"
                f"Distância EMA200: {dist:.2f}%\n"
                f"OI: {oi_now:,.0f}\n"
                f"Tipo: {tipo}\n"
                f"⏰ {now_br()} BR"
            )
            await tg(session, msg)

        if (short_alinhado and na_zona_200 and bb_expandindo and 
            can_alert(sym + "_SHORT")):

            tipo = "ROMPIMENTO" if price < ema200[-1] else "PULLBACK"
            dist = distancia_percentual * 100
            nome = sym.replace("USDT", "")

            msg = (
                f"📉 <b>ALERTAS BINANCE SHORT</b>\n\n"
                f"{nome}\n"
                f"Preço: {price:.4f}\n"
                f"Distância EMA200: {dist:.2f}%\n"
                f"OI: {oi_now:,.0f}\n"
                f"Tipo: {tipo}\n"
                f"⏰ {now_br()} BR"
            )
            await tg(session, msg)

    except Exception as e:
        print(f"Erro em {sym}:", e)

# ====================== MAIN ======================
async def main():
    async with aiohttp.ClientSession() as session:
        await tg(session, "<b>V10 - ZONA DE PRESSÃO ATIVA</b>\nEMA200 + Leque de Médias + BB Abrindo")
        while True:
            try:
                async with session.get(f"{BINANCE}/fapi/v1/ticker/24hr") as r:
                    data = await r.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume", 0)) >= MIN_VOL24
                ][:TOP_N]

                await asyncio.gather(*[scan(session, s) for s in symbols])

            except Exception as e:
                print("Erro principal:", e)

            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))), daemon=True).start()
asyncio.run(main())
