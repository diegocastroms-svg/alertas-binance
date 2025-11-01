# main.py — V10.8 TRIPLO MODO (FINAL)
# +8% em 1h | Volume última vela | MACD forte

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 10 * 60
TOP_N = 120
REQ_TIMEOUT = 10
VERSION = "V10.8 TRIPLO"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | Triplo: Reversão + Fundo Duplo + Continuação (+8%)", 200

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
    h = hist[-4:]
    return all(h[i] > 0.002 for i in range(4)) and h[3] > h[2] > h[1] > h[0]

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
            if change > -3 or change < -60: continue  # -3% a -60%
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
        if change24 > -3 or change24 < -60: return
        low24 = float(ticker["lowPrice"])
        preco = float(ticker["lastPrice"])

        k3m = await get_klines(session, symbol, "3m", 100)
        if not k3m or len(k3m) < 80: return

        close3m = [float(k[4]) for k in k3m[:-1]]
        vol3m = [float(k[5]) for k in k3m[:-1]]
        lows = [float(k[3]) for k in k3m[:-1]]

        # VOLUME: ÚLTIMA VELA > 3x MÉDIA DAS ÚLTIMAS 10
        vol_ultima = vol3m[-1]
        vol_media_10 = sum(vol3m[-11:-1]) / 10 if len(vol3m) > 11 else 1
        vol_ratio = vol_ultima / vol_media_10

        e9 = ema(close3m, 9)
        e20 = ema(close3m, 20)
        if len(e9) < 3 or len(e20) < 1: return

        macd_data = macd(close3m)
        hist = macd_data["hist"]

        rsi = calc_rsi(close3m[-30:])

        # === 1. REVERSÃO FORTE ===
        if (vol_ratio >= 3.0 and
            e9[-1] > e20[-1] and e9[-1] > e9[-2] > e9[-3] and
            macd_hist_expanding(hist) and
            rsi > 50 and
            all(close3m[-i] > close3m[-i-1] for i in range(1, 3)) and
            can_alert(symbol, "FORTE")):

            distancia_fundo = (preco - low24) / low24 * 100
            alvo = preco * 1.10
            stop = min(lows[-6:]) * 0.99

            prob = 80 + (15 if vol_ratio > 5 else 10 if vol_ratio > 3 else 0) + (5 if rsi > 60 else 0)
            prob = min(98, prob)

            msg = (
                f"<b>ROCKET REVERSÃO FORTE</b>\n"
                f"<code>{symbol}</code>\n"
                f"Queda 24h: <b>{change24:+.1f}%</b>\n"
                f"Preço: <b>{preco:.6f}</b>\n"
                f"<b>PROB: {prob}%</b> | 15-45 MIN\n"
                f"Volume ÚLTIMA: +{vol_ratio:.1f}x\n"
                f"Stop: <b>{stop:.6f}</b>\n"
                f"Alvo +10%: <b>{alvo:.6f}</b>\n"
                f"<i>{now_br()} BR</i>"
            )
            await tg(session, msg)

        # === 2. FUNDO DUPLO ===
        recent_lows = lows[-12:]
        if len(recent_lows) >= 2:
            min_low = min(recent_lows)
            max_low = max(recent_lows)
            if (max_low - min_low) / min_low <= 0.005:
                touches = sum(1 for l in recent_lows if abs(l - min_low)/min_low <= 0.005)
                if touches >= 2 and preco > min_low * 1.06:
                    if (vol_ratio >= 3.0 and
                        e9[-1] > e20[-1] and
                        macd_hist_expanding(hist) and
                        rsi > 55 and
                        can_alert(symbol, "DUPLO")):

                        alvo50 = preco * 1.50
                        alvo100 = preco * 2.00
                        stop = min_low * 0.99

                        msg = (
                            f"<b>ROCKET FOGUETE DUPLO</b>\n"
                            f"<code>{symbol}</code>\n"
                            f"FUNDO DUPLO: {min_low:.6f} (2+ toques)\n"
                            f"Preço: <b>{preco:.6f}</b>\n"
                            f"<b>95%+</b> | 1-3 HORAS\n"
                            f"Volume ÚLTIMA: +{vol_ratio:.1f}x\n"
                            f"Stop: <b>{stop:.6f}</b>\n"
                            f"Alvo +50%: <b>{alvo50:.6f}</b>\n"
                            f"Alvo +100%: <b>{alvo100:.6f}</b>\n"
                            f"<i>{now_br()} BR</i>"
                        )
                        await tg(session, msg)

        # === 3. CONTINUAÇÃO DE ALTA (+8% em 1h) ===
        if len(close3m) >= 30:
            # Alta forte anterior: +8% em 1h (20 velas)
            high_1h = max(close3m[-20:])
            low_1h = min(close3m[-20:-10])  # antes do pullback
            if high_1h / low_1h >= 1.08:  # +8%
                # Pullback até EMA9 ou EMA20 (sem quebrar)
                pullback_low = min(close3m[-10:])
                if pullback_low >= min(e9[-1], e20[-1]) * 0.995:  # não quebrou
                    # Volume caiu no pullback
                    vol_pullback = sum(vol3m[-5:-1]) / 4
                    vol_anterior = sum(vol3m[-10:-5]) / 5
                    if vol_pullback < vol_anterior * 0.6:
                        # Volume explode na retomada
                        if vol_ratio >= 3.0:
                            # Preço quebra máxima anterior
                            max_anterior = max(close3m[-10:-1])
                            if preco > max_anterior:
                                if macd_data["hist"][-1] > 0.001 and can_alert(symbol, "CONT"):
                                    alvo30 = preco * 1.30
                                    alvo60 = preco * 1.60
                                    stop = pullback_low * 0.99

                                    msg = (
                                        f"<b>ROCKET CONTINUAÇÃO DE ALTA</b>\n"
                                        f"<code>{symbol}</code>\n"
                                        f"Alta anterior: +{((high_1h/low_1h)-1)*100:.1f}% (1h)\n"
                                        f"Pullback: {pullback_low:.6f} → EMA\n"
                                        f"Volume ÚLTIMA: +{vol_ratio:.1f}x\n"
                                        f"Quebra topo: {preco:.6f}\n"
                                        f"<b>95%+</b> | 30-90 MIN\n"
                                        f"Stop: <b>{stop:.6f}</b>\n"
                                        f"Alvo +30%: <b>{alvo30:.6f}</b>\n"
                                        f"Alvo +60%: <b>{alvo60:.6f}</b>\n"
                                        f"<i>{now_br()} BR</i>"
                                    )
                                    await tg(session, msg)

    except Exception as e:
        pass

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        await tg(session, f"<b>{VERSION} ATIVO</b>\nTriplo Modo: Reversão + Duplo + Continuação (+8%)\n{now_br()} BR")

        while True:
            symbols = await get_top_symbols(session)
            print(f"[{now_br()}] V10.8: {len(symbols)} moedas em -3% a -60%...")
            await asyncio.gather(*[scan_symbol(session, s) for s in symbols], return_exceptions=True)
            await asyncio.sleep(30)

def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main_loop())

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
