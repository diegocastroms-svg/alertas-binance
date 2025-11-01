# main.py — V6.3B – OURO CONFLUÊNCIA CURTA (AGRESSIVA) – CORRIGIDO GLOBAL
# 3m: EMA9 acima da EMA20 + RSI 40–80
# 5m, 15m e 30m: MACD verde (alinhamento)
# histograma crescente
# liquidez mínima 20M USDT + 1000 trades
# bloqueio automático de moedas mortas
# alerta com “TENDÊNCIA CURTA”
# cooldown de 10 minutos
# top 50 pares de maior volume (atualizado a cada 5 min)

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 10 * 60
TOP_N = 50
REQ_TIMEOUT = 8
UPDATE_TOP_INTERVAL = 10  # ciclos (30s cada) → 5 min
VERSION = "V6.3B - OURO CONFLUÊNCIA CURTA (AGRESSIVA)"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return f"{VERSION} | 3m EMA+RSI (40–80) | 5m/15m/30m MACD | 50 pares", 200

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

def ema(seq, span):
    if not seq: return []
    alpha = 2 / (span + 1)
    e = seq[0]
    out = [e]
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def macd(seq, fast=12, slow=26, signal=9):
    if len(seq) < slow:
        return {"macd": [], "signal": [], "hist": []}
    ema_fast = ema(seq, fast)
    ema_slow = ema(seq, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    if len(macd_line) < signal:
        return {"macd": macd_line, "signal": [], "hist": []}
    signal_line = ema(macd_line, signal)
    hist = [m - s for m, s in zip(macd_line[-len(signal_line):], signal_line)]
    pad = len(macd_line) - len(hist)
    return {
        "macd": macd_line,
        "signal": [0]*pad + signal_line,
        "hist": [0]*pad + hist
    }

def calc_rsi(seq, period=14):
    if len(seq) < period + 1:
        return [50.0] * len(seq)
    rsi = []
    gain = loss = 0.0
    for i in range(1, period + 1):
        delta = seq[i] - seq[i - 1]
        gain += max(delta, 0)
        loss += abs(min(delta, 0))
    gain /= period
    loss /= period
    rs = gain / (loss + 1e-12)
    rsi.append(100 - 100 / (1 + rs))
    for i in range(period + 1, len(seq)):
        delta = seq[i] - seq[i - 1]
        gain = (gain * (period - 1) + max(delta, 0)) / period
        loss = (loss * (period - 1) + abs(min(delta, 0))) / period
        rs = gain / (loss + 1e-12)
        rsi.append(100 - 100 / (1 + rs))
    return [50.0] * (len(seq) - len(rsi)) + rsi

def ema_alinhada(c, p9=9, p20=20):
    if len(c) < p20: return False
    e9, e20 = ema(c, p9), ema(c, p20)
    return e9[-1] > e20[-1]

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=100):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            if r.status != 200:
                return []
            return await r.json()
    except:
        return []

async def get_top_usdt_symbols(session):
    try:
        url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            if r.status != 200:
                return []
            data = await r.json()
        blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","EUR","BRL","PERP","TEST","USDE")
        pares = []
        for d in data:
            s = d.get("symbol", "")
            if not s.endswith("USDT"): continue
            if any(x in s for x in blocked): continue
            qv = float(d.get("quoteVolume", 0) or 0)
            count = int(d.get("count", 0))
            if qv < 20_000_000 or count < 1000: continue
            pares.append((s, qv))
        pares.sort(key=lambda x: x[1], reverse=True)
        return pares[:TOP_N]
    except:
        return []

# ---------------- COOLDOWN ----------------
cooldowns = {}
def can_alert(symbol, cooldown_sec):
    now = time.time()
    last = cooldowns.get(symbol, 0)
    if now - last > cooldown_sec:
        cooldowns[symbol] = now
        # Limpeza de entradas antigas (>1h)
        cutoff = now - 3600
        global cooldowns
        new_cooldowns = {k: v for k, v in cooldowns.items() if v > cutoff}
        cooldowns.clear()
        cooldowns.update(new_cooldowns)
        return True
    return False

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        k3  = await get_klines(session, symbol, "3m", 100)
        k5  = await get_klines(session, symbol, "5m", 100)
        k15 = await get_klines(session, symbol, "15m", 100)
        k30 = await get_klines(session, symbol, "30m", 100)
        if not (k3 and k5 and k15 and k30): return

        c3  = [float(k[4]) for k in k3]
        c5  = [float(k[4]) for k in k5]
        c15 = [float(k[4]) for k in k15]
        c30 = [float(k[4]) for k in k30]

        macd3 = macd(c3)
        macd5 = macd(c5)
        macd15 = macd(c15)
        macd30 = macd(c30)

        acima3 = ema_alinhada(c3, 9, 20)
        rsi3 = calc_rsi(c3)[-1]

        hist5 = macd5["hist"]
        hist15 = macd15["hist"]
        hist30 = macd30["hist"]

        hist5_ok = len(hist5) >= 1 and hist5[-1] > 0
        hist15_ok = len(hist15) >= 1 and hist15[-1] > 0
        hist30_ok = len(hist30) >= 1 and hist30[-1] > 0
        hist_crescente = len(hist5) >= 2 and hist5[-1] > hist5[-2]

        cond = (
            acima3 and
            hist5_ok and hist15_ok and hist30_ok and
            hist_crescente and
            40 <= rsi3 <= 80
        )

        if cond and can_alert(symbol, COOLDOWN_SEC):
            preco = c5[-2]  # vela fechada
            l5 = [float(k[3]) for k in k5]
            ema21 = ema(c5[:-1], 21)
            ema21_val = ema21[-1] if ema21 else preco
            stop = min(l5[-2], ema21_val)
            risco = max(preco - stop, 1e-12)
            alvo1, alvo2 = preco + 2.5*risco, preco + 5*risco
            preco_anterior = c5[-3] if len(c5) >= 3 else preco
            variacao = ((preco - preco_anterior) / preco_anterior) * 100 if preco_anterior != 0 else 0

            msg = (
                f"<b>TENDÊNCIA CURTA CONFIRMADA (AGRESSIVA)</b>\n"
                f"{symbol}\n"
                f"3m RSI:{rsi3:.1f} | 5m 15m 30m\n"
                f"Preço: {preco:.6f} ({variacao:+.2f}%)\n"
                f"Stop: {stop:.6f}\n"
                f"Alvo1: {alvo1:.6f} (1:2.5)\n"
                f"Alvo2: {alvo2:.6f} (1:5)\n"
                f"{now_br()}"
            )
            await tg(session, msg)
            print(f"[ALERTA] {symbol} | RSI={rsi3:.1f}")

    except Exception as e:
        print(f"[ERRO] {symbol}: {e}")

# ---------------- MAIN ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        pares = await get_top_usdt_symbols(session)
        if pares:
            await tg(session, f"<b>{VERSION} ATIVO</b>\n3m EMA+RSI (40–80) | 5m/15m/30m MACD\n{len(pares)} pares\n{now_br()}")
            print(f"[{now_br()}] MONITORANDO {len(pares)} PARES USDT...")

        cycle = 0
        while True:
            cycle += 1
            if cycle % UPDATE_TOP_INTERVAL == 1:
                pares = await get_top_usdt_symbols(session)
                print(f"[{now_br()}] Top 50 atualizado: {len(pares)} pares")

            print(f"[{now_br()}] Iniciando varredura...")
            await asyncio.gather(*[scan_symbol(session, s) for s, _ in pares])
            print(f"[{now_br()}] Varredura concluída. Aguardando 30s...")
            await asyncio.sleep(30)

def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main_loop())
    except Exception as e:
        print(f"[FATAL] {e}")
        time.sleep(5)
        start_bot()

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
