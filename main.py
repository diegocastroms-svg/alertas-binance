import os
import asyncio
import aiohttp
import time
from flask import Flask
from threading import Thread

# -----------------------------
# CONFIGURAÇÕES
# -----------------------------
BINANCE_URL = "https://api.binance.com/api/v3/klines"
TELEGRAM_URL = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendMessage"
CHAT_ID = os.getenv("CHAT_ID")
INTERVALS = ["5m", "15m"]
ALERT_INTERVAL = 600  # 10 minutos (em segundos)
last_alert = {}  # controle de tempo dos alertas

# -----------------------------
# FLASK PARA RENDER
# -----------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot TITAM rodando", 200

@app.route("/health")
def health():
    return "ok", 200

@app.route("/status")
def status():
    return {"status": "running", "intervals": INTERVALS}, 200

# -----------------------------
# INDICADORES
# -----------------------------
def ema(values, period):
    if len(values) < period: return [None] * len(values)
    k = 2 / (period + 1)
    ema_vals = [sum(values[:period]) / period]
    for price in values[period:]:
        ema_vals.append(price * k + ema_vals[-1] * (1 - k))
    return [None] * (period - 1) + ema_vals

def ma(values, period):
    if len(values) < period: return [None] * len(values)
    ma_vals = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        ma_vals.append(sum(window) / period)
    return ma_vals

def rsi(values, period=14):
    if len(values) < period + 1: return [None] * len(values)
    rsi_vals = [None] * period
    for i in range(period, len(values)):
        gains, losses = 0, 0
        for j in range(i - period + 1, i + 1):
            diff = values[j] - values[j - 1]
            if diff > 0: gains += diff
            else: losses -= diff
        avg_gain = gains / period
        avg_loss = losses / period if losses != 0 else 0.000001
        rs = avg_gain / avg_loss
        rsi_vals.append(100 - (100 / (1 + rs)))
    return rsi_vals

def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)
    atr_vals = []
    for i in range(len(trs)):
        if i < period:
            atr_vals.append(None)
        else:
            atr_vals.append(sum(trs[i - period + 1:i + 1]) / period)
    return [None] + atr_vals

# -----------------------------
# TELEGRAM (com filtro 10min)
# -----------------------------
async def send_alert(session, symbol, alert_type):
    now = time.time()
    key = f"{symbol}_{alert_type}"

    # verifica se já mandou alerta recente
    if key in last_alert and now - last_alert[key] < ALERT_INTERVAL:
        return  # ignora alerta duplicado

    last_alert[key] = now

    icons = {
        "reversal": "🚀 Reversão confirmada (5m + 15m)",
        "exhaustion": "⚠️ Exaustão vendedora (5m)"
    }
    text = f"{icons[alert_type]} detectada em {symbol}"
    payload = {"chat_id": CHAT_ID, "text": text}
    async with session.post(TELEGRAM_URL, json=payload) as resp:
        await resp.text()

# -----------------------------
# COLETA DE DADOS
# -----------------------------
async def fetch_klines(session, symbol, interval, limit=100):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    async with session.get(BINANCE_URL, params=params) as resp:
        data = await resp.json()
        opens = [float(x[1]) for x in data]
        highs = [float(x[2]) for x in data]
        lows = [float(x[3]) for x in data]
        closes = [float(x[4]) for x in data]
        volumes = [float(x[5]) for x in data]
        return opens, highs, lows, closes, volumes

# -----------------------------
# FILTRO DE PARES
# -----------------------------
async def get_usdt_pairs(session):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    async with session.get(url) as resp:
        data = await resp.json()
        pairs = []
        for d in data:
            symbol = d["symbol"].upper()
            if (
                symbol.endswith("USDT") and symbol.count("USDT") == 1
                and not any(x in symbol for x in ["BUSD","USDC","FDUSD","TUSD","EUR","TRY"])
            ):
                try:
                    volume = float(d.get("quoteVolume", 0))
                    price = float(d.get("lastPrice", 0))
                    if volume >= 10000000 and price >= 0.0005:
                        pairs.append((symbol, volume))
                except:
                    continue
        pairs = [s for s,v in sorted(pairs, key=lambda x: x[1], reverse=True)]
        return pairs[:50]

# -----------------------------
# ANÁLISE MULTI-TIMEFRAME
# -----------------------------
async def analyze_symbol(session, symbol):
    try:
        # coleta 5m e 15m
        o5, h5, l5, c5, v5 = await fetch_klines(session, symbol, "5m")
        o15, h15, l15, c15, v15 = await fetch_klines(session, symbol, "15m")

        # indicadores 5m
        rsi5 = rsi(c5)
        atr5 = atr(h5, l5, c5)
        ema9_5 = ema(c5, 9)
        ma50_5 = ma(c5, 50)
        vol_mean5 = sum(v5[-20:]) / 20 if len(v5) >= 20 else v5[-1]

        # indicadores 15m
        ema9_15 = ema(c15, 9)
        ma20_15 = ma(c15, 20)
        ma50_15 = ma(c15, 50)
        rsi15 = rsi(c15)
        vol_mean15 = sum(v15[-20:]) / 20 if len(v15) >= 20 else v15[-1]

        # 1️⃣ Exaustão vendedora (5m)
        body = abs(c5[-1] - o5[-1])
        exaustao = (
            rsi5[-1] < 35
            and v5[-1] < vol_mean5
            and body < 0.7 * atr5[-1]
            and c5[-1] < ma50_5[-1]
        )

        # 2️⃣ Reversão confirmada (15m)
        cruzou_ma20 = (ema9_15[-1] > ma20_15[-1] and ema9_15[-2] <= ma20_15[-2])
        cruzou_ma50 = (ema9_15[-1] > ma50_15[-1] and ema9_15[-2] <= ma50_15[-2])
        reversao = (
            (cruzou_ma20 or cruzou_ma50)
            and rsi15[-1] > 50
            and v15[-1] > 1.2 * vol_mean15
        )

        # ALERTAS
        if exaustao and not reversao:
            await send_alert(session, symbol, "exhaustion")
        elif exaustao and reversao:
            await send_alert(session, symbol, "reversal")

    except Exception as e:
        print(f"Erro em {symbol}: {e}")

# -----------------------------
# LOOP PRINCIPAL
# -----------------------------
async def monitor():
    async with aiohttp.ClientSession() as session:
        pairs = await get_usdt_pairs(session)
        print(f"Monitorando {len(pairs)} pares USDT (5m + 15m)...")

        while True:
            tasks = [analyze_symbol(session, s) for s in pairs]
            await asyncio.gather(*tasks)
            print("Ciclo concluído. Aguardando 60s...\n")
            await asyncio.sleep(60)

# -----------------------------
# EXECUÇÃO (Render)
# -----------------------------
if __name__ == "__main__":
    def start_monitor():
        asyncio.run(monitor())

    Thread(target=start_monitor, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
