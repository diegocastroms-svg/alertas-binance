# main.py — V7.3 OURO CONFLUÊNCIA REAL – TENDÊNCIA CURTA
# Estratégia: Entrada antecipada com confluência entre 15m, 30m e 1h
# Foco: detectar início de tendência real (não topos)

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading

app = Flask(__name__)
@app.route("/")
def home():
    return "V7.3 OURO CONFLUÊNCIA REAL – TENDÊNCIA CURTA ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ===== PARÂMETROS =====
MIN_VOL24 = 10_000_000
VOL_STRENGTH_MIN = 120
COOLDOWN = {"15m": 900, "30m": 1800, "1h": 3600}
TOP_N = 50
# ======================

def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    try:
        await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                     timeout=10)
    except Exception as e:
        print("Erro Telegram:", e)

def ema(data, p):
    if not data: return []
    a = 2 / (p + 1)
    e = data[0]
    out = [e]
    for x in data[1:]:
        e = a * x + (1 - a) * e
        out.append(e)
    return out

def sma(data, p):
    if len(data) < p: return []
    s = sum(data[:p])
    out = [s/p]
    for i in range(p, len(data)):
        s += data[i] - data[i-p]
        out.append(s/p)
    return out

def rsi(prices, p=14):
    if len(prices) < p+1: return 50
    d = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(x, 0) for x in d[-p:]]
    losses = [abs(min(x, 0)) for x in d[-p:]]
    ag, al = sum(gains)/p, (sum(losses)/p or 1e-12)
    return 100 - 100/(1 + ag/al)

cooldown = {"15m": {}, "30m": {}, "1h": {}}
def can_alert(tf, sym):
    n = time.time()
    if n - cooldown[tf].get(sym, 0) >= COOLDOWN[tf]:
        cooldown[tf][sym] = n
        return True
    return False

async def klines(s, sym, tf, lim=100):
    async with s.get(f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit={lim}", timeout=10) as r:
        return await r.json() if r.status == 200 else []

async def ticker(s, sym):
    async with s.get(f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}", timeout=10) as r:
        return await r.json() if r.status == 200 else None

async def scan_tf(s, sym, tf):
    try:
        t = await ticker(s, sym)
        if not t: return
        vol24 = float(t["quoteVolume"])
        if vol24 < MIN_VOL24: return
        k = await klines(s, sym, tf, 100)
        if len(k) < 50: return

        close = [float(x[4]) for x in k]
        ema9 = ema(close, 9)
        ema20 = ema(close, 20)
        ema50 = ema(close, 50)
        ema100 = ema(close, 100)
        ema200 = ema(close, 200)
        current_rsi = rsi(close)

        macd_line = ema(close, 12)[-1] - ema(close, 26)[-1]
        signal_line = ema(close, 9)[-1]
        macd_hist = macd_line - signal_line

        ma9 = sum(close[-9:])/9
        ma21 = sum(close[-21:])/21
        base = (ma9 + ma21) / 2
        volume_strength = (float(k[-1][5]) / base) * 100

        taker_buy = float(t.get("takerBuyQuoteAssetVolume", 0))
        real_flow = (taker_buy / (vol24 or 1e-12)) * 100

        price = close[-1]
        nome = sym.replace("USDT", "")

        # Condições principais
        if price > ema200[-1] * 1.03:  # evita topos
            return
        if not (macd_hist > 0 and current_rsi > 50 and volume_strength >= VOL_STRENGTH_MIN and real_flow > 0):
            return
        if not can_alert(tf, sym): return

        stop = min(float(x[3]) for x in k[-10:]) * 0.98
        alvo1, alvo2 = price*1.025, price*1.05

        msg = (
            f"🔥 <b>INÍCIO DE TENDÊNCIA REAL ({tf.upper()})</b>\n\n"
            f"{nome}\n\n"
            f"Preço: <b>{price:.6f}</b>\n"
            f"RSI: <b>{current_rsi:.1f}</b> | MACD: <b>{'positivo' if macd_hist>0 else 'negativo'}</b>\n"
            f"Vol força: <b>{volume_strength:.0f}%</b>\n"
            f"Fluxo real: <b>{real_flow:.1f}%</b>\n"
            f"Stop: <b>{stop:.6f}</b>\n"
            f"Alvo1: <b>{alvo1:.6f}</b> | Alvo2: <b>{alvo2:.6f}</b>\n"
            f"📊 Volume 24h: ${vol24:,.0f}\n"
            f"⏱ {now_br()} BR"
        )
        await tg(s, msg)

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V7.3 OURO CONFLUÊNCIA REAL – TENDÊNCIA CURTA ATIVO</b>")
        while True:
            try:
                data = await (await s.get(f"{BINANCE}/api/v3/ticker/24hr")).json()
                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d["quoteVolume"]) > MIN_VOL24
                    and not any(x in d["symbol"] for x in ["UP", "DOWN", "BUSD", "FDUSD", "USDC", "TUSD"])
                ]
                symbols = sorted(symbols, key=lambda x: next((float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0), reverse=True)[:TOP_N]
                tasks = []
                for sym in symbols:
                    for tf in ["15m", "30m", "1h"]:
                        tasks.append(scan_tf(s, sym, tf))
                await asyncio.gather(*tasks)
            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(60)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
