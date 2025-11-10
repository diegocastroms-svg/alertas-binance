# main.py — OURO CONFLUÊNCIA EMA (15m/30m/1h) — v1.1 Render Safe
# - Só 15m, 30m e 1h
# - Médias: EMA9/20/50/200 (tudo EMA)
# - Indicadores: RSI(14), MACD(12,26,9), Bollinger(EMA20, desvio 1.8)
# - Personalizados: volume_strength (vs MA9/MA21 de volume), real_money_flow (taker buy vs sell)
# - Filtros: volume 24h >= 10M USDT, remove UP/DOWN/stables/exóticos
# - 6 alertas: Romp200 (1h), Reteste (30m), Continuação (15m), Reteste no Tempo (30m), Perda de Força (15m), Entrada Antecipada (15m)
# - Mensagens: sem <code>, símbolo sem "USDT", com espaçamento

import os, asyncio, aiohttp, time, math
from datetime import datetime, timedelta, timezone
from flask import Flask

app = Flask(__name__)

BINANCE = "https://api.binance.com"
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# ----------------- Utils -----------------
def now_br():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%d/%m %H:%M")

async def tg(session, text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(text)
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

# ----------------- Indicators -----------------
def ema(series, period):
    n = len(series)
    if n == 0: return []
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
    ema_fast = ema(prices, fast)
    ema_slow = ema(prices, slow)
    macd_line = [a-b for a,b in zip(ema_fast[-len(ema_slow):], ema_slow)]
    signal_line = ema(macd_line, signal)
    hist = macd_line[-1] - signal_line[-1]
    return macd_line[-1], signal_line[-1], hist

def bollinger_ema(prices, period=20, dev=1.8):
    # base na EMA20 + desvio padrão das últimas 'period' velas
    if len(prices) < period: 
        return None, None, None, None
    ema20_series = ema(prices, period)
    basis = ema20_series[-1]
    last = prices[-period:]
    m = sum(last) / period
    var = sum((x - m) ** 2 for x in last) / period
    sd = var ** 0.5
    upper = basis + dev * sd
    lower = basis - dev * sd
    width = (upper - lower) / basis if basis else 0.0
    return lower, basis, upper, width

def volume_strength(volumes):
    # volume atual comparado à média MA9 e MA21 de volume
    if len(volumes) < 22: return 100.0
    v_now = volumes[-1]
    ma9 = sma(volumes, 9)
    ma21 = sma(volumes, 21)
    base = (ma9 + ma21) / 2 if (ma9 and ma21) else (ma9 or ma21 or 1e-9)
    return 100.0 * (v_now / base) if base > 0 else 100.0

def real_money_flow(taker_buy_q, taker_sell_q):
    total = (taker_buy_q or 0.0) + (taker_sell_q or 0.0)
    if total <= 0: return 0.0
    return 100.0 * ((taker_buy_q or 0.0) - (taker_sell_q or 0.0)) / total

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

def remove_usdt(sym):
    return sym[:-4] if sym.endswith("USDT") else sym

# ----------------- TF analyzer -----------------
def analyze_tf(opens, highs, lows, closes, volumes, tfname):
    price = closes[-1]
    e9 = ema(closes, 9)[-1]
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1] if len(closes) >= 50 else None
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
    r = rsi(closes, 14)
    m_line, m_sig, m_hist = macd(closes, 12, 26, 9)
    bl, bm, bu, bw = bollinger_ema(closes, 20, 1.8)
    vs = volume_strength(volumes)
    # tamanho do corpo da última vela (%)
    prev_close = closes[-2] if len(closes) >= 2 else price
    body_pct = ((price - prev_close) / prev_close) * 100 if prev_close else 0.0
    return {
        "price": price, "ema9": e9, "ema20": e20, "ema50": e50, "ema200": e200,
        "rsi": r, "macd": m_line, "macd_sig": m_sig, "macd_hist": m_hist,
        "boll_low": bl, "boll_mid": bm, "boll_up": bu, "boll_w": bw,
        "vol_strength": vs, "body_pct": body_pct,
        "high": highs[-1], "low": lows[-1]
    }

# ----------------- Decisão (regras dos 6 alertas) -----------------
def decide_alerts(sym, tf1h, tf30, tf15, taker_buy_q, taker_sell_q):
    out = []
    rmf = real_money_flow(taker_buy_q, taker_sell_q)

    # 1) Rompimento da EMA200 (1h)
    if tf1h["ema200"] and tf1h["price"] > tf1h["ema200"] and tf1h["rsi"] > 50 and tf1h["macd"] > 0 and tf1h["vol_strength"] > 120 and rmf > 0:
        out.append(("1h","ROMP200",
            f"⚡ ROMPIMENTO CONFIRMADO (1H)\n"
            f"{remove_usdt(sym)}\n\n"
            f"Preço: {tf1h['price']:.6f}\n"
            f"RSI: {tf1h['rsi']:.1f} | MACD: {tf1h['macd']:.3f}\n"
            f"VS: {tf1h['vol_strength']:.0f}% | RMF: {rmf:.0f}\n"
            f"{now_br()} BR"
        ))

    # 2) Reteste (30m) — encosta na EMA20 ou EMA50 e reage
    near50 = tf30["ema50"] and abs(tf30["price"] - tf30["ema50"]) / tf30["price"] < 0.006
    near20 = abs(tf30["price"] - tf30["ema20"]) / tf30["price"] < 0.006
    if (near20 or near50) and tf1h["ema200"] and tf1h["price"] > tf1h["ema200"] and tf30["rsi"] > 50 and tf30["macd"] >= 0 and tf30["body_pct"] > 0 and rmf >= 0:
        alvo = "EMA50" if near50 else "EMA20"
        out.append(("30m","RETESTE",
            f"🟡 RETESTE CONFIRMADO {alvo} (30M)\n"
            f"{remove_usdt(sym)}\n\n"
            f"Preço: {tf30['price']:.6f}\n"
            f"RSI: {tf30['rsi']:.1f} | MACD: {tf30['macd']:.3f}\n"
            f"VS: {tf30['vol_strength']:.0f}% | RMF: {rmf:.0f}\n"
            f"{now_br()} BR"
        ))

    # 3) Continuação (15m)
    if tf1h["ema200"] and tf1h["price"] > tf1h["ema200"] and tf30["rsi"] > 50 and tf30["macd"] > 0:
        if tf15["ema9"] > tf15["ema20"] and tf15["rsi"] > 55 and tf15["macd"] > 0 and tf15["vol_strength"] > 110 and rmf > 0:
            out.append(("15m","CONTINUACAO",
                f"🔵 CONTINUAÇÃO CONFIRMADA (15M)\n"
                f"{remove_usdt(sym)}\n\n"
                f"Preço: {tf15['price']:.6f}\n"
                f"RSI: {tf15['rsi']:.1f} | MACD: {tf15['macd']:.3f}\n"
                f"VS: {tf15['vol_strength']:.0f}% | RMF: {rmf:.0f}\n"
                f"Alvos: +2.5% e +5%\n"
                f"{now_br()} BR"
            ))

    # 4) Reteste no Tempo (30m): lateral com Bollinger apertando e força voltando no 15m
    if tf30["rsi"] >= 50 and tf30["rsi"] <= 60 and tf30["boll_w"] and tf30["boll_w"] < 0.02:
        if tf15["vol_strength"] > 110 and rmf > 0 and tf15["macd"] >= 0:
            out.append(("30m","RETESTE_TEMPO",
                f"🟣 RETESTE NO TEMPO (30M)\n"
                f"{remove_usdt(sym)}\n\n"
                f"Lateral com força mantida; Bollinger apertando.\n"
                f"RSI30: {tf30['rsi']:.1f} | VS15: {tf15['vol_strength']:.0f}% | RMF: {rmf:.0f}\n"
                f"{now_br()} BR"
            ))

    # 5) Perda de Força (15m)
    if tf15["rsi"] < 45 and tf15["macd"] < 0 and rmf < 0 and tf15["vol_strength"] < 90:
        out.append(("15m","PERDA_FORCA",
            f"🔴 TENDÊNCIA ENFRAQUECENDO (15M)\n"
            f"{remove_usdt(sym)}\n\n"
            f"RSI: {tf15['rsi']:.1f} | MACD: {tf15['macd']:.3f}\n"
            f"VS: {tf15['vol_strength']:.0f}% | RMF: {rmf:.0f}\n"
            f"Evitar novas entradas por enquanto.\n"
            f"{now_br()} BR"
        ))

    # 6) Entrada Antecipada Real (15m)
    if tf15["macd_hist"] > 0 and tf15["rsi"] > 50 and tf15["vol_strength"] > 120 and rmf > 0 and (tf15["ema9"] >= tf15["ema20"] or tf30["macd"] > 0):
        out.append(("15m","ANTECIPADA",
            f"⚡ ENTRADA ANTECIPADA REAL (15M)\n"
            f"{remove_usdt(sym)}\n\n"
            f"RSI: {tf15['rsi']:.1f} | MACD(H): {tf15['macd_hist']:.3f}\n"
            f"VS: {tf15['vol_strength']:.0f}% | RMF: {rmf:.0f}\n"
            f"Bollinger abrindo? {'SIM' if (tf15['boll_w'] and tf15['boll_w']>0.02) else 'NÃO'}\n"
            f"{now_br()} BR"
        ))
    return out

# ----------------- Scan de símbolo -----------------
async def scan_symbol(session, symbol):
    t24 = await ticker24(session, symbol)
    if not t24: return
    try:
        vol_quote_24h = float(t24.get("quoteVolume", "0") or 0)
    except:
        vol_quote_24h = 0.0
    if vol_quote_24h < 10_000_000:  # bloqueio moedas mortas
        return

    taker_buy_q = float(t24.get("takerBuyQuoteAssetVolume", "0") or 0.0)
    taker_sell_q = max(vol_quote_24h - taker_buy_q, 0.0)

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

    tf15 = analyze_tf(o15, h15, l15, c15, v15, "15m")
    tf30 = analyze_tf(o30, h30, l30, c30, v30, "30m")
    tf1h = analyze_tf(o1h, h1h, l1h, c1h, v1h, "1h")

    alerts = decide_alerts(symbol, tf1h, tf30, tf15, taker_buy_q, taker_sell_q)
    for tf, key, msg in alerts:
        if can_alert(tf, symbol, key):
            await tg(session, msg)

# ----------------- Loop principal -----------------
async def main_loop():
    async with aiohttp.ClientSession() as session:
        await tg(session, f"<b>OURO CONFLUÊNCIA EMA ATIVO</b>\n15m/30m/1h • {now_br()} BR")
        while True:
            try:
                data = await all_tickers24(session)
                if not data:
                    await asyncio.sleep(10); continue
                syms = [
                    d["symbol"] for d in data
                    if d["symbol"].endswith("USDT")
                    and "UP" not in d["symbol"] and "DOWN" not in d["symbol"]
                    and not any(x in d["symbol"] for x in ["BUSD","FDUSD","USDC","TUSD","AEUR","XAUT"])
                ]
                top = sorted(
                    syms,
                    key=lambda s: float(next((x["quoteVolume"] for x in data if x["symbol"]==s), "0") or 0.0),
                    reverse=True
                )[:80]

                tasks = [scan_symbol(session, s) for s in top]
                await asyncio.gather(*tasks)
            except Exception as e:
                print("Loop erro:", e)
            await asyncio.sleep(60)

# ----------------- Flask -----------------
@app.route("/")
def home():
    return "OURO CONFLUÊNCIA EMA v1.1 (15m/30m/1h) ATIVO", 200

@app.route("/health")
def health():
    return "OK", 200

# ----------------- Start -----------------
if __name__ == "__main__":
    import threading
    threading.Thread(target=lambda: asyncio.run(main_loop()), daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
