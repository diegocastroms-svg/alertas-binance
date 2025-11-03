# main.py — V17.0 EMA9 CROSS EMA20 30m + RSI 40-80
import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/"); def home(): return "V17.0 EMA CROSS ATIVO", 200
@app.route("/health"); def health(): return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

def now_br(): return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")

async def tg(s, msg):
    if not TELEGRAM_TOKEN: print(msg); return
    await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)

def ema(data, p):
    if not data: return []
    a = 2/(p+1); e = data[0]; out = [e]
    for x in data[1:]: e = a*x + (1-a)*e; out.append(e)
    return out

def rsi(prices, p=14):
    if len(prices) < p+1: return 50
    d = [prices[i]-prices[i-1] for i in range(1, len(prices))]
    g = [max(x,0) for x in d[-p:]]; l = [abs(min(x,0)) for x in d[-p:]]
    ag, al = sum(g)/p, sum(l)/p or 1e-12
    return 100 - 100/(1 + ag/al)

async def klines(s, sym, intv, lim=100):
    url = f"{BINANCE}/api/v3/klines?symbol={sym}&interval={intv}&limit={lim}"
    async with s.get(url, timeout=10) as r: return await r.json() if r.status == 200 else []

async def ticker(s, sym):
    url = f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}"
    async with s.get(url, timeout=10) as r: return await r.json() if r.status == 200 else None

cooldown = {}
def can_alert(sym):
    n = time.time()
    if n - cooldown.get(sym, 0) >= 1800: cooldown[sym] = n; return True  # 1h cooldown
    return False

async def scan(s, sym):
    try:
        t = await ticker(s, sym); if not t: return
        p = float(t["lastPrice"])
        vol24 = float(t["quoteVolume"])
        if vol24 < 20_000_000: return  # Volume mínimo

        k30 = await klines(s, sym, "30m", 100); if len(k30) < 50: return
        close = [float(x[4]) for x in k30[:-1]]  # Até vela anterior

        ema9 = ema(close, 9)
        ema20 = ema(close, 20)
        if len(ema9) < 2 or len(ema20) < 2: return

        # Cruzamento: EMA9 cruzou pra cima da EMA20 na última vela fechada
        if not (ema9[-2] <= ema20[-2] and ema9[-1] > ema20[-1]): return

        current_rsi = rsi(close)
        if current_rsi < 40 or current_rsi > 80: return

        if can_alert(sym):
            stop = min(float(x[3]) for x in k30[-10:-1]) * 0.98
            alvo1 = p * 1.08
            alvo2 = p * 1.15
            msg = (
                f"<b>EMA9 CROSS EMA20 30m</b>\n"
                f"<code>{sym}</code>\n"
                f"Preço: <b>{p:.6f}</b>\n"
                f"RSI: <b>{current_rsi:.1f}</b>\n"
                f"Stop: <b>{stop:.6f}</b>\n"
                f"Alvo +8%: <b>{alvo1:.6f}</b>\n"
                f"Alvo +15%: <b>{alvo2:.6f}</b>\n"
                f"<i>{now_br()} BR</i>"
            )
            await tg(s, msg)
    except: pass

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V17.0 EMA CROSS ATIVO</b>\n30m + RSI 40-80")
        while True:
            try:
                data = await (await s.get(f"{BINANCE}/api/v3/ticker/24hr")).json()
                symbols = [d["symbol"] for d in data if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) > 20_000_000]
                await asyncio.gather(*[scan(s, sym) for sym in symbols[:100]])
            except: pass
            await asyncio.sleep(60)  # A cada 1 min

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()
app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
