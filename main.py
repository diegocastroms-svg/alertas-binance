# main_breakout_v1_render_hibrido.py
# V6.2 – BOT PERFEITO | MACD > 0.03 + CRUZAMENTO 5M FECHADO + LOGS + ZERO FALSOS

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 15 * 60
TOP_N = 90
REQ_TIMEOUT = 8
MIN_HIST = 0.03  # FORÇA MÁXIMA
VERSION = "V6.2 - BOT PERFEITO"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | MACD > {MIN_HIST} | 50 pares | {now_br()}", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " BR"

# LOG + TELEGRAM
async def tg(session, text: str):
    log = f"[TG] {text[:100]}{'...' if len(text)>100 else ''}"
    print(log)
    if not (TELEGRAM_TOKEN and CHAT_ID):
        print("[TG SIMULADO] Token ou Chat ID ausente.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        async with session.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=REQ_TIMEOUT) as resp:
            result = await resp.json()
            if result.get("ok"):
                print("[TG OK] Enviado!")
            else:
                print(f"[TG ERRO] {result}")
    except Exception as e:
        print(f"[TG FALHA] {e}")

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
    if len(seq) < slow + signal + 1: return {"hist": [0.0]*len(seq)}
    ema_fast = ema(seq, fast); ema_slow = ema(seq, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal)
    m, s = len(macd_line), len(signal_line)
    if s < m: signal_line = [signal_line[0]] * (m - s) + signal_line
    hist = [m_ - s_ for m_, s_ in zip(macd_line, signal_line)]
    return {"hist": hist}

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
    except Exception as e:
        print(f"[BINANCE ERRO] {symbol} {interval}: {e}")
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
            if qv < 10_000_000: continue  # LIQUIDEZ MÍNIMA
            pares.append((s, qv))
        pares.sort(key=lambda x: x[1], reverse=True)
        return pares[:TOP_N]
    except Exception as e:
        print(f"[TOP PAIRS ERRO] {e}")
        return []

# ---------------- COOLDOWNS ----------------
cooldowns = {}
def can_alert(symbol):
    now = time.time()
    key = f"ALERT_{symbol}"
    last = cooldowns.get(key, 0)
    if now - last > COOLDOWN_SEC:
        cooldowns[key] = now
        return True
    return False

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol, qv):
    try:
        k5 = await get_klines(session, symbol, "5m", 100)
        k15 = await get_klines(session, symbol, "15m", 100)
        k30 = await get_klines(session, symbol, "30m", 100)
        k1h = await get_klines(session, symbol, "1h", 100)
        if not all([k5, k15, k30, k1h]): return

        c5 = [float(k[4]) for k in k5]
        c15 = [float(k[4]) for k in k15]
        c30 = [float(k[4]) for k in k30]
        c1h = [float(k[4]) for k in k1h]
        v5 = [float(k[5]) for k in k5]

        volmed5 = sum(v5[-10:]) / 10 if len(v5) >= 10 else v5[-1]
        if v5[-1] <= volmed5 * 1.1: return  # VOLUME BAIXO

        # CRUZAMENTO 5M FECHADO
        c5_closed = c5[:-1]
        cruzou_5m = cruzou_de_baixo(c5_closed, 9, 20)
        if not cruzou_5m: return

        # MACD FECHADO
        macd5 = macd(c5_closed)
        macd15 = macd(c15[:-1])
        macd30 = macd(c30[:-1])
        macd1h = macd(c1h[:-1])

        h5 = macd5["hist"]
        h15 = macd15["hist"]
        h30 = macd30["hist"]
        h1h = macd1h["hist"]

        # FORÇA BRUTA: MACD > 0.03 + CRESCENTE
        if not all([
            len(h) >= 2 and h[-1] > MIN_HIST and h[-1] > h[-2]
            for h in [h5, h15, h30, h1h]
        ]): return

        # FILTROS
        rsi15 = calc_rsi(c15, 14)[-1]
        if not (45 <= rsi15 <= 68): return

        preco = c5[-1]
        ema20_1h = ema(c1h, 20)[-1]
        if preco <= ema20_1h: return

        # ALERTA
        if can_alert(symbol):
            stop = min([float(k[3]) for k in k5][-2:], ema(c5, 21)[-1])
            risco = max(preco - stop, 1e-8)
            alvo1 = preco + 2.5 * risco
            alvo2 = preco + 5.0 * risco

            liq = "Alta" if qv >= 1e8 else "Média"
            msg = (
                f"<b>FORÇA BRUTA DETECTADA</b>\n"
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

    except Exception as e:
        print(f"[ERRO {symbol}] {e}")

# ---------------- MAIN ----------------
async def main_loop():
    print(f"\n{VERSION} INICIANDO...")
    async with aiohttp.ClientSession() as session:
        pares = await get_top_usdt_symbols(session)
        await tg(session, f"<b>{VERSION} ONLINE</b>\nMACD > {MIN_HIST} | {len(pares)} pares | {now_br()}")
        while True:
            try:
                await asyncio.gather(*[scan_symbol(session, s, qv) for s, qv in pares])
                await asyncio.sleep(30)
            except Exception as e:
                await tg(session, f"<b>ERRO CRÍTICO</b>\n{e}\n{now_br()}")
                await asyncio.sleep(10)

def start_bot():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception as e:
            print(f"[REINICIANDO] {e}")
            time.sleep(5)

print(f"\n{VERSION} CARREGADO. AGUARDANDO INÍCIO...")
threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
