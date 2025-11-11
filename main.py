# main.py — V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA (com Reteste Antecipado + novos alertas)
# Mantida toda a lógica original — apenas novos alertas e logs adicionados

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading
from math import inf

app = Flask(__name__)
@app.route("/")
def home():
    return "V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA (Reteste Antecipado) ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ===== PARÂMETROS =====
MIN_VOL24 = 10_000_000
TOP_N = 50
SCAN_INTERVAL = 30  # segundos
COOLDOWN = {"15m": 15*60, "30m": 15*60, "1h": 15*60}

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

BOOK_DOMINANCE = 1.10  # takerBuy >= 1.1 * takerSell

# ===== FUNÇÕES =====
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")

async def tg(s, msg):
    try:
        await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})
        print("[OK] Alerta Telegram enviado.")
    except Exception as e:
        print("Erro Telegram:", e)

def ema_series(data, p):
    a = 2 / (p + 1)
    e = data[0]
    out = [e]
    for x in data[1:]:
        e = a * x + (1 - a) * e
        out.append(e)
    return out

def ema_last(data, p):
    return ema_series(data, p)[-1]

def rsi(prices, p=14):
    if len(prices) < p+1: return 50
    d = [prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains = [max(x,0) for x in d[-p:]]
    losses = [abs(min(x,0)) for x in d[-p:]]
    ag, al = (sum(gains)/p), (sum(losses)/p or 1e-12)
    return 100 - 100/(1 + ag/al)

def macd_12269(close):
    e12 = ema_series(close, 12)
    e26 = ema_series(close, 26)
    macd_line_series = [a-b for a,b in zip(e12, e26)]
    signal_series = ema_series(macd_line_series, 9)
    macd_line = macd_line_series[-1]
    signal = signal_series[-1]
    hist = macd_line - signal
    hist_prev = macd_line_series[-2] - signal_series[-2] if len(macd_line_series) >= 2 else hist
    hist_up = hist > hist_prev
    return macd_line, signal, hist, hist_up

def volume_strength(vol_series):
    ma9  = sum(vol_series[-9:])/9
    ma21 = sum(vol_series[-21:])/21
    base = (ma9 + ma21)/2 or 1e-12
    return (vol_series[-1]/base)*100, base, vol_series[-1]

def taker_split_24h(t24):
    vol_quote = float(t24.get("quoteVolume", 0))
    taker_buy_q = float(t24.get("takerBuyQuoteAssetVolume", 0))
    taker_sell_q = max(vol_quote - taker_buy_q, 0.0)
    return vol_quote, taker_buy_q, taker_sell_q

def touched(avg, low, close):
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
        state[key] = {"broke_base": False, "watch_retest": False, "last_taker_buy": 0.0, "last_break_ts": 0.0}

# ===== LÓGICA PRINCIPAL =====
async def klines(s, sym, tf, lim=220):
    async with s.get(f"{BINANCE}/api/v3/klines?symbol={sym}&interval={tf}&limit={lim}") as r:
        return await r.json() if r.status == 200 else []

async def ticker24(s, sym):
    async with s.get(f"{BINANCE}/api/v3/ticker/24hr?symbol={sym}") as r:
        return await r.json() if r.status == 200 else None

async def scan_tf(s, sym, tf):
    try:
        t24 = await ticker24(s, sym)
        if not t24: return
        vol24 = float(t24["quoteVolume"])
        if vol24 < MIN_VOL24: return
        k = await klines(s, sym, tf)
        if len(k) < 60: return

        close = [float(x[4]) for x in k]
        low   = [float(x[3]) for x in k]
        vol   = [float(x[5]) for x in k]

        ema9 = ema_last(close, 9)
        ema20 = ema_last(close, 20)
        ema100 = ema_last(close, 100)
        ema200 = ema_last(close, 200)

        r = rsi(close)
        macd_line, signal, hist, hist_up = macd_12269(close)
        vs, vs_base, vs_now = volume_strength(vol)
        vol_quote, taker_buy_q, taker_sell_q = taker_split_24h(t24)

        print(f"{now_br()} | {sym} | {tf} | RSI {r:.1f} | MACD {'+' if macd_line>0 else '-'} | Vol {vs:.1f}% | Book {taker_buy_q:.0f}/{taker_sell_q:.0f}")

        # ==== ENTRADA ANTECIPADA (15M)
        if tf == "15m" and hist_up and (r >= RSI_MIN_EARLY) and (vs >= VOL_STRENGTH_MIN_EARLY):
            if can_alert(tf, sym, "early"):
                await tg(s, f"⚡ <b>ENTRADA ANTECIPADA DETECTADA (15M)</b>\n{sym}\nRSI: {r:.1f} | Vol: {vs:.1f}%\n<i>{now_br()} BR</i>")

        # ==== ROMPIMENTO (30M/1H)
        if tf in ("30m","1h") and (close[-1] > ema200*(1+BREAK_TOL)) and (r >= RSI_MIN_BREAK):
            if can_alert(tf, sym, "break"):
                await tg(s, f"💥 <b>ROMPIMENTO CONFIRMADO ({tf.upper()})</b>\n{sym}\nPreço acima da EMA200\nRSI {r:.1f} | Vol {vs:.1f}%\n<i>{now_br()} BR</i>")

        # ==== RETESTE (15M)
        if tf == "15m" and (close[-1] > ema9) and touched(ema200, low[-1], close[-1]) and (r >= RSI_MIN_RETEST):
            if can_alert(tf, sym, "retest"):
                await tg(s, f"📘 <b>RETESTE ANTECIPADO (15M)</b>\n{sym}\nRSI {r:.1f} | Vol {vs:.1f}%\n<i>{now_br()} BR</i>")

        # ==== CONTINUAÇÃO (1H)
        if tf == "1h" and hist_up and (r >= RSI_MIN_CONT):
            if can_alert(tf, sym, "continue"):
                await tg(s, f"🔥 <b>CONTINUAÇÃO CONFIRMADA (1H)</b>\n{sym}\nRSI {r:.1f} | Vol {vs:.1f}%\n<i>{now_br()} BR</i>")

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "✅ V7.3 ATIVO — TENDÊNCIA CURTA (Entradas + Reteste + Continuação)")
        while True:
            try:
                data = await (await s.get(f"{BINANCE}/api/v3/ticker/24hr")).json()
                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT") and float(d["quoteVolume"]) > MIN_VOL24
                    and not any(x in d["symbol"] for x in ["UP","DOWN","BUSD","FDUSD","USDC","TUSD"])
                ][:TOP_N]
                tasks = []
                for sym in symbols:
                    for tf in ["1h","30m","15m"]:
                        tasks.append(scan_tf(s, sym, tf))
                await asyncio.gather(*tasks)
            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
