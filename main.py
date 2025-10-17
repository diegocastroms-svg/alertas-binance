# main.py — Alerts v3.intrabar (apenas cruzamentos de médias)
# - Intrabar: usa o último candle em formação
# - 5m Iniciando: EMA9 cruza MA20 e MA50
# - 5m Pré-confirmada: MA20 e MA50 acima da MA200 e cruzando neste candle
# - 15m Pré-confirmada: EMA9 cruza MA200
# - 15m Confirmada: MA20 e MA50 acima da MA200 e cruzando neste candle
# - Cooldown curto: 15 minutos
# - Filtro: apenas pares SPOT relevantes USDT (exclui perp/stables/fiat/índices)
# - Flask: porta aberta para Render

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta

import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"

INTERVAL_5M  = "5m"
INTERVAL_15M = "15m"

SHORTLIST_N        = 120         # até 120 pares SPOT
REFRESH_SHORTLIST  = 10 * 60     # 10 min
COOLDOWN_SEC       = 15 * 60     # 15 min

MIN_PCT = 0.5                    # variação mínima 24h p/ entrar no radar
MIN_QV  = 5_00_000.0             # quote volume mínimo 24h

# MAs
EMA_FAST = 9
MA_20    = 20
MA_50    = 50
MA_200   = 200

# Telegram / Webhook
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID        = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE   = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- Utils -----------------
def fmt_symbol(symbol):
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_links(symbol):
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

def ts_brazil_now():
    # Horário Brasília
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session, text):
    # Webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    # Telegram
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except:
            pass

# ----------------- MAs -----------------
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    if not seq: return []
    out = []
    alpha = 2.0 / (span + 1.0)
    e = seq[0]; out.append(e)
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

# ----------------- Binance -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    """Inclui o ÚLTIMO candle (intrabar)."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    o,h,l,c,v = [],[],[],[],[]
    for k in data:  # sem remover [-1]
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=400):
    """Filtro rígido de SPOT USDT (sem perp, sem fiat/stables, sem índices)."""
    usdt = []
    # blocos para evitar fiat/stables/índices/perp
    blocked_tokens = {
        "BUSD","FDUSD","TUSD","USDC","USDP","DAI","USD",
        "EUR","TRY","BRL","GBP","AUD","RUB","JPY","UAH","NGN","ZAR",
        "BULL","BEAR","DOWN","UP","PERP","_PERP","USD_","_USD","BTCST"
    }
    for t in tickers:
        s = t.get("symbol","")
        if not s.endswith("USDT"):
            continue
        # exclui bases fiat/stables/índices/perp/leveraged
        base = s.replace("USDT","")
        if any(x in s for x in blocked_tokens) or base in blocked_tokens:
            continue
        # número/forex pseudo (ex.: EUR/USDT), já filtrado acima
        pct = float(t.get("priceChangePercent","0") or 0.0)
        qv  = float(t.get("quoteVolume","0") or 0.0)
        if abs(pct) >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Cruzamentos -----------------
def cross_up(prev_a, prev_b, now_a, now_b):
    return prev_a <= prev_b and now_a > now_b

# ----------------- Mensagens -----------------
def build_msg(symbol, title, price, bullet):
    sym = fmt_symbol(symbol)
    header = title  # já vem com emojis
    return (
        f"{header}\n"
        f"💰 <code>{price:.6f}</code>\n"
        f"🧠 {bullet}\n"
        f"⏰ {ts_brazil_now()}\n"
        f"{binance_links(symbol)}"
    )

# ----------------- Monitor -----------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)

    def allowed(self, symbol, kind):
        return time.time() - self.cooldown[(symbol, kind)] >= COOLDOWN_SEC

    def mark(self, symbol, kind):
        self.cooldown[(symbol, kind)] = time.time()

# ----------------- Worker (curto: 5m/15m) -----------------
async def candle_worker(session, symbol, monitor: Monitor):
    try:
        # ===== 5m =====
        o5,h5,l5,c5,v5 = await get_klines(session, symbol, interval=INTERVAL_5M, limit=200)
        if len(c5) < 60: 
            return
        ema9_5  = ema(c5, EMA_FAST)
        ma20_5  = sma(c5, MA_20)
        ma50_5  = sma(c5, MA_50)
        ma200_5 = sma(c5, MA_200)
        i5 = len(c5)-1; p5 = i5-1

        # ===== 15m =====
        o15,h15,l15,c15,v15 = await get_klines(session, symbol, interval=INTERVAL_15M, limit=200)
        if len(c15) < 60:
            return
        ema9_15  = ema(c15, EMA_FAST)
        ma20_15  = sma(c15, MA_20)
        ma50_15  = sma(c15, MA_50)
        ma200_15 = sma(c15, MA_200)
        i15 = len(c15)-1; p15 = i15-1

        # ---------------- 5m — Tendência iniciando ----------------
        # EMA9 cruza MA20 e MA50 neste candle (intrabar)
        ini_5m = (
            cross_up(ema9_5[p5], ma20_5[p5], ema9_5[i5], ma20_5[i5]) and
            (ema9_5[i5] > ma50_5[i5] or cross_up(ema9_5[p5], ma50_5[p5], ema9_5[i5], ma50_5[i5]))
        )
        if ini_5m and monitor.allowed(symbol, "TENDENCIA_INICIANDO_5M"):
            msg = build_msg(
                symbol,
                f"🟢 {fmt_symbol(symbol)} ⬆️ Tendência iniciando (5m)",
                c5[i5],
                "EMA9 cruzou MA20/MA50 (5m) — intrabar"
            )
            await send_alert(session, msg)
            monitor.mark(symbol, "TENDENCIA_INICIANDO_5M")

        # --------------- 5m — Tendência pré-confirmada ---------------
        # MA20 e MA50 acima da MA200 e ao menos uma cruza agora
        pre_5m = (
            ma20_5[i5] > ma200_5[i5] and
            ma50_5[i5] > ma200_5[i5] and
            (cross_up(ma20_5[p5], ma200_5[p5], ma20_5[i5], ma200_5[i5]) or
             cross_up(ma50_5[p5], ma200_5[p5], ma50_5[i5], ma200_5[i5]))
        )
        if pre_5m and monitor.allowed(symbol, "TENDENCIA_PRECONFIRMADA_5M"):
            msg = build_msg(
                symbol,
                f"🟡 {fmt_symbol(symbol)} ⬆️ Tendência pré-confirmada (5m)",
                c5[i5],
                "MA20 e MA50 cruzaram/estão acima da MA200 (5m)"
            )
            await send_alert(session, msg)
            monitor.mark(symbol, "TENDENCIA_PRECONFIRMADA_5M")

        # --------------- 15m — Tendência pré-confirmada ---------------
        pre_15m = cross_up(ema9_15[p15], ma200_15[p15], ema9_15[i15], ma200_15[i15])
        if pre_15m and monitor.allowed(symbol, "TENDENCIA_PRECONFIRMADA_15M"):
            msg = build_msg(
                symbol,
                f"🟡 {fmt_symbol(symbol)} ⬆️ Tendência pré-confirmada (15m)",
                c15[i15],
                "EMA9 cruzou acima da MA200 (15m)"
            )
            await send_alert(session, msg)
            monitor.mark(symbol, "TENDENCIA_PRECONFIRMADA_15M")

        # --------------- 15m — Tendência confirmada ------------------
        conf_15m = (
            ma20_15[i15] > ma200_15[i15] and
            ma50_15[i15] > ma200_15[i15] and
            (cross_up(ma20_15[p15], ma200_15[p15], ma20_15[i15], ma200_15[i15]) or
             cross_up(ma50_15[p15], ma200_15[p15], ma50_15[i15], ma200_15[i15]))
        )
        if conf_15m and monitor.allowed(symbol, "TENDENCIA_CONFIRMADA_15M"):
            msg = build_msg(
                symbol,
                f"🟢 {fmt_symbol(symbol)} 🚀 Tendência confirmada (15m)",
                c15[i15],
                "MA20 e MA50 acima da MA200 (15m) — confirmação no cruzamento"
            )
            await send_alert(session, msg)
            monitor.mark(symbol, "TENDENCIA_CONFIRMADA_15M")

    except Exception as e:
        print("worker error", symbol, e)

# ----------------- Main Loop -----------------
async def main():
    monitor = Monitor()
    async with aiohttp.ClientSession() as session:
        # shortlist inicial
        tickers   = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        hello = f"✅ v3.intrabar ativo | {len(watchlist)} pares SPOT | cooldown {COOLDOWN_SEC//60}m | {ts_brazil_now()}"
        await send_alert(session, hello)
        print(hello)

        last_refresh = time.time()

        while True:
            tasks = [candle_worker(session, s, monitor) for s in watchlist]
            await asyncio.gather(*tasks)

            # refresh periódico da shortlist
            if time.time() - last_refresh > REFRESH_SHORTLIST:
                try:
                    tickers   = await get_24h(session)
                    watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
                    last_refresh = time.time()
                    print(f"shortlist atualizada: {len(watchlist)} pares")
                except Exception as e:
                    print("erro na atualização da shortlist:", e)

            await asyncio.sleep(15)  # frequência de varredura

# ----------------- Flask (Render) -----------------
def start_bot():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    import threading
    threading.Thread(target=start_bot, daemon=True).start()

    app = Flask(__name__)

    @app.route("/")
    def home():
        return "✅ Binance Alerts v3.intrabar — 5m/15m cruzamentos de médias (EMA9, MA20/50/200) — ON"

    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
