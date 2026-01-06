import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V8.3R — MA200 (15M + 1H + 4H + 1D) | SPOT ONLY", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

MIN_VOL24 = 500_000
MIN_VOLAT = 2.0
TOP_N = 100
COOLDOWN = 900
SCAN_INTERVAL = 30

ENABLE_ALERT_15M = True
ENABLE_ALERT_1H  = True
ENABLE_ALERT_4H  = True
ENABLE_ALERT_1D  = True

# ===== REFRESH SPOT (1X POR DIA) =====
SPOT_REFRESH_SECONDS = 86400

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    try:
        await s.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print("Erro Telegram:", e)

cooldown_15m, cooldown_1h, cooldown_4h, cooldown_1d = {}, {}, {}, {}

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
# CARREGA MERCADOS SPOT ATIVOS
# =====================================================
async def load_spot_symbols(session):
    async with session.get(f"{BINANCE}/api/v3/exchangeInfo", timeout=15) as r:
        data = await r.json()

    allowed = set()
    for s in data.get("symbols", []):
        if (
            s.get("status") == "TRADING"
            and "SPOT" in s.get("permissions", [])
        ):
            allowed.add(s["symbol"])
    return allowed

# =====================================================
# ALERTAS (MA200)
# =====================================================
async def scan_tf(s, sym, tf):
    try:
        t = await ticker(s, sym)
        if not t:
            return

        if float(t.get("quoteVolume", 0) or 0) < MIN_VOL24:
            return

        k = await klines(s, sym, tf)
        if len(k) < 200:
            return

        close = [float(x[4]) for x in k]
        ma200 = sum(close[-200:]) / 200
        price = close[-1]

        if close[-2] < ma200 and close[-1] > ma200 and can_alert(sym, tf):
            await tg(
                s,
                f"<b>CRUZAMENTO MA200 ({tf.upper()})</b>\n\n"
                f"{sym.replace('USDT','')}\n"
                f"Preço: {price:.6f}\n"
                f"MA200: {ma200:.6f}\n"
                f"⏱ {now_br()} BR"
            )

    except Exception as e:
        print(f"Erro scan_tf {tf}:", e)

# =====================================================
# LOOP PRINCIPAL
# =====================================================
async def main_loop():
    async with aiohttp.ClientSession() as s:
        ALLOWED_SPOT = await load_spot_symbols(s)
        last_spot_refresh = time.time()

        await tg(s, "<b>V8.3R — MA200 | SPOT ONLY</b>")

        while True:
            try:
                # ===== REFRESH 1X POR DIA =====
                if time.time() - last_spot_refresh >= SPOT_REFRESH_SECONDS:
                    try:
                        ALLOWED_SPOT = await load_spot_symbols(s)
                        last_spot_refresh = time.time()
                        await tg(s, f"<b>SPOT REFRESH OK</b>\n⏱ {now_br()} BR")
                    except Exception as e:
                        print("Erro refresh spot:", e)

                r = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if r.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL)
                    continue

                data = await r.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"] in ALLOWED_SPOT
                    and d["symbol"].endswith("USDT")
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
                        (float(t.get("quoteVolume", 0) or 0)
                         for t in data if t["symbol"] == x),
                        0
                    ),
                    reverse=True
                )[:TOP_N]

                tasks = []
                for sym in symbols:
                    if ENABLE_ALERT_15M: tasks.append(scan_tf(s, sym, "15m"))
                    if ENABLE_ALERT_1H:  tasks.append(scan_tf(s, sym, "1h"))
                    if ENABLE_ALERT_4H:  tasks.append(scan_tf(s, sym, "4h"))
                    if ENABLE_ALERT_1D:  tasks.append(scan_tf(s, sym, "1d"))

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
```0
