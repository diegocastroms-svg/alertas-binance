# main_py15.py
# Bot Binance SPOT - versão estável com alertas curtos e longos

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"
INTERVAL_CONF = "15m"
SHORTLIST_N = 65
COOLDOWN_SEC = 15 * 60
COOLDOWN_LONGTERM = 60 * 60
MIN_PCT = 1.0
MIN_QV = 300_000.0

EMA_FAST = 9
MA_SLOW = 20
MA_MED = 50
MA_LONG = 200
RSI_LEN = 14
VOL_MA = 9
BB_LEN = 20
ADX_LEN = 14

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Funções -----------------
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

def rolling_std(seq, n):
    out, q = [], deque()
    for x in seq:
        q.append(x)
        if len(q) > n: q.popleft()
        m = sum(q) / len(q)
        var = sum((v - m) ** 2 for v in q) / len(q)
        out.append(math.sqrt(var))
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
    atr = [sum(tr[:period])/period]*n
    plus_dm, minus_dm = [0.0]*n, [0.0]*n
    for i in range(1,n):
        up, down = h[i]-h[i-1], l[i-1]-l[i]
        plus_dm[i] = up if up>down and up>0 else 0
        minus_dm[i] = down if down>up and down>0 else 0
    pdm, mdm = [sum(plus_dm[:period])]*n, [sum(minus_dm[:period])]*n
    plus_di = [100*pdm[i]/(atr[i]+1e-12) for i in range(n)]
    minus_di = [100*mdm[i]/(atr[i]+1e-12) for i in range(n)]
    dx = [100*abs(plus_di[i]-minus_di[i])/(plus_di[i]+minus_di[i]+1e-12) for i in range(n)]
    adx_vals = [sum(dx[:period])/period]*n
    return adx_vals, plus_di, minus_di

def compute_indicators(o,h,l,c,v):
    ema9 = ema(c, EMA_FAST)
    ma20 = sma(c, MA_SLOW)
    ma50 = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    return ema9, ma20, ma50, ma200, rsi14, volma

# ----------------- Monitor -----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)
        self.cooldown_long = defaultdict(lambda: 0.0)
    def allowed(self,s,k): return time.time()-self.cooldown[(s,k)]>=COOLDOWN_SEC
    def mark(self,s,k): self.cooldown[(s,k)]=time.time()
    def allowed_long(self,s): return time.time()-self.cooldown_long[s]>=COOLDOWN_LONGTERM
    def mark_long(self,s): self.cooldown_long[s]=time.time()

# ----------------- Workers -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=10) as r:
        r.raise_for_status(); data = await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def candle_worker(session, symbol, monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval="5m", limit=200)
        ema9,ma20,ma50,ma200,rsi14,volma = compute_indicators(o,h,l,c,v)
        last=len(c)-1
        if ema9[last]>ma20[last]>ma50[last] and c[last]>ma200[last] and monitor.allowed(symbol,"TENDENCIA_PRE_CONF_5M"):
            msg=f"🌕 <b>{fmt_symbol(symbol)} — PRÉ-CONFIRMADA (5m)</b>\n💰 <code>{c[last]:.6f}</code>\n🧠 Médias cruzaram MA200\n⏰ {ts_brazil_now()}\n{binance_links(symbol)}"
            await send_alert(session,msg); monitor.mark(symbol,"TENDENCIA_PRE_CONF_5M")
    except Exception as e:
        print("erro curto",symbol,e)

async def longterm_worker(session, symbol, monitor):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval="1h", limit=120)
        ema9,ma20,ma50,ma200,rsi14,volma = compute_indicators(o,h,l,c,v)
        last=len(c)-1
        if ema9[last]>ma20[last]>ma50[last]>ma200[last] and monitor.allowed_long(symbol):
            msg=f"🌕 <b>{fmt_symbol(symbol)} — CONFIRMADA (1h)</b>\n💰 <code>{c[last]:.6f}</code>\n⏰ {ts_brazil_now()}\n{binance_links(symbol)}"
            await send_alert(session,msg); monitor.mark_long(symbol)
    except Exception as e:
        print("erro longo",symbol,e)

# ----------------- Main -----------------
async def main():
    monitor=Monitor()
    async with aiohttp.ClientSession() as session:
        hello=f"✅ Bot ativo | {ts_brazil_now()}"
        await send_alert(session,hello)
        while True:
            tickers=["BTCUSDT","ETHUSDT"]
            tasks=[]
            for s in tickers:
                tasks.append(candle_worker(session,s,monitor))
                tasks.append(longterm_worker(session,s,monitor))
            await asyncio.gather(*tasks)
            await asyncio.sleep(180)

def start_bot():
    try: asyncio.run(main())
    except KeyboardInterrupt: pass

if __name__=="__main__":
    import threading
    threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home(): return "✅ Binance Alerts Bot v15 ativo"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
