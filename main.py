# main_hibrido_vflex.py
# ✅ Híbrido (3m + 5m + 15m) com alertas limpos
# ✅ Flex: faixas de RSI e volume configuráveis (não engessado)
# ✅ Novos alertas:
#    🟣 PRIMEIRO MOVIMENTO (3m) — preço fecha acima da MA200 com força (antes do cruzamento da EMA9)
#    🟡 ROMPIMENTO (3m) — EMA9 cruza MA200 de baixo para cima
#    🟠 CONFIRMAÇÃO (5m) — EMA9 cruza MA200
#    🟢 TENDÊNCIA (15m) — alinhamento completo, dispara só quando forma
# ✅ Filtro moedas mortas (blocklist + volume 24h mínimo)
# ✅ Estrutura original preservada + melhorias Aurora (validação real e continuidade)

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

# ---------------- AJUSTES DINÂMICOS ----------------
RSI_RANGE_REVERSAO = (45, 65)
RSI_RANGE_CONF     = (55, 70)
VOL_MULTIPLIER     = 1.2
MIN_VOL_24H        = 15_000_000
NAME_BLOCKLIST = ("PEPE", "FLOKI", "BONK", "SHIB", "DOGE")

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m, 5m + 15m) — flex RSI/Volume | 🇧🇷", 200

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
    blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","USDP","USD1","USDE","XUSD","USDX","GUSD","BFUSD","EUR","EURS","CEUR","BRL","TRY","PERP","_PERP","STABLE","TEST")
    pares = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"): continue
        if any(x in s for x in blocked): continue
        if any(x in s for x in NAME_BLOCKLIST): continue
        try: qv = float(d.get("quoteVolume", "0") or 0.0)
        except: qv = 0.0
        if qv < float(MIN_VOL_24H): continue
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
        # -------- 3m --------
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if len(k3) >= 210:
            c3 = [float(k[4]) for k in k3]
            v3 = [float(k[5]) for k in k3]
            ema9_3  = ema(c3, 9)
            ma200_3 = sma(c3, 200)
            rsi3 = calc_rsi(c3, 14)
            vma20_3 = sum(v3[-20:]) / 20.0
            i3 = len(c3)-1

            # ⚙️ FILTRO DE CONTEXTO (evita lateralizações fracas)
            if len(c3) > 30:
                recent_20 = c3[-20:]
                mean_price = sum(recent_20)/20
                dev = statistics.pstdev(recent_20)
                lateral = (dev / mean_price) < 0.003
                below_ma200 = all(p < ma200_3[-1] for p in recent_20)
                rising_rsi = (rsi3[-1] > rsi3[-5])
                volume_ok = (v3[-1] > VOL_MULTIPLIER * vma20_3)
                if below_ma200 and lateral and not (rising_rsi and volume_ok):
                    return

            rsi_ok_3_rev  = (RSI_RANGE_REVERSAO[0] <= rsi3[-1] <= RSI_RANGE_REVERSAO[1])
            rsi_ok_3_conf = (RSI_RANGE_CONF[0]     <= rsi3[-1] <= RSI_RANGE_CONF[1])
            vol_ok_3      = (v3[-1] >= VOL_MULTIPLIER * (vma20_3 + 1e-12))

            first_move_3m = (ema9_3[i3] < ma200_3[i3]) and (c3[i3] > ma200_3[i3]) and rsi_ok_3_rev and vol_ok_3
            if first_move_3m and allowed(symbol, "FIRST_3M"):
                msg = (f"🟣 {symbol} — PRIMEIRO MOVIMENTO (3m)\n"
                       f"• Preço FECHOU acima da MA200 com força\n"
                       f"• RSI:{rsi3[-1]:.1f} • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n"
                       f"💰 {fmt_price(c3[i3])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "FIRST_3M")

            cross_9_200_3m = (ema9_3[i3-1] <= ma200_3[i3-1]) and (ema9_3[i3] > ma200_3[i3]) and (rsi_ok_3_rev or rsi_ok_3_conf) and vol_ok_3
            if cross_9_200_3m and allowed(symbol, "ROMP_3M"):
                msg = (f"🟡 {symbol} — ROMPIMENTO EMA9×MA200 (3m)\n"
                       f"• EMA9 cruzou MA200 de baixo para cima\n"
                       f"• RSI:{rsi3[-1]:.1f} • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n"
                       f"💰 {fmt_price(c3[i3])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "ROMP_3M")

                # 🔍 VALIDAÇÃO DE CONTINUIDADE (3–5 velas)
                if len(c3) > 205:
                    next_prices = [float(k[4]) for k in k3[-5:]]
                    next_vols   = [float(k[5]) for k in k3[-5:]]
                    next_rsi    = rsi3[-5:]
                    price_gain = (next_prices[-1] - next_prices[0]) / next_prices[0]
                    vol_trend  = next_vols[-1] >= 0.8 * max(next_vols)
                    rsi_trend  = next_rsi[-1] > next_rsi[0]
                    if price_gain > 0.008 and vol_trend and rsi_trend and allowed(symbol, "CONT_3M"):
                        msg = (f"🟢 {symbol} — CONTINUIDADE DETECTADA (3m)\n"
                               f"• Preço subiu {price_gain*100:.1f}% nas últimas 5 velas\n"
                               f"• RSI subindo e volume mantido\n"
                               f"💰 {fmt_price(c3[i3])}\n🕒 {now_br()}\n──────────────────────────────")
                        await tg(session, msg)
                        mark(symbol, "CONT_3M")

        # -------- 5m --------
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if len(k5) >= 210:
            c5 = [float(k[4]) for k in k5]
            v5 = [float(k[5]) for k in k5]
            ema9_5  = ema(c5, 9)
            ma200_5 = sma(c5, 200)
            rsi5 = calc_rsi(c5, 14)
            vma20_5 = sum(v5[-20:]) / 20.0
            i5 = len(c5)-1

            rsi_ok_5_rev  = (RSI_RANGE_REVERSAO[0] <= rsi5[-1] <= RSI_RANGE_REVERSAO[1])
            rsi_ok_5_conf = (RSI_RANGE_CONF[0]     <= rsi5[-1] <= RSI_RANGE_CONF[1])
            vol_ok_5      = (v5[-1] >= VOL_MULTIPLIER * (vma20_5 + 1e-12))

            cross_9_200_5m = (ema9_5[i5-1] <= ma200_5[i5-1]) and (ema9_5[i5] > ma200_5[i5]) and (rsi_ok_5_rev or rsi_ok_5_conf) and vol_ok_5
            if cross_9_200_5m and allowed(symbol, "CONF_5M"):
                msg = (f"🟠 {symbol} — CONFIRMAÇÃO (5m)\n"
                       f"• EMA9 cruzou MA200 de baixo para cima\n"
                       f"• RSI:{rsi5[-1]:.1f} • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n"
                       f"💰 {fmt_price(c5[i5])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "CONF_5M")

                # 🔍 CONFIRMAÇÃO REAL (5m)
                if len(c5) > 205:
                    next_prices = [float(k[4]) for k in k5[-5:]]
                    next_vols   = [float(k[5]) for k in k5[-5:]]
                    next_rsi    = rsi5[-5:]
                    price_pullback = min(next_prices) > ma200_5[i5] * 0.995
                    price_gain = (next_prices[-1] - next_prices[0]) / next_prices[0]
                    vol_trend  = next_vols[-1] >= 0.8 * max(next_vols)
                    rsi_hold   = all(r > 55 for r in next_rsi[-3:])
                    if price_pullback and price_gain > 0.008 and vol_trend and rsi_hold and allowed(symbol, "REAL_5M"):
                        msg = (f"🟢 {symbol} — CONFIRMAÇÃO REAL (5m)\n"
                               f"• Preço manteve acima da MA200 (sem devolução)\n"
                               f"• RSI >55 e volume estável\n"
                               f"💰 {fmt_price(c5[i5])}\n🕒 {now_br()}\n──────────────────────────────")
                        await tg(session, msg)
                        mark(symbol, "REAL_5M")

        # -------- 15m --------
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if len(k15) >= 210:
            c15 = [float(k[4]) for k in k15]
            v15 = [float(k[5]) for k in k15]
            ema9_15  = ema(c15, 9)
            ema20_15 = ema(c15, 20)
            ma50_15  = sma(c15, 50)
            ma200_15 = sma(c15, 200)
            rsi15 = calc_rsi(c15, 14)
            vma20_15 = sum(v15[-20:]) / 20.0
            j = len(c15)-1

            rsi_ok_15 = (RSI_RANGE_CONF[0] <= rsi15[-1] <= RSI_RANGE_CONF[1])
            vol_ok_15 = (v15[-1] >= VOL_MULTIPLIER * (vma20_15 + 1e-12))

            aligned_prev = (ema9_15[j-1] > ema20_15[j-1] > ma50_15[j-1] > ma200_15[j-1])
            aligned_now  = (ema9_15[j]   > ema20_15[j]   > ma50_15[j]   > ma200_15[j])
            formed_now_15m = (not aligned_prev) and aligned_now and rsi_ok_15 and vol_ok_15

            if formed_now_15m and allowed(symbol, "TEND_15M"):
                msg = (f"🟢 {symbol} — TENDÊNCIA CONSOLIDADA (15m)\n"
                       f"• EMA9>EMA20>MA50>MA200 • RSI:{rsi15[-1]:.1f} • Vol ≥ {VOL_MULTIPLIER:.1f}×MA20\n"
                       f"💰 {fmt_price(c15[j])}\n🕒 {now_br()}\n──────────────────────────────")
                await tg(session, msg)
                mark(symbol, "TEND_15M")

                # 💚 CONFIRMAÇÃO REAL (15m)
                if len(c15) > 205:
                    next_prices = [float(k[4]) for k in k15[-5:]]
                    next_vols   = [float(k[5]) for k in k15[-5:]]
                    next_rsi    = rsi15[-5:]
                    price_above_ma50 = all(p > ma50_15[j] for p in next_prices)
                    steady_vol = (min(next_vols) > 0.7 * max(next_vols))
                    strong_rsi = all(r > 60 for r in next_rsi[-3:])
                    if price_above_ma50 and steady_vol and strong_rsi and allowed(symbol, "REAL_15M"):
                        msg = (f"💚 {symbol} — TENDÊNCIA CONFIRMADA (15m)\n"
                               f"• Mantém acima da MA50, RSI>60 e volume constante\n"
                               f"💰 {fmt_price(c15[j])}\n🕒 {now_br()}\n──────────────────────────────")
                        await tg(session, msg)
                        mark(symbol, "
