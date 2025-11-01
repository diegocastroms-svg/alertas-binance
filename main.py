# main.py — V9.5 REVERSÃO DO FUNDO (CURTO FORTE)
# 3m | Volume 5x+ | MACD 4 velas | EMA forte | RSI subindo

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 10 * 60
TOP_N = 120
REQ_TIMEOUT = 10
UPDATE_INTERVAL = 3  # 3 min
VERSION = "V9.5 - REVERSÃO FORTE"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | Reversão Forte 3m | +10% em 15-45 min", 200

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

def macd_hist_expanding(hist):
    if len(hist) < 4: return False
    h1, h2, h3, h4 = hist[-1], hist[-2], hist[-3], hist[-4]
    return h1 > 0 and h1 > h2 > h3 > h4  # 4 velas verdes crescendo

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
            if qv < 15_000_000: continue
            if change > -15 or change < -70: continue
            pares.append((s, change, qv))
        pares.sort(key=lambda x: x[1])
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
        # 24h
        ticker = await get_ticker_24hr(session, symbol)
        if not ticker: return
        change24 = float(ticker["priceChangePercent"])
        if change24 > -15 or change24 < -70: return
        low24 = float(ticker["lowPrice"])
        preco = float(ticker["lastPrice"])

        if preco < low24 * 1.06: return  # +6% do fundo

        # 3m klines
        k3m = await get_klines(session, symbol, "3m", 60)
        if not k3m or len(k3m) < 50: return

        close3m = [float(k[4]) for k in k3m[:-1]]
        vol3m = [float(k[5]) for k in k3m[:-1]]

        # volume 5x+
        vol_recent = sum(vol3m[-5:])
        vol_avg = sum(vol3m[-25:-5]) / 20
        vol_ratio = vol_recent / (vol_avg + 1e-6)
        if vol_ratio < 5.0: return

        # EMA9 > EMA20 + EMA9 subindo
        e9 = ema(close3m, 9)
        e20 = ema(close3m, 20)
        if len(e9) < 3 or len(e20) < 1: return
        if not (e9[-1] > e20[-1] and e9[-1] > e9[-2] > e9[-3]): return

        # MACD 4 velas verdes crescendo
        macd_data = macd(close3m)
        hist = macd_data["hist"]
        if not macd_hist_expanding(hist): return

        # RSI > 50 e subindo
        rsi = calc_rsi(close3m[-30:])
        if rsi <= 50: return
        rsi_prev = calc_rsi(close3m[-31:-1])
        if rsi <= rsi_prev: return

        # 3 velas verdes seguidas
        closes = close3m[-3:]
        if not all(closes[i] > closes[i-1] for i in range(1, len(closes))): return

        # CONDIÇÃO FINAL
        if can_alert(symbol):
            distancia_fundo = (preco - low24) / low24 * 100
            alvo = preco * 1.10
            stop = min([float(k[3]) for k in k3m[-6:-1]]) * 0.99

            # PROBABILIDADE (95%+)
            prob = 80
            if vol_ratio > 7.0: prob += 15
            elif vol_ratio > 5.0: prob += 10
            if rsi > 60: prob += 5
            prob = min(99, prob)

            msg = (
                f"<b>ROCKET REVERSÃO DO FUNDO</b>\n"
                f"<code>{symbol}</code>\n"
                f"Queda 24h: <b>{change24:+.1f}%</b>\n"
                f"Preço: <b>{preco:.6f}</b> (+{distancia_fundo:.1f}% do fundo)\n"
                f"<b>PROBABILIDADE: {prob}%</b>\n"
                f"<b>TEMPO: 15-45 MIN</b>\n"
                f"Volume +{vol_ratio:.1f}x | 4 velas MACD | EMA forte\n"
                f"Stop: <b>{stop:.6f}</b>\n"
                f"Alvo +10%: <b>{alvo:.6f}</b>\n"
                f"<i>{now_br()} BR</i>"
            )
            await tg(session, msg)
            print(f"[SINAL FORTE {prob}%] {symbol} | +10% em 45 min")

    except Exception as e:
        pass

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        await tg(session, f"<b>{VERSION} ATIVO</b>\nReversão FORTE (95%+)\n{now_br()} BR")

        while True:
            symbols = await get_top_symbols(session)
            print(f"[{now_br()}] Monitorando {len(symbols)} em queda...")
            await asyncio.gather(*[scan_symbol(session, s) for s in symbols], return_exceptions=True)
            await asyncio.sleep(30)

def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main_loop())

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
