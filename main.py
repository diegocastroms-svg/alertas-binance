# main.py — V8.3R-3M OURO CONFLUÊNCIA ULTRARÁPIDA — 3M + FUNDO REAL DINÂMICO 30M/15M

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V8.3R-3M — 3M + FUNDO REAL DINÂMICO 30M/15M ATIVO", 200

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

cooldown_early, cooldown_confirm, cooldown_bottom = {}, {}, {}

def can_alert(sym, stage="early"):
    n = time.time()
    if stage == "early":
        cd = cooldown_early
    elif stage == "confirm":
        cd = cooldown_confirm
    else:
        cd = cooldown_bottom
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

# =====================================================
# 3M ORIGINAL — alterei SOMENTE o rompimento confirmado
# =====================================================
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

        # Entrada antecipada ORIGINAL
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

        # ===================================================
        # ROMPIMENTO CONFIRMADO (3M) — B1 ANTECIPADO (NOVO)
        # ===================================================
        ema9_3m  = ema(close, 9)[-1]
        ema21_3m = ema(close, 21)[-1]

        confirm_ok = (
            ema9_3m > ema21_3m
            and hist_up
            and r > 55
            and vs >= 120
            and (price >= ema200 * 0.997 or price > ema200)
            and book_ok
            and can_alert(sym, "confirm")
        )

        if confirm_ok:
            msg2 = (
                f"💥 <b>ROMPIMENTO CONFIRMADO (3M) — B1 ANTECIPADO</b>\n\n"
                f"{nome}\nPreço: {price:.6f}\n"
                f"RSI: {r:.1f}\n"
                f"EMA9/21: {ema9_3m:.6f} / {ema21_3m:.6f}\n"
                f"Vol força: {vs:.0f}%\n"
                f"EMA200: {ema200:.6f}\n"
                f"Fluxo: {taker_buy:,.0f} vs {taker_sell:,.0f}\n"
                f"⏱ {now_br()} BR"
            )
            await tg(s, msg2)
    except Exception as e:
        print("Erro scan_tf (3m):", e)

# =====================================================
# FUNDO REAL DINÂMICO — 30M + 15M (SEM NÚMERO FIXO)
# =====================================================
async def scan_bottom(s, sym):
    try:
        t = await ticker(s, sym)
        if not t: return

        vol24 = float(t.get("quoteVolume", 0) or 0)
        if vol24 < MIN_VOL24: return

        k30 = await klines(s, sym, "30m")
        k15 = await klines(s, sym, "15m")
        if len(k30) < 50 or len(k15) < 30:
            return

        close30 = [float(x[4]) for x in k30]
        vol30   = [float(x[5]) for x in k30]

        last30 = k30[-1]
        o30 = float(last30[1])
        h30 = float(last30[2])
        l30 = float(last30[3])
        c30 = float(last30[4])
        range30 = max(h30 - l30, 1e-12)

        pavio30_forte   = (min(c30, o30) - l30) >= range30 * 0.30
        candle30_fraco  = abs(c30 - o30) <= range30 * 0.40
        vol30_estavel   = vol30[-1] <= max(vol30[-4:-1])

        taker_buy  = float(t.get("takerBuyQuoteAssetVolume", 0) or 0)
        taker_sell = max(float(t.get("quoteVolume", 0) or 0) - taker_buy, 0)
        fluxo30_ok = taker_buy >= taker_sell * 0.85

        bw30 = bollinger_width(close30)

        base30_ok = pavio30_forte and candle30_fraco and vol30_estavel and fluxo30_ok and bw30 <= 25

        if not base30_ok:
            return

        close15 = [float(x[4]) for x in k15]
        vol15   = [float(x[5]) for x in k15]

        last15 = k15[-1]
        o15 = float(last15[1])
        h15 = float(last15[2])
        l15 = float(last15[3])
        c15 = float(last15[4])
        v15 = float(last15[5])

        nome = sym.replace("USDT", "")

        vela_verde = c15 > o15
        rompendo_max = c15 > max(float(k15[-2][4]), float(k15[-3][4]))

        ema9_15  = ema(close15, 9)
        ema21_15 = ema(close15, 21)
        ema_virando = ema9_15[-1] > ema21_15[-1]

        if len(vol15) >= 6:
            vol15_media = sum(vol15[-6:-1]) / 5
        else:
            vol15_media = sum(vol15[:-1]) / max(len(vol15) - 1, 1)

        vol15_ok = v15 >= vol15_media

        hist15_up, _ = macd_virando(close15)

        fundo_ok = vela_verde and rompendo_max and ema_virando and vol15_ok and hist15_up

        if fundo_ok and can_alert(sym, "bottom"):
            msgF = (
                f"🟢 <b>FUNDO REAL DINÂMICO (30M + 15M)</b>\n\n"
                f"{nome}\n"
                f"30m: pavio forte + candle fraco\n"
                f"30m: volume estabilizando, volatilidade reduzindo\n"
                f"30m: fluxo vendedor enfraquecendo\n"
                f"15m: candle verde rompendo máximas\n"
                f"15m: EMA9 cruzando EMA21 pra cima\n"
                f"15m: volume reagindo\n"
                f"15m: MACD começando a virar\n"
                f"⏱ {now_br()} BR"
            )
            await tg(s, msgF)

    except Exception as e:
        print("Erro scan_bottom (30m/15m):", e)

# =====================================================
# LOOP PRINCIPAL
# =====================================================
async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V8.3R — 3M + FUNDO REAL DINÂMICO 30M/15M ATIVO</b>")
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
                    key=lambda x: next(
                        (float(t.get("quoteVolume", 0) or 0) for t in data if t["symbol"] == x),
                        0
                    ),
                    reverse=True
                )[:TOP_N]

                tasks = []
                for sym in symbols:
                    tasks.append(scan_tf(s, sym))
                    tasks.append(scan_bottom(s, sym))

                await asyncio.gather(*tasks)

            except Exception as e:
                print("Erro main_loop:", e)

            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(
    target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000))),
    daemon=True
).start()

asyncio.run(main_loop())
