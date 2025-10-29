# main_breakout_v1_render_hibrido.py
# V3.1 - PUMP INTELIGENTE (5m) | 15 MIN COOLDOWN | ALERTA GARANTIDO

import os, asyncio, aiohttp, time, statistics
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 15 * 60          # 15 MINUTOS
TOP_N = 50
REQ_TIMEOUT = 8
VERSION = "V3.1 - PUMP INTELIGENTE"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | 5m | 15 min | 50 pares", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " BR"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        print(f"[TG] {text}")  # LOG NO CONSOLE
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
    alpha = 2.0/(span+1.0)
    out = [seq[0]]
    e = seq[0]
    for x in seq[1:]:
        e = alpha*x + (1-alpha)*e
        out.append(e)
    return out

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
    for i in range(period, len(seq)-1):
        diff = seq[i] - seq[i-1]
        gain = max(diff, 0)
        loss = abs(min(diff, 0))
        avg_gain = (avg_gain * (period-1) + gain) / period
        avg_loss = (avg_loss * (period-1) + loss) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsi.append(100 - (100 / (1 + rs)))
    return [50.0]*(len(seq)-len(rsi)) + rsi

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=300):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[BINANCE ERRO] {symbol} {interval}: {e}")
        return []

async def get_top_usdt_symbols(session):
    try:
        url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
        blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","EUR","BRL","PERP","TEST")
        pares = []
        for d in data:
            s = d.get("symbol", "")
            if not s.endswith("USDT"): continue
            if any(x in s for x in blocked): continue
            qv = float(d.get("quoteVolume", 0) or 0)
            pares.append((s, qv))
        pares.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in pares[:TOP_N]]
    except Exception as e:
        print(f"[SYMBOLS ERRO] {e}")
        return []

# ---------------- ALERT STATE ----------------
LAST_HIT = {}
def allowed(symbol, kind): return (time.time() - LAST_HIT.get((symbol, kind), 0)) >= COOLDOWN_SEC
def mark(symbol, kind): LAST_HIT[(symbol, kind)] = time.time()

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        k5 = await get_klines(session, symbol, "5m", limit=300)
        k1h = await get_klines(session, symbol, "1h", limit=100)
        if len(k5) < 300 or len(k1h) < 100: return

        c5 = [float(k[4]) for k in k5]
        o5 = [float(k[1]) for k in k5]
        h5 = [float(k[2]) for k in k5]
        l5 = [float(k[3]) for k in k5]
        v5 = [float(k[5]) for k in k5]
        i = len(c5) - 1

        # FILTRO 1h (relaxado: permite até 5% abaixo)
        c1h = [float(k[4]) for k in k1h]
        ema50_1h = ema(c1h, 50)
        if c1h[-1] < ema50_1h[-1] * 0.95:  # MUDOU: 5% ao invés de 2%
            return

        # VOLUME (relaxado)
        vol_med_20 = sum(v5[-20:]) / 20
        volume_acima = v5[i] > vol_med_20 * 1.2  # MUDOU: 1.2x ao invés de 1.5x
        volume_subindo = any(v5[j] > v5[j-1] for j in range(-3, 0))  # MUDOU: qualquer aumento

        # ALTA (relaxado)
        net_up = (c5[i] - c5[i-10]) / c5[i-10] >= 0.015  # MUDOU: +1.5% em 10c
        verdes = sum(1 for j in range(-5, 0) if c5[j] > o5[j]) >= 3  # 3/5 verdes

        # RSI (flexível)
        rsi = calc_rsi(c5, 14)[i]
        rsi_ok = 30 <= rsi <= 70

        # GATILHO
        candle_forte = (c5[i] - o5[i]) / o5[i] >= 0.003  # +0.3%
        acima_ema9 = c5[i] > ema(c5, 9)[i]

        if (volume_acima and volume_subindo and net_up and verdes and 
            rsi_ok and candle_forte and acima_ema9 and allowed(symbol, "PUMP_INT")):

            stop = min(l5[i], ema(c5, 50)[i])
            risco = c5[i] - stop
            alvo = c5[i] + 2 * risco  # 1:2

            msg = (f"<b>PUMP INTELIGENTE!</b>\n"
                   f"<b>{symbol}</b>\n"
                   f"Preço: <b>{fmt_price(c5[i])}</b>\n"
                   f"+{net_up*100:.1f}% em 10c\n"
                   f"Stop: <code>{fmt_price(stop)}</code>\n"
                   f"Alvo 1:2: <code>{fmt_price(alvo)}</code>\n"
                   f"{now_br()}\n"
                   f"──────────────────────────────")
            await tg(session, msg)
            mark(symbol, "PUMP_INT")

    except Exception as e:
        print(f"[ERRO {symbol}] {e}")

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"<b>{VERSION} ATIVO</b>\n"
                         f"5m | 15 min | {len(symbols)} pares\n"
                         f"{now_br()}\n"
                         f"──────────────────────────────")
        print(f"[BOT] {len(symbols)} pares carregados")
        while True:
            await asyncio.gather(*[scan_symbol(session, s) for s in symbols])
            await asyncio.sleep(15)  # MUDOU: 15s ao invés de 10s

def start_bot():
    while True:
        try: asyncio.run(main_loop())
        except Exception as e: 
            print(f"[RESTART] {e}")
            time.sleep(5)

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
