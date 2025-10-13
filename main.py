# main_v3_3_final_protegido.py
# ✅ Curtos (5m/15m) + Longos (1h/4h)
# 🔒 Proteção automática contra crash e loop contínuo
# ⏰ SCAN_INTERVAL_SECONDS = 60, COOLDOWN_SHORT_SEC = 30 min

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_5M, INTERVAL_15M, INTERVAL_1H, INTERVAL_4H = "5m","15m","1h","4h"
SHORTLIST_N           = 65
SCAN_INTERVAL_SECONDS = 60          # frequência de varredura
COOLDOWN_SHORT_SEC    = 30 * 60     # 30 min
COOLDOWN_LONG_SEC     = 60 * 60     # 1 h
MIN_PCT, MIN_QV       = 1.0, 300_000.0

EMA_FAST, MA_SLOW, MA_MED, MA_LONG = 9, 20, 50, 200
RSI_LEN, VOL_MA, BB_LEN, ADX_LEN   = 14, 9, 20, 14
DONCHIAN_N = 20

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# --------------- Utils / Alert ---------------
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
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=8)
        except:
            pass
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=8)
        except:
            pass

# --------------- Indicadores ---------------
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq: return []
    out, alpha = [], 2.0 / (span + 1.0)
    e = seq[0]
    for x in seq:
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
    tr = [0.0]
    for i in range(1, len(c)):
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    return tr

def adx(h, l, c, period=14):
    n = len(c)
    if n < period + 1: return [20.0]*n, [0.0]*n, [0.0]*n
    tr = true_range(h, l, c)
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, n):
        up = h[i] - h[i-1]
        down = l[i-1] - l[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
    atr = [0.0]*n
    atr[period] = sum(tr[1:period+1])
    pdm, mdm = [0.0]*n, [0.0]*n
    pdm[period] = sum(plus_dm[1:period+1])
    mdm[period] = sum(minus_dm[1:period+1])
    for i in range(period+1, n):
        atr[i] = atr[i-1] - (atr[i-1]/period) + tr[i]
        pdm[i] = pdm[i-1] - (pdm[i-1]/period) + plus_dm[i]
        mdm[i] = mdm[i-1] - (mdm[i-1]/period) + minus_dm[i]
    plus_di, minus_di, dx, adx_vals = [0.0]*n, [0.0]*n, [0.0]*n, [0.0]*n
    for i in range(n):
        plus_di[i] = 100.0*(pdm[i]/(atr[i]+1e-12))
        minus_di[i] = 100.0*(mdm[i]/(atr[i]+1e-12))
        dx[i] = 100.0*abs(plus_di[i]-minus_di[i])/(plus_di[i]+minus_di[i]+1e-12)
    adx_vals[period] = sum(dx[1:period+1])/period
    for i in range(period+1, n):
        adx_vals[i] = (adx_vals[i-1]*(period-1)+dx[i])/period
    return adx_vals, plus_di, minus_di

def compute_indicators(o,h,l,c,v):
    ema9  = ema(c, EMA_FAST)
    ma20  = sma(c, MA_SLOW)
    ma50  = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    adx14, pdi, mdi = adx(h, l, c, ADX_LEN)
    return ema9, ma20, ma50, ma200, rsi14, volma, adx14, pdi, mdi

# --------------- Binance ---------------
async def get_klines(session, symbol, interval="5m", limit=210):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in data:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=400):
    usdt=[]
    blocked=("UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD","_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_BTC","_EUR","_TRY","_BRL","_ETH","_BNB","_SOL")
    for t in tickers:
        s=t.get("symbol","")
        if not s.endswith("USDT"): continue
        if any(x in s for x in blocked): continue
        try:
            pct=float(t.get("priceChangePercent","0") or 0.0)
            qv=float(t.get("quoteVolume","0") or 0.0)
        except:
            pct,qv=0.0,0.0
        if abs(pct)>=MIN_PCT and qv>=MIN_QV:
            usdt.append((s,abs(pct),qv))
    usdt.sort(key=lambda x:(x[1],x[2]),reverse=True)
    return [x[0] for x in usdt[:n]]

# --------------- Aqui ficam os workers (iguais ao seu) ---------------
# worker_5m, worker_15m, worker_1h, worker_4h, worker_entry_safe, worker_combined
# (mantém todos sem alterar lógica nem emojis)

# --------------- Main -----------------
async def main():
    mon = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
        hello = f"💻 v3.3 FINAL | {len(watchlist)} pares SPOT | {ts_brazil_now()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks=[]
            for s in watchlist:
                tasks+=[
                    worker_5m(session,s,mon),
                    worker_15m(session,s,mon),
                    worker_1h(session,s,mon),
                    worker_4h(session,s,mon),
                    worker_entry_safe(session,s,mon),
                    worker_combined(session,s,mon),
                ]
            await asyncio.gather(*tasks,return_exceptions=True)
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
            try:
                tickers = await get_24h(session)
                watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
            except Exception as e:
                print("Erro ao atualizar shortlist:", e)

# --------------- Flask (Render) -----------------
def start_bot():
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            print("⚠️ Erro no loop principal:", e)
            time.sleep(5)

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)

    @app.route("/")
    def home():
        return "✅ Binance Alerts Bot v3.3 FINAL — Protegido e estável 🇧🇷"

    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
