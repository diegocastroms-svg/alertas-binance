# V6.1 — CRUZAMENTO 5M FECHADO + MACD > 0.02 + CRESCENTE EM TODOS
# SEM ROUND, SEM FALSOS, SÓ PUMP COM GÁS

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 15 * 60
TOP_N = 90
REQ_TIMEOUT = 8
MIN_HIST = 0.02  # FORÇA MÍNIMA
VERSION = "V6.1 - MACD > 0.02 + CRUZAMENTO 5M FECHADO"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | MACD > {MIN_HIST} | 50 pares", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " BR"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID): print(f"[TG] {text}"); return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=REQ_TIMEOUT)
    except Exception as e: print(f"[TG ERRO] {e}")

def fmt_price(x: float) -> str:
    return f"{x:.8f}".rstrip("0").rstrip(".") or "0"

def ema(seq, span):
    if not seq: return []
    alpha = 2.0 / (span + 1.0)
    out = [seq[0]]; e = seq[0]
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def macd(seq, fast=12, slow=26, signal=9):
    if len(seq) < slow + signal + 1:
        n = len(seq)
        return {"macd": [0.0]*n, "signal": [0.0]*n, "hist": [0.0]*n}
    ema_fast = ema(seq, fast); ema_slow = ema(seq, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    m, s = len(macd_line), len(signal_line)
    if s < m: signal_line = [signal_line[0]] * (m - s) + signal_line
    hist = [m_ - s_ for m_, s_ in zip(macd_line, signal_line)]
    return {"macd": macd_line, "signal": signal_line, "hist": hist}

def cruzou_de_baixo(c, p9=9, p20=20):
    if len(c) < p20 + 2: return False
    e9 = ema(c, p9); e20 = ema(c, p20)
    return e9[-2] <= e20[-2] and e9[-1] > e20[-1]

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=100):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
            return data if isinstance(data, list) and len(data) > 0 else []
    except: return []

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
            pares.append((s, qv))
        pares.sort(key=lambda x: x[1], reverse=True)
        return pares[:TOP_N]
    except: return []

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
        # === DADOS ===
        k5  = await get_klines(session, symbol, "5m",  100)
        k15 = await get_klines(session, symbol, "15m", 100)
        k30 = await get_klines(session, symbol, "30m", 100)
        k1h = await get_klines(session, symbol, "1h",  100)
        if not all([k5, k15, k30, k1h]): return

        c5  = [float(k[4]) for k in k5]
        c15 = [float(k[4]) for k in k15]
        c30 = [float(k[4]) for k in k30]
        c1h = [float(k[4]) for k in k1h]
        v5  = [float(k[5]) for k in k5]

        volmed5 = sum(v5[-10:]) / 10 if len(v5) >= 10 else v5[-1]

        # === CRUZAMENTO 5M FECHADO ===
        c5_closed = c5[:-1]
        cruzou_5m = cruzou_de_baixo(c5_closed, 9, 20)

        # === MACD FECHADO ===
        macd5  = macd(c5_closed)
        macd15 = macd(c15[:-1])
        macd30 = macd(c30[:-1])
        macd1h = macd(c1h[:-1])

        h5  = macd5["hist"]
        h15 = macd15["hist"]
        h30 = macd30["hist"]
        h1h = macd1h["hist"]

        # === FORÇA REAL: MACD > 0.02 + CRESCENTE ===
        hist_ok = (
            len(h5)  >= 2 and h5[-1]  > MIN_HIST and h5[-1]  > h5[-2]  and
            len(h15) >= 2 and h15[-1] > MIN_HIST and h15[-1] > h15[-2] and
            len(h30) >= 2 and h30[-1] > MIN_HIST and h30[-1] > h30[-2] and
            len(h1h) >= 2 and h1h[-1] > MIN_HIST and h1h[-1] > h1h[-2]
        )

        # === FILTROS ===
        rsi15 = calc_rsi(c15, 14)[-1]
        preco = c5[-1]
        ema20_1h = ema(c1h, 20)[-1]
        filtro_forte = preco > ema20_1h and 45 <= rsi15 <= 68 and v5[-1] > volmed5 * 1.1

        # === ALERTA ===
        if cruzou_5m and hist_ok and filtro_forte and can_alert(symbol, "ALERTA", COOLDOWN_SEC):
            stop = min([float(k[3]) for k in k5][-2:], ema(c5, 21)[-1])
            risco = max(preco - stop, 1e-8)
            alvo1 = preco + 2.5 * risco
            alvo2 = preco + 5.0 * risco

            liq = "Alta" if qv >= 1e8 else "Média" if qv >= 2e7 else "Baixa"
            msg = (
                f"<b>FORÇA BRUTA CONFIRMADA</b>\n"
                f"<code>{symbol}</code>\n"
                f"Preço: <b>{fmt_price(preco)}</b>\n"
                f"Stop: {fmt_price(stop)}\n"
                f"Alvo1: {fmt_price(alvo1)} (+{((alvo1/preco)-1)*100:.1f}%)\n"
                f"Alvo2: {fmt_price(alvo2)} (+{((alvo2/preco)-1)*100:.1f}%)\n"
                f"RSI15: {rsi15:.1f} | Vol: +{((v5[-1]/volmed5)-1)*100:.1f}%\n"
                f"Liquidez: {liq} (${qv/1e6:.0f}M)\n"
                f"{now_br()}"
            )
            await tg(session, msg)

    except Exception as e: print(f"[ERRO {symbol}] {e}")

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        pares = await get_top_usdt_symbols(session)
        await tg(session, f"<b>{VERSION} ONLINE</b>\nMACD > {MIN_HIST} | 50 pares | {now_br()}")
        while True:
            try:
                await asyncio.gather(*[scan_symbol(session, s, qv) for s, qv in pares])
                await asyncio.sleep(30)
            except Exception as e:
                await tg(session, f"<b>ERRO</b>: {e}")
                await asyncio.sleep(10)

def start_bot():
    while True:
        try: asyncio.run(main_loop())
        except: time.sleep(5)

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
