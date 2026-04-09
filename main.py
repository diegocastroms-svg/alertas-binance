import os, asyncio, aiohttp, time, math
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V10 - ANTECIPACAO REAL (OI + CVD)", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://fapi.binance.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 10_000_000
TOP_N = 80
SCAN_INTERVAL = 30

STOCH_PERIOD = 14

cooldown = {}
alert_state = {}

COOLDOWN_SECONDS = 14400  # 4 horas

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

def now_ts():
    return int(time.time())

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

def can_alert(sym):
    t = cooldown.get(sym, 0)
    if now_ts() - t >= COOLDOWN_SECONDS:
        cooldown[sym] = now_ts()
        return True
    return False

def sma(data, n):
    if len(data) < n: return 0
    return sum(data[-n:]) / n

def rsi_calc(data, period):
    if len(data) < period * 2: return 50
    gains, losses = [], []
    for i in range(len(data)-1):
        d = data[i+1] - data[i]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def ema(data, period):
    if len(data) < period: return []
    k = 2 / (period + 1)
    ema_vals = [sum(data[:period]) / period]
    for price in data[period:]:
        ema_vals.append(price * k + ema_vals[-1] * (1 - k))
    return ema_vals

def macd_calc(closes):
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)

    if len(ema12) < 2 or len(ema26) < 2:
        return None, None, None, None

    macd_line = [a - b for a, b in zip(ema12[-len(ema26):], ema26)]
    signal = ema(macd_line, 9)

    if len(signal) < 2:
        return None, None, None, None

    hist = macd_line[-1] - signal[-1]
    hist_prev = macd_line[-2] - signal[-2]

    return macd_line[-1], signal[-1], hist, hist_prev

async def get_oi(session, symbol):
    try:
        async with session.get(f"{BINANCE}/fapi/v1/openInterest?symbol={symbol}") as r:
            data = await r.json()
            return float(data["openInterest"])
    except:
        return 0

async def scan(session, sym):
    try:
        async with session.get(f"{BINANCE}/fapi/v1/klines?symbol={sym}&interval=15m&limit=100") as r:
            k = await r.json()

        if len(k) < 60: return

        closes = [float(x[4]) for x in k]
        volumes = [float(x[5]) for x in k]
        taker_buy = [float(x[10]) for x in k]

        price = closes[-1]

        ma9 = sma(closes, 9)
        ma20 = sma(closes, 20)
        ma50 = sma(closes, 50)
        ma200 = sma(closes, 100)

        rsi = rsi_calc(closes, STOCH_PERIOD)

        if ma20 == 0: return

        vol_avg = sum(volumes[-10:]) / 10
        vol_now = volumes[-1]

        cvd_up = sum(taker_buy[-3:]) > sum(volumes[-3:]) * 0.55

        oi_now = await get_oi(session, sym)

        async with session.get(f"{BINANCE}/fapi/v1/klines?symbol={sym}&interval=1h&limit=100") as r:
            k1h = await r.json()

        async with session.get(f"{BINANCE}/fapi/v1/klines?symbol={sym}&interval=4h&limit=100") as r:
            k4h = await r.json()

        closes_15 = closes
        closes_1h = [float(x[4]) for x in k1h]
        closes_4h = [float(x[4]) for x in k4h]

        macd15, sig15, hist15, hist15_prev = macd_calc(closes_15)
        macd1h, sig1h, hist1h, hist1h_prev = macd_calc(closes_1h)
        macd4h, sig4h, hist4h, hist4h_prev = macd_calc(closes_4h)

        if None in [macd15, sig15, hist15, hist15_prev, macd1h, sig1h, hist1h, hist1h_prev, macd4h, sig4h, hist4h, hist4h_prev]:
            return

        # ===== INÍCIO REAL =====
        long_15m = macd15 > sig15 and hist15 > 0 and hist15 > hist15_prev and hist15_prev > 0

        long_1h = (
            macd1h > sig1h
            and hist1h > 0
            and hist1h_prev < 0
        )

        long_4h = macd4h > sig4h and hist4h > hist4h_prev

        short_15m = macd15 < sig15 and hist15 < 0 and hist15 < hist15_prev and hist15_prev < 0

        short_1h = (
            macd1h < sig1h
            and hist1h < 0
            and hist1h_prev > 0
        )

        short_4h = macd4h < sig4h and hist4h < hist4h_prev

        if long_15m and long_1h and long_4h and can_alert(sym):
            nome = sym.replace("USDT","")
            msg = (
                f"🚀 <b>CONFLUÊNCIA MACD LONG</b>\n\n"
                f"#{nome}\n"
                f"Preço: {price}\n"
                f"RSI: {rsi:.1f}\n"
                f"OI: {oi_now}\n"
                f"⏰ {now_br()} BR"
            )
            await tg(session, msg)

        if short_15m and short_1h and short_4h and can_alert(sym):
            nome = sym.replace("USDT","")
            msg = (
                f"🔻 <b>CONFLUÊNCIA MACD SHORT</b>\n\n"
                f"#{nome}\n"
                f"Preço: {price}\n"
                f"RSI: {rsi:.1f}\n"
                f"OI: {oi_now}\n"
                f"⏰ {now_br()} BR"
            )
            await tg(session, msg)

    except Exception as e:
        print("Erro:", e)

async def main():
    async with aiohttp.ClientSession() as session:
        await tg(session, "<b>V10 - ANTECIPACAO REAL ATIVA</b>")
        while True:
            try:
                async with session.get(f"{BINANCE}/fapi/v1/ticker/24hr") as r:
                    data = await r.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume",0)) >= MIN_VOL24
                ]

                symbols = symbols[:TOP_N]

                await asyncio.gather(*[scan(session, s) for s in symbols])

            except Exception as e:
                print(e)

            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000))), daemon=True).start()
asyncio.run(main())
