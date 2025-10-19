# main_v4_0.py
# ✅ Intrabar real
# ✅ Flask ativo
# ✅ Top 50 pares SPOT (sem UP/DOWN/Perp)
# ✅ Alertas 5m e 15m completos
# ✅ Cooldown 15 min
# ✅ Mensagens Telegram completas

import os, asyncio, aiohttp, math, time
from datetime import datetime, timezone
from flask import Flask

BINANCE_HTTP = "https://api.binance.com"
COOLDOWN = 15 * 60
LIMIT = 50
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

ULTIMO_ALERTA = {}
app = Flask(__name__)

def agora_br():
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S 🇧🇷")

async def enviar(session, msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})
    except Exception as e:
        print("Erro envio:", e)

def ema(values, period):
    if len(values) < period: return []
    k = 2 / (period + 1)
    e = values[0]
    out = [e]
    for v in values[1:]:
        e = (v * k) + (e * (1 - k))
        out.append(e)
    return out

def sma(values, period):
    return [sum(values[max(0, i-period+1):i+1]) / len(values[max(0, i-period+1):i+1]) for i in range(len(values))]

def rsi(values, period=14):
    if len(values) < period+1: return [50]*len(values)
    deltas = [values[i]-values[i-1] for i in range(1,len(values))]
    gains = [max(d,0) for d in deltas]
    losses = [max(-d,0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rsis = [50]*(period+1)
    for i in range(period+1, len(values)):
        avg_gain = (avg_gain*(period-1)+gains[i-1])/period
        avg_loss = (avg_loss*(period-1)+losses[i-1])/period
        rs = avg_gain / (avg_loss+1e-9)
        rsis.append(100 - (100/(1+rs)))
    return rsis

def adx(high, low, close, period=14):
    if len(close) < period+1: return [20]*len(close)
    plus_dm, minus_dm, tr = [0],[0],[0]
    for i in range(1,len(close)):
        up = high[i]-high[i-1]
        down = low[i-1]-low[i]
        plus_dm.append(up if up>down and up>0 else 0)
        minus_dm.append(down if down>up and down>0 else 0)
        tr_curr = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
        tr.append(tr_curr)
    atr, pdi, mdi, adx_vals = [0]*len(close), [0]*len(close), [0]*len(close), [0]*len(close)
    atr[period] = sum(tr[1:period+1])/period
    pdm = sum(plus_dm[1:period+1])/period
    mdm = sum(minus_dm[1:period+1])/period
    for i in range(period+1,len(close)):
        atr[i]=(atr[i-1]*(period-1)+tr[i])/period
        pdm=(pdm*(period-1)+plus_dm[i])/period
        mdm=(mdm*(period-1)+minus_dm[i])/period
        pdi[i]=100*(pdm/atr[i])
        mdi[i]=100*(mdm/atr[i])
        adx_vals[i]=100*abs(pdi[i]-mdi[i])/(pdi[i]+mdi[i]+1e-9)
    return adx_vals

async def get_klines(session, symbol, interval):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit=200"
    async with session.get(url, timeout=10) as r:
        return await r.json()

async def get_top50(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=10) as r:
        data = await r.json()
    spots = [d for d in data if d["symbol"].endswith("USDT") and all(x not in d["symbol"] for x in ["UP","DOWN","BULL","BEAR","PERP","BUSD","TUSD","FDUSD","USDC"])]
    spots.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
    return [x["symbol"] for x in spots[:LIMIT]]

async def analisar(session, symbol):
    global ULTIMO_ALERTA
    try:
        for tf in ["5m","15m"]:
            kl = await get_klines(session, symbol, tf)
            if not kl or isinstance(kl, dict): continue
            c = [float(k[4]) for k in kl]
            h = [float(k[2]) for k in kl]
            l = [float(k[3]) for k in kl]
            ema9, ma20, ma50, ma200 = ema(c,9), sma(c,20), sma(c,50), sma(c,200)
            rsi14, adx14 = rsi(c), adx(h,l,c)
            last = -1
            preco = c[last]
            chave = f"{symbol}-{tf}"
            if chave in ULTIMO_ALERTA and time.time()-ULTIMO_ALERTA[chave]<COOLDOWN:
                continue

            # 🚀 Tendência iniciando (5m)
            if tf=="5m" and ema9[last]>ma20[last] and ema9[last]>ma50[last] and c[last-1]<ma20[last-1]:
                msg=f"🟢 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {preco:.6f}\n⏰ {agora_br()}"
                await enviar(session,msg); ULTIMO_ALERTA[chave]=time.time()

            # 🌕 Pré-confirmada (5m)
            if tf=="5m" and ema9[last]>ma20[last]>ma50[last]>ma200[last]:
                msg=f"🌕 {symbol} ⚡ Tendência pré-confirmada (5m)\n💰 {preco:.6f}\n⏰ {agora_br()}"
                await enviar(session,msg); ULTIMO_ALERTA[chave]=time.time()

            # 🌕 Pré-confirmada (15m)
            if tf=="15m" and ema9[last]>ma200[last]:
                msg=f"🌕 {symbol} ⚡ Tendência pré-confirmada (15m)\n💰 {preco:.6f}\n⏰ {agora_br()}"
                await enviar(session,msg); ULTIMO_ALERTA[chave]=time.time()

            # 🚀 Confirmada (15m)
            if tf=="15m" and ema9[last]>ma20[last]>ma50[last]>ma200[last] and rsi14[last]>55 and adx14[last]>25:
                msg=f"🚀 {symbol} 🔥 Tendência confirmada (15m)\n💰 {preco:.6f}\n⏰ {agora_br()}"
                await enviar(session,msg); ULTIMO_ALERTA[chave]=time.time()

            # 📈 Rompimento (15m)
            if tf=="15m" and preco>max(c[-21:-1]):
                msg=f"📈 {symbol} 💥 Rompimento da resistência (15m)\n💰 {preco:.6f}\n⏰ {agora_br()}"
                await enviar(session,msg); ULTIMO_ALERTA[chave]=time.time()

            # ♻️ Retestes (15m)
            if tf=="15m" and (abs(c[last]-ema9[last])/ema9[last]<0.002 or abs(c[last]-ma20[last])/ma20[last]<0.002):
                msg=f"♻️ {symbol} 💚 Reteste (15m)\n💰 {preco:.6f}\n⏰ {agora_br()}"
                await enviar(session,msg); ULTIMO_ALERTA[chave]=time.time()
    except Exception as e:
        print("Erro",symbol,e)

async def principal():
    async with aiohttp.ClientSession() as session:
        pares = await get_top50(session)
        await enviar(session,f"✅ Bot ativo | {len(pares)} pares SPOT | cooldown 15m | {agora_br()}")
        while True:
            tarefas = [analisar(session,p) for p in pares]
            await asyncio.gather(*tarefas)
            await asyncio.sleep(60)

@app.route("/")
def home():
    return "Bot Binance v4.0 rodando com sucesso 🚀", 200

if __name__=="__main__":
    import threading
    def loop(): asyncio.run(principal())
    threading.Thread(target=loop,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",10000)))
