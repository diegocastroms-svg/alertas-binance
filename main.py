# main.py — V11.0 GOLDEN CROSS BREAKOUT
# EMA9 > EMA21 + Volume 3x + RSI subindo + Rompimento | 3m

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 10 * 60
TOP_N = 120
REQ_TIMEOUT = 10
VERSION = "V11.0 GOLDEN CROSS"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | Golden Cross + Breakout | Tendência Inicial", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%H:%M")

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        print(f"[ALERTA] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=REQ_TIMEOUT)
    except Exception as e:
        print(f"[TG ERRO] {e}")

def ema(seq, period):
    if len(seq) < 1: return []
    alpha = 2 / (period + 1)
    e = seq[0]
    out = [e]
    for p in seq[1:]:
        e = alpha * p + (1 - alpha) * e
        out.append(e)
    return out

def calc_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [abs(min(d, 0)) for d in deltas[:period]]
    avg_g = sum(gains)/period
    avg_l = sum(losses)/period or 1e-12
    rs = avg_g / avg_l
    rsi = 100 - 100/(1+rs)
    for i in range(period, len(deltas)):
        d = deltas[i]
        g = d if d > 0 else 0
        l = -d if d < 0 else 0
        avg_g = (avg_g * (period-1) + g) / period
        avg_l = (avg_l * (period-1) + l) / period
        rs = avg_g / (avg_l + 1e-12)
        rsi = 100 - 100/(1+rs)
    return rsi

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=100):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            if r.status != 200: return []
            return await r.json()
    except:
        return []

async def get_ticker_24hr(session, symbol):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr?symbol={symbol}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            if r.status != 200: return None
            return await r.json()
    except:
        return None

async def get_top_symbols(session):
    try:
        url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            if r.status != 200: return []
            data = await r.json()
        blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","EUR","BRL","PERP")
        pares = []
        for d in data:
            s = d["symbol"]
            if not s.endswith("USDT"): continue
            if any(x in s for x in blocked): continue
            change = float(d.get("priceChangePercent", 0))
            qv = float(d.get("quoteVolume", 0))
            if qv < 10_000_000: continue
            pares.append((s, change, qv))
        pares.sort(key=lambda x: x[1])
        return [p[0] for p in pares[:TOP_N]]
    except:
        return []

# ---------------- COOLDOWN ----------------
cooldowns = {}
def can_alert(s, mode):
    key = f"{s}_{mode}"
    now = time.time()
    if now - cooldowns.get(key, 0) >= COOLDOWN_SEC:
        cooldowns[key] = now
        return True
    return False

# ---------------- MACD ----------------
def macd(prices):
    if len(prices) < 26: return {"hist": []}
    ef = ema(prices, 12)
    es = ema(prices, 26)
    macd_line = [f - s for f, s in zip(ef, es)]
    sig = ema(macd_line, 9)
    hist = [m - sg for m, sg in zip(macd_line[-len(sig):], sig)]
    return {"hist": hist}

# ---------------- SCAN ----------------
async def scan_symbol(session, symbol):
    try:
        ticker = await get_ticker_24hr(session, symbol)
        if not ticker: return
        change24 = float(ticker["priceChangePercent"])
        low24 = float(ticker["lowPrice"])
        preco = float(ticker["lastPrice"])

        k3m = await get_klines(session, symbol, "3m", 100)
        if not k3m or len(k3m) < 80: return

        close3m = [float(k[4]) for k in k3m[:-1]]
        vol3m = [float(k[5]) for k in k3m[:-1]]

        vol_ultima = vol3m[-1]
        vol_media_10 = sum(vol3m[-11:-1]) / 10 if len(vol3m) > 11 else 1
        vol_ratio = vol_ultima / vol_media_10

        e9 = ema(close3m, 9)
        e21 = ema(close3m, 21)
        if len(e9) < 2 or len(e21) < 1: return

        rsi = calc_rsi(close3m[-30:])

        macd_data = macd(close3m)
        hist = macd_data["hist"]

        # === GOLDEN CROSS BREAKOUT (NOVO PADRÃO) ===
        if (change24 <= -5 and
            vol_ratio >= 3.0 and
            e9[-1] > e21[-1] and e9[-2] <= e21[-2] and  # Cruzamento recente
            rsi > 50 and rsi < 70 and  # Momentum saindo do oversold
            len(hist) >= 2 and hist[-1] > 0.001 and hist[-1] > hist[-2] and
            preco > max(close3m[-10:]) and  # Rompimento de resistência
            can_alert(symbol, "GOLDEN")):

            distancia_fundo = (preco - low24) / low24 * 100
            alvo = preco * 1.30
            stop = min([float(k[3]) for k in k3m[-6:-1]]) * 0.99

            prob = 90 + (5 if vol_ratio > 5 else 0) + (5 if rsi > 60 else 0)
            prob = min(98, prob)

            msg = (
                f"<b>ROCKET GOLDEN CROSS BREAKOUT</b>\n"
                f"<code>{symbol}</code>\n"
                f"Queda 24h: <b>{change24:+.1f}%</b>\n"
                f"Preço: <b>{preco:.6f}</b> (+{distancia_fundo:.1f}% do fundo)\n"
                f"<b>PROBABILIDADE: {prob}%</b>\n"
                f"<b>TEMPO: 15-60 MIN</b>\n"
                f"Volume +{vol_ratio:.1f}x | EMA9 cruzou EMA21 | RSI subindo\n"
                f"Stop: <b>{stop:.6f}</b>\n"
                f"Alvo +30%: <b>{alvo:.6f}</b>\n"
                f"<i>{now_br()} BR</i>"
            )
            await tg(session, msg)

    except Exception as e:
        pass

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        await tg(session, f"<b>{VERSION} ATIVO</b>\nGolden Cross + Breakout | Subida Inicial\n{now_br()} BR")

        while True:
            symbols = await get_top_symbols(session)
            print(f"[{now_br()}] V11.0: {len(symbols)} moedas...")
            await asyncio.gather(*[scan_symbol(session, s) for s in symbols], return_exceptions=True)
            await asyncio.sleep(30)

def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main_loop())

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
