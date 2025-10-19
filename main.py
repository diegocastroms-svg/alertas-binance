import os
import asyncio
import aiohttp
import math
from flask import Flask
from threading import Thread

# -----------------------------
# CONFIGURAÇÕES
# -----------------------------
BINANCE_URL = "https://api.binance.com/api/v3/klines"
TELEGRAM_URL = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendMessage"
CHAT_ID = os.getenv("CHAT_ID")
INTERVALS = ["5m", "15m"]

# -----------------------------
# FLASK PARA RENDER
# -----------------------------
app = Flask(__name__)

@app.route("/health")
def health():
    return "ok", 200

@app.route("/status")
def status():
    return {"status": "running", "intervals": INTERVALS}, 200

# -----------------------------
# FUNÇÕES DE INDICADORES
# -----------------------------
def ema(values, period):
    if len(values) < period:
        return values
    k = 2 / (period + 1)
    ema_values = [sum(values[:period]) / period]
    for price in values[period:]:
        ema_values.append(price * k + ema_values[-1] * (1 - k))
    return [None] * (period - 1) + ema_values

def ma(values, period):
    if len(values) < period:
        return [None] * len(values)
    ma_values = [None] * (period - 1)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        ma_values.append(sum(window) / period)
    return ma_values

def rsi(values, period=14):
    if len(values) < period + 1:
        return [None] * len(values)
    rsi_values = [None] * period
    for i in range(period, len(values)):
        gains, losses = 0, 0
        for j in range(i - period + 1, i + 1):
            diff = values[j] - values[j - 1]
            if diff > 0:
                gains += diff
            else:
                losses -= diff
        avg_gain = gains / period
        avg_loss = losses / period if losses != 0 else 0.000001
        rs = avg_gain / avg_loss
        rsi_values.append(100 - (100 / (1 + rs)))
    return rsi_values

def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)
    atr_values = []
    for i in range(len(trs)):
        if i < period:
            atr_values.append(None)
        else:
            atr_values.append(sum(trs[i - period + 1:i + 1]) / period)
    return [None] + atr_values

# -----------------------------
# TELEGRAM
# -----------------------------
async def send_alert(session, symbol, interval, alert_type):
    icons = {
        "reversal": "📈 Reversão confirmada",
        "exhaustion": "⚠️ Exaustão vendedora"
    }
    text = f"{icons[alert_type]} ({interval}) detectada em {symbol}"
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
        closes = [float(x[4]) for x in data]
        opens = [float(x[1]) for x in data]
        highs = [float(x[2]) for x in data]
        lows = [float(x[3]) for x in data]
        volumes = [float(x[5]) for x in data]
        return opens, highs, lows, closes, volumes

# -----------------------------
# ANÁLISE DE CONDIÇÕES
# -----------------------------
async def analyze_symbol(session, symbol, interval):
    try:
        opens, highs, lows, closes, volumes = await fetch_klines(session, symbol, interval)
        if len(closes) < 50:
            return

        ema9 = ema(closes, 9)
        ma20 = ma(closes, 20)
        ma50 = ma(closes, 50)
        rsi14 = rsi(closes, 14)
        atr14 = atr(highs, lows, closes, 14)

        vol_mean20 = []
        for i in range(len(volumes)):
            if i < 20:
                vol_mean20.append(None)
            else:
                vol_mean20.append(sum(volumes[i - 20:i]) / 20)

        # --- REVERSÃO CONFIRMADA ---
        cruzou_ma20 = False
        cruzou_ma50 = False
        for i in range(-3, 0):
            if ema9[i] and ma20[i] and ema9[i - 1] and ma20[i - 1]:
                if ema9[i] > ma20[i] and ema9[i - 1] <= ma20[i - 1]:
                    cruzou_ma20 = True
            if ema9[i] and ma50[i] and ema9[i - 1] and ma50[i - 1]:
                if ema9[i] > ma50[i] and ema9[i - 1] <= ma50[i - 1]:
                    cruzou_ma50 = True

        if (
            (cruzou_ma20 or cruzou_ma50)
            and rsi14[-1]
            and rsi14[-1] > 50
            and volumes[-1]
            and vol_mean20[-1]
            and volumes[-1] > 1.2 * vol_mean20[-1]
        ):
            await send_alert(session, symbol, interval, "reversal")

        # --- EXAUSTÃO VENDEDORA ---
        body = abs(closes[-1] - opens[-1])
        if (
            rsi14[-1] < 30
            and volumes[-1] < vol_mean20[-1]
            and body < 0.5 * atr14[-1]
            and closes[-1] < ma50[-1]
        ):
            await send_alert(session, symbol, interval, "exhaustion")

    except Exception as e:
        print(f"[{interval}] Erro em {symbol}: {e}")

# -----------------------------
# FILTRO DE PARES (USDT legítimos, spot e fortes)
# -----------------------------
async def get_usdt_pairs(session):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    async with session.get(url) as resp:
        data = await resp.json()
        pairs = []

        for d in data:
            symbol = d["symbol"].upper()

            # apenas pares spot legítimos terminando exatamente em USDT
            if (
                symbol.endswith("USDT")
                and symbol.count("USDT") == 1
                and not any(x in symbol for x in ["BUSD", "USDC", "FDUSD", "TUSD", "EUR", "TRY"])
            ):
                try:
                    volume_usdt = float(d.get("quoteVolume", 0))
                    last_price = float(d.get("lastPrice", 0))

                    # remove moedas fracas (baixo volume ou preço muito baixo)
                    if volume_usdt >= 10000000 and last_price >= 0.0005:
                        pairs.append((symbol, volume_usdt))
                except Exception:
                    continue

        # ordena por volume e retorna top 50
        pairs = [s for s, v in sorted(pairs, key=lambda x: x[1], reverse=True)]
        return pairs[:50]

# -----------------------------
# LOOP PRINCIPAL
# -----------------------------
async def monitor():
    async with aiohttp.ClientSession() as session:
        pairs = await get_usdt_pairs(session)
        print(f"Monitorando {len(pairs)} pares USDT nos intervalos {INTERVALS}...")

        while True:
            tasks = []
            for interval in INTERVALS:
                for symbol in pairs:
                    tasks.append(analyze_symbol(session, symbol, interval))
            await asyncio.gather(*tasks)
            print("Ciclo concluído. Aguardando 60s...\n")
            await asyncio.sleep(60)

# -----------------------------
# EXECUÇÃO (Render Web Service)
# -----------------------------
if __name__ == "__main__":
    def start_monitor():
        asyncio.run(monitor())

    Thread(target=start_monitor, daemon=True).start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
