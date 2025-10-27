# main_breakout_v1_render_hibrido.py
# ✅ Híbrido (3m + 5m + 15m) com confirmação multi-tempo
# ✅ Breakout (entrada) nos 3m, 5m e 15m, dentro de 5% da MA200
# ✅ Apenas pares spot reais em USDT
# ✅ Cooldown 8 minutos
# ✅ Inclui stop loss e take profit
# ✅ Adaptação dinâmica à volatilidade do mercado
# ✅ Monitora as 50 moedas com maior volume

import os, asyncio, aiohttp, time, math, statistics
from datetime import datetime, timedelta
from flask import Flask
import threading

# ---------------- CONFIG ----------------
BINANCE_HTTP = "https://api.binance.com"
COOLDOWN_SEC = 8 * 60          # 8 minutos
TOP_N = 50
REQ_TIMEOUT = 8

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m + 15m híbrido) — breakout perto MA200 | Adaptação à volatilidade | 🇧🇷 | 50 maiores volumes", 200

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

def cross_up(a_prev, a_now, b_prev, b_now) -> bool:
    return a_prev <= b_prev and a_now > b_now

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
        "PERP", "_PERP", "STABLE", "TEST",
        "HIFI", "BAKE"  # Bloqueia HIFIUSDT e BAKEUSDT
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

# ---------------- CHECK HELPERS ----------------
def band_width(upper, mid, lower):
    if not upper or not mid or not lower: return 0.0
    return (upper[-1] - lower[-1]) / (mid[-1] + 1e-12)

def widening_now(upper, mid, lower):
    if len(upper) < 2: return False
    bw_now = (upper[-1] - lower[-1]) / (mid[-1] + 1e-12)
    bw_prev = (upper[-2] - lower[-2]) / (mid[-2] + 1e-12)
    return bw_now > bw_prev

def touches_and_closes_above(low, close, ref):
    return (low <= ref) and (close > ref)

def touches_and_closes_below(high, close, ref):
    return (high >= ref) and (close < ref)

def candle_green(close_, open_): return close_ > open_
def candle_red(close_, open_):   return close_ < open_

def calculate_volatility(prices):
    if len(prices) < 2:
        return 0.0
    mean_price = sum(prices) / len(prices)
    if mean_price == 0:
        return 0.0
    std_dev = statistics.pstdev(prices)
    # Volatilidade percentual por hora (ajustada pelo timeframe)
    return (std_dev / mean_price) * 100 * (60 / 15)  # Assume 15m como base, ajuste para outros

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # -------- 3m (Sinal Inicial) --------
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            v3 = [float(k[5]) for k in k3]
            o3 = [float(k[1]) for k in k3]
            ema9_3 = ema(c3, 9)
            ma200_3 = sma(c3, 200)
            rsi3 = calc_rsi(c3, 14)
            vol_ma20_3 = sum(v3[-20:]) / 20.0
            volatility_3m = calculate_volatility(c3[-20:])
            i = len(ema9_3) - 1
            if len(ema9_3) > 2:
                ma200_tolerance = ma200_3[-1] * 0.05  # 5% de tolerância
                within_ma200 = abs(c3[-1] - ma200_3[-1]) <= ma200_tolerance
                cruza = ema9_3[i-1] <= ma200_3[i-1] and ema9_3[i] > ma200_3[i]
                encostar = abs(ema9_3[i] - ma200_3[i]) / (ma200_3[i] + 1e-12) <= 0.001
                # Ajuste dinâmico
                rsi_min = 30 if volatility_3m > 2.0 else (40 if volatility_3m < 0.5 else 35)
                rsi_max = 50 if volatility_3m > 2.0 else (60 if volatility_3m < 0.5 else 55)
                vol_multiplier = 1.8 if volatility_3m > 2.0 else (1.2 if volatility_3m < 0.5 else 1.5)
                stop_loss_factor = 0.96 if volatility_3m > 2.0 else (0.98 if volatility_3m < 0.5 else 0.97)
                take_profit_factor = 1.15 if volatility_3m > 2.0 else (1.05 if volatility_3m < 0.5 else 1.10)
                if (cruza or encostar) and rsi_min <= rsi3[-1] <= rsi_max and v3[-1] >= vol_multiplier * (vol_ma20_3 + 1e-12) and within_ma200 and allowed(symbol, "SIG_3M"):
                    stop_loss = c3[i] * stop_loss_factor
                    take_profit = c3[i] * take_profit_factor
                    msg = (f"🟢 {symbol} ⬆️ Sinal Inicial (3m)\n"
                           f"💰 Preço: {fmt_price(c3[i])}\n"
                           f"🛑 Stop Loss: {fmt_price(stop_loss)} ({(1-stop_loss_factor)*100:.0f}%)\n"
                           f"🎯 Take Profit: {fmt_price(take_profit)} (+{(take_profit_factor-1)*100:.0f}%)\n"
                           f"🕒 {now_br()} (UTC-3) | Volatilidade: {volatility_3m:.2f}%\n"
                           f"──────────────────────────────")
                    await tg(session, msg)
                    mark(symbol, "SIG_3M")

        # -------- 5m (Confirmação Intermediária) --------
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) < 210: return
        o5 = [float(k[1]) for k in k5]
        h5 = [float(k[2]) for k in k5]
        l5 = [float(k[3]) for k in k5]
        c5 = [float(k[4]) for k in k5]
        v5 = [float(k[5]) for k in k5]

        ema9_5  = ema(c5, 9)
        ma50_5  = sma(c5, 50)
        ma200_5 = sma(c5, 200)
        upper5, mid5, lower5 = bollinger_bands(c5, 20, 2)
        rsi5 = calc_rsi(c5, 14)
        vma20_5 = sum(v5[-20:]) / 20.0
        volatility_5m = calculate_volatility(c5[-20:])
        i5 = len(c5) - 1

        # Calcule MACD
        if len(c5) >= 26:
            exp1 = ema(c5, 12)
            exp2 = ema(c5, 26)
            dif = [exp1[i] - exp2[i] for i in range(len(exp1))]
            dea = ema(dif, 9)
            macd_line = dif[-1] - dea[-1] if len(dif) > 0 and len(dea) > 0 else 0
        else:
            macd_line = 0

        cross_up_9_50_5 = (ema9_5[i5-1] <= ma50_5[i5-1]) and (ema9_5[i5] > ma50_5[i5])
        bb_open_5 = widening_now(upper5, mid5, lower5)
        if len(ema9_5) > 2:
            ma200_tolerance = ma200_5[-1] * 0.05
            within_ma200 = abs(c5[-1] - ma200_5[-1]) <= ma200_tolerance
            # Ajuste dinâmico
            rsi_min = 30 if volatility_5m > 2.0 else (40 if volatility_5m < 0.5 else 35)
            rsi_max = 50 if volatility_5m > 2.0 else (60 if volatility_5m < 0.5 else 55)
            vol_multiplier = 1.8 if volatility_5m > 2.0 else (1.2 if volatility_5m < 0.5 else 1.5)
            stop_loss_factor = 0.96 if volatility_5m > 2.0 else (0.98 if volatility_5m < 0.5 else 0.97)
            take_profit_factor = 1.15 if volatility_5m > 2.0 else (1.05 if volatility_5m < 0.5 else 1.10)
            if cross_up_9_50_5 and rsi_min <= rsi5[-1] <= rsi_max and v5[-1] >= vol_multiplier * (vma20_5 + 1e-12) and bb_open_5 and within_ma200 and allowed(symbol, "CONF_5M"):
                stop_loss = c5[i5] * stop_loss_factor
                take_profit = c5[i5] * take_profit_factor
                msg = (f"🔵 {symbol} ⬆️ Confirmação Intermediária (5m)\n"
                       f"💰 Preço: {fmt_price(c5[i5])}\n"
                       f"🛑 Stop Loss: {fmt_price(stop_loss)} ({(1-stop_loss_factor)*100:.0f}%)\n"
                       f"🎯 Take Profit: {fmt_price(take_profit)} (+{(take_profit_factor-1)*100:.0f}%)\n"
                       f"🕒 {now_br()} (UTC-3) | Volatilidade: {volatility_5m:.2f}%\n"
                       f"──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "CONF_5M")
            # Novo alerta para pumps com filtros adicionais
            if rsi5[-1] > 65 and v5[-1] >= 2.0 * (vma20_5 + 1e-12) and macd_line > 0 and c5[-1] > ema9_5[-1] and allowed(symbol, "PUMP_5M"):
                stop_loss = c5[i5] * 0.95  # 5% stop loss fixo
                take_profit = c5[i5] * 1.20  # 20% take profit fixo
                msg = (f"🚀 {symbol} ⬆️ Pump Detectado (5m)\n"
                       f"💰 Preço: {fmt_price(c5[i5])}\n"
                       f"🛑 Stop Loss: {fmt_price(stop_loss)} (-5%)\n"
                       f"🎯 Take Profit: {fmt_price(take_profit)} (+20%)\n"
                       f"🕒 {now_br()} (UTC-3) | Volatilidade: {volatility_5m:.2f}%\n"
                       f"──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "PUMP_5M")

        # -------- 15m (Confirmação Final) --------
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) < 210: return
        o15 = [float(k[1]) for k in k15]
        h15 = [float(k[2]) for k in k15]
        l15 = [float(k[3]) for k in k15]
        c15 = [float(k[4]) for k in k15]
        v15 = [float(k[5]) for k in k15]
        sar = [float(k[8]) for k in k15]  # SAR Parabólico

        ema9_15  = ema(c15, 9)
        ma50_15  = sma(c15, 50)
        ma200_15 = sma(c15, 200)
        upper15, mid15, lower15 = bollinger_bands(c15, 20, 2)
        rsi15 = calc_rsi(c15, 14)
        vma20_15 = sum(v15[-20:]) / 20.0
        volatility_15m = calculate_volatility(c15[-20:])
        j = len(c15) - 1
        if len(ema9_15) > 2:
            ma200_tolerance = ma200_15[-1] * 0.05
            within_ma200 = abs(c15[-1] - ma200_15[-1]) <= ma200_tolerance
            # Ajuste dinâmico
            rsi_min = 30 if volatility_15m > 2.0 else (40 if volatility_15m < 0.5 else 35)
            rsi_max = 50 if volatility_15m > 2.0 else (60 if volatility_15m < 0.5 else 55)
            vol_multiplier = 1.8 if volatility_15m > 2.0 else (1.2 if volatility_15m < 0.5 else 1.5)
            stop_loss_factor = 0.96 if volatility_15m > 2.0 else (0.98 if volatility_15m < 0.5 else 0.97)
            take_profit_factor = 1.15 if volatility_15m > 2.0 else (1.05 if volatility_15m < 0.5 else 1.10)
            if ema9_15[j] > ma50_15[j] and rsi_min <= rsi15[-1] <= rsi_max and sar[-1] < c15[-1] and v15[-1] >= vol_multiplier * (vma20_15 + 1e-12) and within_ma200 and allowed(symbol, "CONF_15M"):
                stop_loss = c15[j] * stop_loss_factor
                take_profit = c15[j] * take_profit_factor
                msg = (f"🚀 {symbol} ⬆️ Confirmação Final (15m)\n"
                       f"💰 Preço: {fmt_price(c15[j])}\n"
                       f"🛑 Stop Loss: {fmt_price(stop_loss)} ({(1-stop_loss_factor)*100:.0f}%)\n"
                       f"🎯 Take Profit: {fmt_price(take_profit)} (+{(take_profit_factor-1)*100:.0f}%)\n"
                       f"🕒 {now_br()} (UTC-3) | Volatilidade: {volatility_15m:.2f}%\n"
                       f"──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "CONF_15M")

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
