# main_hibrido_vflex.py
# ✅ Final - ALERTA INÍCIO TENDÊNCIA (foco 3m) + confirmações 5m/15m
# ✅ Estrutura preservada. Apenas: startup corrigida + logs de DEBUG para entender descartes.
# ✅ MIN_VOL_24H = 50_000_000 (ajustado conforme pedido)

import os, asyncio, aiohttp, time, math, statistics, traceback
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

# ---------------- PARÂMETROS ----------------
MIN_VOL_24H = 50_000_000
NAME_BLOCKLIST = (
    "PEPE","FLOKI","BONK","SHIB","DOGE",
    "HIFI","BAKE","WIF","MEME","1000","ORDI","ZK","ZRO","SAGA"
)
HYPE_SUBSTRINGS = ("AI","GPT","BOT")

# Sensibilidades (não engessadas; podem ser ajustadas)
BAND_200_BASE = 0.012   # ±1.2% tolerância para considerar "tocando/abaixo" (pequeno ajuste)
VOL_MULT_MIN  = 1.05
VOL_MULT_MAX  = 1.30
RSI_CENTER_WIN = 20
RSI_BAND = 5
RSI_MIN_FLOOR = 42
RSI_MAX_CEIL = 63

DEBUG = True  # ativa logs explicativos para diagnóstico no Render

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Scanner ativo (3m,5m,15m) — INÍCIO DE TENDÊNCIA (foco 3m) — Aurora", 200

# ---------------- UTIL ----------------
def now_br():
    return (datetime.utcnow() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def tg(session, text: str):
    if not (TELEGRAM_TOKEN and CHAT_ID):
        if DEBUG: print(f"{now_br()} - TELEGRAM MISSING TOKENS OR CHAT_ID")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        await session.post(url, data=payload, timeout=REQ_TIMEOUT)
        if DEBUG: print(f"{now_br()} - Telegram sent for text starting: {text[:30]}...")
    except Exception as e:
        if DEBUG: print(f"{now_br()} - ERROR sending telegram: {e}")

def fmt_price(x: float) -> str:
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s if s else "0"

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

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

def bollinger_bands(seq, n=20, mult=2.0):
    if len(seq) < n:
        return [], [], []
    out_mid, out_upper, out_lower = [], [], []
    for i in range(len(seq)):
        window = seq[max(0, i-n+1):i+1]
        m = sum(window)/len(window)
        s = statistics.pstdev(window)
        out_mid.append(m)
        out_upper.append(m + mult*s)
        out_lower.append(m - mult*s)
    return out_upper, out_mid, out_lower

def calc_sar(highs, lows, step=0.02, max_step=0.2):
    if len(highs) < 2 or len(lows) < 2:
        return [0.0]*len(highs)
    sar = [0.0]*len(highs)
    uptrend = True
    af = step
    ep = highs[0]
    sar[0] = lows[0]
    for i in range(1, len(highs)):
        prev_sar = sar[i-1]
        if uptrend:
            sar_candidate = prev_sar + af*(ep - prev_sar)
            sar[i] = min(sar_candidate, lows[i-1], lows[i])
            if highs[i] > ep:
                ep = highs[i]; af = min(af + step, max_step)
            if lows[i] < sar[i]:
                uptrend = False; sar[i] = ep; af = step; ep = lows[i]
        else:
            sar_candidate = prev_sar + af*(ep - prev_sar)
            sar[i] = max(sar_candidate, highs[i-1], highs[i])
            if lows[i] < ep:
                ep = lows[i]; af = min(af + step, max_step)
            if highs[i] > sar[i]:
                uptrend = True; sar[i] = ep; af = step; ep = highs[i]
    return sar

# ---------------- ALERT STATE ----------------
LAST_HIT = {}
def allowed(symbol, kind):
    ts = LAST_HIT.get((symbol, kind), 0.0)
    return (time.time() - ts) >= COOLDOWN_SEC
def mark(symbol, kind):
    LAST_HIT[(symbol, kind)] = time.time()

# ---------------- BINANCE ----------------
async def get_klines(session, symbol, interval, limit=210):
    url = f"{BINANCE_HTTP}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
            if isinstance(data, list):
                return data
            return []
    except Exception as e:
        if DEBUG: print(f"{now_br()} - get_klines error {symbol} {interval}: {e}")
        return []

async def get_top_usdt_symbols(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    try:
        async with session.get(url, timeout=REQ_TIMEOUT) as r:
            data = await r.json()
    except Exception as e:
        if DEBUG: print(f"{now_br()} - get_top_usdt_symbols error: {e}")
        return []
    blocked = ("UP","DOWN","BULL","BEAR","BUSD","FDUSD","TUSD","USDC","USDP","USD1","USDE","XUSD","USDX","GUSD","BFUSD",
               "EUR","EURS","CEUR","BRL","TRY","PERP","_PERP","STABLE","TEST")
    pares = []
    for d in data:
        s = d.get("symbol", "")
        if not s.endswith("USDT"): continue
        if any(x in s for x in blocked): continue
        if any(x in s for x in NAME_BLOCKLIST): continue
        if any(h in s for h in HYPE_SUBSTRINGS): continue
        try:
            qv = float(d.get("quoteVolume", "0") or 0.0)
        except:
            qv = 0.0
        if qv < float(MIN_VOL_24H): continue
        pares.append((s, qv))
    pares.sort(key=lambda x: x[1], reverse=True)
    if DEBUG: print(f"{now_br()} - top pairs count: {len(pares)}")
    return [s for s, _ in pares[:TOP_N]]

# ---------------- NÚCLEO: DETECÇÃO (foco 3m) ----------------
async def detectar_inicio(session, symbol, k, timeframe_tag):
    """
    Alerta INÍCIO DE TENDÊNCIA REAL:
    - foco em 3m: preço ABAIXO/TOCANDO MA200 + EMA9 cruza EMA20 pra cima (recente)
    - confirmações no 5m/15m (mesma lógica)
    """
    try:
        if len(k) < 60:  # suficiente para indicadores
            if DEBUG: print(f"{now_br()} - {symbol} {timeframe_tag} skip short klines {len(k)}")
            return
        closes = [float(x[4]) for x in k]
        highs  = [float(x[2]) for x in k]
        lows   = [float(x[3]) for x in k]
        vols   = [float(x[5]) for x in k]
        i = len(closes) - 1

        ema9v = ema(closes, 9)
        ema20v = ema(closes, 20)
        ma50v = sma(closes, 50)
        ma200v = sma(closes, 200)
        rsi = calc_rsi(closes, 14)
        bb_u, bb_m, bb_l = bollinger_bands(closes, n=20, mult=2.0)
        sar = calc_sar(highs, lows, step=0.02, max_step=0.2)

        close_now = closes[i]
        ma200_now = ma200v[i] if len(ma200v)>i else closes[i]
        ema9_now = ema9v[i] if len(ema9v)>i else closes[i]
        ema20_now = ema20v[i] if len(ema20v)>i else closes[i]
        ma50_now = ma50v[i] if len(ma50v)>i else closes[i]
        rsi_now = rsi[-1] if rsi else 50.0
        bbm_now = bb_m[i] if bb_m else close_now

        # volatilidade local p/ ajustar tolerância e multiplicador
        win = closes[-20:] if len(closes)>=20 else closes
        mean_p = sum(win)/len(win)
        dev_p = statistics.pstdev(win) if len(win) >= 2 else 0.0
        vol_norm = dev_p / max(mean_p, 1e-12)

        # banda MA200 dinâmica (permite tocar/ligeramente abaixo/na borda)
        band200 = clamp(BAND_200_BASE * (1.0 + 8.0*vol_norm), 0.007, 0.025)  # entre 0.7% e 2.5%
        # condição: abaixo ou tocando (aceita até ma200*(1+band200)) but we prefer below or touching
        near_or_below_200 = (close_now <= ma200_now * (1.0 + band200))

        # cruzamento recente EMA9>EMA20 (checar últimas 3 velas)
        crossed_recent = False
        if ema9_now > ema20_now:
            for off in (1,2,3):
                if i-off < 0: break
                if ema9v[i-off] <= ema20v[i-off]:
                    crossed_recent = True
                    break

        # MA50 ainda não acima da MA200 (início)
        early_trend = ma50_now < ma200_now

        # RSI dinâmico
        rsi_window = rsi[-RSI_CENTER_WIN:] if len(rsi) >= RSI_CENTER_WIN else rsi
        rsi_center = sum(rsi_window)/len(rsi_window) if rsi_window else 50.0
        rsi_low = clamp(rsi_center - RSI_BAND, RSI_MIN_FLOOR, RSI_MAX_CEIL-2)
        rsi_high = clamp(rsi_center + RSI_BAND, rsi_low+2, RSI_MAX_CEIL)
        rsi_ok = (rsi_now >= rsi_low) and (rsi_now <= rsi_high)

        # volume dinâmico
        avg_vol20 = sum(vols[-20:])/20.0 if len(vols) >= 20 else (sum(vols)/len(vols))
        vol_mult = clamp(VOL_MULT_MIN + 15.0*vol_norm, VOL_MULT_MIN, VOL_MULT_MAX)
        vol_ok = vols[-1] >= vol_mult * (avg_vol20 + 1e-12)

        # bollinger + sar
        bb_ok = close_now > bbm_now
        sar_ok = sar[i] < close_now if len(sar)>i else True

        # construindo razões de descarte para DEBUG
        reasons = []
        if not near_or_below_200:
            reasons.append(f"price not near/below 200 ({close_now:.6f} vs {ma200_now:.6f})")
        if not crossed_recent:
            reasons.append("no recent EMA9>EMA20 cross")
        if not early_trend:
            reasons.append("MA50 >= MA200 (not early)")
        if not rsi_ok:
            reasons.append(f"RSI {rsi_now:.1f} not in {rsi_low:.1f}-{rsi_high:.1f}")
        if not vol_ok:
            reasons.append(f"vol {vols[-1]/max(avg_vol20,1e-12):.2f}x < needed {vol_mult:.2f}x")
        if not bb_ok:
            reasons.append("not above bollinger middle")
        if not sar_ok:
            reasons.append("SAR not below price")

        # condição final
        if (near_or_below_200 and crossed_recent and early_trend and rsi_ok and vol_ok and bb_ok and sar_ok and allowed(symbol, f"TEND_{timeframe_tag}")):
            msg = (f"🚀 {symbol} — INÍCIO DE TENDÊNCIA REAL ({timeframe_tag})\n"
                   f"• RSI: {rsi_now:.1f} | Faixa din.: {rsi_low:.0f}–{rsi_high:.0f}\n"
                   f"• Vol: {vols[-1]/max(avg_vol20,1e-12):.2f}×MA20 (mult alvo {vol_mult:.2f}×)\n"
                   f"• Dist MA200: {(close_now/ma200_now-1.0)*100:+.2f}% | EMA9>EMA20 | SAR abaixo | Bollinger>média\n"
                   f"💰 {fmt_price(close_now)}\n🕒 {now_br()}\n──────────────────────────────")
            await tg(session, msg)
            mark(symbol, f"TEND_{timeframe_tag}")
            if DEBUG: print(f"{now_br()} - ALERT FIRED {symbol} {timeframe_tag}")
        else:
            if DEBUG:
                # print concise discard reason
                if len(reasons) == 0:
                    print(f"{now_br()} - {symbol} {timeframe_tag} checked: conditions not met (unknown reason)")
                else:
                    print(f"{now_br()} - {symbol} {timeframe_tag} discarded: " + "; ".join(reasons))
    except Exception as e:
        if DEBUG:
            print(f"{now_br()} - Exception in detectar_inicio {symbol} {timeframe_tag}: {e}")
            traceback.print_exc()
        return

# ---------------- WORKER ----------------
async def scan_symbol(session, symbol):
    try:
        # roda 3m primeiro (foco)
        k3 = await get_klines(session, symbol, "3m", limit=210)
        if k3 and len(k3) >= 60:
            await detectar_inicio(session, symbol, k3, "3m")
        # 5m
        k5 = await get_klines(session, symbol, "5m", limit=210)
        if k5 and len(k5) >= 60:
            await detectar_inicio(session, symbol, k5, "5m")
        # 15m
        k15 = await get_klines(session, symbol, "15m", limit=210)
        if k15 and len(k15) >= 60:
            await detectar_inicio(session, symbol, k15, "15m")
    except Exception as e:
        if DEBUG: print(f"{now_br()} - scan_symbol exception {symbol}: {e}")
        return

# ---------------- MAIN LOOP ----------------
async def main_loop():
    try:
        async with aiohttp.ClientSession() as session:
            symbols = await get_top_usdt_symbols(session)
            if not symbols:
                if DEBUG: print(f"{now_br()} - no symbols returned by get_top_usdt_symbols()")
                return
            await tg(session, f"✅ Scanner ativo | {len(symbols)} pares | cooldown {COOLDOWN_SEC//60}m | {now_br()} (UTC-3)\n──────────────────────────────")
            while True:
                tasks = [scan_symbol(session, s) for s in symbols]
                await asyncio.gather(*tasks)
                await asyncio.sleep(10)
    except Exception as e:
        if DEBUG: print(f"{now_br()} - main_loop exception: {e}")
        traceback.print_exc()
        # small sleep then retry
        await asyncio.sleep(5)
        return

# ---------------- RUN (corrigido) ----------------
def start_bot():
    # cria loop e executa main_loop sem bloquear Flask
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(main_loop())
    threading.Thread(target=loop.run_forever, daemon=True).start()
    # roda Flask (blocking) depois de ter o evento assíncrono ativo
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))

if __name__ == "__main__":
    start_bot()
