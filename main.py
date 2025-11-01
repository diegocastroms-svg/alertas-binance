# main.py — V9.0 REVERSÃO DO FUNDO (PEGAR +10% APÓS QUEDA)
# Detecta moedas que caíram 15-50% e estão revertendo
# Entrada no fundo → alvo +10% em 3-6h

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 12 * 60
TOP_N = 100
REQ_TIMEOUT = 10
UPDATE_INTERVAL = 6  # ~3 min
VERSION = "V9.0 - REVERSÃO DO FUNDO"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | Queda 15-50% + Reversão | +10% em 3-6h", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%H:%M")

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        print(f"[REVERSÃO] {text}")
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

def macd_hist_expanding(hist):
    if len(hist) < 3: return False
    h1, h2, h3 = hist[-1], hist[-2], hist[-3]
    return h1 > 0 and h1 > h2 > h3  # verde e acelerando

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
            if change > -15 or change < -70: continue  # só quedas fortes
            pares.append((s, change, qv))
        pares.sort(key=lambda x: x[1])  # mais negativa primeiro
        return [p[0] for p in pares[:TOP_N]]
    except:
        return []

# ---------------- COOLDOWN ----------------
cooldowns = {}
def can_alert(s):
    now = time.time()
    if now - cooldowns.get(s, 0) >= COOLDOWN_SEC:
        cooldowns[s] = now
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
        # 24h ticker
        ticker = await get_ticker_24hr(session, symbol)
        if not ticker: return
        change24 = float(ticker["priceChangePercent"])
        if change24 > -15 or change24 < -70: return
        low24 = float(ticker["lowPrice"])
        high24 = float(ticker["highPrice"])
        preco = float(ticker["lastPrice"])
        vol24 = float(ticker["quoteVolume"])

        # recuperação do fundo?
        if preco < low24 * 1.05: return  # ainda no fundo

        # klines
        k3m = await get_klines(session, symbol, "3m", 50)
        k1h = await get_klines(session, symbol, "1h", 30)
        if not k3m or len(k3m) < 40: return

        close3m = [float(k[4]) for k in k3m[:-1]]
        vol3m = [float(k[5]) for k in k3m[:-1]]

        # volume alto recente
        vol_recent = sum(vol3m[-6:])
        vol_avg = sum(vol3m[-20:-6]) / 14
        vol_ok = vol_recent > vol_avg * 1.5

        # EMA9 > EMA20
        e9 = ema(close3m, 9)
        e20 = ema(close3m, 20)
        ema_ok = len(e9) > 0 and len(e20) > 0 and e9[-1] > e20[-1] and e9[-2] <= e20[-2]

        # MACD
        macd_data = macd(close3m)
        hist = macd_data["hist"]
        macd_ok = len(hist) >= 3 and macd_hist_expanding(hist)

        # RSI
        rsi = calc_rsi(close3m[-30:])
        rsi_ok = 30 <= rsi <= 65

        # CONDIÇÃO FINAL
        if vol_ok and ema_ok and macd_ok and rsi_ok and can_alert(symbol):
            distancia_fundo = (preco - low24) / low24 * 100
            alvo = preco * 1.10
            stop = min([float(k[3]) for k in k3m[-5:-1]]) * 0.99

            msg = (
                f"<b>REVERSÃO DO FUNDO DETECTADA</b>\n"
                f"<code>{symbol}</code>\n"
                f"Queda 24h: <b>{change24:+.1f}%</b> → Fundo: {low24:.6f}\n"
                f"Preço: <b>{preco:.6f}</b> (+{distancia_fundo:.1f}% do fundo)\n"
                f"Volume +{vol_recent/vol_avg:.1f}x | EMA cruzou | MACD verde\n"
                f"Stop: <b>{stop:.6f}</b>\n"
                f"Alvo +10%: <b>{alvo:.6f}</b>\n"
                f"<i>{now_br()} BR</i>"
            )
            await tg(session, msg)
            print(f"[REVERSÃO] {symbol} | {change24:+.1f}% → {preco:.6f}")

    except Exception as e:
        pass

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        await tg(session, f"<b>{VERSION} ATIVO</b>\nCaça reversão após queda forte\n{now_br()} BR")

        while True:
            symbols = await get_top_symbols(session)
            print(f"[{now_br()}] Monitorando {len(symbols)} em queda forte...")
            await asyncio.gather(*[scan_symbol(session, s) for s in symbols], return_exceptions=True)
            await asyncio.sleep(35)

def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main_loop())

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
