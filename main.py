# main_py15m_stable.py
# ✅ Versão estável — 5m, 15m, 1h, 4h independentes
# Correção: cooldowns separados por timeframe para evitar bloqueios

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
SHORTLIST_N = 65
COOLDOWN_SEC = 15 * 60
COOLDOWN_LONGTERM = 60 * 60
EMA_FAST, MA_SLOW, MA_MED, MA_LONG, RSI_LEN, VOL_MA = 9, 20, 50, 200, 14, 9
MIN_PCT, MIN_QV = 1.0, 300_000.0

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
def fmt_symbol(s): return s[:-4] + "/USDT" if s.endswith("USDT") else s
def binance_links(s):
    b=s.upper().replace("USDT","")
    a=f"https://www.binance.com/en/trade/{b}_USDT?type=spot"
    c=f"https://www.binance.com/en/trade?type=spot&symbol={b}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{c}">Abrir (B)</a>'
def ts_brazil_now(): return (datetime.now(timezone.utc)-timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")+" 🇧🇷"

async def send_alert(session,text):
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try: await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}",json={"message":text},timeout=10)
        except: pass
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            await session.post(url,data={"chat_id":CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":True},timeout=10)
        except: pass

# ----------------- Indicadores -----------------
def sma(seq,n):
    out,q,s=[],deque(),0
    for x in seq:
        q.append(x);s+=x
        if len(q)>n:s-=q.popleft()
        out.append(s/len(q))
    return out
def ema(seq,span):
    if not seq:return []
    a=2/(span+1);e=seq[0];out=[e]
    for x in seq[1:]:
        e=a*x+(1-a)*e;out.append(e)
    return out
def rsi_wilder(c,period=14):
    if len(c)==0:return []
    d=[0]+[c[i]-c[i-1] for i in range(1,len(c))]
    g=[max(x,0) for x in d];l=[max(-x,0) for x in d];r=[50]*len(c)
    if len(c)<period+1:return r
    ag=sum(g[1:period+1])/period;al=sum(l[1:period+1])/period
    for i in range(period+1,len(c)):
        ag=(ag*(period-1)+g[i])/period;al=(al*(period-1)+l[i])/period
        rs=ag/(al+1e-12);r[i]=100-(100/(1+rs))
    return r
def compute_indicators(o,h,l,c,v):
    return ema(c,EMA_FAST),sma(c,MA_SLOW),sma(c,MA_MED),sma(c,MA_LONG),rsi_wilder(c,RSI_LEN),sma(v,VOL_MA)

# ----------------- Binance -----------------
async def get_klines(s,sym,itv="5m",lim=200):
    url=f"{BINANCE_HTTP}/api/v3/klines?{urlencode({'symbol':sym,'interval':itv,'limit':lim})}"
    async with s.get(url,timeout=12) as r:
        r.raise_for_status();d=await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in d[:-1]:
        o.append(float(k[1]));h.append(float(k[2]));l.append(float(k[3]));c.append(float(k[4]));v.append(float(k[5]))
    return o,h,l,c,v
async def get_24h(s):
    async with s.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr",timeout=15) as r:
        r.raise_for_status();return await r.json()
def shortlist_from_24h(ticks,n=400):
    out=[];blk=("UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD","_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_BTC","_EUR","_TRY","_BRL","_ETH","_BNB","_SOL")
    for t in ticks:
        s=t.get("symbol","")
        if not s.endswith("USDT") or any(x in s for x in blk):continue
        pct=float(t.get("priceChangePercent","0")or 0);qv=float(t.get("quoteVolume","0")or 0)
        if abs(pct)>=1 and qv>=300000:out.append((s,pct,qv))
    out.sort(key=lambda x:(abs(x[1]),x[2]),reverse=True)
    return [x[0] for x in out[:n]]

# ----------------- Monitor com cooldown separado -----------------
class Monitor:
    def __init__(s):
        s.cd=defaultdict(lambda:0.0)
    def allowed(s,a,k,tf):return time.time()-s.cd[(a,k,tf)]>=COOLDOWN_SEC
    def mark(s,a,k,tf):s.cd[(a,k,tf)]=time.time()

# ----------------- Worker 5m -----------------
async def worker_5m(sess,sym,m):
    try:
        o,h,l,c,v=await get_klines(sess,sym,"5m",200)
        if len(c)<60:return
        e9,m20,m50,m200,rsi,vm=compute_indicators(o,h,l,c,v);i=len(c)-1;j=i-1
        if e9[j]<=min(m20[j],m50[j]) and e9[i]>m20[i] and e9[i]>m50[i] and m.allowed(sym,"INI5","5m"):
            await send_alert(sess,f"⭐ {fmt_symbol(sym)} ⬆️ — TENDÊNCIA INICIANDO (5m)\n💰 <code>{c[i]:.6f}</code>\n🧠 EMA9 cruzou MA20/MA50\n⏰ {ts_brazil_now()}\n{binance_links(sym)}");m.mark(sym,"INI5","5m")
        if e9[i]>m20[i]>m50[i]>m200[i] and m.allowed(sym,"PRE5","5m"):
            await send_alert(sess,f"🌕 {fmt_symbol(sym)} — PRÉ-CONFIRMADA (5m)\n💰 <code>{c[i]:.6f}</code>\n🧠 Médias cruzaram MA200\n⏰ {ts_brazil_now()}\n{binance_links(sym)}");m.mark(sym,"PRE5","5m")
    except Exception as e:print("5m",sym,e)

# ----------------- Worker 15m -----------------
async def worker_15m(sess,sym,m):
    try:
        o,h,l,c,v=await get_klines(sess,sym,"15m",200)
        if len(c)<60:return
        e9,m20,m50,m200,rsi,vm=compute_indicators(o,h,l,c,v);i=len(c)-1;j=i-1
        if e9[j]<=m200[j] and e9[i]>m200[i] and rsi[i]>=50 and m.allowed(sym,"PRE15","15m"):
            await send_alert(sess,f"🌕 {fmt_symbol(sym)} — PRÉ-CONFIRMADA (15m)\n💰 <code>{c[i]:.6f}</code>\n🧠 EMA9 cruzou MA200\n⏰ {ts_brazil_now()}\n{binance_links(sym)}");m.mark(sym,"PRE15","15m")
        if e9[i]>m20[i]>m50[i]>m200[i] and rsi[i]>55 and m.allowed(sym,"CONF15","15m"):
            await send_alert(sess,f"💎 {fmt_symbol(sym)} — TENDÊNCIA CONFIRMADA (15m)\n💰 <code>{c[i]:.6f}</code>\n🧠 Médias alinhadas + RSI {rsi[i]:.1f}\n⏰ {ts_brazil_now()}\n{binance_links(sym)}");m.mark(sym,"CONF15","15m")
    except Exception as e:print("15m",sym,e)

# ----------------- Worker Longo -----------------
async def worker_long(sess,sym,m):
    try:
        o1,h1,l1,c1,v1=await get_klines(sess,sym,"1h",200)
        o4,h4,l4,c4,v4=await get_klines(sess,sym,"4h",200)
        if len(c1)<60 or len(c4)<60:return
        e91,m201,m501,m2001,rsi1,vm1=compute_indicators(o1,h1,l1,c1,v1)
        e94,m204,m504,m2004,rsi4,vm4=compute_indicators(o4,h4,l4,c4,v4)
        i1=len(c1)-1;i4=len(c4)-1
        if e91[i1-1]<=m201[i1-1] and e91[i1]>m201[i1] and 50<=rsi1[i1]<=60 and m.allowed(sym,"PRE1H","1h"):
            await send_alert(sess,f"🌕 <b>{fmt_symbol(sym)} — PRÉ-CONFIRMAÇÃO LONGA (1H)</b>\n💰 <code>{c1[i1]:.6f}</code>\n🧠 EMA9 cruzou MA20 + RSI {rsi1[i1]:.1f}\n⏰ {ts_brazil_now()}\n{binance_links(sym)}");m.mark(sym,"PRE1H","1h")
        if e91[i1]>m201[i1]>m501[i1]>m2001[i1] and rsi1[i1]>55 and m.allowed(sym,"CONF1H","1h"):
            await send_alert(sess,f"🚀 <b>{fmt_symbol(sym)} — TENDÊNCIA LONGA CONFIRMADA (1H)</b>\n💰 <code>{c1[i1]:.6f}</code>\n🧠 Médias alinhadas + RSI {rsi1[i1]:.1f}\n⏰ {ts_brazil_now()}\n{binance_links(sym)}");m.mark(sym,"CONF1H","1h")
        if e94[i4-1]<=m204[i4-1] and e94[i4]>m204[i4] and rsi4[i4]>50 and m.allowed(sym,"PRE4H","4h"):
            await send_alert(sess,f"🌕 <b>{fmt_symbol(sym)} — PRÉ-CONFIRMAÇÃO LONGA (4H)</b>\n💰 <code>{c4[i4]:.6f}</code>\n🧠 EMA9 cruzou MA20 + RSI {rsi4[i4]:.1f}\n⏰ {ts_brazil_now()}\n{binance_links(sym)}");m.mark(sym,"PRE4H","4h")
        if e94[i4]>m204[i4]>m504[i4]>m2004[i4] and rsi4[i4]>55 and m.allowed(sym,"CONF4H","4h"):
            await send_alert(sess,f"🚀 <b>{fmt_symbol(sym)} — TENDÊNCIA LONGA CONFIRMADA (4H)</b>\n💰 <code>{c4[i4]:.6f}</code>\n🧠 Médias alinhadas + RSI {rsi4[i4]:.1f}\n⏰ {ts_brazil_now()}\n{binance_links(sym)}");m.mark(sym,"CONF4H","4h")
    except Exception as e:print("long",sym,e)

# ----------------- Main -----------------
async def main():
    m=Monitor()
    async with aiohttp.ClientSession() as s:
        t=await get_24h(s);w=shortlist_from_24h(t,SHORTLIST_N)
        hello=f"💻 py15m_stable | {len(w)} pares SPOT | {ts_brazil_now()}";await send_alert(s,hello);print(hello)
        while True:
            tasks=[]
            for x in w:
                tasks+=[worker_5m(s,x,m),worker_15m(s,x,m),worker_long(s,x,m)]
            await asyncio.gather(*tasks)
            await asyncio.sleep(180)

# ----------------- Flask -----------------
def start_bot():
    try:asyncio.run(main())
    except KeyboardInterrupt:pass
if __name__=="__main__":
    import threading
    threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home():return "✅ Binance Alerts Bot py15m_stable 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
