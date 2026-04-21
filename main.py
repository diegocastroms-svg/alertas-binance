import os, asyncio, aiohttp, time, math
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    # Identificação da versão no Dashboard
    return "V11 LIGHT - 1H - DIST 2.5% (EMA200 + Leque + BB)", 200

BINANCE = "https://fapi.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Configurações de Filtro
MIN_VOL24 = 5_000_000
TOP_N = 180
SCAN_INTERVAL = 30
COOLDOWN_SECONDS = 3600  # 1 hora de intervalo por moeda

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
        # TF alterado para 1h
        async with session.get(f"{BINANCE}/fapi/v1/klines?symbol={sym}&interval=1h&limit=500") as r:
            k = await r.json()

        if len(k) < 200: return

        closes = [float(x[4]) for x in k]
        price = closes[-1]

        # Cálculo das Médias e Bandas
        ema9 = ema(closes, 9)
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        ema200 = ema(closes, 200)
        bb_up, bb_down = bollinger_bands(closes)

        # 1. ZONA DE PRESSÃO (Ajustada para 2.5%)
        distancia_200 = abs(price - ema200[-1]) / ema200[-1]
        na_zona_200 = distancia_200 <= 0.025 

        # 2. TENDÊNCIA (LEQUE DE MÉDIAS)
        tendencia_long = (ema9[-1] > ema50[-1] and ema20[-1] > ema50[-1])
        tendencia_short = (ema9[-1] < ema50[-1] and ema20[-1] < ema50[-1])

        # 3. MOMENTO (PREÇO VS EMA9)
        preco_ok_long = price > ema9[-1]
        preco_ok_short = price < ema9[-1]

        # 4. EXPLOSÃO DE VOLATILIDADE
        bb_expandindo = (bb_up[-1] > bb_up[-2] and bb_down[-1] < bb_down[-2])

        # Verificação do Setup
        setup_long = (na_zona_200 and tendencia_long and preco_ok_long and bb_expanding)
        setup_short = (na_zona_200 and tendencia_short and preco_ok_short and bb_expanding)

        if (setup_long or setup_short) and can_alert(sym):
            oi_now = await get_oi(session, sym)
            dist_perc = distancia_200 * 100
            side = "LONG 🚀" if setup_long else "SHORT 📉"
            tipo = "ROMPIMENTO" if (setup_long and price > ema200[-1]) or (setup_short and price < ema200[-1]) else "PULLBACK"
            
            msg = (
                f"<b>ALERTA V11 LIGHT ({side})</b>\n\n"
                f"Moeda: {sym.replace('USDT', '')}\n"
                f"Preço: {price:.5f}\n"
                f"Dist. EMA200: {dist_perc:.2f}%\n"
                f"Tipo: {tipo}\n"
                f"OI: {oi_now:,.0f}\n"
                f"⏰ {now_br()} BR (TF: 1h)"
            )
            await tg(session, msg)

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
            except: pass
            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=10000), daemon=True).start()
asyncio.run(main())
