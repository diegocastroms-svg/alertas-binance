# main.py — V7.4 OURO CONFLUÊNCIA REAL | ENTRADA ANTECIPADA REAL
# Timeframe: 15m — foco em detectar início real da virada (antes do topo)
# Confirmações: RSI, MACD (hist↑), VolumeStrength, Book
# Cooldown: 15 minutos
# Volume mínimo: 10M USDT (24h)
# Top N por volume: 50
# Scan: 30s

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading
from math import inf

app = Flask(__name__)
@app.route("/")
def home():
    return "V7.4 OURO CONFLUÊNCIA REAL | ENTRADA ANTECIPADA REAL ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ===== PARÂMETROS =====
MIN_VOL24 = 10_000_000
TOP_N = 50
SCAN_INTERVAL = 30
COOLDOWN = {"15m": 15*60}
VOL_STRENGTH_MIN = 120
RSI_MIN = 50
BOOK_DOMINANCE = 1.10

# ===== FUNÇÕES =====
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print("\n[TELEGRAM_SIM]\n" + msg + "\n"); return
    try:
        await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                     timeout=10)
    except Exception as e:
        print("Erro Telegram:", e)

def ema_series(data, p):
    if not data: return []
    a = 2/(p+1); e = data[0]; out = [e]
    for x in data[1:]:
        e = a*x + (1-a)*e; out.append(e)
    return out

def ema_last(data, p):
    s = ema_series(data, p)
    return s[-1] if s else 0.0

def rsi(prices, p=14):
    if len(prices) < p+1: return 50
    d = [prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains = [max(x,0) for x in d[-p:]]
    losses = [abs(min(x,0)) for x in d[-p:]]
    ag, al = (sum(gains)/p), (sum(losses)/p or 1e-12)
    return 100 - 100/(1 + ag/al)

def macd_12269(close):
    if len(close) < 26: return 0.0, 0.0, 0.0, False
    e12 = ema_series(close, 12)
    e26 = ema_series(close, 26)
    n = min(len(e12), len(e26))
    macd_line_series = [a-b for a,b in zip(e12[-n:], e26[-n:])]
    signal_series = ema_series(macd_line_series, 9)
    macd_line, signal = macd_line_series[-1], signal_series[-1]
    hist = macd_line - signal
    hist_prev = macd_line_series[-2] - signal_series[-2] if len(macd_line_series)>=2 else hist
    hist_up = hist > hist_prev
    return macd_line, signal, hist, hist_up

def volume_strength(vol_series):
    n = len(vol_series)
    if n < 21: return 100.0, (sum(vol_series[-n:])/max(n,1)), vol_series[-1]
    ma9  = sum(vol_series[-9:])/9
    ma21 = sum(vol_series[-21:])/21
    base = (ma9 + ma21)/2 or 1e-12
    return (vol_series[-1]/base)*100, base, vol_series[-1]

def taker_split_24h(t24):
    vol_quote = float(t24.get("quoteVolume", 0) or 0.0)
    taker_buy_q = float(t24.get("takerBuyQuoteAssetVolume", 0) or 0.0)
    taker_sell_q = max(vol_quote - taker_buy_q, 0.0)
    return vol_quote, taker_buy_q, taker_sell_q

cooldown = {"15m": {}}
def can_alert(tf, sym):
    n = time.time()
    if n - cooldown[tf].get(sym, 0) >= COOLDOWN[tf]:
        cooldown[tf][sym] = n
        return True
    return False

# ===== CORE =====
async def klines(s, sym, tf, lim=220):
    url = f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit={lim}"
    async with s.get(url, timeout=10) as r:
        return await r.json() if r.status == 200 else []

async def ticker24(s, sym):
    url = f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}"
    async with s.get(url, timeout=10) as r:
        return await r.json() if r.status == 200 else None

async def scan_tf(s, sym, tf):
    try:
        t24 = await ticker24(s, sym)
        if not t24: return
        vol24 = float(t24["quoteVolume"])
        if vol24 < MIN_VOL24: return
        k = await klines(s, sym, tf, 220)
        if len(k) < 60: return

        close = [float(x[4]) for x in k]
        vol   = [float(x[5]) for x in k]
        price = close[-1]

        ema9 = ema_last(close, 9)
        ema20 = ema_last(close, 20)
        r = rsi(close)
        macd_line, signal, hist, hist_up = macd_12269(close)
        vs, vs_base, vs_now = volume_strength(vol)
        vol_quote, taker_buy_q, taker_sell_q = taker_split_24h(t24)
        book_ok = (taker_buy_q >= taker_sell_q * BOOK_DOMINANCE)

        if tf == "15m":
            early_ok = (r >= RSI_MIN) and hist_up and (vs >= VOL_STRENGTH_MIN) and book_ok
            closes_above_fast = (price > ema9) and (price > ema20)
            if early_ok and closes_above_fast and can_alert(tf, sym):
                nome = sym.replace("USDT", "")
                msg = (
                    f"<b>⚡ ENTRADA ANTECIPADA REAL (15M)</b>\n\n"
                    f"{nome}\n\n"
                    f"Preço: <b>{price:.6f}</b>\n"
                    f"RSI: <b>{r:.1f}</b> | MACD: <b>melhorando</b> (hist ↑)\n"
                    f"Vol força: <b>{vs:.0f}%</b> | Fluxo real: <b>{taker_buy_q:,.0f}</b> vs <b>{taker_sell_q:,.0f}</b>\n"
                    f"📊 Volume 24h: ${vol24:,.0f}\n"
                    f"<i>{now_br()} BR</i>"
                )
                await tg(s, msg)

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V7.4 ATIVO — ENTRADA ANTECIPADA REAL (15M)</b>")
        while True:
            try:
                resp = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if resp.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL); continue
                data = await resp.json()
                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume") or 0) >= MIN_VOL24
                    and not any(x in d["symbol"] for x in ["UP","DOWN"])
                ]
                symbols = sorted(
                    symbols,
                    key=lambda x: next((float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0.0),
                    reverse=True
                )[:TOP_N]
                tasks = [scan_tf(s, sym, "15m") for sym in symbols]
                await asyncio.gather(*tasks)
            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
