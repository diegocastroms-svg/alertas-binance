# main_breakout_v1_render_hibrido.py
# V6.2 – CONFLUÊNCIA SIMPLES (3m MACD, 5m CRUZAMENTO + MACD, 15m MACD)

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 10 * 60  # 10 minutos
TOP_N = 50
REQ_TIMEOUT = 8
VERSION = "V6.2 - CONFLUÊNCIA SIMPLES (3m/5m/15m)"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | MACD 3m+15m e Cruzamento 5m | 50 pares", 200

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
    hist = [m_ - s_ for m_, s_ in zip(macd_line, signal_line)]
    return {"macd": macd_line, "signal": signal_line, "hist": hist}

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
            return data if isinstance(data, list) and len(data) > 0 else []
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

# ---------------- WORKER (ALERTA NOVO) ----------------
async def scan_symbol(session, symbol, qv):
    try:
        k3  = await get_klines(session, symbol, "3m", 100)
        k5  = await get_klines(session, symbol, "5m", 100)
        k15 = await get_klines(session, symbol, "15m", 100)
        if not (len(k3) and len(k5) and len(k15)): return

        c3  = [float(k[4]) for k in k3]
        c5  = [float(k[4]) for k in k5]
        c15 = [float(k[4]) for k in k15]

        i3, i5, i15 = len(c3)-1, len(c5)-1, len(c15)-1

        # === MACD ===
        macd3 = macd(c3)
        macd5 = macd(c5)
        macd15 = macd(c15)

        macd_3_ok = macd3["hist"][-1] > 0
        macd_5_ok = macd5["hist"][-1] > 0
        macd_15_ok = macd15["hist"][-1] > 0

        # === CRUZAMENTO 5M ===
        cruzou_5m = cruzou_de_baixo(c5)

        # === CONDIÇÃO FINAL ===
        if macd_3_ok and macd_5_ok and macd_15_ok and cruzou_5m:
            if can_alert(symbol, "CONFLUENCIA_SIMPLES", COOLDOWN_SEC):
                preco = c5[-1]
                msg = (
                    f"💎 <b>CONFLUÊNCIA SIMPLES</b>\n"
                    f"{symbol}\n"
                    f"3m: MACD ✅\n"
                    f"5m: EMA9>EMA20 ✅ + MACD ✅\n"
                    f"15m: MACD ✅\n"
                    f"Preço: {fmt_price(preco)}\n"
                    f"{now_br()}"
                )
                await tg(session, msg)
    except Exception as e:
        print(f"[ERRO] {symbol}: {e}")

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        pares = await get_top_usdt_symbols(session)
        await tg(session, f"<b>{VERSION} ATIVO</b>\n3m MACD + 5m cruzamento + 15m MACD\n{len(pares)} pares\n{now_br()}\n──────────────────────────────")
        while True:
            try:
                await asyncio.gather(*[scan_symbol(session, s, qv) for s, qv in pares])
                await asyncio.sleep(20)
            except Exception as e:
                await tg(session, f"<b>ERRO NO BOT</b>\n{e}\nReiniciando em 10s...\n{now_br()}")
                print(f"[LOOP ERRO] {e}")
                await asyncio.sleep(10)

def start_bot():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception as e:
            print(f"[LOOP ERRO] {e}")
            time.sleep(5)

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
