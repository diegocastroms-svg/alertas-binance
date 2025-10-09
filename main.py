# main_py15_corrigido.py
# Estrutura e alertas idênticos ao py15 anterior
# Correção: compute_indicators reposicionado antes dos workers

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"
INTERVAL_CONF = "15m"
SHORTLIST_N = 65
COOLDOWN_SEC = 15 * 60
COOLDOWN_LONGTERM = 60 * 60
MIN_PCT = 1.0
MIN_QV = 300_000.0

EMA_FAST, MA_SLOW, MA_MED, MA_LONG = 9, 20, 50, 200
RSI_LEN, VOL_MA, BB_LEN, ADX_LEN = 14, 9, 20, 14

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
def fmt_symbol(symbol):
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol):
    base = symbol.upper().replace("USDT", "")
    return f'🔗 <a href="https://www.binance.com/en/trade/{base}_USDT?type=spot">Abrir</a>'

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
            await session.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=10)
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
    alpha = 2.0 / (span + 1.0)
    e = seq[0]; out = [e]
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def rolling_std(seq, n):
    out, q = [], deque()
    for x in seq:
        q.append(x)
        if len(q) > n: q.popleft()
        m = sum(q)/len(q)
        var = sum((v - m) ** 2 for v in q) / len(q)
        out.append(math.sqrt(var))
    return out

def rsi_wilder(closes, period=14):
    if len(closes) == 0: return []
    deltas = [0.0] + [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains, losses = [max(d,0) for d in deltas], [max(-d,0) for d in deltas]
    rsis = [50.0]*len(closes)
    if len(closes) < period+1: return rsis
    avg_gain = sum(gains[1:period+1])/period
    avg_loss = sum(losses[1:period+1])/period
    for i in range(period+1, len(closes)):
        avg_gain = (avg_gain*(period-1)+gains[i])/period
        avg_loss = (avg_loss*(period-1)+losses[i])/period
        rs = avg_gain/(avg_loss+1e-12)
        rsis[i] = 100 - (100/(1+rs))
    return rsis

def true_range(h, l, c):
    tr = [0.0]
    for i in range(1, len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return tr

def adx(h, l, c, period=14):
    n = len(c)
    if n < period+1: return [20.0]*n, [0]*n, [0]*n
    tr = true_range(h,l,c)
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1,n):
        up, down = h[i]-h[i-1], l[i-1]-l[i]
        plus_dm.append(up if up>down and up>0 else 0)
        minus_dm.append(down if down>up and down>0 else 0)
    atr, pdm, mdm = [0]*n, [0]*n, [0]*n
    atr[period] = sum(tr[1:period+1])
    pdm[period] = sum(plus_dm[1:period+1])
    mdm[period] = sum(minus_dm[1:period+1])
    for i in range(period+1,n):
        atr[i] = atr[i-1]-(atr[i-1]/period)+tr[i]
        pdm[i] = pdm[i-1]-(pdm[i-1]/period)+plus_dm[i]
        mdm[i] = mdm[i-1]-(mdm[i-1]/period)+minus_dm[i]
    plus_di = [100*(pdm[i]/(atr[i]+1e-12)) for i in range(n)]
    minus_di = [100*(mdm[i]/(atr[i]+1e-12)) for i in range(n)]
    dx = [100*abs(plus_di[i]-minus_di[i])/(plus_di[i]+minus_di[i]+1e-12) for i in range(n)]
    adx_vals = [sum(dx[1:period+1])/period]*(period+1)
    for i in range(period+1,n):
        adx_vals.append((adx_vals[-1]*(period-1)+dx[i])/period)
    return adx_vals, plus_di, minus_di

def compute_indicators(o,h,l,c,v):
    ema9 = ema(c, EMA_FAST)
    ma20 = sma(c, MA_SLOW)
    ma50 = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    bb_std = rolling_std(c, BB_LEN)
    bb_up = [ma20[i] + 2*bb_std[i] for i in range(len(bb_std))]
    bb_low = [ma20[i] - 2*bb_std[i] for i in range(len(bb_std))]
    adx14, pdi, mdi = adx(h,l,c,ADX_LEN)
    return ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, pdi, mdi

# ----------------- Binance -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol":symbol,"interval":interval,"limit":limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url,timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1]));h.append(float(k[2]));l.append(float(k[3]))
        c.append(float(k[4]));v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr",timeout=15) as r:
        r.raise_for_status()
        return await r.json()

# ----------------- Workers -----------------
async def candle_worker(session,symbol,monitor):
    try:
        o,h,l,c,v = await get_klines(session,symbol,interval=INTERVAL_MAIN,limit=200)
        ema9,ma20,ma50,ma200,rsi14,volma,bb_up,bb_low,adx14,pdi,mdi = compute_indicators(o,h,l,c,v)
        last=len(c)-1
        if ema9[last]>ma20[last]>ma50[last] and l[last]<=ema9[last] and c[last]>=ema9[last]:
            msg=f"⬆️ {fmt_symbol(symbol)} — Tendência iniciando 5m\n💰<code>{c[last]:.6f}</code>\n⏰{ts_brazil_now()}\n{binance_links(symbol)}"
            await send_alert(session,msg)
    except Exception as e:
        print("erro curto do trabalhador",symbol,e)

async def longterm_worker(session,symbol,monitor):
    try:
        o1,h1,l1,c1,v1 = await get_klines(session,symbol,interval="1h",limit=120)
        ema9_1,ma20_1,ma50_1,ma200_1,rsi1,volma1,bb1u,bb1l,adx1,pdi1,mdi1 = compute_indicators(o1,h1,l1,c1,v1)
        last1=len(c1)-1
        if ema9_1[last1]>ma20_1[last1]>ma50_1[last1]>ma200_1[last1] and rsi1[last1]>55:
            msg=f"🌕 {fmt_symbol(symbol)} — Tendência longa confirmada (1h)\n💰<code>{c1[last1]:.6f}</code>\n⏰{ts_brazil_now()}\n{binance_links(symbol)}"
            await send_alert(session,msg)
    except Exception as e:
        print("erro longo do trabalhador",symbol,e)

# ----------------- Main -----------------
async def main():
    monitor=defaultdict(lambda:0.0)
    async with aiohttp.ClientSession() as session:
        tickers=await get_24h(session)
        watchlist=[t["symbol"] for t in tickers if t["symbol"].endswith("USDT")][:65]
        hello=f"💻 v15_corrigido | {len(watchlist)} pares SPOT | {ts_brazil_now()}"
        await send_alert(session,hello)
        print(hello)
        while True:
            tasks=[candle_worker(session,s,monitor) for s in watchlist]
            tasks+=[longterm_worker(session,s,monitor) for s in watchlist]
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
    def home(): return "✅ Binance Alerts Bot — Flask ativo e workers rodando 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
