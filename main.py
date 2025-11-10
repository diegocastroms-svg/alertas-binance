# main_short.py — V7.1D (TENDÊNCIA CURTA — ALERTAS INTELIGENTES)
# - Timeframes: 15m, 30m, 1h (somente esses alertam)
# - Filtros: volume 24h >= 10M USDT; remove UP/DOWN/stables/fiat/sintéticos
# - Indicadores: EMA9/20/50/200, RSI(14), MACD(12,26,9), ATR(14), Volume Strength (vs MA9/MA21 do volume)
# - Alertas: ROMPIMENTO / RETESTE(20/50) / CONTINUAÇÃO / ANTECIPADA
# - Mensagem: Entrada/Stop/Alvo (ATR), Probabilidade (%), moeda sem "USDT"
# - Render Safe: Flask + /health, loop assíncrono

import os, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone
from flask import Flask

# ----------------- Flask (Render-safe) -----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "V7.1D TENDÊNCIA CURTA (15m/30m/1h) ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

# ----------------- Config -----------------
BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

VOLUME_MIN_USDT = 10_000_000  # filtro de liquidez
COOLDOWN_SECONDS = {"15m": 15*60, "30m": 30*60, "1h": 60*60}
cooldowns = {"15m": {}, "30m": {}, "1h": {}}

# ----------------- Utils -----------------
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%d/%m %H:%M BR")

def remove_usdt(sym: str) -> str:
    return sym[:-4] if sym.endswith("USDT") else sym

async def tg(session, text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("\n===== TELEGRAM (preview) =====\n" + text + "\n==============================\n")
        return
    try:
        await session.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
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

def can_alert(tf, sym, key):
    bucket = cooldowns[tf].setdefault(sym, {})
    now = time.time()
    last = bucket.get(key, 0)
    if now - last >= COOLDOWN_SECONDS[tf]:
        bucket[key] = now
        return True
    return False

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
    if len(closes) < p + 1: return None
    trs = []
    for i in range(1, len(closes)):
        hl = highs[i] - lows[i]
        hc = abs(highs[i] - closes[i-1])
        lc = abs(lows[i] - closes[i-1])
        trs.append(max(hl, hc, lc))
    if len(trs) < p: return None
    return sum(trs[-p:]) / p  # média simples (robusta)

def volume_strength(volumes):
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

# ----------------- Parsing de klines -----------------
def extract_ohlcv(k):
    o = [float(x[1]) for x in k]
    h = [float(x[2]) for x in k]
    l = [float(x[3]) for x in k]
    c = [float(x[4]) for x in k]
    v = [float(x[7]) for x in k]  # volume base
    return o,h,l,c,v

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
    score = 50.0
    rsi_v = tf_data["rsi"]
    hist = tf_data["macd_hist"]
    vs = tf_data["vol_strength"]
    price = tf_data["price"]
    e9 = tf_data["ema9"]; e20 = tf_data["ema20"]

    # RSI
    if 55 <= rsi_v <= 65: score += 8
    elif rsi_v > 65: score += 5
    elif rsi_v < 45: score -= 8

    # MACD hist
    if hist > 0.002: score += 10
    elif hist > 0: score += 6
    else: score -= 10

    # Volume
    if vs >= 140: score += 12
    elif vs >= 110: score += 8
    elif vs < 90: score -= 6

    # Estrutura
    if price > e20 and price > e9: score += 6
    if context_above_200: score += 6

    return int(max(5.0, min(95.0, score)))

# ----------------- Classificação do alerta (sem mudar gatilho) -----------------
def classify_flags(tf_tag, t, prev_t=None, allow_anticipada=True):
    """
    Gera flags para rótulo do alerta:
      - break_ema50 / break_ema200
      - retest_ema20 / retest_ema50
      - continuation
      - antecipada
    Usa comparação simples com médias e corpo/VS/MACD; não altera o momento do disparo.
    """
    flags = {}

    # Rompimento (quando preço cruza/regressa e agora está acima)
    if t["ema50"] and t["price"] > t["ema50"] and prev_t and prev_t["price"] <= prev_t["ema50"]:
        flags["break_ema50"] = True
    if t["ema200"] and t["price"] > t["ema200"] and prev_t and prev_t["price"] <= prev_t["ema200"]:
        flags["break_ema200"] = True

    # Reteste (preço próximo da 20 ou 50 e reagindo positivo)
    near20 = abs(t["price"] - t["ema20"]) / t["price"] < 0.006
    near50 = (t["ema50"] is not None) and abs(t["price"] - t["ema50"]) / t["price"] < 0.006
    if near20 and t["body_pct"] >= 0 and t["rsi"] >= 50:
        flags["retest_ema20"] = True
    if near50 and t["body_pct"] >= 0 and t["rsi"] >= 50:
        flags["retest_ema50"] = True

    # Continuação (acima da 9 e histograma positivo/subindo)
    if t["price"] > t["ema9"] and t["macd_hist"] >= 0:
        flags["continuation"] = True

    # Antecipada (momento antes do “setup clássico”)
    if allow_anticipada and (t["rsi"] >= 52 and t["macd_hist"] > 0 and t["vol_strength"] >= 120):
        flags["antecipada"] = True

    return flags

def kind_from_flags(flags):
    if flags.get("retest_ema200"): return "🔁 RETESTE EMA200"
    if flags.get("retest_ema50"):  return "🔁 RETESTE EMA50"
    if flags.get("break_ema200"):  return "⚡ ROMPIMENTO EMA200"
    if flags.get("break_ema50"):   return "⚡ ROMPIMENTO EMA50"
    if flags.get("antecipada"):    return "🚀 ENTRADA ANTECIPADA"
    if flags.get("continuation"):  return "💪 CONTINUAÇÃO"
    return "📊 OPORTUNIDADE"

def fmt_alert(tf_tag, sym, t, entry, stop, target, prob, flags):
    name = remove_usdt(sym)
    titulo = f"{kind_from_flags(flags)} <b>TENDÊNCIA CURTA ({tf_tag.upper()})</b>\n<b>{name}</b>"
    corpo = (
        f"\n\n<b>Preço:</b> <code>{t['price']:.6f}</code>"
        f"\n<b>RSI:</b> <code>{t['rsi']:.1f}</code> | <b>MACD:</b> <code>{t['macd']:.3f}</code> | <b>VS:</b> <code>{int(t['vol_strength'])}%</code>"
        f"\n<b>🎯 Entrada:</b> <code>{entry:.6f}</code>"
        f"\n<b>🟥 Stop:</b> <code>{stop:.6f}</code>"
        f"\n<b>🟩 Alvo:</b> <code>{target:.6f}</code>"
        f"\n<b>📈 Probabilidade:</b> <code>{prob}%</code>"
        f"\n<i>{now_br()}</i>"
    )
    return titulo + corpo

# ----------------- Regras de alerta (mantendo lógica de disparo base) -----------------
def decide_alerts(sym, tf1h, tf30, tf15, prev1h=None, prev30=None, prev15=None):
    out = []

    context_1h_above200 = bool(tf1h["ema200"] and tf1h["price"] > tf1h["ema200"])

    # 1) 1h — confirmação estrutural (sem mudar timing)
    if tf1h["atr"] and tf1h["ema200"] and tf1h["price"] > tf1h["ema200"] and tf1h["rsi"] > 50 and tf1h["macd"] > 0 and tf1h["vol_strength"] > 110:
        entry = tf1h["price"]; atrv = tf1h["atr"]
        stop  = entry - 1.5*atrv
        target= entry + 2.5*atrv
        prob  = prob_score(tf1h, context_above_200=True)
        flags = classify_flags("1h", tf1h, prev1h, allow_anticipada=False)  # 1h foca estrutura
        out.append(("1h", "ESTRUTURA", fmt_alert("1h", sym, tf1h, entry, stop, target, prob, flags)))

    # 2) 30m — reteste/continuação (sem mudar timing)
    if tf30["atr"]:
        entry = tf30["price"]; atrv = tf30["atr"]
        stop  = entry - 1.5*atrv
        target= entry + 2.5*atrv
        flags = classify_flags("30m", tf30, prev30, allow_anticipada=False)
        prob  = prob_score(tf30, context_above_200=context_1h_above200)
        out.append(("30m", "M30", fmt_alert("30m", sym, tf30, entry, stop, target, prob, flags)))

    # 3) 15m — gatilho curto (sem mudar timing)
    if tf15["atr"]:
        entry = tf15["price"]; atrv = tf15["atr"]
        stop  = entry - 1.5*atrv
        target= entry + 2.5*atrv
        flags = classify_flags("15m", tf15, prev15, allow_anticipada=True)
        prob  = prob_score(tf15, context_above_200=context_1h_above200)
        out.append(("15m", "M15", fmt_alert("15m", sym, tf15, entry, stop, target, prob, flags)))

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
        return

    k15 = await klines(session, symbol, "15m", 240)
    k30 = await klines(session, symbol, "30m", 240)
    k1h = await klines(session, symbol, "1h", 300)
    if not (k15 and k30 and k1h): 
        return

    o15,h15,l15,c15,v15 = extract_ohlcv(k15)
    o30,h30,l30,c30,v30 = extract_ohlcv(k30)
    o1h,h1h,l1h,c1h,v1h = extract_ohlcv(k1h)

    tf15 = analyze_tf(o15,h15,l15,c15,v15)
    tf30 = analyze_tf(o30,h30,l30,c30,v30)
    tf1h = analyze_tf(o1h,h1h,l1h,c1h,v1h)

    # estados anteriores (para detectar rompimento/reteste recente)
    if len(c15) > 1:
        prev15 = analyze_tf(o15[:-1], h15[:-1], l15[:-1], c15[:-1], v15[:-1])
    else:
        prev15 = None
    if len(c30) > 1:
        prev30 = analyze_tf(o30[:-1], h30[:-1], l30[:-1], c30[:-1], v30[:-1])
    else:
        prev30 = None
    if len(c1h) > 1:
        prev1h = analyze_tf(o1h[:-1], h1h[:-1], l1h[:-1], c1h[:-1], v1h[:-1])
    else:
        prev1h = None

    alerts = decide_alerts(symbol, tf1h, tf30, tf15, prev1h, prev30, prev15)
    for tf, key, msg in alerts:
        if can_alert(tf, symbol, key):
            await tg(session, msg)

# ----------------- Loop principal -----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        await tg(session, f"<b>BOT V7.1D ATIVO</b>\nMonitorando 15m, 30m e 1h • {now_br()}")
        while True:
            try:
                data = await all_tickers24(session)
                if not data:
                    await asyncio.sleep(10); continue
                syms = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and "UP" not in d["symbol"] and "DOWN" not in d["symbol"]
                    and not any(x in d["symbol"] for x in [
                        "BUSD","FDUSD","USDC","TUSD","AEUR","XAUT",  # stables/sintéticos
                        "EUR","GBP","TRY","AUD","BRL","RUB","CAD","CHF","JPY"  # fiat
                    ])
                ]
                # top por volume
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
