# main_v3.1_debug.py
# ✅ Igual ao v3.1, apenas com logs e contadores de debug
# ✅ Não altera lógica de alertas — apenas exibe onde está o bloqueio
# ✅ Cooldown reduzido para 5min só para teste

import os, asyncio, math, time
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVALS = ["5m", "15m"]
SHORTLIST_N = 80
MIN_PCT = -1000.0  # Sem filtro temporariamente
MIN_QV = 100_000.0 # Volume mínimo reduzido p/ debug
COOLDOWN_SEC = 300 # 5min p/ testar
DEBUG = True

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# Contadores globais
counts = {
    "loop": 0,
    "pairs": 0,
    "candidates": {"START_5M":0, "PRE_5M":0, "PRE_15M":0, "CONF_15M":0},
    "sent": {"START_5M":0, "PRE_5M":0, "PRE_15M":0, "CONF_15M":0},
    "blocked_cooldown": {"START_5M":0, "PRE_5M":0, "PRE_15M":0, "CONF_15M":0}
}

# ---------------- UTILS ----------------
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")

def fmt_symbol(symbol):
    return symbol.replace("USDT", "/USDT")

async def send_alert(session, text):
    try:
        if WEBHOOK_BASE and WEBHOOK_SECRET:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        if TELEGRAM_TOKEN and CHAT_ID:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
    except Exception as e:
        print("Telegram/Webhook error:", e)

# ---------------- INDICADORES ----------------
def sma(seq,n):
    out,q,s=[],deque(),0.0
    for x in seq:
        q.append(x); s+=x
        if len(q)>n: s-=q.popleft()
        out.append(s/len(q))
    return out

def ema(seq,span):
    if not seq: return []
    a=2/(span+1); e=seq[0]; out=[e]
    for x in seq[1:]:
        e=a*x+(1-a)*e; out.append(e)
    return out

# ---------------- CLASSES ----------------
class Cooldown:
    def __init__(self): self.last=defaultdict(lambda:0.0)
    def allow(self,key): return time.time()-self.last[key]>=COOLDOWN_SEC
    def mark(self,key): self.last[key]=time.time()

# ---------------- DATA ----------------
async def get_klines(session,symbol,interval="5m",limit=200):
    try:
        url=f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
        async with session.get(url,timeout=10) as r:
            r.raise_for_status()
            data=await r.json()
        o,h,l,c=[],[],[],[]
        for k in data[:-1]:
            o.append(float(k[1]));h.append(float(k[2]));l.append(float(k[3]));c.append(float(k[4]))
        return o,h,l,c
    except Exception as e:
        print("get_klines error",symbol,interval,e)
        return [],[],[],[]

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr",timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers,n=100):
    pairs=[]
    for t in tickers:
        s=t.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(x in s for x in ["UP","DOWN","BULL","BEAR","PERP"]): continue
        pct=float(t.get("priceChangePercent","0") or 0)
        qv=float(t.get("quoteVolume","0") or 0)
        if qv>=MIN_QV and abs(pct)>=MIN_PCT:
            pairs.append((s,pct,qv))
    pairs.sort(key=lambda x:x[2],reverse=True)
    return [x[0] for x in pairs[:n]]

# ---------------- MENSAGENS ----------------
def msg_trend_start(symbol,price):
    return f"🟢 {fmt_symbol(symbol)} ⬆️ Tendência iniciando (5m)\n💰 {price}\n🕒 {now_br()}"

def msg_pre5(symbol,price):
    return f"🌕 {fmt_symbol(symbol)} ⬆️ Tendência pré-confirmada (5m)\n💰 {price}\n🕒 {now_br()}"

def msg_pre15(symbol,price):
    return f"🌕 {fmt_symbol(symbol)} ⬆️ Tendência pré-confirmada (15m)\n💰 {price}\n🕒 {now_br()}"

def msg_conf15(symbol,price):
    return f"🚀 {fmt_symbol(symbol)} ⬆️ Tendência confirmada (15m)\n💰 {price}\n🕒 {now_br()}"

# ---------------- WORKER ----------------
async def worker_tf(session,symbol,interval,cool):
    try:
        o,h,l,c=await get_klines(session,symbol,interval)
        if len(c)<60: return
        ema9=ema(c,9);ma20=sma(c,20);ma50=sma(c,50);ma200=sma(c,200)
        last=len(c)-1;price=c[last]
        dropped=c[last]<ma200[last]*0.97  # após queda

        # Tendência iniciando (5m)
        if interval=="5m" and dropped and ema9[last]>ma20[last]>ma50[last]:
            counts["candidates"]["START_5M"]+=1
            if cool.allow((symbol,"START_5M")):
                await send_alert(session,msg_trend_start(symbol,price))
                cool.mark((symbol,"START_5M")); counts["sent"]["START_5M"]+=1
            else:
                counts["blocked_cooldown"]["START_5M"]+=1

        # Pré-confirmada (5m)
        if interval=="5m" and ema9[last]>ma20[last]>ma50[last]>ma200[last]:
            counts["candidates"]["PRE_5M"]+=1
            if cool.allow((symbol,"PRE_5M")):
                await send_alert(session,msg_pre5(symbol,price))
                cool.mark((symbol,"PRE_5M")); counts["sent"]["PRE_5M"]+=1
            else:
                counts["blocked_cooldown"]["PRE_5M"]+=1

        # Pré-confirmada (15m)
        if interval=="15m" and ema9[last]>ma200[last]:
            counts["candidates"]["PRE_15M"]+=1
            if cool.allow((symbol,"PRE_15M")):
                await send_alert(session,msg_pre15(symbol,price))
                cool.mark((symbol,"PRE_15M")); counts["sent"]["PRE_15M"]+=1
            else:
                counts["blocked_cooldown"]["PRE_15M"]+=1

        # Confirmada (15m)
        if interval=="15m" and ema9[last]>ma20[last]>ma50[last]>ma200[last]:
            counts["candidates"]["CONF_15M"]+=1
            if cool.allow((symbol,"CONF_15M")):
                await send_alert(session,msg_conf15(symbol,price))
                cool.mark((symbol,"CONF_15M")); counts["sent"]["CONF_15M"]+=1
            else:
                counts["blocked_cooldown"]["CONF_15M"]+=1

    except Exception as e:
        print("worker",symbol,interval,"err:",e)

# ---------------- MAIN LOOP ----------------
async def main_loop():
    cool=Cooldown()
    async with aiohttp.ClientSession() as session:
        tickers=await get_24h(session)
        watch=shortlist_from_24h(tickers,SHORTLIST_N)
        counts["pairs"]=len(watch)
        print(f"💻 v3.1_debug — {len(watch)} pares SPOT — {now_br()}")
        await send_alert(session,f"💻 v3.1_debug — {len(watch)} pares SPOT — {now_br()}")

        while True:
            tasks=[]
            for s in watch:
                for tf in INTERVALS:
                    tasks.append(worker_tf(session,s,tf,cool))
            await asyncio.gather(*tasks)

            counts["loop"]+=1
            if DEBUG:
                print(f"[LOOP {counts['loop']}] pairs={counts['pairs']} "
                      f"cand={counts['candidates']} sent={counts['sent']} "
                      f"cooldown={counts['blocked_cooldown']} @ {now_br()}")

            await asyncio.sleep(60)

# ---------------- FLASK ----------------
def start_bot():
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass

if __name__=="__main__":
    import threading
    threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home():
        return "✅ Binance Alerts Bot v3.1_debug rodando 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
