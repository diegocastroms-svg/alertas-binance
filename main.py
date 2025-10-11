# =====================================================
# 📁 main_v2_6_debug.py — Modo de Diagnóstico Completo
# =====================================================
# Igual ao main_v2_6_full.py, mas com logs detalhados.
# =====================================================

import os
import asyncio
import aiohttp
import threading
from datetime import datetime, timedelta
from statistics import mean
from flask import Flask

# ======================
# 🔧 Variáveis
# ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BASE = "https://api.binance.com/api/v3"

TOP_N = 50
COOLDOWN_MIN = 15
COOLDOWN = timedelta(minutes=COOLDOWN_MIN)
TOP_REFRESH_EVERY = timedelta(hours=1)
ANTI_LIST = ["USD","FDUSD","BUSD","TUSD","USDC","DAI","AEUR","EUR","PYUSD"]

cooldowns = {tf:{} for tf in ["5m","15m","1h","4h"]}
top_pairs_cache = []
next_top_refresh_at = None

app = Flask(__name__)

@app.route("/")
def home():
    return "OK — BOT DEBUG ativo", 200

async def send_telegram(msg:str):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    url=f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload={"chat_id":CHAT_ID,"text":msg,"parse_mode":"HTML","disable_web_page_preview":True}
    async with aiohttp.ClientSession() as s:
        try: await s.post(url,data=payload)
        except Exception as e: print("Erro Telegram:",e)

def chart_link(symbol,tf): return f"binance://app/spot/trade?symbol={symbol.replace('USDT','')}_USDT"

def ma(series,p): return mean(series[-p:]) if len(series)>=p else None

def ema(series,p):
    if len(series)<p: return None
    k=2/(p+1); e=series[-p]
    for x in series[-p+1:]: e=x*k+e*(1-k)
    return e

def rsi(series,p=14):
    if len(series)<p+1: return None
    g,l=[],[]
    for i in range(-p,0):
        d=series[i]-series[i-1]
        (g if d>0 else l).append(abs(d))
    ag=mean(g) if g else 0; al=mean(l) if l else 1e-9
    rs=ag/al; return 100-(100/(1+rs))

async def get_json(session,url):
    async with session.get(url) as r: return await r.json()

async def get_tickers(session): return await get_json(session,f"{BASE}/ticker/24hr")

async def get_klines(session,symbol,interval,limit=240):
    return await get_json(session,f"{BASE}/klines?symbol={symbol}&interval={interval}&limit={limit}")

async def compute_top50(session):
    tick=await get_tickers(session); ranked=[]
    for t in tick:
        s=t["symbol"]
        if not s.endswith("USDT"): continue
        if any(x in s.replace("USDT","") for x in ANTI_LIST): continue
        try:q=float(t["quoteVolume"])
        except:q=0
        ranked.append((s,q))
    ranked.sort(key=lambda x:x[1],reverse=True)
    return [s for s,_ in ranked[:TOP_N]]

async def ensure_top(session):
    global top_pairs_cache,next_top_refresh_at
    now=datetime.utcnow()
    if not next_top_refresh_at or now>=next_top_refresh_at:
        newlist=await compute_top50(session)
        if newlist:
            top_pairs_cache=newlist
            print(f"[{datetime.utcnow()}] 🔄 Lista TOP 50 atualizada ({len(top_pairs_cache)} pares).")
            await send_telegram("🔄 Lista TOP 50 SPOT atualizada (modo debug).")
        next_top_refresh_at=now+TOP_REFRESH_EVERY
    return top_pairs_cache

def was_falling_then_sideways(c):
    if len(c)<60:return False
    ma20_now=ma(c,20); ma20_prev=ma(c[:-20],20)
    falling=(ma20_prev and ma20_now and ma20_now<ma20_prev)
    window=c[-6:]; amp=max(window)-min(window); base=ma20_now or c[-1]
    sideways=base>0 and (amp/base)<0.01
    return falling and sideways

# ======================
# 🔍 Análises
# ======================
async def analyze_5m(session,symbol):
    now=datetime.utcnow()
    if symbol in cooldowns["5m"] and now-cooldowns["5m"][symbol]<COOLDOWN:return
    k=await get_klines(session,symbol,"5m",240)
    if not k or len(k)<210:return
    c=[float(x[4]) for x in k]; v=[float(x[5]) for x in k]; price=c[-1]
    ema9=ema(c,9);ma20=ma(c,20);ma50=ma(c,50);ma200=ma(c,200);rsi14=rsi(c,14)
    volr=(v[-1]/ma(v,20)) if ma(v,20) else 1
    if not all([ema9,ma20,ma50,ma200,rsi14]):return
    print(f"[5m] {symbol}: P={price:.6f} EMA9={ema9:.6f} MA20={ma20:.6f} MA50={ma50:.6f} MA200={ma200:.6f} RSI={rsi14:.1f} VolR={volr:.2f}")

    if ema9>ma20>ma50 and price<ma200 and rsi14>50 and was_falling_then_sideways(c):
        print(f"🟢 {symbol} -> Tendência iniciando (5m)")
        await send_telegram(f"🟢 <b>[TENDÊNCIA INICIANDO 5m]</b> {symbol}\nEMA9>MA20>MA50 abaixo da MA200.\n💰{price:.6f}")
        cooldowns["5m"][symbol]=now

async def analyze_15m(session, symbol):
    now = datetime.utcnow()
    if symbol in cooldowns["15m"] and now - cooldowns["15m"][symbol] < COOLDOWN:
        return
    k = await get_klines(session, symbol, "15m", 240)
    if not k or len(k) < 210:
        return
    c = [float(x[4]) for x in k]
    price = c[-1]
    ema9 = ema(c, 9)
    ma20 = ma(c, 20)
    ma50 = ma(c, 50)
    ma200 = ma(c, 200)
    rsi14 = rsi(c, 14)
    if not all([ema9, ma20, ma50, ma200, rsi14]):
        return
    print(f"[15m] {symbol}: P={price:.6f} EMA9={ema9:.6f} MA20={ma20:.6f} MA50={ma50:.6f} MA200={ma200:.6f} RSI={rsi14:.1f}")
    
    # Verificar condições para notificação
    if ema9 > ma20 > ma50 and price < ma200 and rsi14 > 50 and was_falling_then_sideways(c):
        print(f"🟢 {symbol} -> Tendência iniciando (15m)")
        msg = f"🟢 <b>[TENDÊNCIA INICIANDO 15m]</b> {symbol}\nEMA9>MA20>MA50 abaixo da MA200.\n💰{price:.6f}"
        await send_telegram(msg)
        cooldowns["15m"][symbol] = now

async def analyze_1h(session, symbol):
    now = datetime.utcnow()
    if symbol in cooldowns["1h"] and now - cooldowns["1h"][symbol] < COOLDOWN:
        return
    k = await get_klines(session, symbol, "1h", 240)
    if not k or len(k) < 210:
        return
    c = [float(x[4]) for x in k]
    price = c[-1]
    ema9 = ema(c, 9)
    ma20 = ma(c, 20)
    ma50 = ma(c, 50)
    ma200 = ma(c, 200)
    rsi14 = rsi(c, 14)
    if not all([ema9, ma20, ma50, ma200, rsi14]):
        return
    print(f"[1h] {symbol}: P={price:.6f} EMA9={ema9:.6f} MA20={ma20:.6f} MA50={ma50:.6f} MA200={ma200:.6f} RSI={rsi14:.1f}")
    
    # Verificar condições para notificação
    if ema9 > ma20 > ma50 and price < ma200 and rsi14 > 50 and was_falling_then_sideways(c):
        print(f"🟢 {symbol} -> Tendência iniciando (1h)")
        msg = f"🟢 <b>[TENDÊNCIA INICIANDO 1h]</b> {symbol}\nEMA9>MA20>MA50 abaixo da MA200.\n💰{price:.6f}"
        await send_telegram(msg)
        cooldowns["1h"][symbol] = now

async def analyze_4h(session, symbol):
    now = datetime.utcnow()
    if symbol in cooldowns["4h"] and now - cooldowns["4h"][symbol] < COOLDOWN:
        return
    k = await get_klines(session, symbol, "4h", 240)
    if not k or len(k) < 210:
        return
    c = [float(x[4]) for x in k]
    price = c[-1]
    ema9 = ema(c, 9)
    ma20 = ma(c, 20)
    ma50 = ma(c, 50)
    ma200 = ma(c, 200)
    rsi14 = rsi(c, 14)
    if not all([ema9, ma20, ma50, ma200, rsi14]):
        return
    print(f"[4h] {symbol}: P={price:.6f} EMA9={ema9:.6f} MA20={ma20:.6f} MA50={ma50:.6f} MA200={ma200:.6f} RSI={rsi14:.1f}")
    
    # Verificar condições para notificação
    if ema9 > ma20 > ma50 and price < ma200 and rsi14 > 50 and was_falling_then_sideways(c):
        print(f"🟢 {symbol} -> Tendência iniciando (4h)")
        msg = f"🟢 <b>[TENDÊNCIA INICIANDO 4h]</b> {symbol}\nEMA9>MA20>MA50 abaixo da MA200.\n💰{price:.6f}"
        await send_telegram(msg)
        cooldowns["4h"][symbol] = now

# ======================
# 🔁 LOOP
# ======================
async def main_loop():
    await send_telegram("✅ BOT ATIVO — MODO DEBUG (v2.6)")
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                pairs=await ensure_top(s)
                tasks=[]
                for sym in pairs:
                    tasks+=[analyze_5m(s,sym),analyze_15m(s,sym),analyze_1h(s,sym),analyze_4h(s,sym)]
                await asyncio.gather(*tasks)
            await asyncio.sleep(60)
        except Exception as e:
            print("Erro loop:",e)
            await asyncio.sleep(10)

def _start(): asyncio.run(main_loop())

if __name__=="__main__":
    threading.Thread(target=_start,daemon=True).start()
    port=int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0",port=port,use_reloader=False)
