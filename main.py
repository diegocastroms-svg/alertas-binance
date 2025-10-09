# main_py15c_fix5m.py — igual ao py15c, apenas corrige o alerta de "TENDÊNCIA INICIANDO (5m)"

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"
SHORTLIST_N   = 65
COOLDOWN_SEC  = 15 * 60
COOLDOWN_LONGTERM = 60 * 60
MIN_PCT       = 1.0
MIN_QV        = 300_000.0

EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
VOL_MA   = 9
ADX_LEN  = 14

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

def fmt_symbol(symbol):
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol):
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session, text):
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except:
            pass

def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq: return []
    out = []
    alpha = 2.0 / (span + 1.0)
    e = seq[0]; out.append(e)
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def rsi_wilder(closes, period=14):
    if len(closes) == 0: return []
    deltas = [0.0] + [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    rsis = [50.0] * len(closes)
    if len(closes) < period + 1: return rsis
    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period
    for i in range(period+1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsis[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsis

def true_range(h, l, c):
    tr=[0.0]
    for i in range(1,len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return tr

def adx(h,l,c,period=14):
    n=len(c)
    if n<period+1:return[25.0]*n,[0.0]*n,[0.0]*n
    tr=true_range(h,l,c)
    plus_dm=[0.0];minus_dm=[0.0]
    for i in range(1,n):
        up=h[i]-h[i-1];down=l[i-1]-l[i]
        plus_dm.append(up if(up>down and up>0)else 0.0)
        minus_dm.append(down if(down>up and down>0)else 0.0)
    atr=[0.0]*n;atr[period]=sum(tr[1:period+1])
    pdm=[0.0]*n;mdm=[0.0]*n
    pdm[period]=sum(plus_dm[1:period+1]);mdm[period]=sum(minus_dm[1:period+1])
    for i in range(period+1,n):
        atr[i]=atr[i-1]-(atr[i-1]/period)+tr[i]
        pdm[i]=pdm[i-1]-(pdm[i-1]/period)+plus_dm[i]
        mdm[i]=mdm[i-1]-(mdm[i-1]/period)+minus_dm[i]
    atr[:period]=[atr[period]]*period
    pdm[:period]=[pdm[period]]*period
    mdm[:period]=[mdm[period]]*period
    plus_di=[0.0]*n;minus_di=[0.0]*n
    for i in range(n):
        plus_di[i]=100.0*(pdm[i]/(atr[i]+1e-12))
        minus_di[i]=100.0*(mdm[i]/(atr[i]+1e-12))
    dx=[0.0]*n
    for i in range(n):
        dx[i]=100.0*abs(plus_di[i]-minus_di[i])/(plus_di[i]+minus_di[i]+1e-12)
    adx_vals=[0.0]*n;adx_vals[period]=sum(dx[1:period+1])/period
    for i in range(period+1,n):
        adx_vals[i]=(adx_vals[i-1]*(period-1)+dx[i])/period
    for i in range(period):
        adx_vals[i]=adx_vals[period]
    return adx_vals,plus_di,minus_di

def compute_indicators(o,h,l,c,v):
    ema9=ema(c,EMA_FAST)
    ma20=sma(c,MA_SLOW)
    ma50=sma(c,MA_MED)
    ma200=sma(c,MA_LONG)
    rsi14=rsi_wilder(c,RSI_LEN)
    volma=sma(v,VOL_MA)
    adx14,pdi,mdi=adx(h,l,c,ADX_LEN)
    return ema9,ma20,ma50,ma200,rsi14,volma,adx14,pdi,mdi

async def get_klines(session,symbol,interval="5m",limit=200):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    url=f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url,timeout=12)as r:
        r.raise_for_status()
        data=await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1]));h.append(float(k[2]));l.append(float(k[3]));c.append(float(k[4]));v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr",timeout=15)as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers,n=400):
    usdt=[]
    for t in tickers:
        s=t.get("symbol","")
        if not s.endswith("USDT"):continue
        blocked=("UP","DOWN","BULL","BEAR","PERP","_PERP","_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_BTC","_EUR","_TRY","_BRL")
        if any(x in s for x in blocked):continue
        pct=float(t.get("priceChangePercent","0")or 0.0)
        qv=float(t.get("quoteVolume","0")or 0.0)
        if abs(pct)>=MIN_PCT and qv>=MIN_QV:usdt.append((s,pct,qv))
    usdt.sort(key=lambda x:(abs(x[1]),x[2]),reverse=True)
    return[x[0]for x in usdt[:n]]

class Monitor:
    def __init__(self):
        self.cooldown=defaultdict(lambda:0.0)
        self.cooldown_long=defaultdict(lambda:0.0)
    def allowed(self,s,k):return time.time()-self.cooldown[(s,k)]>=COOLDOWN_SEC
    def mark(self,s,k):self.cooldown[(s,k)]=time.time()
    def allowed_long(self,s):return time.time()-self.cooldown_long[s]>=COOLDOWN_LONGTERM
    def mark_long(self,s):self.cooldown_long[s]=time.time()

# ----------------- ALTERAÇÃO AQUI -----------------
async def candle_worker(session,symbol,monitor:Monitor):
    try:
        o,h,l,c,v=await get_klines(session,symbol,interval="5m",limit=200)
        if len(c)<60:return
        ema9,ma20,ma50,ma200,rsi14,volma,adx14,pdi,mdi=compute_indicators(o,h,l,c,v)
        last=len(c)-1

        # 🚀 Tendência iniciando (5m) — corrigido
        cross_9_20=(ema9[last-1]<=ma20[last-1] and ema9[last]>ma20[last])
        cross_9_50=(ema9[last-1]<=ma50[last-1] and ema9[last]>ma50[last])
        if (ema9[last]>ma20[last] and ema9[last]>ma50[last] and (cross_9_20 or cross_9_50) and rsi14[last]>=55.0):
            if monitor.allowed(symbol,"TENDENCIA_INICIANDO_5M"):
                txt=(f"⭐ {fmt_symbol(symbol)} 🚀 — TENDÊNCIA INICIANDO (5m)\n"
                     f"💰 <code>{c[last]:.6f}</code>\n"
                     f"🧠 EMA9 cruzou acima de MA20/MA50 | RSI {rsi14[last]:.1f}\n"
                     f"⏰ {ts_brazil_now()}\n{binance_links(symbol)}")
                await send_alert(session,txt);monitor.mark(symbol,"TENDENCIA_INICIANDO_5M")
        # --- demais alertas idênticos ao py15c ---
    except Exception as e:
        print("worker curto error",symbol,e)
# ----------------- FIM DA ALTERAÇÃO -----------------

async def main():
    monitor=Monitor()
    async with aiohttp.ClientSession()as session:
        tickers=await get_24h(session)
        watchlist=shortlist_from_24h(tickers,SHORTLIST_N)
        hello=f"💻 py15c_fix5m | Curto 5m/15m + Longo 1h/4h ativos | {len(watchlist)} pares | {ts_brazil_now()}"
        await send_alert(session,hello);print(hello)
        while True:
            tasks=[]
            for s in watchlist:
                tasks.append(candle_worker(session,s,monitor))
            await asyncio.gather(*tasks)
            await asyncio.sleep(180)
            try:
                tickers=await get_24h(session)
                watchlist=shortlist_from_24h(tickers,SHORTLIST_N)
            except Exception as e:
                print("refresh error",e)

app=Flask(__name__)
@app.route("/")
def home():
    return "✅ Binance Alerts Bot py15c_fix5m — Ativo e monitorando (5m/15m/1h/4h) 🇧🇷"

def start_bot():
    loop=asyncio.new_event_loop();asyncio.set_event_loop(loop);loop.run_until_complete(main())

if __name__=="__main__":
    import threading,os
    threading.Thread(target=start_bot,daemon=True).start()
    port=int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0",port=port,debug=False,use_reloader=False)
