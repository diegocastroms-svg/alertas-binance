# main_curto_v3.3.py
# ✅ Estrutura original preservada
# ✅ Apenas adicionado alerta intrabar 5m
# ✅ Compatível com Procfile e requirements.txt atuais

import os, asyncio, aiohttp, math, time
from datetime import datetime, timezone
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVALS = ["5m", "15m"]
MIN_PCT = 0.0
MIN_QV = 10000.0
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

def nowbr():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=50):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with session.get(url, timeout=10) as r:
        return await r.json()

async def shortlist_from_24h(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=10) as r:
        data = await r.json()
    symbols = []
    for d in data:
        s = d["symbol"]
        if not s.endswith("USDT"): continue
        if any(x in s for x in ["UP", "DOWN", "BUSD", "FDUSD", "TUSD", "USDC", "USD1"]): continue
        try:
            qv = float(d["quoteVolume"])
            pct = abs(float(d["priceChangePercent"]))
            if qv > MIN_QV and pct >= MIN_PCT:
                symbols.append(s)
        except:
            continue
    # 🔹 limita aos 50 maiores volumes
    symbols = sorted(symbols, key=lambda sym: next((float(x["quoteVolume"]) for x in data if x["symbol"] == sym), 0), reverse=True)[:50]
    return symbols

def ema(values, period):
    k = 2 / (period + 1)
    ema_values = []
    for i, price in enumerate(values):
        if i == 0:
            ema_values.append(price)
        else:
            ema_values.append(price * k + ema_values[-1] * (1 - k))
    return ema_values

def sma(values, period):
    return [sum(values[i-period+1:i+1])/period if i+1>=period else sum(values[:i+1])/(i+1) for i in range(len(values))]

# ---------------- ALERTAS ----------------
def cruzamento_up(a, b): return a[-2] < b[-2] and a[-1] > b[-1]
def cruzamento_down(a, b): return a[-2] > b[-2] and a[-1] < b[-1]

async def process_symbol(session, symbol):
    try:
        k5 = await get_klines(session, symbol, "5m")
        k15 = await get_klines(session, symbol, "15m")
        c5 = [float(k[4]) for k in k5]
        c15 = [float(k[4]) for k in k15]
        v5 = [float(k[5]) for k in k5]

        ema9_5, ma20_5, ma50_5, ma200_5 = ema(c5,9), sma(c5,20), sma(c5,50), sma(c5,200)
        ema9_15, ma20_15, ma50_15, ma200_15 = ema(c15,9), sma(c15,20), sma(c15,50), sma(c15,200)

        p = fmt(c5[-1])
        hora = nowbr()

        # ---------------------------------------------------
        # 🚀 NOVO ALERTA — TENDÊNCIA INICIANDO (5m intrabar)
        # ---------------------------------------------------
        if cruzamento_up(ema9_5, ma20_5) and v5[-1] > sum(v5[-10:]) / 10 * 1.3:
            await send_msg(session, f"🚀 {symbol} ⬆️ Tendência iniciando (5m intrabar)\n💰 {p}\n🕒 {hora}")

        # ---------------------------------------------------
        # 🔹 ALERTAS ORIGINAIS (mantidos)
        # ---------------------------------------------------
        ini_5m = cruzamento_up(ema9_5, ma20_5) or cruzamento_up(ema9_5, ma50_5)
        pre_5m = cruzamento_up(ma20_5, ma200_5) or cruzamento_up(ma50_5, ma200_5)
        pre_15m = cruzamento_up(ema9_15, ma200_15)
        conf_15m = cruzamento_up(ma20_15, ma200_15) or cruzamento_up(ma50_15, ma200_15)

        if ini_5m:
            await send_msg(session, f"🟢 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {p}\n🕒 {hora}")
        if pre_5m:
            await send_msg(session, f"🟡 {symbol} ⬆️ Tendência pré-confirmada (5m)\n💰 {p}\n🕒 {hora}")
        if pre_15m:
            await send_msg(session, f"🌕 {symbol} ⬆️ Tendência pré-confirmada (15m)\n💰 {p}\n🕒 {hora}")
        if conf_15m:
            await send_msg(session, f"🚀 {symbol} ⬆️ Tendência confirmada (15m)\n💰 {p}\n🕒 {hora}")

    except Exception as e:
        print(f"Erro {symbol}:", e)

# ---------------- LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await shortlist_from_24h(session)
        total = len(symbols)
        await send_msg(session, f"✅ v3.3 intrabar ativo | {total} pares SPOT | cooldown 15m | {nowbr()} 🇧🇷")

        if total == 0:
            print("⚠️ Nenhum par encontrado, revise filtros.")
            return

        tasks = [process_symbol(session, s) for s in symbols]
        await asyncio.gather(*tasks)

@app.route("/")
def home():
    return "Binance Alertas v3.3 ativo", 200

if __name__ == "__main__":
    import threading

    def runner():
        while True:
            try:
                asyncio.run(main_loop())
            except Exception as e:
                print("Loop error:", e)
            time.sleep(COOLDOWN)

    threading.Thread(target=runner, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
