# main_breakout_v1_render_hibrido.py
# V6.2 – CONFLUÊNCIA MACD + CRUZAMENTO 5M + TENDÊNCIA CURTA + LIQUIDEZ REAL

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 15 * 60
TOP_N = 50
MIN_LIQUIDITY = 20_000_000  # 🔹 Liquidez mínima exigida (USDT)
REQ_TIMEOUT = 8
VERSION = "V6.2 - CONFLUÊNCIA MACD + 5M + TENDÊNCIA CURTA + LIQUIDEZ REAL"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | 3m/5m/15m/30m/1h + Liquidez > 20M | 50 pares", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " BR"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        print(f"[TG] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=REQ_TIMEOUT)
    except Exception as e:
        print(f"[TG ERRO] {e}")

def fmt_price(x: float) -> str:
    return f"{x:.8f}".rstrip("0").rstrip(".") or "0"

def ema(seq, span):
    if not seq: return []
    alpha = 2.0 / (span + 1.0)
    out = [seq[0]]
    e = seq[0]
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def macd(seq, fast=12, slow=26, signal=9):
    if len(seq) < slow + signal + 1:
        n = len(seq)
        return {"macd": [0.0]*n, "signal": [0.0]*n, "hist": [0.0]*n}
    ema_fast = ema(seq, fast)
    ema_slow = ema(seq, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return {"macd": macd_line, "signal": signal_line, "hist": hist}

def calc_rsi(seq, period=14):
    if len(seq) < period + 1: return [50.0] * len(seq)
    gains, losses = [], []
    for i in range(1, len(seq)):
        diff = seq[i] - seq[i-1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    rsi = []
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / (avg_loss + 1e-12)
    rsi.append(100 - (100 / (1 + rs)))
    for i in range(period, len(seq) - 1):
        diff = seq[i] - seq[i-1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi.append(100 - (100 / (1 + rs)))
    return [50.0] * (len(seq) - len(rsi)) + rsi

def cruzou_de_baixo(c, p9=9, p20=20):
    if len(c) < p20 + 2: return False
    e9 = ema(c, p9)
    e20 = ema(c, p20)
    return e9[-2] <= e20[-2] and e9[-1] > e20[-1]

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=100):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
            return data if isinstance(data, list) else []
    except:
        return []

async def get_top_usdt_symbols(session):
    try:
        url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
        blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","EUR","BRL","PERP","TEST","USDE")
        pares = []
        for d in data:
            s = d.get("symbol", "")
            if not s.endswith("USDT"): continue
            if any(x in s for x in blocked): continue
            qv = float(d.get("quoteVolume", 0) or 0)
            if qv < MIN_LIQUIDITY: continue  # 🔹 ignora pares mortos
            pares.append((s, qv))
        pares.sort(key=lambda x: x[1], reverse=True)
        return pares[:TOP_N]
    except:
        return []

# ---------------- COOLDOWNS ----------------
cooldowns = {}
def can_alert(symbol, tipo, cooldown_sec):
    now = time.time()
    key = f"{symbol}_{tipo}"
    last = cooldowns.get(key, 0)
    if now - last > cooldown_sec:
        cooldowns[key] = now
        return True
    return False

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol, qv):
    try:
        k3  = await get_klines(session, symbol, "3m", 100)
        k5  = await get_klines(session, symbol, "5m", 100)
        k15 = await get_klines(session, symbol, "15m", 100)
        k30 = await get_klines(session, symbol, "30m", 100)
        k1h = await get_klines(session, symbol, "1h", 100)
        if not (k3 and k5 and k15 and k30 and k1h): return

        c3, c5, c15, c30, c1h = [list(map(lambda x: float(x[4]), ks)) for ks in (k3, k5, k15, k30, k1h)]
        v5 = [float(k[5]) for k in k5]
        i5 = len(c5)-1
        vol_med = sum(v5[-10:])/10 if len(v5) >= 10 else v5[-1]

        # --- Condições ---
        cruzou_5m = cruzou_de_baixo(c5, 9, 20)
        macd3, macd5, macd15, macd30, macd1h = map(macd, (c3, c5, c15, c30, c1h))
        h3, h5, h15, h30, h1h = [m["hist"] for m in (macd3, macd5, macd15, macd30, macd1h)]

        def crescente(h): return len(h) >= 2 and h[-1] > 0 and h[-1] > h[-2]

        hist_ok = all([crescente(h3), crescente(h5), crescente(h15), crescente(h30), crescente(h1h)])

        rsi5 = calc_rsi(c5, 14)[-1]
        preco = c5[-1]
        l5 = [float(k[3]) for k in k5]
        stop = min(l5[-1], ema(c5,21)[-1])
        risco = preco - stop
        alvo1 = preco + 2.5 * risco
        alvo2 = preco + 5.0 * risco
        tp_parcial = preco + risco

        if cruzou_5m and hist_ok and 45 <= rsi5 <= 65 and v5[-1] > vol_med * 1.1:
            if can_alert(symbol, "TENDENCIA_CURTA", COOLDOWN_SEC):
                liq = f"{qv/1_000_000:.1f}M USDT"
                msg = (
                    f"💎 <b>TENDÊNCIA CURTA – CONFLUÊNCIA TOTAL</b>\n"
                    f"<b>{symbol}</b>\n\n"
                    f"MACD: 3m✅ 5m✅ 15m✅ 30m✅ 1h✅\n"
                    f"RSI5: {rsi5:.1f}\n"
                    f"Liquidez: {liq}\n\n"
                    f"💰 Preço: <b>{fmt_price(preco)}</b>\n"
                    f"🛡️ Stop: {fmt_price(stop)} (-{(risco/preco)*100:.1f}%)\n"
                    f"🎯 Alvo1: {fmt_price(alvo1)} (+{(alvo1/preco-1)*100:.1f}%)\n"
                    f"🎯 Alvo2: {fmt_price(alvo2)} (+{(alvo2/preco-1)*100:.1f}%)\n"
                    f"💫 Parcial: {fmt_price(tp_parcial)} (+{(tp_parcial/preco-1)*100:.1f}%)\n\n"
                    f"📊 Volume: +{((v5[-1]/vol_med)-1)*100:.0f}% da média\n"
                    f"{now_br()}"
                )
                await tg(session, msg)
    except Exception as e:
        print(f"[ERRO] {symbol}: {e}")

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        pares = await get_top_usdt_symbols(session)
        await tg(session, f"<b>{VERSION} ATIVO</b>\nFiltro: Liquidez ≥ 20M USDT\n{len(pares)} pares\n{now_br()}\n──────────────────────────────")
        while True:
            await asyncio.gather(*[scan_symbol(session, s, qv) for s, qv in pares])
            await asyncio.sleep(30)

def start_bot():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception as e:
            print(f"[LOOP ERRO] {e}")
            time.sleep(5)

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
