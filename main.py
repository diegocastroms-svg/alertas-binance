# main_breakout_v1_render_hibrido.py
# ✅ Híbrido (3m + 5m + 15m) com confirmação multi-tempo
# ✅ Agora com filtro de cruzamento recente (evita alertas atrasados)
# ✅ Apenas pares spot reais em USDT
# ✅ Cooldown 8 minutos

import os, asyncio, aiohttp, time, math, statistics
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 8 * 60
TOP_N = 50
REQ_TIMEOUT = 8

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m + 15m híbrido) — breakout e confirmação | 🇧🇷", 200

# ---------------- UTILS ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

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

def sma(seq, n):
    out, s = [], 0.0
    from collections import deque
    q = deque()
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s/len(q))
    return out

def ema(seq, span):
    if not seq: return []
    alpha = 2.0/(span+1.0)
    out = [seq[0]]
    e = seq[0]
    for x in seq[1:]:
        e = alpha*x + (1-alpha)*e
        out.append(e)
    return out

def bollinger_bands(seq, n=20, mult=2):
    if len(seq) < n: return [], [], []
    out_mid, out_upper, out_lower = [], [], []
    for i in range(len(seq)):
        window = seq[max(0, i-n+1):i+1]
        m = sum(window)/len(window)
        s = statistics.pstdev(window)
        out_mid.append(m)
        out_upper.append(m + mult*s)
        out_lower.append(m - mult*s)
    return out_upper, out_mid, out_lower

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
async def get_klines(session, symbol, interval, limit=210):
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
        "PERP", "_PERP", "STABLE", "TEST"
    )
    pares = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        if any(x in s for x in blocked):
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

def widening_now(upper, mid, lower):
    if len(upper) < 2: return False
    bw_now = (upper[-1] - lower[-1]) / (mid[-1] + 1e-12)
    bw_prev = (upper[-2] - lower[-2]) / (mid[-2] + 1e-12)
    return bw_now > bw_prev

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # -------- 3m --------
        k3 = await get_klines(session, symbol, "3m", limit=210)
        three_ready = False
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            v3 = [float(k[5]) for k in k3]
            ema9_3, ema20_3, ma50_3 = ema(c3,9), ema(c3,20), sma(c3,50)
            upper3, mid3, lower3 = bollinger_bands(c3, 20, 2)
            rsi7_3, rsi14_3 = calc_rsi(c3,7), calc_rsi(c3,14)
            vma20_3 = sum(v3[-20:]) / 20.0
            i3 = len(c3)-1

            cross_recent_3 = (ema9_3[i3-2] <= ema20_3[i3-2] and ema9_3[i3] > ema20_3[i3])

            cond_3m = (ema9_3[i3] > ema20_3[i3] > ma50_3[i3]) and (rsi7_3[-1] > 55 and rsi14_3[-1] > 50) \
                      and (v3[-1] >= 1.5*(vma20_3+1e-12)) and widening_now(upper3, mid3, lower3) \
                      and (c3[-1] > mid3[-1]) and cross_recent_3

            if cond_3m:
                three_ready = True
                if allowed(symbol, "ALRT_3M"):
                    msg = (f"🟦 {symbol} — 3m PRONTO (cruzamento recente)\n"
                           f"• EMA9>EMA20>MA50 • RSI7:{rsi7_3[-1]:.1f} RSI14:{rsi14_3[-1]:.1f}\n"
                           f"• Vol {fmt_price(v3[-1])} > 1.5×MA20 • BB abrindo\n"
                           f"💰 {fmt_price(c3[i3])}\n🕒 {now_br()}\n──────────────────────────────")
                    await tg(session, msg)
                    mark(symbol, "ALRT_3M")

        # -------- 5m --------
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) < 210: return
        c5 = [float(k[4]) for k in k5]
        v5 = [float(k[5]) for k in k5]
        ema9_5, ema20_5, ma50_5 = ema(c5,9), ema(c5,20), sma(c5,50)
        upper5, mid5, lower5 = bollinger_bands(c5,20,2)
        rsi7_5, rsi14_5 = calc_rsi(c5,7), calc_rsi(c5,14)
        vma20_5 = sum(v5[-20:]) / 20.0
        i5 = len(c5)-1

        cross_recent_5 = (ema9_5[i5-2] <= ema20_5[i5-2] and ema9_5[i5] > ema20_5[i5])

        cond_5m = (ema9_5[i5] > ema20_5[i5] > ma50_5[i5]) and (rsi7_5[-1] > 55 and rsi14_5[-1] > 50) \
                  and (v5[-1] >= 1.5*(vma20_5+1e-12)) and widening_now(upper5, mid5, lower5) \
                  and (c5[-1] > mid5[-1]) and cross_recent_5

        if cond_5m and allowed(symbol, "ALRT_5M"):
            msg = (f"🟩 {symbol} — 5m PRONTO (cruzamento recente)\n"
                   f"• EMA9>EMA20>MA50 • RSI7:{rsi7_5[-1]:.1f} RSI14:{rsi14_5[-1]:.1f}\n"
                   f"• Vol {fmt_price(v5[-1])} > 1.5×MA20 • BB abrindo\n"
                   f"💰 {fmt_price(c5[i5])}\n🕒 {now_br()}\n──────────────────────────────")
            await tg(session, msg)
            mark(symbol, "ALRT_5M")

        # -------- 15m --------
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) < 210: return
        c15 = [float(k[4]) for k in k15]
        v15 = [float(k[5]) for k in k15]
        ema9_15, ema20_15 = ema(c15,9), ema(c15,20)
        upper15, mid15, lower15 = bollinger_bands(c15,20,2)
        rsi14_15 = calc_rsi(c15,14)
        vma20_15 = sum(v15[-20:]) / 20.0
        j = len(c15)-1
        cond_15m_confirm = (ema9_15[j] > ema20_15[j]) and (rsi14_15[-1] > 50) and widening_now(upper15, mid15, lower15)

        # -------- ENTRADA (3m+5m alinhados) --------
        if three_ready and cond_5m and allowed(symbol, "ENTRY_35"):
            msg = (f"🚀 {symbol} — ENTRADA (3m + 5m cruzaram agora)\n"
                   f"• EMA9>EMA20>MA50 • RSI7>55 RSI14>50\n"
                   f"• Vol > 1.5×MA20 • BB abrindo\n"
                   f"💰 {fmt_price(c5[i5])}\n🕒 {now_br()}\n──────────────────────────────")
            await tg(session, msg)
            mark(symbol, "ENTRY_35")

        # -------- CONFIRMAÇÃO 15m --------
        entry_age = time.time() - LAST_HIT.get((symbol, "ENTRY_35"), 0.0)
        if entry_age <= 60*60 and cond_15m_confirm and allowed(symbol, "CONF_15"):
            msg = (f"✅ {symbol} — CONFIRMADO (15m alinhado)\n"
                   f"• EMA9>EMA20 • RSI14:{rsi14_15[-1]:.1f} • BB abrindo\n"
                   f"💰 {fmt_price(c15[j])}\n🕒 {now_br()}\n──────────────────────────────")
            await tg(session, msg)
            mark(symbol, "CONF_15")

    except:
        return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        symbols = await get_top_usdt_symbols(session)
        await tg(session, f"✅ Scanner ativo | {len(symbols)} pares | cooldown 8m | {now_br()} (UTC-3)\n──────────────────────────────")
        if not symbols: return
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
