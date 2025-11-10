# main_short.py — v7.0D TENDÊNCIA CURTA (15m/30m/1h) — Render Safe
# - Timeframes: 15m, 30m, 1h (somente esses alertam)
# - Médias: EMA9/20/50/200
# - Indicadores: RSI(14), MACD(12,26,9), ATR(14), Volume Strength (vs MA9/MA21 do volume)
# - Filtros: volume 24h >= 10M USDT, remove UP/DOWN/stables/exóticos
# - Alertas dinâmicos com negrito + Entrada/Stop/Alvo (ATR) + Probabilidade (%)
# - Cooldowns: 15m=15min, 30m=30min, 1h=60min
# - Render-safe: Flask + /health, loop assíncrono (aiohttp)

import os, asyncio, aiohttp, time, math
from datetime import datetime, timedelta, timezone
from flask import Flask

# ----------------- Flask (Render-safe) -----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "v7.0D TENDÊNCIA CURTA (15m/30m/1h) ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

# ----------------- Config -----------------
BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

VOLUME_MIN_USDT = 10_000_000  # filtro de liquidez

# ----------------- Utils -----------------
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%d/%m %H:%M")

async def tg(session, text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("\n================ TELEGRAM (preview) ================\n" + text + "\n====================================================\n")
        return
    try:
        await session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=12
        )
    except Exception as e:
        print("Erro Telegram:", e)

async def get_json(session, url, timeout=12):
    try:
        async with session.get(url, timeout=timeout) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        print("HTTP erro:", e, url)
    return None

def remove_usdt(sym: str) -> str:
    return sym[:-4] if sym.endswith("USDT") else sym

# ----------------- Indicadores -----------------
def ema(series, period):
    if not series: return []
    a = 2.0 / (period + 1.0)
    e = series[0]
    out = [e]
    for x in series[1:]:
        e = a * x + (1 - a) * e
        out.append(e)
    return out

def sma(values, p):
    if len(values) < p: return None
    return sum(values[-p:]) / p

def rsi(prices, p=14):
    if len(prices) < p + 1: return 50.0
    dif = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [max(x, 0) for x in dif[-p:]]
    losses = [abs(min(x, 0)) for x in dif[-p:]]
    ag = sum(gains) / p
    al = sum(losses) / p if sum(losses) != 0 else 1e-12
    rs = ag / al
    return 100 - 100/(1+rs)

def macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal: 
        return 0.0, 0.0, 0.0
    ef = ema(prices, fast)
    es = ema(prices, slow)
    macd_line_full = [a-b for a,b in zip(ef[-len(es):], es)]
    signal_line_full = ema(macd_line_full, signal)
    macd_line = macd_line_full[-1]
    signal_line = signal_line_full[-1]
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def atr(highs, lows, closes, p=14):
    # True Range: max( high-low, abs(high-prev_close), abs(low-prev_close) )
    if len(closes) < p + 1: 
        return None
    trs = []
    for i in range(1, len(closes)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        trs.append(max(hl, hc, lc))
    if len(trs) < p: 
        return None
    # EMA do TR (ATR clássico) — simples média para robustez
    return sum(trs[-p:]) / p

def volume_strength(volumes):
    # volume atual vs médias MA9/MA21 do próprio volume
    if len(volumes) < 22: return 100.0
    v_now = volumes[-1]
    ma9 = sma(volumes, 9)
    ma21 = sma(volumes, 21)
    base = (ma9 + ma21) / 2 if (ma9 and ma21) else (ma9 or ma21 or 1e-9)
    return 100.0 * (v_now / base) if base > 0 else 100.0

# ----------------- Binance helpers -----------------
async def klines(session, symbol, interval, limit=240):
    url = f"{BINANCE}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    return await get_json(session, url) or []

async def ticker24(session, symbol):
    url = f"{BINANCE}/api/v3/ticker/24hr?symbol={symbol}"
    return await get_json(session, url)

async def all_tickers24(session):
    url = f"{BINANCE}/api/v3/ticker/24hr"
    return await get_json(session, url) or []

# ----------------- Cooldowns -----------------
cooldowns = {"15m": {}, "30m": {}, "1h": {}}
COOLDOWN_SECONDS = {"15m": 15*60, "30m": 30*60, "1h": 60*60}

def can_alert(tf, sym, key):
    bucket = cooldowns[tf].setdefault(sym, {})
    now = time.time()
    last = bucket.get(key, 0)
    if now - last >= COOLDOWN_SECONDS[tf]:
        bucket[key] = now
        return True
    return False

# ----------------- Análise por timeframe -----------------
def analyze_tf(opens, highs, lows, closes, volumes):
    price = closes[-1]
    e9  = ema(closes, 9)[-1]
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]  if len(closes) >= 50  else None
    e200= ema(closes, 200)[-1] if len(closes) >= 200 else None
    r = rsi(closes, 14)
    m_line, m_sig, m_hist = macd(closes, 12, 26, 9)
    vs = volume_strength(volumes)
    a = atr(highs, lows, closes, 14)
    prev_close = closes[-2] if len(closes) >= 2 else price
    body_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0
    return {
        "price": price, "ema9": e9, "ema20": e20, "ema50": e50, "ema200": e200,
        "rsi": r, "macd": m_line, "macd_sig": m_sig, "macd_hist": m_hist,
        "atr": a, "vol_strength": vs, "body_pct": body_pct
    }

# ----------------- Probabilidade -----------------
def prob_score(tf_data, context_above_200=False):
    # Base: 50%. Ajustes por confluência.
    score = 50.0
    rsi = tf_data["rsi"]
    macd_hist = tf_data["macd_hist"]
    vs = tf_data["vol_strength"]
    price = tf_data["price"]
    e9 = tf_data["ema9"]; e20 = tf_data["ema20"]

    # RSI faixa boa (55–65): +8 | >65:+5 | <45:-8
    if rsi >= 55 and rsi <= 65: score += 8
    elif rsi > 65: score += 5
    elif rsi < 45: score -= 8

    # MACD hist > 0: +10 | leve >0: +6 | <0: -10
    if macd_hist > 0.002: score += 10
    elif macd_hist > 0: score += 6
    else: score -= 10

    # Volume strength
    if vs >= 140: score += 12
    elif vs >= 110: score += 8
    elif vs < 90: score -= 6

    # Estrutura: preço acima de EMA20 e EMA9
    if price > e20 and price > e9: score += 6

    # Contexto 1h acima da 200 reforça
    if context_above_200: score += 6

    # Limites
    score = max(5.0, min(95.0, score))
    return round(score)

# ----------------- Mensagens -----------------
def fmt_alert(tf_tag, sym, t, msg_tipo, entry_price, stop, target, prob):
    name = remove_usdt(sym)
    return (
        f"{msg_tipo} <b>TENDÊNCIA CURTA ({tf_tag.upper()})</b>\n"
        f"<b>{name}</b>\n\n"
        f"<b>Preço:</b> <code>{entry_price:.6f}</code>\n"
        f"<b>RSI:</b> <code>{t['rsi']:.1f}</code> | <b>MACD:</b> <code>{t['macd']:.3f}</code>\n"
        f"<b>VS:</b> <code>{t['vol_strength']:.0f}%</code>\n"
        f"<b>🎯 Entrada:</b> <code>{entry_price:.6f}</code>\n"
        f"<b>🟥 Stop:</b> <code>{stop:.6f}</code>\n"
        f"<b>🟩 Alvo:</b> <code>{target:.6f}</code>\n"
        f"<b>📈 Probabilidade:</b> <code>{prob}%</code>\n"
        f"<i>{now_br()} BR</i>"
    )

# ----------------- Regras de alerta -----------------
def decide_alerts(sym, tf1h, tf30, tf15):
    out = []

    # Contexto: 1h acima da 200 = estrutura forte
    context_1h_above200 = bool(tf1h["ema200"] and tf1h["price"] > tf1h["ema200"])

    # 1) 1h — Estrutura acima da EMA200 (força principal)
    if tf1h["ema200"] and tf1h["price"] > tf1h["ema200"] and tf1h["rsi"] > 50 and tf1h["macd"] > 0 and tf1h["vol_strength"] > 110 and tf1h["atr"]:
        atrv = tf1h["atr"]
        entry = tf1h["price"]
        stop = entry - 1.5*atrv
        target = entry + 2.5*atrv
        prob = prob_score(tf1h, context_above_200=True)
        msg = fmt_alert("1h", sym, tf1h, "🏗️", entry, stop, target, prob)
        out.append(("1h", "ESTRUTURA", msg))

    # 2) 30m — Continuação / Reteste
    if tf30["atr"]:
        entry = tf30["price"]
        atrv = tf30["atr"]
        stop = entry - 1.5*atrv
        target = entry + 2.5*atrv

        # Reteste curto (EMA20 ou EMA50 por proximidade) com reação positiva
        near20 = abs(tf30["price"] - tf30["ema20"]) / tf30["price"] < 0.006
        near50 = (tf30["ema50"] is not None) and abs(tf30["price"] - tf30["ema50"]) / tf30["price"] < 0.006
        if context_1h_above200 and (near20 or near50) and tf30["rsi"] > 50 and tf30["macd"] >= 0 and tf30["vol_strength"] >= 100 and tf30["body_pct"] >= 0:
            prob = prob_score(tf30, context_above_200=context_1h_above200)
            extra_emoji = "🔁"
            msg = fmt_alert("30m", sym, tf30, extra_emoji, entry, stop, target, prob)
            out.append(("30m", "RETESTE", msg))
        # Continuação em força
        elif tf30["ema9"] > tf30["ema20"] and tf30["rsi"] > 55 and tf30["macd"] > 0 and tf30["vol_strength"] > 110:
            prob = prob_score(tf30, context_above_200=context_1h_above200)
            msg = fmt_alert("30m", sym, tf30, "💪", entry, stop, target, prob)
            out.append(("30m", "CONTINUACAO", msg))

    # 3) 15m — Gatilho (fechamento acima da EMA9 com força)
    if tf15["atr"] and tf15["ema9"] and tf15["price"] > tf15["ema9"] and tf15["rsi"] >= 52 and tf15["macd"] >= 0 and tf15["vol_strength"] >= 105:
        entry = tf15["price"]
        atrv = tf15["atr"]
        stop = entry - 1.5*atrv
        target = entry + 2.5*atrv
        prob = prob_score(tf15, context_above_200=context_1h_above200)
        msg = fmt_alert("15m", sym, tf15, "🚀", entry, stop, target, prob)
        out.append(("15m", "GATILHO", msg))

    return out

# ----------------- Scan por símbolo -----------------
async def scan_symbol(session, symbol):
    t24 = await ticker24(session, symbol)
    if not t24: return

    try:
        vol_quote_24h = float(t24.get("quoteVolume", "0") or 0.0)
    except:
        vol_quote_24h = 0.0
    if vol_quote_24h < VOLUME_MIN_USDT:
        return  # moeda fraca

    k15 = await klines(session, symbol, "15m", 240)
    k30 = await klines(session, symbol, "30m", 240)
    k1h = await klines(session, symbol, "1h", 300)
    if not (k15 and k30 and k1h): 
        return

    def extract(k):
        o = [float(x[1]) for x in k]
        h = [float(x[2]) for x in k]
        l = [float(x[3]) for x in k]
        c = [float(x[4]) for x in k]
        v = [float(x[7]) for x in k]  # volume base
        return o,h,l,c,v

    o15,h15,l15,c15,v15 = extract(k15)
    o30,h30,l30,c30,v30 = extract(k30)
    o1h,h1h,l1h,c1h,v1h = extract(k1h)

    tf15 = analyze_tf(o15,h15,l15,c15,v15)
    tf30 = analyze_tf(o30,h30,l30,c30,v30)
    tf1h = analyze_tf(o1h,h1h,l1h,c1h,v1h)

    alerts = decide_alerts(symbol, tf1h, tf30, tf15)
    for tf, key, msg in alerts:
        if can_alert(tf, symbol, key):
            await tg(session, msg)

# ----------------- Loop principal -----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        await tg(session, f"<b>v7.0D TENDÊNCIA CURTA ATIVO</b>\n15m/30m/1h • {now_br()} BR")
        while True:
            try:
                data = await all_tickers24(session)
                if not data:
                    await asyncio.sleep(10); continue
                # Filtra pares válidos
                syms = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and "UP" not in d["symbol"] and "DOWN" not in d["symbol"]
                    and not any(x in d["symbol"] for x in [
                        "BUSD","FDUSD","USDC","TUSD","AEUR","XAUT",  # stables e sintéticos
                        "EUR","GBP","TRY","AUD","BRL","RUB","CAD","CHF","JPY"  # fiat
                    ])
                ]
                # Top por volume
                top = sorted(
                    syms,
                    key=lambda s: float(next((x["quoteVolume"] for x in data if x["symbol"]==s), "0") or 0.0),
                    reverse=True
                )[:80]

                await asyncio.gather(*(scan_symbol(session, s) for s in top))
            except Exception as e:
                print("Loop erro:", e)
            await asyncio.sleep(60)

# ----------------- Start -----------------
if __name__ == "__main__":
    import threading
    threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
