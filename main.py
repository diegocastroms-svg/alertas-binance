# main.py — V8.3R-3M OURO CONFLUÊNCIA ULTRARÁPIDA — APENAS 3M
# Fundo removido. 30m removido. 15m removido.

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V8.3R-3M — SOMENTE 3M ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 5_000_000
MIN_VOLAT = 2.0
TOP_N = 50
COOLDOWN = 900
BOOK_DOM = 1.05
SCAN_INTERVAL = 30

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg); return
    try:
        await s.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print("Erro Telegram:", e)

def ema(data, p):
    if not data: return []
    a = 2 / (p + 1); e = data[0]; out = [e]
    for x in data[1:]:
        e = a * x + (1 - a) * e
        out.append(e)
    return out

def rsi(prices, p=14):
    if len(prices) < p + 1: return 50
    d = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(x, 0) for x in d[-p:]]
    losses = [abs(min(x, 0)) for x in d[-p:]]
    ag, al = sum(gains)/p, (sum(losses)/p or 1e-12)
    return 100 - 100 / (1 + ag / al)

def macd_virando(close):
    if len(close) < 26: return False, 0.0
    e12 = ema(close, 12); e26 = ema(close, 26)
    macd_series = [a - b for a, b in zip(e12, e26)]
    signal_series = ema(macd_series, 9)
    hist = macd_series[-1] - signal_series[-1]
    hist_prev = macd_series[-2] - signal_series[-2] if len(macd_series) >= 2 else hist
    return hist > hist_prev, hist

def vol_strength(vol):
    if len(vol) < 21: return 100.0
    ma9 = sum(vol[-9:]) / 9
    ma21 = sum(vol[-21:]) / 21
    base = (ma9 + ma21) / 2 or 1e-12
    return (vol[-1] / base) * 100.0

def bollinger_width(close, p=20):
    if len(close) < p: return 0.0
    m = sum(close[-p:]) / p
    std = (sum((x - m)**2 for x in close[-p:]) / p) ** 0.5
    up = m + 2*std; dn = m - 2*std
    return ((up - dn) / m) * 100.0

cooldown_early, cooldown_confirm = {}, {}

def can_alert(sym, stage="early"):
    n = time.time()
    cd = cooldown_early if stage=="early" else cooldown_confirm
    if n - cd.get(sym, 0) >= COOLDOWN:
        cd[sym] = n
        return True
    return False

async def klines(s, sym, tf):
    async with s.get(f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit=200", timeout=10) as r:
        return await r.json() if r.status == 200 else []

async def ticker(s, sym):
    async with s.get(f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}", timeout=10) as r:
        return await r.json() if r.status == 200 else None

async def scan_tf(s, sym):
    try:
        t = await ticker(s, sym)
        if not t: return

        vol24 = float(t.get("quoteVolume", 0) or 0)
        if vol24 < MIN_VOL24: return

        k = await klines(s, sym, "3m")
        if len(k) < 200: return

        close = [float(x[4]) for x in k]
        vol   = [float(x[5]) for x in k]

        ema200 = ema(close, 200)[-1]
        price  = close[-1]
        r      = rsi(close)
        vs     = vol_strength(vol)
        bw     = bollinger_width(close)
        hist_up, _ = macd_virando(close)

        taker_buy  = float(t.get("takerBuyQuoteAssetVolume", 0) or 0)
        taker_sell = max(float(t.get("quoteVolume", 0) or 0) - taker_buy, 0)
        book_ok = (taker_buy >= taker_sell * BOOK_DOM) or taker_buy == 0

        nome = sym.replace("USDT", "")

        # ------------------------------
        # Entrada antecipada 100% ORIGINAL
        # ------------------------------
        rsi_ok  = 60 <= r <= 70
        vol_ok  = vs >= 140
        bb_ok   = bw <= 18
        price_ok = (price > ema200) or (abs(price - ema200)/ema200 <= 0.01)

        if rsi_ok and vol_ok and hist_up and bb_ok and price_ok and book_ok and can_alert(sym, "early"):
            msg = (
                f"⚡ <b>ENTRADA ANTECIPADA (3M)</b>\n\n"
                f"{nome}\nPreço: {price:.6f}\n"
                f"RSI: {r:.1f} | MACD virando\n"
                f"Vol força: {vs:.0f}%\n"
                f"Bollinger: {bw:.1f}% | EMA200: {ema200:.6f}\n"
                f"Fluxo: {taker_buy:,.0f} vs {taker_sell:,.0f}\n"
                f"⏱ {now_br()} BR"
            )
            await tg(s, msg)

        # ------------------------------
        # Rompimento confirmado ORIGINAL
        # ------------------------------
        confirm_ok = (
            len(close) >= 3
            and close[-3] < ema200
            and close[-2] > ema200
            and close[-1] > ema200
            and hist_up and r > 65 and vs >= 150
            and book_ok and can_alert(sym, "confirm")
        )

        if confirm_ok:
            msg2 = (
                f"💥 <b>ROMPIMENTO CONFIRMADO (3M)</b>\n\n"
                f"{nome}\nPreço: {price:.6f}\n"
                f"RSI: {r:.1f}\n"
                f"Vol força: {vs:.0f}%\n"
                f"EMA200: {ema200:.6f}\n"
                f"Fluxo: {taker_buy:,.0f} vs {taker_sell:,.0f}\n"
                f"⏱ {now_br()} BR"
            )
            await tg(s, msg2)

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V8.3R — SOMENTE 3M ATIVO</b>")
        while True:
            try:
                data_resp = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if data_resp.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL); continue

                data = await data_resp.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume", 0) or 0) >= MIN_VOL24
                    and abs(float(d.get("priceChangePercent", 0))) >= MIN_VOLAT
                    and not any(x in d["symbol"] for x in [
                        "UP","DOWN","BUSD","FDUSD","USDC","TUSD",
                        "EUR","USDE","TRY","GBP","BRL","AUD","CAD"
                    ])
                ]

                symbols = sorted(
                    symbols,
                    key=lambda x: next((float(t.get("quoteVolume") or 0) for t in data if t["symbol"] == x), 0),
                    reverse=True
                )[:TOP_N]

                tasks = [scan_tf(s, sym) for sym in symbols]
                await asyncio.gather(*tasks)

            except Exception as e:
                print("Erro main_loop:", e)

            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(
    target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))),
    daemon=True
).start()

asyncio.run(main_loop())
