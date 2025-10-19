import os, asyncio, time, logging, statistics, threading
from datetime import datetime, timezone
from typing import List, Dict, Any
import aiohttp
from flask import Flask, jsonify

# ===================== LOG =====================
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("alertas-binance")

# ===================== CONFIG =====================
BINANCE = "https://api.binance.com"
INTERVAL = "5m"
TOP_COUNT = 50
REFRESH_SYMBOLS_MIN = 30
SLEEP_SECONDS = 20

EXCLUDE = ("USDC", "BUSD", "EUR", "FDUSD", "TUSD", "DAI", "TRY", "BRL", "UPUSDT", "DOWNUSDT")
PRICE_MIN = 0.01

RSI_EXHAUSTION = 30
RSI_REVERSAL = 50
VOLUME_RATIO = 1.2
ATR_FACTOR = 0.5

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

FLASK_HOST = "0.0.0.0"
FLASK_PORT = int(os.getenv("PORT", 10000))

_start = time.time()
_last_state: Dict[str, str] = {}
_last_alerts: List[Dict[str, Any]] = []
_symbols: List[str] = []

# ===================== UTILS =====================
def now_iso(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def uptime(): return int(time.time() - _start)

async def http_get_json(session, url, params=None):
    for i in range(3):
        try:
            async with session.get(url, params=params, timeout=15) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as e:
            log.warning(f"Erro GET {url}: {e}")
        await asyncio.sleep(1 + i)
    return None

async def send_telegram(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    async with aiohttp.ClientSession() as s:
        try:
            await s.post(api, data=payload, timeout=10)
        except Exception as e:
            log.warning(f"Erro Telegram: {e}")

# ===================== INDICADORES =====================
def sma(v, n):
    if len(v) < n: return []
    out, s = [], sum(v[:n]); out.append(s / n)
    for i in range(n, len(v)):
        s += v[i] - v[i - n]; out.append(s / n)
    return [None]*(n-1) + out

def ema(v, n):
    if len(v) < n: return []
    k = 2/(n+1); e = [sum(v[:n])/n]
    for p in v[n:]: e.append(p*k + e[-1]*(1-k))
    return [None]*(n-1) + e

def rsi(c, n=14):
    if len(c)<=n: return []
    gains, losses = [], []
    for i in range(1, len(c)):
        ch = c[i] - c[i-1]
        gains.append(max(ch,0)); losses.append(max(-ch,0))
    ag, al = sum(gains[:n])/n, sum(losses[:n])/n
    out = [None]*n
    for i in range(n,len(gains)):
        ag = (ag*(n-1)+gains[i])/n
        al = (al*(n-1)+losses[i])/n
        rs = 100 if al==0 else ag/al
        out.append(100-(100/(1+rs)))
    return out

def atr(h,l,c,n=14):
    if len(c)<n+1: return []
    trs=[max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1,len(c))]
    out=[None]*(n-1); first=sum(trs[:n])/n; out.append(first)
    for i in range(n,len(trs)): out.append((out[-1]*(n-1)+trs[i])/n)
    return out

def crossed_up(a_prev,a_now,b_prev,b_now): 
    return a_prev<=b_prev and a_now>b_now

# ===================== BINANCE =====================
async def fetch_top(session):
    url = f"{BINANCE}/api/v3/ticker/24hr"
    data = await http_get_json(session, url)
    if not data: return []
    ranked = []
    for d in data:
        sym = d["symbol"]
        if not sym.endswith("USDT"): continue
        if any(x in sym for x in EXCLUDE): continue
        try:
            p = float(d["lastPrice"]); qv = float(d["quoteVolume"])
            if p < PRICE_MIN: continue
            ranked.append((sym, qv))
        except: continue
    return [s for s,_ in sorted(ranked, key=lambda x:x[1], reverse=True)[:TOP_COUNT]]

async def fetch_klines(session, symbol):
    url=f"{BINANCE}/api/v3/klines"
    return await http_get_json(session, {"symbol":symbol,"interval":INTERVAL,"limit":250})

# ===================== DETECÇÃO =====================
def detect(symbol,o,h,l,c,v):
    if len(c)<60: return ""
    rsi14=rsi(c,14); ema9=ema(c,9); ma20=sma(c,20); ma50=sma(c,50); atr14=atr(h,l,c,14)
    if not (rsi14 and ema9 and ma20 and ma50 and atr14): return ""
    i,ip=len(c)-1,len(c)-2
    body=abs(c[i]-o[i]); atrv=atr14[i] or 1
    vol_m20=statistics.mean(v[-20:])
    vol_dec=all(v[-j]>v[-j+1] for j in range(6,1,-1)) if len(v)>=6 else False
    lateral=body<ATR_FACTOR*atrv
    exaustao=rsi14[i]<RSI_EXHAUSTION and lateral and c[i]<ma50[i] and vol_dec
    cruz20=crossed_up(ema9[ip],ema9[i],ma20[ip],ma20[i])
    cruz50=crossed_up(ema9[ip],ema9[i],ma50[ip],ma50[i])
    reversao=cruz20 and cruz50 and rsi14[i]>RSI_REVERSAL and v[i]>VOLUME_RATIO*vol_m20
    prev=_last_state.get(symbol,"")
    if exaustao and prev!="exaustao":
        _last_state[symbol]="exaustao"
        return f"⚠️ Exaustão vendedora {symbol} | RSI={rsi14[i]:.1f}"
    if reversao and prev=="exaustao":
        _last_state[symbol]="reversao"
        return f"📈 Reversão confirmada {symbol} | RSI={rsi14[i]:.1f}"
    return ""

# ===================== LOOP =====================
async def loop():
    global _symbols
    async with aiohttp.ClientSession() as s:
        _symbols=await fetch_top(s)
        last=time.time()
        while True:
            if time.time()-last>REFRESH_SYMBOLS_MIN*60:
                _symbols=await fetch_top(s); last=time.time()
            tasks=[fetch_klines(s,x) for x in _symbols]
            res=await asyncio.gather(*tasks,return_exceptions=True)
            for sym,data in zip(_symbols,res):
                if not data or isinstance(data,Exception): continue
                o=[float(k[1]) for k in data]; h=[float(k[2]) for k in data]
                l=[float(k[3]) for k in data]; c=[float(k[4]) for k in data]; v=[float(k[5]) for k in data]
                msg=detect(sym,o,h,l,c,v)
                if msg:
                    log.info(msg)
                    await send_telegram(msg)
            await asyncio.sleep(SLEEP_SECONDS)

# ===================== FLASK =====================
app = Flask(__name__)
@app.route("/health") 
def health(): return jsonify({"status":"ok","uptime":uptime()})
@app.route("/status")
def status(): 
    return jsonify({"uptime":uptime(),"symbols":_symbols[:10],"alerts":_last_alerts[-5:]})

def run_flask():
    app.run(host=FLASK_HOST, port=FLASK_PORT)

# ===================== MAIN =====================
def main():
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(loop())

if __name__=="__main__":
    main()
