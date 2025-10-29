# main_breakout_v1_render_hibrido.py
# ALERTA ÚNICO: PUMP INTELIGENTE (5m) — 15 MIN COOLDOWN
# Monitora as 50 moedas com maior volume
# Foco: INÍCIO de alta (explosiva ou gradual)
# Adaptativo à volatilidade | 1 alerta = 1 entrada real

import os, asyncio, aiohttp, time, math, statistics
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 15 * 60          # 15 MINUTOS — AGORA OFICIAL
TOP_N = 50
REQ_TIMEOUT = 8

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "PUMP INTELIGENTE V3 (5m) | 15 min cooldown | 50 maiores volumes", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " Brasil"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        await session.post(url, data=payload, timeout=REQ_TIMEOUT)
    except:
        pass

def fmt_price(x: float) -> str:
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"

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
    if len(seq) < period + 1:
        return [50.0] * len(seq)
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
            if isinstance(data, list):
                return data
            return []
    except:
        return []

async def get_top_usdt_symbols(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=REQ_TIMEOUT) as r:
        data = await r.json()
    blocked = (
        "UP", "DOWN", "BULL", "BEAR",
        "BUSD", "FDUSD", "TUSD", "USDC", "USDP", "USD1", "USDE", "XUSD", "USDX", "GUSD", "BFUSD",
        "EUR", "EURS", "CEUR", "BRL", "TRY",
        "PERP", "_PERP", "STABLE", "TEST",
        "HIFI", "BAKE"
    )
    pares = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        if any(x in s for x Celestial in blocked):
            continue
        try:
            qv = float(d.get("quoteVolume", "0") or 0.0)
        except:
            qv = 0.0
        pares.append((s, qv))
    pares.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pares[:TOP_N]]

# ---------------- ALERT STATE ----------------
LAST_HIT = {}

def allowed(symbol, kind):
    ts = LAST_HIT.get((symbol, kind), 0.0)
    return (time.time() - ts) >= COOLDOWN_SEC

def mark(symbol, kind):
    LAST_HIT[(symbol, kind)] = time.time()

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # ==================================================
        # ALERTA ÚNICO: PUMP INTELIGENTE V3 (5m) — 15 MIN COOLDOWN
        # ==================================================
        k5 = await get_klines(session, symbol, "5m", limit=300)
        k1h = await get_klines(session, symbol, "1h", limit=100)
        
        if len(k5) < 300 or len(k1h) < 100:
            return

        c5 = [float(k[4]) for k in k5]
        o5 = [float(k[1]) for k in k5]
        h5 = [float(k[2]) for k in k5]
        l5 = [float(k[3]) for k in k5]
        v5 = [float(k[5]) for k in k5]

        i = len(c5) - 1

        # --- 1. FILTRO 1h (flexível) ---
        c1h = [float(k[4]) for k in k1h]
        ema50_1h = ema(c1h, 50)
        if c1h[-1] < ema50_1h[-1] * 0.98:
            return

        # --- 2. ATR (volatilidade real) ---
        atr = max([h5[j] - l5[j] for j in range(-14, 0)])
        preco = c5[i]
        volatilidade_pct = (atr / preco) * 100 if preco > 0 else 0

        # --- 3. VOLUME CRESCENTE ---
        vol_seq = v5[-5:]
        volume_subindo = all(vol_seq[j] > vol_seq[j-1] * 1.05 for j in range(1, 5))
        vol_med_20 = sum(v5[-20:]) / 20
        volume_acima_media = v5[i] > vol_med_20 * 1.5

        # --- 4. ALTA SUSTENTÁVEL ---
        ultimos_7 = [(c5[j] - o5[j]) / o5[j] for j in range(-6, 1)]
        verdes_fortes = sum(1 for x in ultimos_7 if x > 0.003) >= 3
        net_up = (c5[i] - c5[i-10]) / c5[i-10] >= 0.025

        # --- 5. RSI ADAPTATIVO ---
        rsi = calc_rsi(c5, 14)[i]
        rsi_min = 35 if volatilidade_pct > 3 else 30
        rsi_max = 75 if volatilidade_pct > 3 else 65
        rsi_ok = rsi_min <= rsi <= rsi_max

        # --- 6. GATILHO FINAL ---
        candle_forte = (c5[i] - o5[i]) / o5[i] >= 0.005
        acima_ema9 = c5[i] > ema(c5, 9)[i]

        # --- ALERTA ---
        if (volume_subindo and volume_acima_media and verdes_fortes and net_up and 
            rsi_ok and candle_forte and acima_ema9 and allowed(symbol, "PUMP_INT")):

            stop = min(l5[i], ema(c5, 50)[i])
            risco = preco - stop
            alvo = preco + 2.5 * risco

            msg = (f"<b>PUMP INTELIGENTE!</b>\n"
                   f"<b>{symbol}</b> | Alta iniciando\n"
                   f"Preço: <b>{fmt_price(preco)}</b>\n"
                   f"+{net_up*100:.1f}% em 10c | Vol crescente\n"
                   f"Stop: <code>{fmt_price(stop)}</code>\n"
                   f"Alvo 1:2.5: <code>{fmt_price(alvo)}</code>\n"
                   f"{now_br()}\n"
                   f"──────────────────────────────")
            await tg(session, msg)
            mark(symbol, "PUMP_INT")

    except Exception as e:
        pass  # Silencia erros individuais

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"<b>PUMP INTELIGENTE V3 ATIVO</b>\n"
                         f"15 min cooldown | 50 maiores volumes\n"
                         f"{now_br()}\n"
                         f"──────────────────────────────")
        if not symbols:
            return
        while True:
            tasks = [scan_symbol(session, s) for s in symbols]
            await asyncio.gather(*tasks)
            await asyncio.sleep(10)

# ---------------- RUN ----------------
def start_bot():
    while True:
        try:
            asyncio.run(main_loop())
        except Exception:
            time.sleep(5)

threading.Thread(target=start_bot, daemon=True).start()
app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
