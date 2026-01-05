import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V8.3R — MA200 (1H + 4H + 1D) | 15M DESLIGADO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 500_000
MIN_VOLAT = 2.0
TOP_N = 50
COOLDOWN = 900
SCAN_INTERVAL = 30

# ===== FLAGS =====
ENABLE_ALERT_15M = False
ENABLE_ALERT_1H  = True
ENABLE_ALERT_4H  = True
ENABLE_ALERT_1D  = True

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

cooldown_15m = {}
cooldown_1h  = {}
cooldown_4h  = {}
cooldown_1d  = {}

def can_alert(sym, tf):
    n = time.time()
    cd = {
        "15m": cooldown_15m,
        "1h":  cooldown_1h,
        "4h":  cooldown_4h,
        "1d":  cooldown_1d
    }[tf]
    if n - cd.get(sym, 0) >= COOLDOWN:
        cd[sym] = n
        return True
    return False

async def klines(s, sym, tf):
    async with s.get(
        f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit=200",
        timeout=10
    ) as r:
        return await r.json() if r.status == 200 else []

async def ticker(s, sym):
    async with s.get(
        f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}",
        timeout=10
    ) as r:
        return await r.json() if r.status == 200 else None

# =====================================================
# ALERTA 15M — EXISTE, MAS DESLIGADO
# =====================================================
async def scan_tf_15m(s, sym):
    if not ENABLE_ALERT_15M:
        return

# =====================================================
# ALERTA 1H
# =====================================================
async def scan_tf_1h(s, sym):
    try:
        if not ENABLE_ALERT_1H:
            return

        t = await ticker(s, sym)
        if not t: return

        if float(t.get("quoteVolume", 0) or 0) < MIN_VOL24:
            return

        k = await klines(s, sym, "1h")
        if len(k) < 200: return

        close = [float(x[4]) for x in k]
        ma200 = sum(close[-200:]) / 200
        price = close[-1]

        if close[-2] < ma200 and close[-1] > ma200 and can_alert(sym, "1h"):
            await tg(
                s,
                f"<b>CRUZAMENTO MA200 (1H)</b>\n\n"
                f"{sym.replace('USDT','')}\n"
                f"Preço: {price:.6f}\n"
                f"MA200: {ma200:.6f}\n"
                f"⏱ {now_br()} BR"
            )

    except Exception as e:
        print("Erro scan_tf_1h:", e)

# =====================================================
# ALERTA 4H — IGUAL AO 1H
# =====================================================
async def scan_tf_4h(s, sym):
    try:
        if not ENABLE_ALERT_4H:
            return

        t = await ticker(s, sym)
        if not t: return

        if float(t.get("quoteVolume", 0) or 0) < MIN_VOL24:
            return

        k = await klines(s, sym, "4h")
        if len(k) < 200: return

        close = [float(x[4]) for x in k]
        ma200 = sum(close[-200:]) / 200
        price = close[-1]

        if close[-2] < ma200 and close[-1] > ma200 and can_alert(sym, "4h"):
            await tg(
                s,
                f"<b>CRUZAMENTO MA200 (4H)</b>\n\n"
                f"{sym.replace('USDT','')}\n"
                f"Preço: {price:.6f}\n"
                f"MA200: {ma200:.6f}\n"
                f"⏱ {now_br()} BR"
            )

    except Exception as e:
        print("Erro scan_tf_4h:", e)

# =====================================================
# ALERTA 1D — IGUAL AO 1H
# =====================================================
async def scan_tf_1d(s, sym):
    try:
        if not ENABLE_ALERT_1D:
            return

        t = await ticker(s, sym)
        if not t: return

        if float(t.get("quoteVolume", 0) or 0) < MIN_VOL24:
            return

        k = await klines(s, sym, "1d")
        if len(k) < 200: return

        close = [float(x[4]) for x in k]
        ma200 = sum(close[-200:]) / 200
        price = close[-1]

        if close[-2] < ma200 and close[-1] > ma200 and can_alert(sym, "1d"):
            await tg(
                s,
                f"<b>CRUZAMENTO MA200 (1D)</b>\n\n"
                f"{sym.replace('USDT','')}\n"
                f"Preço: {price:.6f}\n"
                f"MA200: {ma200:.6f}\n"
                f"⏱ {now_br()} BR"
            )

    except Exception as e:
        print("Erro scan_tf_1d:", e)

# =====================================================
# LOOP PRINCIPAL
# =====================================================
async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V8.3R — MA200 (1H + 4H + 1D) | 15M OFF</b>")
        while True:
            try:
                r = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if r.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL)
                    continue

                data = await r.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume", 0) or 0) >= MIN_VOL24
                    and abs(float(d.get("priceChangePercent", 0))) >= MIN_VOLAT
                    and not any(x in d["symbol"] for x in [
                        "UP","DOWN","BUSD","FDUSD","USDC","TUSD",
                        "EUR","USDE","TRY","GBP","BRL","AUD","CAD"
                    ])
                ][:TOP_N]

                tasks = []
                for sym in symbols:
                    tasks.append(scan_tf_1h(s, sym))
                    tasks.append(scan_tf_4h(s, sym))
                    tasks.append(scan_tf_1d(s, sym))
                    tasks.append(scan_tf_15m(s, sym))  # permanece desligado

                await asyncio.gather(*tasks)

            except Exception as e:
                print("Erro main_loop:", e)

            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(
    target=lambda: app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    ),
    daemon=True
).start()

asyncio.run(main_loop())
