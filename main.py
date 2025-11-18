# main.py — V8.3R-3M OURO CONFLUÊNCIA ULTRARÁPIDA — Liquidez Real + FUNDO REAL 15M

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V8.3R-3M OURO CONFLUÊNCIA ULTRARÁPIDA — Liquidez Real + FUNDO REAL ATIVO", 200

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
    macd_line = e12[-1] - e26[-1]
    macd_series = [a - b for a, b in zip(e12, e26)]
    signal_series = ema(macd_series, 9)
    hist = macd_line - signal_series[-1]
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
    cd = cooldown_early if stage=="early" else cooldown_confirm if stage=="confirm" else cooldown_bottom
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

async def scan_tf(s, sym, tf):
    try:
        print(f"[{now_br()}] Analisando {sym} ({tf})...")

        t = await ticker(s, sym)
        if not t: return
        vol24 = float(t.get("quoteVolume", 0) or 0)
        if vol24 < MIN_VOL24: return

        k = await klines(s, sym, tf)
        if len(k) < 200: return

        close = [float(x[4]) for x in k]
        vol = [float(x[5]) for x in k]
        ema200 = ema(close, 200)[-1]
        price = close[-1]
        hist_up, _ = macd_virando(close)
        r = rsi(close)
        vs = vol_strength(vol)
        bw = bollinger_width(close)

        taker_buy = float(t.get("takerBuyQuoteAssetVolume", 0) or 0.0)
        taker_sell = max(float(t.get("quoteVolume", 0) or 0.0) - taker_buy, 0.0)
        book_ok = (taker_buy >= taker_sell * BOOK_DOM) or (taker_buy == 0.0)
        nome = sym.replace("USDT", "")

        rsi_ok  = 60 <= r <= 70
        vol_ok  = vs >= 140
        macd_ok = hist_up
        bb_ok   = bw <= 18
        price_ok = (price > ema200) or (abs((price - ema200)/ema200) <= 0.01)

        # ---------------------------------------------------------
        # ENTRADA ANTECIPADA (3m)
        # ---------------------------------------------------------
        if tf == "3m":
            if rsi_ok and vol_ok and macd_ok and bb_ok and price_ok and book_ok and can_alert(sym, "early"):
                msg = (
                    f"⚡ <b>ENTRADA ANTECIPADA DETECTADA (3M)</b>\n\n"
                    f"{nome}\n\n"
                    f"Preço: <b>{price:.6f}</b>\n"
                    f"RSI: <b>{r:.1f}</b> | MACD: <b>virando</b>\n"
                    f"Volume força: <b>{vs:.0f}%</b>\n"
                    f"Bollinger: <b>{bw:.1f}%</b> | EMA200: <b>{ema200:.6f}</b>\n"
                    f"Fluxo: <b>{taker_buy:,.0f}</b> vs <b>{taker_sell:,.0f}</b>\n"
                    f"⏱ {now_br()} BR"
                )
                await tg(s, msg)

        # ---------------------------------------------------------
        # ROMPIMENTO CONFIRMADO (3m)
        # ---------------------------------------------------------
        if tf == "3m":
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
                    f"{nome}\n\n"
                    f"Preço: <b>{price:.6f}</b>\n"
                    f"RSI: <b>{r:.1f}</b>\n"
                    f"Vol força: <b>{vs:.0f}%</b>\n"
                    f"EMA200: <b>{ema200:.6f}</b>\n"
                    f"Fluxo: <b>{taker_buy:,.0f}</b> vs <b>{taker_sell:,.0f}</b>\n"
                    f"⏱ {now_br()} BR"
                )
                await tg(s, msg2)

        # ---------------------------------------------------------
        # FUNDO REAL (15m) — PADRÃO REAL
        # ---------------------------------------------------------
        if tf == "15m":
            try:
                daily_change = float(t.get("priceChangePercent", 0) or 0.0)

                # 1) Queda forte recente (>= -4%)
                queda_ok = (daily_change <= -4.0)
                if not queda_ok:
                    return

                close15 = close
                last15 = k[-1]
                o15 = float(last15[1])
                h15 = float(last15[2])
                l15 = float(last15[3])
                c15 = float(last15[4])
                v15 = float(last15[5])

                # 2) A queda parou
                candle_pequeno = abs(c15 - o15) <= (h15 - l15) * 0.35
                pavio_inferior = (min(c15, o15) - l15) >= (h15 - l15) * 0.25
                queda_parou = candle_pequeno or pavio_inferior

                # 3) Volume reagindo
                if len(vol) >= 3:
                    vol_reagindo = v15 > vol[-2]
                else:
                    vol_reagindo = True

                # 4) RSI virando
                rsi15 = rsi(close15)
                rsi_ok = 32 <= rsi15 <= 48

                # 5) RSI7 virando
                rsi7_ok = rsi(close15[-7:], 7) > rsi(close15[-8:], 7)

                # 6) Candle verde acima da EMA9
                ema9_15 = ema(close15, 9)
                ema9_ok = c15 > ema9_15[-1]
                vela_verde = c15 > o15

                # 7) MACD desacelerando venda
                hist_up, _ = macd_virando(close15)
                macd_ok = hist_up

                fundo_real_ok = (
                    queda_ok and queda_parou and vol_reagindo and
                    rsi_ok and rsi7_ok and ema9_ok and vela_verde and macd_ok
                )

                if fundo_real_ok and can_alert(sym, "bottom"):
                    msgF = (
                        f"🟢 <b>FUNDO REAL DETECTADO (15M)</b>\n\n"
                        f"{nome}\n\n"
                        f"RSI14: <b>{rsi15:.1f}</b> | RSI7 virando\n"
                        f"Volume: <b>reagindo</b>\n"
                        f"Candle verde acima da EMA9\n"
                        f"MACD: <b>reduzindo força vendedora</b>\n"
                        f"Queda 24h: <b>{daily_change:.2f}%</b>\n"
                        f"⏱ {now_br()} BR"
                    )
                    await tg(s, msgF)

            except Exception as e:
                print("Erro fundo_real_15m:", e)

    except Exception as e:
        print("Erro scan_tf:", e)


async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V8.3R-3M ATIVO — Liquidez Real + FUNDO REAL 15M</b>")
        while True:
            try:
                data_resp = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if data_resp.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL); continue
                data = await data_resp.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume") or 0) >= MIN_VOL24
                    and abs(float(d.get("priceChangePercent") or 0)) >= MIN_VOLAT
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

                print(f"\n[{now_br()}] === Iniciando varredura 3m/15m ({len(symbols)} moedas) ===")

                tasks = []
                for sym in symbols:
                    tasks.append(scan_tf(s, sym, "3m"))
                    tasks.append(scan_tf(s, sym, "15m"))

                await asyncio.gather(*tasks)

                print(f"[{now_br()}] === Varredura finalizada ===\n")

            except Exception as e:
                print("Erro main_loop:", e)

            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(
    target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)) ),
    daemon=True
).start()

asyncio.run(main_loop())
