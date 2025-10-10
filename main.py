# main_v2_1.py
# Ajuste: 5m e 15m disparam no exato cruzamento (sem atraso)
# Todo o restante (alertas longos, mensagens, emojis, etc.) permanece intacto

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- CONFIG -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"
INTERVAL_CONF = "15m"
SHORTLIST_N = 80
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
ADX_LEN = 14

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ----------------- UTILS -----------------
def fmt_symbol(symbol):
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def ts_brazil():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

def binance_links(symbol):
    base = symbol.replace("USDT", "")
    return f'🔗 [Abrir (A)](https://www.binance.com/en/trade/{base}_USDT?type=spot) | [Abrir (B)](https://www.binance.com/en/trade?type=spot&symbol={base}_USDT)'

async def send_alert(session, text):
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            await session.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
        except:
            pass

def sma(arr, n):
    out, s, q = [], 0, deque()
    for x in arr:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq: return []
    out = []; alpha = 2 / (span + 1); e = seq[0]; out.append(e)
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e; out.append(e)
    return out

def rsi(closes, period=14):
    if len(closes) < period + 1: return [50]*len(closes)
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]; losses = [max(-d, 0) for d in deltas]
    avg_gain = sum(gains[:period]) / period; avg_loss = sum(losses[:period]) / period
    rsis = [50]*period
    for i in range(period, len(closes) - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-12); rsis.append(100 - (100 / (1 + rs)))
    return rsis + [rsis[-1]]

# ----------------- FETCH -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status(); data = await r.json()
    o,h,l,c,v = [],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=80):
    coins=[]
    for t in tickers:
        s=t["symbol"]
        if not s.endswith("USDT"): continue
        if any(x in s for x in ("UP","DOWN","BULL","BEAR","PERP","_BUSD","_FDUSD","_TUSD","_USDC","_BTC")):
            continue
        qv=float(t["quoteVolume"]); pct=float(t["priceChangePercent"])
        if qv>300000 and abs(pct)>1: coins.append((s,pct,qv))
    coins.sort(key=lambda x:(abs(x[1]),x[2]),reverse=True)
    return [x[0] for x in coins[:n]]

# ----------------- CORE -----------------
async def candle_worker(session, symbol):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval="5m")
        if len(c)<60: return
        ema9=ema(c,9); ma20=sma(c,20); ma50=sma(c,50); ma200=sma(c,200); rsi14=rsi(c)
        vol_ma=sma(v,9); last=len(c)-1

        # 🚀 Tendência iniciando no 5m — EMA9 cruza acima da MA20 e MA50
        if ema9[last-1]<ma20[last-1] and ema9[last]>ma20[last] and ema9[last]>ma50[last] and rsi14[last]>45 and v[last]>=vol_ma[last]:
            msg=f"🟢 *{fmt_symbol(symbol)}* ⬆️ *TENDÊNCIA INICIANDO (5m)*\n💰 `{c[last]:.6f}`\n🧠 EMA9 cruzou acima das MA20 e MA50 | RSI {rsi14[last]:.1f}\n⏰ {ts_brazil()}\n{binance_links(symbol)}"
            await send_alert(session,msg)

    except Exception as e:
        print("5m error",symbol,e)

async def conf_worker(session, symbol):
    try:
        o,h,l,c,v = await get_klines(session, symbol, interval="15m")
        if len(c)<60: return
        ema9=ema(c,9); ma20=sma(c,20); ma50=sma(c,50); ma200=sma(c,200); rsi14=rsi(c)
        vol_ma=sma(v,9); last=len(c)-1

        # 🌕 Tendência pré-confirmada 15m — 9,20,50 cruzando acima da 200
        if (ema9[last-1]<ma200[last-1] or ma20[last-1]<ma200[last-1]) and ema9[last]>ma200[last] and ma20[last]>ma200[last] and ma50[last]>ma200[last] and rsi14[last]>50 and v[last]>=vol_ma[last]:
            msg=f"🟢 *{fmt_symbol(symbol)}* ⬆️ *TENDÊNCIA PRÉ-CONFIRMADA (15m)*\n💰 `{c[last]:.6f}`\n🧠 Médias 9/20/50 cruzaram acima da MA200 | RSI {rsi14[last]:.1f}\n⏰ {ts_brazil()}\n{binance_links(symbol)}"
            await send_alert(session,msg)

    except Exception as e:
        print("15m error",symbol,e)

# ----------------- MAIN -----------------
async def main():
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers)
        hello = f"💻 v2.1 | Monitorando {len(watchlist)} pares | {ts_brazil()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks=[]
            for s in watchlist:
                tasks.append(candle_worker(session,s))
                tasks.append(conf_worker(session,s))
            await asyncio.gather(*tasks)
            await asyncio.sleep(180)

# ----------------- FLASK -----------------
def start_bot():
    asyncio.run(main())

if __name__=="__main__":
    import threading
    threading.Thread(target=start_bot,daemon=True).start()
    app=Flask(__name__)
    @app.route("/")
    def home():
        return "✅ Binance Alerts Bot v2.1 — 5m/15m em tempo real com cruzamentos imediatos 🇧🇷"
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
