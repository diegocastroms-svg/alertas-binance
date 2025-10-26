# main_hibrido_vflex.py
# ✅ Versão final consolidada — inclui ajustes de volume, allowlist, top400 e inicialização corrigida

import os, asyncio, aiohttp, time, math, statistics, traceback
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 8 * 60
TOP_N = 400
REQ_TIMEOUT = 8

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- PARÂMETROS ----------------
MIN_VOL_24H = 3_000_000
NAME_BLOCKLIST = (
    "PEPE","FLOKI","BONK","SHIB","DOGE",
    "HIFI","BAKE","WIF","MEME","1000","ORDI","ZK","ZRO","SAGA"
)
HYPE_SUBSTRINGS = ("AI","GPT","BOT")
ALLOWLIST = ("DIAUSDT",)  # sempre escaneada

# Sensibilidades
BAND_200_BASE = 0.012
VOL_MULT_MIN  = 1.05
VOL_MULT_MAX  = 1.30
RSI_CENTER_WIN = 20
RSI_BAND = 5
RSI_MIN_FLOOR = 42
RSI_MAX_CEIL = 63
DEBUG = True

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m,5m,15m) — INÍCIO DE TENDÊNCIA (foco 3m) — Aurora", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

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

def clamp(x, lo, hi): return max(lo, min(hi, x))

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

def calc_rsi(seq, period=14):
    if len(seq) < period + 1: return [50.0]*len(seq)
    gains, losses = [], []
    for i in range(1, len(seq)):
        diff = seq[i] - seq[i-1]
        gains.append(max(diff,0))
        losses.append(abs(min(diff,0)))
    rsi = []
    avg_gain = sum(gains[:period])/period
    avg_loss = sum(losses[:period])/period
    rs = avg_gain/(avg_loss+1e-12)
    rsi.append(100-(100/(1+rs)))
    for i in range(period, len(seq)-1):
        diff = seq[i]-seq[i-1]
        gain, loss = max(diff,0), abs(min(diff,0))
        avg_gain=(avg_gain*(period-1)+gain)/period
        avg_loss=(avg_loss*(period-1)+loss)/period
        rs=avg_gain/(avg_loss+1e-12)
        rsi.append(100-(100/(1+rs)))
    return [50.0]*(len(seq)-len(rsi))+rsi

def bollinger_bands(seq, n=20, mult=2.0):
    if len(seq)<n: return [],[],[]
    mid,up,low=[],[],[]
    for i in range(len(seq)):
        w=seq[max(0,i-n+1):i+1]
        m=sum(w)/len(w)
        s=statistics.pstdev(w)
        mid.append(m); up.append(m+mult*s); low.append(m-mult*s)
    return up,mid,low

def calc_sar(highs,lows,step=0.02,max_step=0.2):
    if len(highs)<2: return [0.0]*len(highs)
    sar=[0.0]*len(highs)
    uptrend=True; af=step; ep=highs[0]; sar[0]=lows[0]
    for i in range(1,len(highs)):
        prev=sar[i-1]
        if uptrend:
            c=prev+af*(ep-prev)
            sar[i]=min(c,lows[i-1],lows[i])
            if highs[i]>ep: ep=highs[i]; af=min(af+step,max_step)
            if lows[i]<sar[i]: uptrend=False; sar[i]=ep; af=step; ep=lows[i]
        else:
            c=prev+af*(ep-prev)
            sar[i]=max(c,highs[i-1],highs[i])
            if lows[i]<ep: ep=lows[i]; af=min(af+step,max_step)
            if highs[i]>sar[i]: uptrend=True; sar[i]=ep; af=step; ep=highs[i]
    return sar

# ---------------- COOLDOWN ----------------
LAST_HIT={}
def allowed(s,k): return (time.time()-LAST_HIT.get((s,k),0))>=COOLDOWN_SEC
def mark(s,k): LAST_HIT[(s,k)]=time.time()

# ---------------- BINANCE ----------------
async def get_klines(session,symbol,interval,limit=210):
    url=f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url,timeout=REQ_TIMEOUT) as r:
            d=await r.json()
            return d if isinstance(d,list) else []
    except: return []

async def get_top_usdt_symbols(session):
    url=f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url,timeout=REQ_TIMEOUT) as r:
        data=await r.json()
    blocked=("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","USDP",
             "USD1","USDE","XUSD","USDX","GUSD","BFUSD","EUR","EURS","CEUR",
             "BRL","TRY","PERP","_PERP","STABLE","TEST")
    pares=[]
    for d in data:
        s=d.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(x in s for x in blocked): continue
        if any(x in s for x in NAME_BLOCKLIST): continue
        if any(h in s for h in HYPE_SUBSTRINGS): continue
        try: qv=float(d.get("quoteVolume","0")or 0.0)
        except: qv=0.0
        if (qv>=MIN_VOL_24H) or (s in ALLOWLIST):
            pares.append((s,qv))
    pares.sort(key=lambda x:x[1],reverse=True)
    syms=[s for s,_ in pares[:TOP_N]]
    for s in ALLOWLIST:
        if s not in syms: syms.append(s)
    if DEBUG: print(f"{now_br()} - {len(syms)} pares ativos")
    return syms

# ---------------- ALERTA ----------------
async def detectar_inicio(session,symbol,k,tag):
    try:
        if len(k)<60: return
        closes=[float(x[4]) for x in k]; highs=[float(x[2]) for x in k]; lows=[float(x[3]) for x in k]; vols=[float(x[5]) for x in k]
        i=len(closes)-1
        ema9v=ema(closes,9); ema20v=ema(closes,20); ma50v=sma(closes,50); ma200v=sma(closes,200)
        rsi=calc_rsi(closes,14); bb_u,bb_m,bb_l=bollinger_bands(closes); sar=calc_sar(highs,lows)
        close=closes[i]; ma200=ma200v[i]; ema9=ema9v[i]; ema20=ema20v[i]; ma50=ma50v[i]; rsi_now=rsi[-1]; bbm=bb_m[i]
        win=closes[-20:]; mean=sum(win)/len(win); dev=statistics.pstdev(win); vol_norm=dev/max(mean,1e-12)
        band200=clamp(BAND_200_BASE*(1+8*vol_norm),0.007,0.025)
        near=(close<=ma200*(1+band200))
        crossed=False
        if ema9>ema20:
            for off in (1,2,3):
                if i-off<0: break
                if ema9v[i-off]<=ema20v[i-off]: crossed=True; break
        early=ma50<ma200
        rsi_c=sum(rsi[-RSI_CENTER_WIN:])/len(rsi[-RSI_CENTER_WIN:]) if len(rsi)>=RSI_CENTER_WIN else 50
        rlo=clamp(rsi_c-RSI_BAND,RSI_MIN_FLOOR,RSI_MAX_CEIL-2)
        rhi=clamp(rsi_c+RSI_BAND,rlo+2,RSI_MAX_CEIL)
        rsi_ok=rsi_now>=rlo and rsi_now<=rhi
        avg20=sum(vols[-20:])/20; vol_mult=clamp(VOL_MULT_MIN+15*vol_norm,VOL_MULT_MIN,VOL_MULT_MAX)
        vol_ok=vols[-1]>=vol_mult*(avg20+1e-12)
        bb_ok=close>bbm; sar_ok=sar[i]<close
        if near and crossed and early and rsi_ok and vol_ok and bb_ok and sar_ok and allowed(symbol,f"TEND_{tag}"):
            msg=(f"🚀 {symbol} — INÍCIO DE TENDÊNCIA REAL ({tag})\n"
                 f"• RSI {rsi_now:.1f} ({rlo:.0f}-{rhi:.0f})\n"
                 f"• Vol {vols[-1]/max(avg20,1):.2f}× (alvo {vol_mult:.2f})\n"
                 f"• EMA9>EMA20 | SAR↓ | Boll↑ | MA50<{ma200:.2f}\n"
                 f"💰 {fmt_price(close)}\n🕒 {now_br()}\n──────────────────────────────")
            await tg(session,msg); mark(symbol,f"TEND_{tag}")
    except Exception as e:
        if DEBUG: print(f"{now_br()} - erro {symbol} {tag}: {e}")
        traceback.print_exc()

# ---------------- LOOP ----------------
async def scan_symbol(session,symbol):
    try:
        for tf in ("3m","5m","15m"):
            k=await get_klines(session,symbol,tf,limit=210)
            if k: await detectar_inicio(session,symbol,k,tf)
    except Exception as e:
        if DEBUG: print(f"{now_br()} - scan_symbol {symbol}: {e}")

async def main_loop():
    async with aiohttp.ClientSession() as session:
        syms=await get_top_usdt_symbols(session)
        await tg(session,f"✅ Scanner ativo | {len(syms)} pares | {now_br()} 🇧🇷")
        while True:
            await asyncio.gather(*[scan_symbol(session,s) for s in syms])
            await asyncio.sleep(10)

# ---------------- RUN ----------------
def start_bot():
    loop=asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(main_loop())
    threading.Thread(target=loop.run_forever,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",10000)))

if __name__=="__main__":
    start_bot()
