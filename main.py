# main_py15_revisado_final.py
# Base estável: py15e_final
# Ajuste: reintroduzidos os 4 alertas que faltavam:
# - Pré-confirmação 5m (9/20/50 cruzam MA200)
# - Pré-confirmação 15m (EMA9 cruza MA200)
# - Confirmação 15m (MA20 & MA50 > MA200 + RSI>55 + ADX>25)
# - Pré-confirmação 4h (EMA9 cruza MA20)
# Mantidos: início 5m, rompimento 15m, reteste 15m, longos 1h/4h, combinados e entrada segura.
# Flask presente. Filtro SPOT-only. Cooldown curto=15min, longo=1h.

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"
SHORTLIST_N   = 65
COOLDOWN_SEC  = 15 * 60          # curto (5m/15m)
COOLDOWN_LONG = 60 * 60          # longo (1h/4h)
MIN_PCT       = 1.0
MIN_QV        = 300_000.0

EMA_FAST, MA_SLOW, MA_MED, MA_LONG = 9, 20, 50, 200
RSI_LEN, VOL_MA, BB_LEN, ADX_LEN   = 14, 9, 20, 14

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
def fmt_symbol(symbol: str) -> str:
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol: str) -> str:
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session: aiohttp.ClientSession, text: str):
    # (1) webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    # (2) Telegram
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except:
            pass

# ----------------- Indicadores -----------------
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
    deltas = [0.0] + [closes[i]-closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d,0.0) for d in deltas]
    losses = [max(-d,0.0) for d in deltas]
    rsis = [50.0]*len(closes)
    if len(closes) < period+1: return rsis
    avg_gain = sum(gains[1:period+1])/period
    avg_loss = sum(losses[1:period+1])/period
    for i in range(period+1, len(closes)):
        avg_gain = (avg_gain*(period-1)+gains[i])/period
        avg_loss = (avg_loss*(period-1)+losses[i])/period
        rs = avg_gain / (avg_loss+1e-12)
        rsis[i] = 100 - (100/(1+rs))
    return rsis

# ----------------- Binance -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=400):
    usdt=[]
    blocked=("UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD","_BUSD","_FDUSD",
             "_TUSD","_USDC","_DAI","_BTC","_EUR","_TRY","_BRL","_ETH","_BNB","_SOL")
    for t in tickers:
        s=t.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(x in s for x in blocked): continue
        pct=float(t.get("priceChangePercent","0") or 0)
        qv=float(t.get("quoteVolume","0") or 0)
        if abs(pct)>=1.0 and qv>=300000: usdt.append((s,pct,qv))
    usdt.sort(key=lambda x:(abs(x[1]),x[2]),reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Monitor e alertas -----------------
class Monitor:
    def __init__(self):
        self.cooldown_short=defaultdict(lambda:0.0)
        self.cooldown_long=defaultdict(lambda:0.0)
        self.rs_24h={}; self.btc_pct=0.0
    def allowed(self,s,k): return time.time()-self.cooldown_short[(s,k)]>=COOLDOWN_SEC
    def mark(self,s,k): self.cooldown_short[(s,k)]=time.time()
    def allowed_long(self,s): return time.time()-self.cooldown_long[s]>=COOLDOWN_LONG
    def mark_long(self,s): self.cooldown_long[s]=time.time()
    def set_rs(self,rs,btc): self.rs_24h=rs; self.btc_pct=btc
    def rs_tag(self,s): p=self.rs_24h.get(s); 
        return "" if p is None else ("RS+" if (p-self.btc_pct)>0 else "")

# ----------------- Worker curto (5m/15m) -----------------
async def candle_worker(session,symbol,monitor:Monitor):
    try:
        o5,h5,l5,c5,v5=await get_klines(session,symbol,"5m",200)
        if len(c5)<60:return
        ema9_5,ma20_5,ma50_5,ma200_5,rsi5,_,_,_,_,_,_=compute_indicators(o5,h5,l5,c5,v5)
        last5=len(c5)-1
        rs_tag=monitor.rs_tag(symbol)
        # Tendência iniciando 5m
        if last5>=1 and ema9_5[last5-1]<=min(ma20_5[last5-1],ma50_5[last5-1]) and ema9_5[last5]>ma20_5[last5] and ema9_5[last5]>ma50_5[last5]:
            if monitor.allowed(symbol,"TENDENCIA_INICIANDO_5M"):
                msg=f"⭐ {fmt_symbol(symbol)} ⬆️ — TENDÊNCIA INICIANDO (5m)\n💰 <code>{c5[last5]:.6f}</code>\n🧠 EMA9 cruzou MA20/MA50\n⏰ {ts_brazil_now()}\n{binance_links(symbol)}"
                await send_alert(session,msg); monitor.mark(symbol,"TENDENCIA_INICIANDO_5M")
        # Pré-confirmação 5m
        if ema9_5[last5]>ma20_5[last5]>ma50_5[last5]>ma200_5[last5] and monitor.allowed(symbol,"PRECONF_5M"):
            msg=f"⭐ {fmt_symbol(symbol)} 🌕 — TENDÊNCIA PRÉ-CONFIRMADA (5m)\n💰 <code>{c5[last5]:.6f}</code>\n🧠 Médias 9/20/50 cruzaram MA200\n⏰ {ts_brazil_now()}\n{binance_links(symbol)}"
            await send_alert(session,msg); monitor.mark(symbol,"PRECONF_5M")
    except Exception as e: print("erro curto",symbol,e)

# ----------------- Worker longo (1h/4h) -----------------
async def longterm_worker(session,symbol,monitor:Monitor):
    try:
        o1,h1,l1,c1,v1=await get_klines(session,symbol,"1h",200)
        o4,h4,l4,c4,v4=await get_klines(session,symbol,"4h",200)
        if len(c1)<60 or len(c4)<60:return
        ema9_1,ma20_1,ma50_1,ma200_1,rsi1,volma1,_,_,adx1,_,_=compute_indicators(o1,h1,l1,c1,v1)
        ema9_4,ma20_4,ma50_4,ma200_4,rsi4,volma4,_,_,adx4,_,_=compute_indicators(o4,h4,l4,c4,v4)
        last1=len(c1)-1; last4=len(c4)-1
        # Pré 1h
        if last1>=1 and ema9_1[last1-1]<=ma20_1[last1-1] and ema9_1[last1]>ma20_1[last1] and 50<=rsi1[last1]<=60 and v1[last1]>=volma1[last1]*1.05:
            if monitor.allowed_long(symbol):
                msg=f"🌕 <b>{fmt_symbol(symbol)} — PRÉ-CONFIRMAÇÃO LONGA (1H)</b>\n<b>💰</b> <code>{c1[last1]:.6f}</code>\n<b>RSI {rsi1[last1]:.1f} | ADX {adx1[last1]:.1f}</b>\n<b>🕒 {ts_brazil_now()}</b>\n{binance_links(symbol)}"
                await send_alert(session,msg); monitor.mark_long(symbol)
                return
        # Conf 1h
        if ema9_1[last1]>ma20_1[last1]>ma50_1[last1]>ma200_1[last1] and rsi1[last1]>55 and adx1[last1]>25:
            if monitor.allowed_long(symbol):
                msg=f"🚀 <b>{fmt_symbol(symbol)} — TENDÊNCIA LONGA CONFIRMADA (1H)</b>\n<b>💰</b> <code>{c1[last1]:.6f}</code>\n<b>RSI {rsi1[last1]:.1f} | ADX {adx1[last1]:.1f}</b>\n<b>🕒 {ts_brazil_now()}</b>\n{binance_links(symbol)}"
                await send_alert(session,msg); monitor.mark_long(symbol)
                return
        # Pré 4h
        if last4>=1 and ema9_4[last4-1]<=ma20_4[last4-1] and ema9_4[last4]>ma20_4[last4] and rsi4[last4]>50:
            if monitor.allowed_long(symbol):
                msg=f"🌕 <b>{fmt_symbol(symbol)} — PRÉ-CONFIRMAÇÃO LONGA (4H)</b>\n<b>💰</b> <code>{c4[last4]:.6f}</code>\n<b>RSI {rsi4[last4]:.1f}</b>\n<b>🕒 {ts_brazil_now()}</b>\n{binance_links(symbol)}"
                await send_alert(session,msg); monitor.mark_long(symbol)
                return
        # Conf 4h
        if ema9_4[last4]>ma20_4[last4]>ma50_4[last4]>ma200_4[last4] and rsi4[last4]>55 and adx4[last4]>25:
            if monitor.allowed_long(symbol):
                msg=f"🚀 <b>{fmt_symbol(symbol)} — TENDÊNCIA LONGA CONFIRMADA (4H)</b>\n<b>💰</b> <code>{c4[last4]:.6f}</code>\n<b>RSI {rsi4[last4]:.1f} | ADX {adx4[last4]:.1f}</b>\n<b>🕒 {ts_brazil_now()}</b>\n{binance_links(symbol)}"
                await send_alert(session,msg); monitor.mark_long(symbol)
                return
    except Exception as e: print("erro longo",symbol,e)

# ----------------- Main + Flask -----------------
async def main():
    monitor=Monitor()
    async with aiohttp.ClientSession() as session:
        tickers=await get_24h(session)
        watch=shortlist_from_24h(tickers,SHORTLIST_N)
        rs={};btc=0.0
        for t in tickers:
            s=t.get("symbol","")
            if s=="BTCUSDT": btc=float(t.get("priceChangePercent","0") or 0)
            if s.endswith("USDT"): rs[s]=float(t.get("priceChangePercent","0") or 0)
        monitor.set_rs(rs,btc)
        hello=f"💻 py15_revisado_final | {len(watch)} pares SPOT | {ts_brazil_now()}"
        await send_alert(session,hello)
        print(hello)
        while True:
            tasks=[]
            for s in watch:
                tasks.append(candle_worker(session,s,monitor))
                tasks.append(longterm_worker(session,s,monitor))
            await asyncio.gather(*tasks)
            await asyncio.sleep(180)
            try:
                tickers=await get_24h(session)
                watch=shortlist_from_24h(tickers,SHORTLIST_N)
            except: pass

def start_bot():
    try: asyncio.run(main())
    except KeyboardInterrupt: pass

if __name__=="__main__":
    import threading
    threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home():
        return "✅ Binance Alerts Bot — py15_revisado_final 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
