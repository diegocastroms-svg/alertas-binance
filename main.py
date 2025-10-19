import os
import asyncio
import aiohttp
import numpy as np
import pandas as pd
from flask import Flask
from threading import Thread

# -----------------------------
# CONFIGURAÇÕES GERAIS
# -----------------------------
BINANCE_URL = "https://api.binance.com/api/v3/klines"
TELEGRAM_URL = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_TOKEN')}/sendMessage"
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
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

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

# -----------------------------
# FUNÇÕES DE CÁLCULO TÉCNICO
# -----------------------------
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def ma(series, period):
    return series.rolling(window=period).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def atr(df, period=14):
    high_low = df["high"].astype(float) - df["low"].astype(float)
    high_close = np.abs(df["high"].astype(float) - df["close"].astype(float).shift())
    low_close = np.abs(df["low"].astype(float) - df["close"].astype(float).shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

# -----------------------------
# TELEGRAM
# -----------------------------
async def send_alert(session, symbol, interval, message_type):
    icons = {
        "reversal": "📈 Reversão confirmada",
        "exhaustion": "⚠️ Exaustão vendedora"
    }
    msg = f"{icons[message_type]} ({interval}) detectada em {symbol}"
    payload = {"chat_id": CHAT_ID, "text": msg}
    async with session.post(TELEGRAM_URL, json=payload) as resp:
        await resp.text()

# -----------------------------
# BINANCE API
# -----------------------------
async def fetch_klines(session, symbol, interval, limit=100):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    async with session.get(BINANCE_URL, params=params) as resp:
        data = await resp.json()
        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume","close_time",
            "qav","trades","tbbav","tbqav","ignore"
        ])
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        return df

# -----------------------------
# LÓGICA DE ANÁLISE
# -----------------------------
async def analyze_symbol(session, symbol, interval):
    try:
        df = await fetch_klines(session, symbol, interval)
        if df is None or len(df) < 50:
            return

        df["ema9"] = ema(df["close"], 9)
        df["ma20"] = ma(df["close"], 20)
        df["ma50"] = ma(df["close"], 50)
        df["rsi"] = rsi(df["close"], 14)
        df["vol_mean20"] = df["volume"].rolling(20).mean()
        df["atr14"] = atr(df, 14)

        # --- REVERSÃO CONFIRMADA ---
        cond_cross_ma20 = df["ema9"].iloc[-1] > df["ma20"].iloc[-1] and df["ema9"].iloc[-2] <= df["ma20"].iloc[-2]
        cond_cross_ma50 = df["ema9"].iloc[-1] > df["ma50"].iloc[-1] and df["ema9"].iloc[-2] <= df["ma50"].iloc[-2]
        cond_rsi = df["rsi"].iloc[-1] > 50
        cond_vol = df["volume"].iloc[-1] > 1.2 * df["vol_mean20"].iloc[-1]

        if cond_cross_ma20 and cond_cross_ma50 and cond_rsi and cond_vol:
            await send_alert(session, symbol, interval, "reversal")

        # --- EXAUSTÃO VENDEDORA ---
        body = abs(df["close"].iloc[-1] - df["open"].iloc[-1])
        cond_rsi_exh = df["rsi"].iloc[-1] < 30
        cond_vol_exh = df["volume"].iloc[-1] < df["vol_mean20"].iloc[-1]
        cond_body_exh = body < 0.5 * df["atr14"].iloc[-1]
        cond_price_ma50 = df["close"].iloc[-1] < df["ma50"].iloc[-1]

        if cond_rsi_exh and cond_vol_exh and cond_body_exh and cond_price_ma50:
            await send_alert(session, symbol, interval, "exhaustion")

    except Exception as e:
        print(f"[{interval}] Erro em {symbol}: {e}")

# -----------------------------
# FILTRO DE PARES USDT
# -----------------------------
async def get_usdt_pairs(session):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    async with session.get(url) as resp:
        data = await resp.json()
        pairs = []
        for d in data:
            symbol = d["symbol"]
            if (
                symbol.endswith("USDT")
                and not any(x in symbol for x in ["BUSD", "USDC", "FDUSD", "TUSD", "EUR"])
                and float(d["quoteVolume"]) > 5000000
            ):
                pairs.append(symbol)
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
# EXECUÇÃO
# -----------------------------
if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    asyncio.run(monitor())
