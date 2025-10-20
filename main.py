# main_reversao_v5_renderfix.py
# ✅ Base original (257 linhas)
# ✅ Inclui acumulação 5m e 15m
# ✅ Corrigido disparo de "Tendência iniciando (5m)" (últimos 3 candles)
# ✅ Substituída detecção de exaustão por versão real (queda + lateralização + volume baixo)
# ⚙️ Nenhuma outra modificação feita

import os, asyncio, aiohttp, time, math
from datetime import datetime
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 10 * 60
TOP_N = 50
REQ_TIMEOUT = 8

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (5m & 15m) — reversão por cruzamentos | 🇧🇷", 200

# ---------------- UTILS ----------------
def now_br():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        await session.post(url, data=payload, timeout=REQ_TIMEOUT)
    except:
        pass

def fmt_price(x: float) -> str:
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"

def cross_up(a_prev, a_now, b_prev, b_now) -> bool:
    return a_prev <= b_prev and a_now > b_now

def sma(seq, n):
    out, s = [], 0.0
    from collections import deque
    q = deque()
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s/len(q))
    return out

def ema(seq, span):
    if not seq: return []
    alpha = 2.0/(span+1.0)
    out = [seq[0]]
    e = seq[0]
    for x in seq[1:]:
        e = alpha*x + (1-alpha)*e
        out.append(e)
    return out

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=210):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
            if isinstance(data, list):
                return data
            return []
    except:
        return []

async def get_top_usdt_symbols(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=REQ_TIMEOUT) as r:
        data = await r.json()
    blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","USD1","USDE","PERP","_PERP")
    pares = []
    for d in data:
        s = d.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(x in s for x in blocked): continue
        try:
            qv = float(d.get("quoteVolume","0") or 0.0)
        except:
            qv = 0.0
        pares.append((s,qv))
    pares.sort(key=lambda x:x[1],reverse=True)
    return [s for s,_ in pares[:TOP_N]]

# ---------------- ALERT STATE ----------------
LAST_HIT = {}
def allowed(symbol,kind):
    ts = LAST_HIT.get((symbol,kind),0.0)
    return (time.time()-ts)>=COOLDOWN_SEC
def mark(symbol,kind):
    LAST_HIT[(symbol,kind)] = time.time()

# ---------------- CORE CHECKS ----------------
def detect_exhaustion_5m(o, h, l, c, v):
    if len(c) < 30:
        return False, ""
    last = len(c) - 1

    # Média de volume e variação dos últimos candles
    vol_ma20 = sum(v[-20:]) / 20.0
    range_recent = max(c[-10:]) - min(c[-10:])
    avg_price = sum(c[-10:]) / 10.0

    # Condições da exaustão real
    cond_queda = (c[-15] - c[last]) / (c[-15] + 1e-12) >= 0.03  # queda >3% nos últimos 15 candles
    cond_lateral = (range_recent / (avg_price + 1e-12)) < 0.005  # variação lateral <0.5%
    cond_vol_baixo = v[last] < 0.8 * (vol_ma20 + 1e-12)  # volume abaixo de 80% da média

    if cond_queda and cond_lateral and cond_vol_baixo:
        msg = f"🟫 <b>EXAUSTÃO VENDEDORES (5m)</b>\n💰 {fmt_price(c[last])}\n🕒 {now_br()}"
        return True, msg

    return False, ""

def tendencia_iniciando_5m(ema9,ma20,ma50):
    if len(ema9)<4: return False
    for shift in range(3,0,-1):
        i1=len(ema9)-shift; i0=i1-1
        if i1<1: continue
        cross_9_20=cross_up(ema9[i0],ema9[i1],ma20[i0],ma20[i1])
        cross_9_50=cross_up(ema9[i0],ema9[i1],ma50[i0],ma50[i1])
        if (cross_9_20 and ema9[i1]>ma50[i1]) or (cross_9_50 and ema9[i1]>ma20[i1]) or (cross_9_20 and cross_9_50):
            return True
    return False

def preconf_5m_cross_3_over_200(ema9,ma20,ma50,ma200):
    if len(ema9)<2: return False
    i1=len(ema9)-1; i0=i1-1
    all_above=ema9[i1]>ma200[i1] and ma20[i1]>ma200[i1] and ma50[i1]>ma200[i1]
    c9=cross_up(ema9[i0],ema9[i1],ma200[i0],ma200[i1])
    c20=cross_up(ma20[i0],ma20[i1],ma200[i0],ma200[i1])
    c50=cross_up(ma50[i0],ma50[i1],ma200[i0],ma200[i1])
    return all_above and (c9 or c20 or c50)

def preconf_15m_ema9_over_200(ema9,ma200):
    if len(ema9)<2: return False
    i1=len(ema9)-1; i0=i1-1
    return cross_up(ema9[i0],ema9[i1],ma200[i0],ma200[i1])

def conf_15m_all_over_200_recent(ema9,ma20,ma50,ma200):
    if len(ema9)<2: return False
    i1=len(ema9)-1; i0=i1-1
    structure=(ema9[i1]>ma20[i1]>ma50[i1]>ma200[i1])
    c20=cross_up(ma20[i0],ma20[i1],ma200[i0],ma200[i1])
    c50=cross_up(ma50[i0],ma50[i1],ma200[i0],ma200[i1])
    return structure and (c20 or c50)

# ---------------- WORKER ----------------
async def scan_symbol(session,symbol):
    try:
        # 5m
        k5=await get_klines(session,symbol,"5m",limit=210)
        if len(k5)<210: return
        o5=[float(k[1]) for k in k5]
        h5=[float(k[2]) for k in k5]
        l5=[float(k[3]) for k in k5]
        c5=[float(k[4]) for k in k5]
        v5=[float(k[5]) for k in k5]

        ma200_5=sma(c5,200); ema9_5=ema(c5,9); ma20_5=sma(c5,20); ma50_5=sma(c5,50)
        i5=len(c5)-1; below_200=c5[i5]<ma200_5[i5] if ma200_5[i5] else False

        if below_200:
            ok,msg=detect_exhaustion_5m(o5,h5,l5,c5,v5)
            if ok and allowed(symbol,"EXAUSTAO_5M"):
                await tg(session,f"⭐ {symbol}\n{msg}")
                mark(symbol,"EXAUSTAO_5M")

        if tendencia_iniciando_5m(ema9_5,ma20_5,ma50_5) and allowed(symbol,"INI_5M"):
            if (abs(c5[i5]-ma200_5[i5])/(ma200_5[i5]+1e-12))<=0.05 or c5[i5]>ma200_5[i5]:
                p=fmt_price(c5[i5])
                msg=f"🟢 {symbol} ⬆️ Tendência iniciando (5m)\n💰 {p}\n🕒 {now_br()}"
                await tg(session,msg)
                mark(symbol,"INI_5M")

        range5=max(c5[-5:])-min(c5[-5:])
        avg5=sum(c5[-5:])/5.0
        compact=(range5/(avg5+1e-12))<0.004
        near=(abs(ema9_5[i5]-ma20_5[i5])<avg5*0.002 and abs(ma20_5[i5]-ma50_5[i5])<avg5*0.002)
        if below_200 and compact and near and allowed(symbol,"ACUM_5M"):
            p=fmt_price(c5[i5])
            msg=f"🟤 {symbol} ⚖️ Acumulação (5m)\n💰 {p}\n🕒 {now_br()}"
            await tg(session,msg)
            mark(symbol,"ACUM_5M")

        if preconf_5m_cross_3_over_200(ema9_5,ma20_5,ma50_5,ma200_5) and allowed(symbol,"PRE_5M"):
            p=fmt_price(c5[i5])
            msg=f"🟡 {symbol} ⬆️ Tendência pré-confirmada (5m)\n💰 {p}\n🕒 {now_br()}"
            await tg(session,msg)
            mark(symbol,"PRE_5M")

        # 15m
        k15=await get_klines(session,symbol,"15m",limit=210)
        if len(k15)<210: return
        c15=[float(k[4]) for k in k15]
        ema9_15=ema(c15,9); ma20_15=sma(c15,20); ma50_15=sma(c15,50); ma200_15=sma(c15,200)
        j=len(c15)-1; below200_15=c15[j]<ma200_15[j] if ma200_15[j] else False

        range15=max(c15[-8:])-min(c15[-8:])
        avg15=sum(c15[-8:])/8.0
        compact15=(range15/(avg15+1e-12))<0.006
        near15=(abs(ema9_15[j]-ma20_15[j])<avg15*0.003 and abs(ma20_15[j]-ma50_15[j])<avg15*0.003)
        if below200_15 and compact15 and near15 and allowed(symbol,"ACUM_15M"):
            p=fmt_price(c15[j])
            msg=f"🟤 {symbol} ⚖️ Acumulação (15m)\n💰 {p}\n🕒 {now_br()}"
            await tg(session,msg)
            mark(symbol,"ACUM_15M")

        if preconf_15m_ema9_over_200(ema9_15,ma200_15) and allowed(symbol,"PRE_15M"):
            p=fmt_price(c15[j])
            msg=f"🟡 {symbol} ⬆️ Tendência pré-confirmada (15m)\n💰 {p}\n🕒 {now_br()}"
            await tg(session,msg)
            mark(symbol,"PRE_15M")

        if conf_15m_all_over_200_recent(ema9_15,ma20_15,ma50_15,ma200_15) and allowed(symbol,"CONF_15M"):
            p=fmt_price(c15[j])
            msg=f"🚀 {symbol} ⬆️ Tendência confirmada (15m)\n💰 {p}\n🕒 {now_br()}"
            await tg(session,msg)
            mark(symbol,"CONF_15M")

    except:
        return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols=await get_top_usdt_symbols(session)
        await tg(session,f"✅ Scanner ativo | {len(symbols)} pares | cooldown 15m | {now_br()}")
        if not symbols: return
        while True:
            tasks=[scan_symbol(session,s) for s in symbols]
            await asyncio.gather(*tasks)
            await asyncio.sleep(10)

# ---------------- RUN ----------------
def start_bot():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception:
            time.sleep(5)

threading.Thread(target=start_bot,daemon=True).start()
app.run(host="0.0.0.0",port=int(os.getenv("PORT",10000)))
