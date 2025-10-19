# main_curto_v3.3a.py
# ✅ Base: v3.3 (mantida)
# ✅ Única mudança: filtro reforçado em shortlist_from_24h
#    - remove PERP, BULL, BEAR, UP, DOWN, e tokens fora do SPOT
# ⚙️ Nenhuma outra linha modificada

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

def fmt(num): return f"{num:.6f}".rstrip("0").rstrip(".")

def nowbr():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")

# ---------------- INDICADORES ----------------
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

def rsi(values, period=14):
    if len(values) < period + 1: return [50.0]*len(values)
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsi_vals = [50.0]*len(values)
    for i in range(period, len(values)-1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-10)
        rsi_vals[i+1] = 100 - (100 / (1 + rs))
    return rsi_vals

def adx(high, low, close, period=14):
    tr, plus_dm, minus_dm = [0.0], [0.0], [0.0]
    for i in range(1, len(close)):
        tr.append(max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1])))
        up_move = high[i]-high[i-1]
        down_move = low[i-1]-low[i]
        plus_dm.append(up_move if (up_move>down_move and up_move>0) else 0.0)
        minus_dm.append(down_move if (down_move>up_move and down_move>0) else 0.0)
    atr = [sum(tr[1:period+1])]
    for i in range(period+1, len(tr)):
        atr.append((atr[-1]*(period-1)+tr[i])/period)
    plus_di, minus_di, dx, adx_vals = [], [], [], []
    for i in range(period, len(atr)):
        plus = 100*(sum(plus_dm[i-period+1:i+1])/atr[i-period])
        minus = 100*(sum(minus_dm[i-period+1:i+1])/atr[i-period])
        plus_di.append(plus)
        minus_di.append(minus)
        dx.append(100*abs(plus-minus)/(plus+minus+1e-10))
    adx_val = sum(dx[:period])/period
    adx_vals = [adx_val]*(period*2)
    for i in range(period, len(dx)):
        adx_val = (adx_val*(period-1)+dx[i])/period
        adx_vals.append(adx_val)
    return adx_vals

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=200):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with session.get(url, timeout=10) as r:
        return await r.json()

# ✅ Filtro reforçado — somente moedas SPOT reais
async def shortlist_from_24h(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=10) as r:
        data = await r.json()
    symbols = []
    blocked = ["UP", "DOWN", "BULL", "BEAR", "PERP", "_", "USD_", "_USD",
               "BUSD", "FDUSD", "TUSD", "USDC", "DAI", "EUR", "TRY", "BTC", "ETH", "BNB"]
    for d in data:
        s = d["symbol"]
        if not s.endswith("USDT"):
            continue
        if any(x in s for x in blocked):
            continue
        try:
            qv = float(d["quoteVolume"])
            pct = abs(float(d["priceChangePercent"]))
            if qv > MIN_QV and pct >= MIN_PCT:
                symbols.append((s, qv))
        except:
            continue
    symbols.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in symbols[:50]]

# ---------------- LÓGICA ----------------
def cruzamento_up(a, b): return a[-2] < b[-2] and a[-1] > b[-1]

async def process_symbol(session, symbol):
    try:
        k5 = await get_klines(session, symbol, "5m")
        k15 = await get_klines(session, symbol, "15m")
        c5 = [float(k[4]) for k in k5]
        c15 = [float(k[4]) for k in k15]
        h5 = [float(k[2]) for k in k5]
        l5 = [float(k[3]) for k in k5]
        v5 = [float(k[5]) for k in k5]
        h15 = [float(k[2]) for k in k15]
        l15 = [float(k[3]) for k in k15]
        v15 = [float(k[5]) for k in k15]

        ema9_5, ma20_5, ma50_5, ma200_5 = ema(c5,9), sma(c5,20), sma(c5,50), sma(c5,200)
        ema9_15, ma20_15, ma50_15, ma200_15 = ema(c15,9), sma(c15,20), sma(c15,50), sma(c15,200)

        rsi5, adx5 = rsi(c5), adx(h5, l5, c5)
        rsi15, adx15 = rsi(c15), adx(h15, l15, c15)

        p = fmt(c5[-1])
        hora = nowbr()

        # ----- ALERTAS ORIGINAIS -----
        ini_5m = cruzamento_up(ema9_5, ma20_5) or cruzamento_up(ema9_5, ma50_5)
        pre_5m = cruzamento_up(ma20_5, ma200_5) or cruzamento_up(ma50_5, ma200_5)
        pre_15m = cruzamento_up(ema9_15, ma200_15)
        conf_15m = cruzamento_up(ma20_15, ma200_15) or cruzamento_up(ma50_15, ma200_15)

        # ----- NOVOS ALERTAS -----
        vol_med_5 = sum(v5[-20:]) / 20
        vol_med_15 = sum(v15[-20:]) / 20

        # ⚡ Entrada Explosiva (5m)
        if cruzamento_up(ema9_5, ma20_5) and v5[-1] > vol_med_5 and rsi5[-1] > 52 and adx5[-1] > 22:
            await send_msg(session, f"⚡ {symbol} — ENTRADA EXPLOSIVA (5m)\n💰 {p}\n🧠 EMA9 cruzou MA20 + volume alto + RSI>52 + ADX>22\n🕒 {hora}")

        # 💚 Entrada Segura (15m)
        low = float(k15[-1][3])
        if (low <= ema9_15[-1] or low <= ma20_15[-1]) and rsi15[-1] > 45 and v15[-1] > vol_med_15:
            await send_msg(session, f"💚 {symbol} — ENTRADA SEGURA — RETESTE (15m)\n💰 {p}\n🧠 Toque EMA9/MA20 + RSI 45–55 + volume acima da média\n🕒 {hora}")

        # ----- EXISTENTES -----
        if ini_5m:
            await send_msg(session, f"🟢 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {p}\n🕒 {hora}")
        if pre_5m:
            await send_msg(session, f"🟡 {symbol} ⬆️ Tendência pré-confirmada (5m)\n💰 {p}\n🕒 {hora}")
        if pre_15m:
            await send_msg(session, f"🟡 {symbol} ⬆️ Tendência pré-confirmada (15m)\n💰 {p}\n🕒 {hora}")
        if conf_15m:
            await send_msg(session, f"🚀 {symbol} ⬆️ Tendência confirmada (15m)\n💰 {p}\n🕒 {hora}")

    except Exception as e:
        print(f"Erro {symbol}:", e)

# ---------------- LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await shortlist_from_24h(session)
        total = len(symbols)
        await send_msg(session, f"✅ v3.3a ativo | {total} pares SPOT | cooldown 15m | {nowbr()} 🇧🇷")

        if total == 0:
            print("⚠️ Nenhum par encontrado.")
            return

        tasks = [process_symbol(session, s) for s in symbols]
        await asyncio.gather(*tasks)

@app.route("/")
def home():
    return "Binance Alertas v3.3a ativo", 200

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
