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

MIN_VOL24 = 5_000_000
TOP_N = 50
SCAN_INTERVAL = 30

STOCH_PERIOD = 14

cooldown = {}

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

def can_alert(sym, ma9, ma20, direction):
    estado = cooldown.get(sym, {"liberado": True})

    if not estado["liberado"]:
        if direction == "long" and ma9 < ma20:
            estado["liberado"] = True
        elif direction == "short" and ma9 > ma20:
            estado["liberado"] = True

    if estado["liberado"]:
        estado["liberado"] = False
        cooldown[sym] = estado
        return True

    cooldown[sym] = estado
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
        diff = abs((ma9 - ma20) / ma20) * 100

        vol_avg = sum(volumes[-10:]) / 10
        vol_now = volumes[-1]

        # CVD aproximado
        cvd_up = sum(taker_buy[-3:]) > sum(volumes[-3:]) * 0.55

        # OI
        oi_now = await get_oi(session, sym)

        # Pré-sinal (antecipação)
        pre_signal = 0.1 <= diff <= 0.4 and vol_now < vol_avg

        # Estrutura
        long_ok = ma9 > ma20 > ma50 and price > ma200
        short_ok = ma9 < ma20 < ma50 and price < ma200

        # Fluxo
        fluxo_ok = cvd_up

        # Gatilho
        gatilho_long = rsi > 50
        gatilho_short = rsi < 50

        if pre_signal and fluxo_ok and long_ok and gatilho_long and can_alert(sym, ma9, ma20, "long"):
            nome = sym.replace("USDT","")
            msg = (
                f"🚀 <b>ANTECIPAÇÃO LONG</b>\n\n"
                f"#{nome}\n"
                f"Preço: {price}\n"
                f"Diff: {diff:.2f}%\n"
                f"RSI: {rsi:.1f}\n"
                f"OI: {oi_now}\n"
                f"⏰ {now_br()} BR"
            )
            await tg(session, msg)

        if pre_signal and fluxo_ok and short_ok and gatilho_short and can_alert(sym, ma9, ma20, "short"):
            nome = sym.replace("USDT","")
            msg = (
                f"🔻 <b>ANTECIPAÇÃO SHORT</b>\n\n"
                f"#{nome}\n"
                f"Preço: {price}\n"
                f"Diff: {diff:.2f}%\n"
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
