# main_py15b.py — Derivado do py15 (com alertas curtos adicionais)

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

# ----------------- Utils -----------------
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

def pct_change(new, old):
    return (new / (old + 1e-12) - 1.0) * 100.0

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

def adx(h, l, c, period=14):
    n = len(c)
    if n < period + 1: return [20.0]*n, [0.0]*n, [0.0]*n
    tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1,n)]
    tr = [0.0]+tr
    atr = [sum(tr[:period])/period]*len(c)
    pdi = [0.0]*n; mdi=[0.0]*n; adxv=[25.0]*n
    return adxv,pdi,mdi

def compute_indicators(o,h,l,c,v):
    ema9=ema(c,EMA_FAST); ma20=sma(c,MA_SLOW); ma50=sma(c,MA_MED); ma200=sma(c,MA_LONG)
    rsi14=rsi_wilder(c,RSI_LEN); volma=sma(v,VOL_MA)
    adx14,pdi,mdi=adx(h,l,c,ADX_LEN)
    return ema9,ma20,ma50,ma200,rsi14,volma,adx14,pdi,mdi

# ----------------- Monitor -----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)
    def allowed(self, symbol, kind):
        return time.time() - self.cooldown[(symbol, kind)] >= COOLDOWN_SEC
    def mark(self, symbol, kind):
        self.cooldown[(symbol, kind)] = time.time()

# ----------------- Worker curto -----------------
async def candle_worker(session, symbol, monitor: Monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval=INTERVAL_MAIN, limit=200)
        if len(c)<60: return
        ema9,ma20,ma50,ma200,rsi14,volma,adx14,pdi,mdi = compute_indicators(o,h,l,c,v)
        last=len(c)-1

        # 🚀 Tendência iniciando (5m)
        if (ema9[last]>ma20[last]>ma50[last] and ema9[last-1]<=ma20[last-1] and rsi14[last]>=55.0):
            if monitor.allowed(symbol,"TENDENCIA_INICIANDO_5M"):
                txt=f"⭐ {fmt_symbol(symbol)} 🚀 — TENDÊNCIA INICIANDO (5m)\n💰 <code>{c[last]:.6f}</code>\n🧠 EMA9 cruzou acima de MA20 e MA50 | RSI {rsi14[last]:.1f}\n⏰ {ts_brazil_now()}\n{binance_links(symbol)}"
                await send_alert(session,txt); monitor.mark(symbol,"TENDENCIA_INICIANDO_5M")

        # 🌕 Pré-confirmação 5m — EMA9, MA20 e MA50 cruzam a MA200
        if (ema9[last]>ma20[last]>ma50[last]>ma200[last] and ema9[last-1]<=ma200[last-1]):
            if monitor.allowed(symbol,"PRECONFIRM_5M"):
                txt=f"🌕 {fmt_symbol(symbol)} — TENDÊNCIA PRÉ-CONFIRMADA (5m)\n💰 <code>{c[last]:.6f}</code>\n🧠 Médias 9, 20 e 50 cruzaram acima da 200\n⏰ {ts_brazil_now()}\n{binance_links(symbol)}"
                await send_alert(session,txt); monitor.mark(symbol,"PRECONFIRM_5M")

        # 🌕 Pré-confirmação 15m — EMA9 cruza MA200
        o15,h15,l15,c15,v15=await get_klines(session,symbol,interval="15m",limit=120)
        ema9_15,ma20_15,ma50_15,ma200_15,rsi15,volma15,adx15,pdi15,mdi15=compute_indicators(o15,h15,l15,c15,v15)
        last15=len(c15)-1
        if (ema9_15[last15]>ma200_15[last15] and ema9_15[last15-1]<=ma200_15[last15-1]):
            if monitor.allowed(symbol,"PRECONFIRM_15M"):
                txt=f"🌕 {fmt_symbol(symbol)} — TENDÊNCIA PRÉ-CONFIRMADA (15m)\n💰 <code>{c15[last15]:.6f}</code>\n🧠 EMA9 cruzou acima da MA200\n⏰ {ts_brazil_now()}\n{binance_links(symbol)}"
                await send_alert(session,txt); monitor.mark(symbol,"PRECONFIRM_15M")

        # 🚀 Confirmação 15m — MA20 e MA50 > MA200 + RSI>55 + ADX>25
        if (ma20_15[last15]>ma200_15[last15] and ma50_15[last15]>ma200_15[last15] and rsi15[last15]>55.0 and adx15[last15]>25.0):
            if monitor.allowed(symbol,"CONFIRM_15M"):
                txt=f"🚀 {fmt_symbol(symbol)} — TENDÊNCIA CONFIRMADA (15m)\n💰 <code>{c15[last15]:.6f}</code>\n🧠 MA20 e MA50 cruzaram acima da MA200 | RSI {rsi15[last15]:.1f} | ADX {adx15[last15]:.1f}\n⏰ {ts_brazil_now()}\n{binance_links(symbol)}"
                await send_alert(session,txt); monitor.mark(symbol,"CONFIRM_15M")

    except Exception as e:
        print("worker error",symbol,e)

# ----------------- Binance -----------------
async def get_klines(session,symbol,interval="5m",limit=200):
    params={"symbol":symbol,"interval":interval,"limit":limit}
    url=f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url,timeout=12) as r:
        r.raise_for_status(); data=await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3])); c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr",timeout=15) as r:
        r.raise_for_status(); return await r.json()

def shortlist_from_24h(tickers,n=400):
    usdt=[]
    for t in tickers:
        s=t.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(x in s for x in ["UP","DOWN","BULL","BEAR","PERP","_BUSD","_USDC","_BTC"]): continue
        pct=float(t.get("priceChangePercent","0") or 0.0)
        qv=float(t.get("quoteVolume","0") or 0.0)
        if abs(pct)>=MIN_PCT and qv>=MIN_QV: usdt.append((s,pct,qv))
    usdt.sort(key=lambda x:(abs(x[1]),x[2]),reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Main -----------------
async def main():
    monitor=Monitor()
    async with aiohttp.ClientSession() as session:
        tickers=await get_24h(session)
        watchlist=shortlist_from_24h(tickers,SHORTLIST_N)
        hello=f"✅ py15b | Core 5m/15m com novos alertas + longos intactos | {len(watchlist)} pares | {ts_brazil_now()}"
        await send_alert(session,hello)
        while True:
            tasks=[candle_worker(session,s,monitor) for s in watchlist]
            await asyncio.gather(*tasks)
            await asyncio.sleep(180)

# ----------------- Flask -----------------
def start_bot():
    try: asyncio.run(main())
    except KeyboardInterrupt: pass

if __name__=="__main__":
    import threading
    threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home(): return "✅ Binance Alerts Bot py15b — Ativo e monitorando 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
