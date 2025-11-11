# main.py — V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA (SEM RETESTE)
# Estrutura do V7.3 preservada. Só trocados os ALERTAS.
# Timeframes: 15m (Entrada Antecipada), 30m/1h (Rompimento), 1h (Continuação)
# Filtros: RSI, MACD (12/26/9), VolumeStrength (MA9/MA21), Book (takerBuy vs takerSell, book=0 não bloqueia)
# Cooldown: 15 minutos todos TF | Vol24 min: 10M | Top N: 50 | Scan: 30s

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask
import threading
from math import inf

app = Flask(__name__)
@app.route("/")
def home():
    return "V7.3 OURO CONFLUÊNCIA REAL — TENDÊNCIA CURTA (SEM RETESTE) ATIVO", 200

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

# Limiar de força/confirmadores
VOL_STRENGTH_MIN_EARLY = 120   # 15m entrada antecipada
VOL_STRENGTH_MIN_BREAK = 90    # 30m/1h rompimento
VOL_STRENGTH_MIN_CONT  = 85    # 1h continuação

RSI_MIN_EARLY  = 50
RSI_MIN_BREAK  = 50
RSI_MIN_CONT   = 55

BOOK_DOMINANCE = 1.10  # takerBuy >= 1.1 * takerSell (book=0 não bloqueia)
BREAK_TOL = 0.0015     # 0,15% acima da base (EMA200/EMA100) p/ considerar rompimento
TOP_GUARD = 1.03       # evita topo: preço não pode estar > 3% acima da base

# ===== HELPERS =====
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%H:%M")

async def tg(s, msg):
    if not TELEGRAM_TOKEN:
        print("\n[TELEGRAM_SIM]\n" + msg + "\n"); return
    try:
        await s.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                     timeout=10)
    except Exception as e:
        print("Erro Telegram:", e)

def ema_series(data, p):
    if not data: return []
    a = 2/(p+1); e = data[0]; out = [e]
    for x in data[1:]:
        e = a*x + (1-a)*e; out.append(e)
    return out

def ema_last(data, p):
    s = ema_series(data, p)
    return s[-1] if s else 0.0

def rsi(prices, p=14):
    if len(prices) < p+1: return 50
    d = [prices[i]-prices[i-1] for i in range(1,len(prices))]
    gains = [max(x,0) for x in d[-p:]]
    losses = [abs(min(x,0)) for x in d[-p:]]
    ag, al = (sum(gains)/p), (sum(losses)/p or 1e-12)
    return 100 - 100/(1 + ag/al)

def macd_12269(close):
    if len(close) < 26: return 0.0, 0.0, 0.0, False
    e12 = ema_series(close, 12)
    e26 = ema_series(close, 26)
    n = min(len(e12), len(e26))
    macd_line_series = [e12[i] - e26[i] for i in range(-n,0)]
    signal_series = ema_series(macd_line_series, 9)
    macd_line = macd_line_series[-1]
    signal = signal_series[-1]
    hist = macd_line - signal
    hist_prev = macd_line_series[-2] - signal_series[-2] if len(signal_series) >= 2 else hist
    hist_up = hist > hist_prev
    return macd_line, signal, hist, hist_up

def volume_strength(vol_series):
    n = len(vol_series)
    if n < 21:
        return 100.0, (sum(vol_series[-n:])/max(n,1)), vol_series[-1] if vol_series else 0.0
    ma9  = sum(vol_series[-9:])/9
    ma21 = sum(vol_series[-21:])/21
    base = (ma9 + ma21)/2 or 1e-12
    return (vol_series[-1]/base)*100, base, vol_series[-1]

def taker_split_24h(t24):
    vol_quote = float(t24.get("quoteVolume", 0) or 0.0)
    taker_buy_q = float(t24.get("takerBuyQuoteAssetVolume", 0) or 0.0)
    taker_sell_q = max(vol_quote - taker_buy_q, 0.0)
    return vol_quote, taker_buy_q, taker_sell_q

def is_green(k): return float(k[4]) > float(k[1])
def broke_prev_high(curr, prev): return float(curr[4]) > float(prev[2])

# cooldown
cooldown = {"15m": {}, "30m": {}, "1h": {}}
def can_alert(tf, sym, kind):
    key = f"{sym}:{kind}"
    n = time.time()
    last = cooldown[tf].get(key, 0)
    if n - last >= COOLDOWN[tf]:
        cooldown[tf][key] = n
        return True
    return False

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

async def scan_tf(s, sym, tf, refs=None):
    try:
        t24 = await ticker24(s, sym)
        if not t24: return
        p = float(t24["lastPrice"])
        vol24 = float(t24["quoteVolume"])
        if vol24 < MIN_VOL24: 
            return

        k = await klines(s, sym, tf, 220)
        if len(k) < 60: 
            return

        close = [float(x[4]) for x in k]
        low   = [float(x[3]) for x in k]
        vol   = [float(x[5]) for x in k]

        # médias
        have200 = len(close) >= 200
        ema9   = ema_last(close, 9)    if len(close)>=9   else inf
        ema20  = ema_last(close, 20)   if len(close)>=20  else inf
        ema50  = ema_last(close, 50)   if len(close)>=50  else inf
        ema100 = ema_last(close, 100)  if len(close)>=100 else inf
        ema200 = ema_last(close, 200)  if have200 else None
        base_val = ema200 if have200 else ema100
        base_tag = "EMA200" if have200 else "EMA100(🧩fallback)"

        # indicadores
        r = rsi(close)
        macd_line, signal, hist, hist_up = macd_12269(close)
        macd_pos = (macd_line > 0 and hist >= 0)
        vs, vs_base, vs_now = volume_strength(vol)
        vol_quote, taker_buy_q, taker_sell_q = taker_split_24h(t24)
        book_ok = (taker_buy_q >= taker_sell_q * BOOK_DOMINANCE) or (taker_buy_q == 0.0)
        book_note = "bookOK" if taker_buy_q >= taker_sell_q * BOOK_DOMINANCE else ("book=0 ignorado" if taker_buy_q==0.0 else "book fraco")

        nome = sym.replace("USDT","")
        price = close[-1]

        # ===== 15m — ENTRADA ANTECIPADA (pegar o início, não topo) =====
        if tf == "15m":
            # 1) início: preço volta acima das rápidas e 9>20
            above_fast = (price > ema9) and (price > ema20) and (ema9 > ema20)
            # 2) impulso genuíno: RSI e MACD em virada/continuidade
            momentum_ok = (r >= RSI_MIN_EARLY) and hist_up
            # 3) força real: volume atual >120% da base
            force_ok = (vs >= VOL_STRENGTH_MIN_EARLY)
            # 4) proteção de topo: não estar esticado >3% da base maior (100/200)
            top_guard_ok = True
            if base_val not in (None, 0):
                top_guard_ok = price <= base_val * TOP_GUARD
            if above_fast and momentum_ok and force_ok and book_ok and top_guard_ok:
                if can_alert(tf, sym, "early"):
                    msg = (
                        f"<b>⚡ ENTRADA ANTECIPADA DETECTADA (15M)</b>\n\n"
                        f"{nome}\n\n"
                        f"Entrada sugerida: <b>{price:.6f}</b>\n"
                        f"RSI: <b>{r:.1f}</b> | MACD: <b>melhorando</b> (hist ↑)\n"
                        f"Vol força: <b>{vs:.0f}%</b> (atual {vs_now:,.0f} vs base {vs_base:,.0f})\n"
                        f"💰 Fluxo real: <b>{taker_buy_q:,.0f}</b> vs <b>{taker_sell_q:,.0f}</b> ({book_note})\n"
                        f"<i>{now_br()} BR</i>"
                    )
                    await tg(s, msg)

        # ===== 30m/1h — ROMPIMENTO da base (EMA200 ou fallback EMA100) =====
        if tf in ("30m","1h"):
            broke = False
            if base_val and base_val != 0:
                broke = (price > base_val*(1+BREAK_TOL)) and (r >= RSI_MIN_BREAK) and macd_pos and (vs >= VOL_STRENGTH_MIN_BREAK)
            if broke and book_ok:
                if can_alert(tf, sym, "break"):
                    msg = (
                        f"<b>⚡ TENDÊNCIA CURTA — ROMPIMENTO {base_tag} ({tf.upper()})</b>\n\n"
                        f"{nome}\n\n"
                        f"Entrada sugerida: <b>{price:.6f}</b>\n"
                        f"RSI: <b>{r:.1f}</b> | MACD: <b>positivo</b>\n"
                        f"Vol força: <b>{vs:.0f}%</b> (atual {vs_now:,.0f} vs base {vs_base:,.0f})\n"
                        f"💰 Fluxo real: <b>{taker_buy_q:,.0f}</b> vs <b>{taker_sell_q:,.0f}</b> ({book_note})\n"
                        f"<i>{now_br()} BR</i>"
                    )
                    await tg(s, msg)

        # ===== 1h — CONTINUAÇÃO (primeira vela 1h rompendo topo da anterior) =====
        if tf == "1h":
            prev = k[-2]; curr = k[-1]
            cont_ok = is_green(curr) and broke_prev_high(curr, prev) and (vs >= VOL_STRENGTH_MIN_CONT) and (r >= RSI_MIN_CONT) and macd_pos
            if cont_ok and book_ok:
                if can_alert(tf, sym, "continue"):
                    stop = min(float(x[3]) for x in k[-10:]) * 0.98
                    alvo1, alvo2 = price*1.025, price*1.05
                    msg = (
                        f"<b>🔥 CONTINUAÇÃO CONFIRMADA (1H)</b>\n\n"
                        f"{nome}\n\n"
                        f"Entrada: <b>{price:.6f}</b>\n"
                        f"📉 Stop: <b>{stop:.6f}</b>\n"
                        f"🎯 Alvos: <b>{alvo1:.6f}</b> (+2.5%) | <b>{alvo2:.6f}</b> (+5%)\n"
                        f"RSI: <b>{r:.1f}</b> | MACD: <b>positivo</b> | Vol força: <b>{vs:.0f}%</b>\n"
                        f"💰 Fluxo real: <b>{taker_buy_q:,.0f}</b> vs <b>{taker_sell_q:,.0f}</b>\n"
                        f"<i>{now_br()} BR</i>"
                    )
                    await tg(s, msg)

        # ===== LOGS DE DIAGNÓSTICO =====
        print(f"[{now_br()}] {sym} {tf} | p={price:.6f} rsi={r:.1f} vs={vs:.0f}% macd_hist_up={hist_up} bookTB={taker_buy_q:,.0f} TS={taker_sell_q:,.0f}")

    except Exception as e:
        print("Erro scan_tf:", e)

async def main_loop():
    async with aiohttp.ClientSession() as s:
        await tg(s, "<b>V7.3 ATIVO — TENDÊNCIA CURTA (SEM RETESTE)</b>\n15m/30m/1h | Vol24≥10M | Cooldown 15m | Scan 30s")
        while True:
            try:
                resp = await s.get(f"{BINANCE}/api/v3/ticker/24hr", timeout=10)
                if resp.status != 200:
                    await asyncio.sleep(SCAN_INTERVAL); continue
                data = await resp.json()

                symbols = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and float(d.get("quoteVolume") or 0) >= MIN_VOL24
                    and (lambda base: not (
                        base.endswith("USD") or base in {
                            "BUSD","FDUSD","USDE","USDC","TUSD","CUSD",
                            "EUR","GBP","TRY","AUD","BRL","RUB","CAD","CHF","JPY",
                            "BF","BFC","BFG","BFD","BETA","AEUR","AUSD","CEUR","XAUT"
                        }
                    ))(d["symbol"][:-4])
                    and not any(x in d["symbol"] for x in ["UP","DOWN"])
                ]

                symbols = sorted(
                    symbols,
                    key=lambda x: next((float(t["quoteVolume"]) for t in data if t["symbol"] == x), 0.0),
                    reverse=True
                )[:TOP_N]

                print(f"[{now_br()}] Scan {len(symbols)} pares (Top {TOP_N})")
                tasks = []
                for sym in symbols:
                    tasks += [scan_tf(s, sym, "1h"), scan_tf(s, sym, "30m"), scan_tf(s, sym, "15m")]
                await asyncio.gather(*tasks)

            except Exception as e:
                print("Erro main_loop:", e)
            await asyncio.sleep(SCAN_INTERVAL)

threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
