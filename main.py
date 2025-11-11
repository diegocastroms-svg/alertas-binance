# main.py — V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA (limpo e funcional)
# Entrada antecipada + Reteste antecipado + Continuação confirmada
# Foco: detectar início real da tendência (não topos)

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading
from math import inf

app = Flask(__name__)
@app.route("/")
def home():
    return "V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ===== PARÂMETROS =====
MIN_VOL24 = 10_000_000
TOP_N = 50
SCAN_INTERVAL = 30
COOLDOWN = {"15m": 900, "30m": 900, "1h": 900}
BREAK_TOL = 0.0015
RETEST_TOL = 0.005

VOL_STRENGTH_MIN_EARLY = 120
VOL_STRENGTH_MIN_BREAK = 90
VOL_STRENGTH_MIN_RETEST = 90
VOL_STRENGTH_MIN_CONT = 85

RSI_MIN_EARLY = 50
RSI_MIN_BREAK = 50
RSI_MIN_RETEST = 50
RSI_MIN_CONT = 55

BOOK_DOMINANCE = 1.10

# ===== FUNÇÕES =====
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print(msg)
        return
    try:
        await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print("Erro Telegram:", e)

def ema_series(data, p):
    if not data: return []
    a = 2/(p+1)
    e = data[0]
    out = [e]
    for x in data[1:]:
        e = a*x + (1-a)*e
        out.append(e)
    return out

def ema_last(data, p):
    s = ema_series(data, p)
    return s[-1] if s else 0.0

def rsi(prices, p=14):
    if len(prices) < p+1: return 50
    d = [prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains = [max(x,0) for x in d[-p:]]
    losses = [abs(min(x,0)) for x in d[-p:]]
    ag, al = sum(gains)/p, (sum(losses)/p or 1e-12)
    return 100 - 100/(1 + ag/al)

def macd_12269(close):
    if len(close) < 26: return 0.0, 0.0, 0.0, False
    e12 = ema_series(close, 12)
    e26 = ema_series(close, 26)
    n = min(len(e12), len(e26))
    e12, e26 = e12[-n:], e26[-n:]
    macd_line_series = [a-b for a,b in zip(e12, e26)]
    signal_series = ema_series(macd_line_series, 9)
    macd_line = macd_line_series[-1]
    signal = signal_series[-1]
    hist = macd_line - signal
    hist_prev = macd_line_series[-2] - signal_series[-2] if len(macd_line_series) >= 2 and len(signal_series) >= 2 else hist
    hist_up = hist > hist_prev
    return macd_line, signal, hist, hist_up

def volume_strength(vol_series):
    n = len(vol_series)
    if n < 21:
        return 100.0, (sum(vol_series[-n:])/max(n,1)), vol_series[-1] if vol_series else 0.0
    ma9 = sum(vol_series[-9:])/9
    ma21 = sum(vol_series[-21:])/21
    base = (ma9 + ma21)/2 or 1e-12
    return (vol_series[-1]/base)*100, base, vol_series[-1]

def taker_split_24h(t24):
    vol_quote = float(t24.get("quoteVolume", 0) or 0.0)
    taker_buy_q = float(t24.get("takerBuyQuoteAssetVolume", 0) or 0.0)
    taker_sell_q = max(vol_quote - taker_buy_q, 0.0)
    return vol_quote, taker_buy_q, taker_sell_q

def touched(avg, low, close):
    if avg in (None, 0, inf): return False
    return (low <= avg*(1+RETEST_TOL)) and (close >= avg*(1-RETEST_TOL))

def is_green(k): return float(k[4]) > float(k[1])
def broke_prev_high(curr, prev): return float(curr[4]) > float(prev[2])

cooldown = {"15m": {}, "30m": {}, "1h": {}}
def can_alert(tf, sym, kind):
    key = f"{sym}:{kind}"
    n = time.time()
    last = cooldown[tf].get(key, 0)
    if n - last >= COOLDOWN[tf]:
        cooldown[tf][key] = n
        return True
    return False

state = {}
def ensure_state(key):
    if key not in state:
        state[key] = {
            "broke_base": False,
            "watch_retest": False,
            "last_taker_buy": 0.0,
            "base_ma": "EMA200",
            "last_break_ts": 0.0
        }

# ===== CORE =====
async def klines(s, sym, tf, lim=220):
    url = f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit={lim}"
    async with s.get(url, timeout=10) as r:
        if r.status != 200:
            return []
        return await r.json()

async def ticker24(s, sym):
    url = f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}"
    async with s.get(url, timeout=10) as r:
        if r.status != 200:
            return None
        return await r.json()

async def scan_tf(s, sym, tf):
    try:
        t24 = await ticker24(s, sym)
        if not t24: return
        p = float(t24["lastPrice"])
        vol24 = float(t24["quoteVolume"])
        if vol24 < MIN_VOL24: return

        k = await klines(s, sym, tf, 220)
        if len(k) < 60: return

        close = [float(x[4]) for x in k]
        low = [float(x[3]) for x in k]
        vol = [float(x[5]) for x in k]

        have200 = len(close) >= 200
        ema9 = ema_last(close, 9)
        ma20 = ema_last(close, 20)
        ema50 = ema_last(close, 50)
        ema100 = ema_last(close, 100)
        ema200 = ema_last(close, 200) if have200 else None
        base_ma_val = ema200 if have200 else ema100
        base_ma_tag = "EMA200" if have200 else "EMA100"

        r = rsi(close)
        macd_line, signal, hist, hist_up = macd_12269(close)
        macd_pos = (macd_line > 0 and hist >= 0)
        vs, vs_base, vs_now = volume_strength(vol)
        vol_quote, taker_buy_q, taker_sell_q = taker_split_24h(t24)
        book_ok = (taker_buy_q >= taker_sell_q * BOOK_DOMINANCE) or (taker_buy_q == 0.0)

        key = (sym, tf)
        ensure_state(key)

        if tf == "15m":
            early_ok = (r >= RSI_MIN_EARLY) and hist_up and (vs >= VOL_STRENGTH_MIN_EARLY) and book_ok
            closes_above_fast = (close[-1] > ema9) and (close[-1] > ma20)
            if early_ok and closes_above_fast and can_alert(tf, sym, "early"):
                nome = sym.replace("USDT", "")
                msg = (f"⚡ ENTRADA ANTECIPADA DETECTADA (15M)\n\n{nome}\n\n"
                       f"Preço: {p:.6f}\nRSI: {r:.1f} | MACD: melhorando\n"
                       f"Vol força: {vs:.0f}%\nFluxo: {taker_buy_q:,.0f} vs {taker_sell_q:,.0f}\n"
                       f"{now_br()} BR")
                await tg(s, msg)

        if tf in ("30m", "1h"):
            broke_now = False
            if base_ma_val and base_ma_val != 0:
                above_base = close[-1] > base_ma_val*(1+BREAK_TOL)
                broke_now = above_base and (r >= RSI_MIN_BREAK) and macd_pos and (vs >= VOL_STRENGTH_MIN_BREAK)
            if broke_now and book_ok:
                state[(sym,"15m")]["broke_base"] = True
                state[(sym,"15m")]["watch_retest"] = True
                state[(sym,"15m")]["last_taker_buy"] = taker_buy_q
                state[(sym,"15m")]["base_ma"] = base_ma_tag
                state[(sym,"15m")]["last_break_ts"] = time.time()
                if can_alert(tf, sym, "break"):
                    nome = sym.replace("USDT", "")
                    msg = (f"💥 TENDÊNCIA CURTA — ROMPIMENTO {base_ma_tag} ({tf.upper()})\n\n{nome}\n\n"
                           f"Preço: {p:.6f}\nRSI: {r:.1f} | MACD: positivo\n"
                           f"Vol força: {vs:.0f}%\nFluxo: {taker_buy_q:,.0f} vs {taker_sell_q:,.0f}\n"
                           f"{now_br()} BR")
                    await tg(s, msg)

        if tf == "15m" and state[key]["watch_retest"]:
            if time.time() - state[key]["last_break_ts"] <= 10800:
                prev = k[-2]; curr = k[-1]
                prev_low, prev_close = float(prev[3]), float(prev[4])
                touch100 = touched(ema100, prev_low, prev_close)
                touch200 = touched(ema200, prev_low, prev_close) if ema200 else False
                touched_any = touch100 or touch200
                resume_ok = (r >= RSI_MIN_RETEST) and hist_up and (vs >= VOL_STRENGTH_MIN_RETEST)
                first_green_back = (close[-1] > ema9) and is_green(curr)
                if touched_any and resume_ok and first_green_back and can_alert(tf, sym, "retest_early"):
                    which = "EMA100" if touch100 else "EMA200"
                    nome = sym.replace("USDT", "")
                    msg = (f"📘 RETESTE ANTECIPADO DETECTADO (15M)\n\n{nome}\n\n"
                           f"Média testada: {which}\nRSI: {r:.1f} | MACD: melhorando\n"
                           f"Vol força: {vs:.0f}%\n{now_br()} BR")
                    await tg(s, msg)

        if tf == "1h" and state.get((sym,"15m"), {}).get("broke_base", False):
            prev = k[-2]; curr = k[-1]
            cont_ok = is_green(curr) and broke_prev_high(curr, prev) and (vs >= VOL_STRENGTH_MIN_CONT) and (r >= RSI_MIN_CONT) and macd_pos
            last_tb = state[(sym,"15m")]["last_taker_buy"]
            book_growth = (taker_buy_q >= max(last_tb * BOOK_DOMINANCE, last_tb+1e-9)) or (last_tb == 0.0)
            if cont_ok and book_growth and can_alert(tf, sym, "continue"):
                nome = sym.replace("USDT", "")
                stop = min(float(x[3]) for x in k[-10:]) * 0.98
                alvo1, alvo2 = p*1.025, p*1.05
                msg = (f"🔥 CONTINUAÇÃO CONFIRMADA (1H)\n\n{nome}\n\n"
                       f"Preço: {p:.6f}\nStop: {stop:.6f}\n"
                       f"Alvos: {alvo1:.6f} | {alvo2:.6f}\n"
                       f"RSI: {r:.1f} | MACD: positivo | Vol força: {vs:.0f}%\n"
                       f"Fluxo: {taker_buy_q:,.0f} vs {taker_sell_q:,.0f}\n"
                       f"{now_br()} BR")
                await tg(s, msg)
                state[(sym,"15m")]["broke_base"] = False
                state[(sym,"15m")]["watch_retest"] = False
                state[(sym,"15m")]["last_taker_buy"] = taker_buy_q

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "V7.3 ATIVO — TENDÊNCIA CURTA (Entrada Antecipada + Reteste Antecipado)")
        while True:
            try:
                resp = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if resp.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL)
                    continue
                data = await resp.json()
                symbols = [d["symbol"] for d in data if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) >= MIN_VOL24 and not any(x in d["symbol"] for x in ["UP", "DOWN", "BUSD", "FDUSD", "USDC", "TUSD"])]
                symbols = sorted(symbols, key=lambda x: next((float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0.0), reverse=True)[:TOP_N]
                for sym in symbols:
                    print("Scanning:", sym)
                tasks = []
                for sym in symbols:
                    tasks.append(scan_tf(s, sym, "1h"))
                    tasks.append(scan_tf(s, sym, "30m"))
                    tasks.append(scan_tf(s, sym, "15m"))
                await asyncio.gather(*tasks)
            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
