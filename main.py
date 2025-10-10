# main_v3_2.py
# v3.2 — Correção total dos alertas (5m/15m timing, reset e envio garantido)
# Estrutura idêntica à v3.1 com ajustes mínimos
import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

BINANCE_HTTP = "https://api.binance.com"
INTERVAL_5M, INTERVAL_15M, INTERVAL_1H, INTERVAL_4H = "5m","15m","1h","4h"
SHORTLIST_N = 65
SCAN_INTERVAL_SEC = 15
COOLDOWN_SHORT_SEC = 60   # debug
COOLDOWN_LONG_SEC  = 3600
MIN_PCT, MIN_QV = 1.0, 300_000.0
EMA_FAST, MA_SLOW, MA_MED, MA_LONG = 9, 20, 50, 200
RSI_LEN, VOL_MA, BB_LEN, ADX_LEN, DONCHIAN_N = 14, 9, 20, 14, 20

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ---------- Utils ----------
def fmt_symbol(s): return s[:-4]+"/USDT" if s.endswith("USDT") else s
def binance_links(s):
    base = s.upper().replace("USDT","")
    a=f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b=f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'
def ts_brazil_now(): return (datetime.now(timezone.utc)-timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")+" 🇧🇷"

async def send_alert(session, text):
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try: await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=8)
        except: pass
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            await session.post(url,data={"chat_id":CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":True},timeout=8)
        except: pass

# ---------- Indicators ----------
def sma(a,n):
    out,q,s=[],deque(),0.0
    for x in a:
        q.append(x);s+=x
        if len(q)>n:s-=q.popleft()
        out.append(s/len(q))
    return out
def ema(a,n):
    if not a:return []
    out=[a[0]];alpha=2/(n+1);e=a[0]
    for x in a[1:]:
        e=alpha*x+(1-alpha)*e;out.append(e)
    return out
def rolling_std(a,n):
    out,q=[],deque()
    for x in a:
        q.append(x)
        if len(q)>n:q.popleft()
        m=sum(q)/len(q)
        out.append((sum((v-m)**2 for v in q)/len(q))**0.5)
    return out
def rsi(cl,period=14):
    if len(cl)<period+1:return [50]*len(cl)
    d=[cl[i]-cl[i-1] for i in range(1,len(cl))]
    g=[max(x,0) for x in d];l=[max(-x,0) for x in d]
    rsis=[50]*len(cl);ag=sum(g[:period])/period;al=sum(l[:period])/period
    for i in range(period,len(cl)-1):
        ag=(ag*(period-1)+g[i])/period;al=(al*(period-1)+l[i])/period
        rs=ag/(al+1e-12);rsis[i+1]=100-100/(1+rs)
    return rsis
def true_range(h,l,c):
    tr=[0]
    for i in range(1,len(c)):
        tr.append(max(h[i]-l[i],abs(h[i]-c[i-1]),abs(l[i]-c[i-1])))
    return tr
def adx(h,l,c,period=14):
    n=len(c)
    if n<period+1:return [20]*n,[0]*n,[0]*n
    tr=true_range(h,l,c)
    pdm,mdm=[0],[0]
    for i in range(1,n):
        up=h[i]-h[i-1];dn=l[i-1]-l[i]
        pdm.append(up if up>dn and up>0 else 0)
        mdm.append(dn if dn>up and dn>0 else 0)
    atr=[0]*n;atr[period]=sum(tr[1:period+1])
    pd=[0]*n;md=[0]*n;pd[period]=sum(pdm[1:period+1]);md[period]=sum(mdm[1:period+1])
    for i in range(period+1,n):
        atr[i]=atr[i-1]-(atr[i-1]/period)+tr[i]
        pd[i]=pd[i-1]-(pd[i-1]/period)+pdm[i]
        md[i]=md[i-1]-(md[i-1]/period)+mdm[i]
    pdi=[100*(pd[i]/(atr[i]+1e-9)) for i in range(n)]
    mdi=[100*(md[i]/(atr[i]+1e-9)) for i in range(n)]
    dx=[100*abs(pdi[i]-mdi[i])/(pdi[i]+mdi[i]+1e-9) for i in range(n)]
    adxv=[0]*n;adxv[period]=sum(dx[1:period+1])/period
    for i in range(period+1,n):adxv[i]=(adxv[i-1]*(period-1)+dx[i])/period
    return adxv,pdi,mdi
def compute(o,h,l,c,v):
    e9=ema(c,EMA_FAST);m20=sma(c,MA_SLOW);m50=sma(c,MA_MED);m200=sma(c,MA_LONG)
    r=rsi(c,RSI_LEN);vm=sma(v,VOL_MA);bb=rolling_std(c,BB_LEN)
    ad,_,_=adx(h,l,c,ADX_LEN)
    return e9,m20,m50,m200,r,vm,ad
async def get_klines(session,sym,itv="5m",limit=200):
    u=f"{BINANCE_HTTP}/api/v3/klines?symbol={sym}&interval={itv}&limit={limit}"
    async with session.get(u,timeout=12) as r:r.raise_for_status();d=await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in d:o.append(float(k[1]));h.append(float(k[2]));l.append(float(k[3]));c.append(float(k[4]));v.append(float(k[5]))
    return o,h,l,c,v
async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr",timeout=15) as r:
        r.raise_for_status();return await r.json()
def shortlist(t,n=60):
    out=[]
    for x in t:
        s=x.get("symbol","")
        if not s.endswith("USDT") or any(b in s for b in["UP","DOWN","BULL","BEAR","PERP"]):continue
        try:p=float(x["priceChangePercent"]);q=float(x["quoteVolume"])
        except:p,q=0,0
        if abs(p)>=1 and q>=300000:out.append((s,abs(p),q))
    out.sort(key=lambda x:(x[1],x[2]),reverse=True)
    return [x[0] for x in out[:n]]

# ---------- Core ----------
def crossed_up(a_prev,a_now,b_prev,b_now):return a_prev<=b_prev and a_now>b_now
def is_lateral(c,ma20,win=10,band=0.01):
    if len(c)<win+1:return False
    seg=c[-win:];m=sum(seg)/len(seg)
    return (max(abs(x-m)/m for x in seg)<band)

class Mon:
    def __init__(self):self.cd=defaultdict(lambda:0.0);self.stage=defaultdict(lambda:0)
    def ok(self,sym,k):return time.time()-self.cd[(sym,k)]>=COOLDOWN_SHORT_SEC
    def mark(self,sym,k):self.cd[(sym,k)]=time.time()
    def st(self,sym):return self.stage[sym]
    def setst(self,sym,v):self.stage[sym]=v
    def reset(self,sym,e9,m20,m50,m200,i):
        if not(e9[i]>m20[i]>m50[i]>m200[i]):self.stage[sym]=0

def build_msg(sym,title,price,desc,bold=False):
    symb=fmt_symbol(sym)
    h=f"<b>{symb} — {title}</b>" if bold else f"{symb} — {title}"
    return f"⭐ {h}\n💰 <code>{price:.6f}</code>\n🧠 {desc}\n⏰ {ts_brazil_now()}\n{binance_links(sym)}"

async def w5(session,sym,m:Mon):
    try:
        o,h,l,c,v=await get_klines(session,sym,INTERVAL_5M,210)
        e9,m20,m50,m200,r,vm,ad=compute(o,h,l,c,v);i=len(c)-1;ip=i-1
        m.reset(sym,e9,m20,m50,m200,i)
        if m.st(sym)==0 and crossed_up(e9[ip],e9[i],m20[ip],m20[i]) and e9[i]>m50[i]:
            if m.ok(sym,"INICIO_5M"):
                await send_alert(session,build_msg(sym,"TENDÊNCIA INICIANDO (5m)",c[i],f"EMA9 cruzou MA20 > MA50 | RSI {r[i]:.1f}"))
                print(sym,"5m inicio");m.mark(sym,"INICIO_5M");m.setst(sym,1)
        if m.st(sym)<=1:
            prev=(e9[ip]<=m200[ip])or(m20[ip]<=m200[ip])or(m50[ip]<=m200[ip])
            now=(e9[i]>m200[i]and m20[i]>m200[i]and m50[i]>m200[i])
            if prev and now and m.ok(sym,"PRECONF_5M"):
                await send_alert(session,build_msg(sym,"TENDÊNCIA PRÉ-CONFIRMADA (5m)",c[i],"Médias 9/20/50 cruzaram MA200"))
                print(sym,"5m preconf");m.mark(sym,"PRECONF_5M");m.setst(sym,2)
        if is_lateral(c,m20):m.setst(sym,0)
    except Exception as e:print("w5",sym,e)

async def w15(session,sym,m:Mon):
    try:
        o,h,l,c,v=await get_klines(session,sym,INTERVAL_15M,210)
        e9,m20,m50,m200,r,vm,ad=compute(o,h,l,c,v);i=len(c)-1;ip=i-1
        if crossed_up(e9[ip],e9[i],m200[ip],m200[i]) and m.ok(sym,"PRECONF_15M"):
            await send_alert(session,build_msg(sym,"TENDÊNCIA PRÉ-CONFIRMADA (15m)",c[i],f"EMA9 cruzou MA200 | RSI {r[i]:.1f} | ADX {ad[i]:.1f}"))
            print(sym,"15m preconf");m.mark(sym,"PRECONF_15M")
        prev=not(e9[ip]>m20[ip]>m50[ip]>m200[ip]and r[ip]>55 and ad[ip]>25)
        now=(e9[i]>m20[i]>m50[i]>m200[i]and r[i]>55 and ad[i]>25)
        if prev and now and m.ok(sym,"CONFIRM_15M"):
            await send_alert(session,build_msg(sym,"TENDÊNCIA CONFIRMADA (15m)",c[i],f"EMA9>MA20>MA50>MA200 | RSI {r[i]:.1f} | ADX {ad[i]:.1f}"))
            print(sym,"15m confirm");m.mark(sym,"CONFIRM_15M")
    except Exception as e:print("w15",sym,e)

async def w1(session,sym,m:Mon):
    try:
        o,h,l,c,v=await get_klines(session,sym,INTERVAL_1H,210)
        e9,m20,m50,m200,r,vm,ad=compute(o,h,l,c,v);i=len(c)-1;ip=i-1
        if crossed_up(e9[ip],e9[i],m20[ip],m20[i]) and 50<=r[i]<=60 and v[i]>=vm[i]*1.05 and m.ok(sym,"PRECONF_1H"):
            await send_alert(session,build_msg(sym,"PRÉ-CONFIRMAÇÃO LONGA (1h)",c[i],"EMA9 cruzou MA20 + RSI e Volume",True))
            m.mark(sym,"PRECONF_1H")
        prev=not(e9[ip]>m20[ip]>m50[ip]and r[ip]>55 and ad[ip]>25)
        now=(e9[i]>m20[i]>m50[i]and r[i]>55 and ad[i]>25)
        if prev and now and m.ok(sym,"CONFIRM_1H"):
            await send_alert(session,build_msg(sym,"TENDÊNCIA LONGA CONFIRMADA (1h)",c[i],"EMA9>MA20>MA50 + RSI>55 + ADX>25",True))
            m.mark(sym,"CONFIRM_1H")
    except Exception as e:print("w1",sym,e)

async def w4(session,sym,m:Mon):
    try:
        o,h,l,c,v=await get_klines(session,sym,INTERVAL_4H,210)
        e9,m20,m50,m200,r,vm,ad=compute(o,h,l,c,v);i=len(c)-1;ip=i-1
        if crossed_up(e9[ip],e9[i],m20[ip],m20[i]) and r[i]>50 and m.ok(sym,"PRECONF_4H"):
            await send_alert(session,build_msg(sym,"PRÉ-CONFIRMAÇÃO LONGA (4h)",c[i],"EMA9 cruzou MA20 + RSI>50",True))
            m.mark(sym,"PRECONF_4H")
        if e9[i]>m20[i]>m50[i]and e9[ip]>m20[ip]>m50[ip]and r[i]>55 and m.ok(sym,"CONFIRM_4H"):
            await send_alert(session,build_msg(sym,"TENDÊNCIA 4H CONFIRMADA",c[i],"Estrutura mantida 2 velas + RSI>55",True))
            m.mark(sym,"CONFIRM_4H")
    except Exception as e:print("w4",sym,e)

async def main():
    m=Mon()
    async with aiohttp.ClientSession() as s:
        t=await get_24h(s);wl=shortlist(t,SHORTLIST_N)
        hello=f"💻 v3.2 ativo — {len(wl)} pares SPOT | {ts_brazil_now()}";await send_alert(s,hello);print(hello)
        while True:
            tasks=[]
            for x in wl:tasks+=[w5(s,x,m),w15(s,x,m),w1(s,x,m),w4(s,x,m)]
            await asyncio.gather(*tasks,return_exceptions=True)
            await asyncio.sleep(SCAN_INTERVAL_SEC)

def start_bot():
    try:asyncio.run(main())
    except KeyboardInterrupt:pass

if __name__=="__main__":
    import threading;threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home():return "✅ Binance Alerts Bot v3.2 — fix timing 5m/15m + stable long alerts 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
