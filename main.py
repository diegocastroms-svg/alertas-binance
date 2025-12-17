import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)

@app.route("/")
def home():
    return "V8.3R — MA200 ATIVO (1H) / 15M DESLIGADO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ===== VOLUME AJUSTADO =====
MIN_VOL24 = 500_000

MIN_VOLAT = 2.0
TOP_N = 50
COOLDOWN = 900
SCAN_INTERVAL = 30

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M:%S")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    try:
        await s.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": msg,
                "parse_mode": "HTML"
            },
            timeout=10
        )
    except Exception as e:
        print("Erro Telegram:", e)

cooldown_cross = {}

def can_alert(sym):
    n = time.time()
    if n - cooldown_cross.get(sym, 0) >= COOLDOWN:
        cooldown_cross[sym] = n
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
# ALERTA ATIVO: CRUZAMENTO MA200 (1H)
# ALERTA 15M EXISTE MAS ESTÁ DESLIGADO
# =====================================================
async def scan_tf(s, sym):
    try:
        t = await ticker(s, sym)
        if not t:
            return

        vol24 = float(t.get("quoteVolume", 0) or 0)
        if vol24 < MIN_VOL24:
            return

        # ===== TIMEFRAME 1H =====
        k = await klines(s, sym, "1h")
        if len(k) < 200:
            return

        close = [float(x[4]) for x in k]

        # ===== MA200 =====
        ma200 = sum(close[-200:]) / 200
        price = close[-1]

        nome = sym.replace("USDT", "")

        cruzamento_1h = (
            close[-2] < ma200 and
            close[-1] > ma200 and
            can_alert(sym)
        )

        if cruzamento_1h:
            msg = (
                "<b>CRUZAMENTO MA200 (1H)</b>\n\n"
                f"{nome}\n"
                f"Preco: {price:.6f}\n"
                f"MA200: {ma200:.6f}\n"
                f"Hora: {now_br()} BR"
            )
            await tg(s, msg)

        # ===== 15M EXISTE MAS ESTÁ DESLIGADO =====
        # Nenhuma lógica executa aqui

    except Exception as e:
        print("Erro scan_tf:", e)

# =====================================================
# LOOP PRINCIPAL
# =====================================================
async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>BOT INICIADO — MA200 1H ATIVO</b>")
        while True:
            try:
                data_resp = await s.get(
                    f"{BINANCE}/api/v3/ticker/24hr",
                    timeout=10
                )
                if data_resp.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL)
                    continue

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
                        (float(t.get("quoteVolume", 0) or 0)
                         for t in data if t["symbol"] == x),
                        0
                    ),
                    reverse=True
                )[:TOP_N]

                tasks = [scan_tf(s, sym) for sym in symbols]
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
