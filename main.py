import os, asyncio, time
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone

import aiohttp

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"  # .com para evitar erro 451
INTERVAL = "5m"                  # 1m/3m/5m/15m
SHORTLIST_N = 40                 # quantos pares USDT “quentes” monitorar
COOLDOWN_SEC = 15 * 60           # 1 alerta por símbolo a cada 15 min
MIN_PCT = 1.0                    # filtro inicial 24h
MIN_QV = 300_000.0               # filtro inicial 24h (quote volume)

EMA_FAST = 9
MA_SLOW = 20
MA_MED = 50
RSI_LEN = 14
VOL_MA = 9
HH_WIN = 20

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# --------------- Utils / Alert ---------------
def fmt_symbol(symbol: str) -> str:
    return symbol[:-4] + "/USDT" if symbol.endswith("USDT") else symbol

def binance_pair_link(symbol: str) -> str:
    base = symbol.replace("USDT", "_USDT")
    return f"https://www.binance.com/pt/trade/{base}"

async def send_alert(session: aiohttp.ClientSession, text: str):
    # (1) webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            async with session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Webhook error:", e)
    # (2) Telegram direto
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
            async with session.post(url, data=payload, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Telegram error:", e)

# --------------- Indicadores (sem pandas) ---------------
def sma(seq, n):
    out, q, s = [], deque(), 0.0
    for x in seq:
        q.append(x); s += x
        if len(q) > n: s -= q.popleft()
        out.append(s / len(q))
    return out

def ema(seq, span):
    out = []
    if not seq: return out
    alpha = 2.0 / (span + 1.0)
    e = seq[0]
    out.append(e)
    for x in seq[1:]:
        e = alpha * x + (1 - alpha) * e
        out.append(e)
    return out

def rolling_max(seq, n):
    out = []
    q = deque()
    for i, x in enumerate(seq):
        q.append(x)
        if len(q) > n: q.popleft()
        out.append(max(q))
    return out

def rsi_wilder(closes, period=14):
    if len(closes) == 0: return []
    deltas = [0.0] + [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    rsis = [50.0] * len(closes)
    if len(closes) < period + 1: return rsis

    avg_gain = sum(gains[1:period+1]) / period
    avg_loss = sum(losses[1:period+1]) / period

    for i in range(period+1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / (avg_loss + 1e-12)
        rsis[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsis

def compute_indicators(open_, high, low, close, volume):
    ema9   = ema(close, EMA_FAST)
    ma20   = sma(close, MA_SLOW)
    ma50   = sma(close, MA_MED)
    rsi14  = rsi_wilder(close, RSI_LEN)
    vol_ma = sma(volume, VOL_MA)
    hh20   = rolling_max(high, HH_WIN)
    return ema9, ma20, ma50, rsi14, vol_ma, hh20

# --------------- Regras (5 modelos) ---------------
def check_signals(close, high, low, volume, ema9, ma20, ma50, rsi14, vol_ma, hh20):
    n = len(close)
    if n < 60: return []
    last, prev = n - 1, n - 2
    out = []

    # 1) PUMP Explosivo
    if (volume[last] > (vol_ma[last] * 2.0)
        and rsi14[last] > 60
        and ema9[last] > ma20[last]
        and close[last] > close[prev] * 1.01):
        out.append(("PUMP", f"Vol {volume[last]:.0f} > 2x média | RSI {rsi14[last]:.1f} | EMA9>MA20"))

    # 2) Rompimento (Breakout)
    if (close[last] > hh20[last]
        and volume[last] > vol_ma[last] * 1.2
        and rsi14[last] > 55
        and ema9[last] > ma20[last]):
        out.append(("BREAKOUT", f"Fechou acima da máxima 20 | Vol>média | RSI {rsi14[last]:.1f}"))

    # 3) Tendência Sustentada
    if (ema9[last-2] > ma20[last-2] and ema9[last-1] > ma20[last-1] and ema9[last] > ma20[last]
        and ma20[last] > ma50[last]
        and 55 <= rsi14[last] <= 70):
        out.append(("TENDÊNCIA", f"EMA9>MA20>MA50 | RSI {rsi14[last]:.1f}"))

    # 4) Reversão de Fundo
    prev_rsi = rsi14[last-3] if last >= 3 else 50.0
    if (prev_rsi < 45 and rsi14[last] > 50
        and ema9[last-1] <= ma20[last-1] and ema9[last] > ma20[last]
        and close[last] > close[prev]
        and volume[last] >= vol_ma[last] * 1.10):
        out.append(("REVERSÃO", f"RSI {prev_rsi:.1f}→{rsi14[last]:.1f} | EMA9 cruzou MA20 | Vol>média"))

    # 5) Reteste / Pullback
    touched_ma20 = any(low[i] <= ma20[i] for i in range(max(0, last-2), last+1))
    touched_ema9 = any(low[i] <= ema9[i] for i in range(max(0, last-2), last+1))
    if (ma20[last] > ma50[last]
        and (touched_ma20 or touched_ema9)
        and close[last] > ema9[last]
        and rsi14[last] > 55
        and volume[last] >= vol_ma[last] * 1.00):
        out.append(("RETESTE", f"Retomada após toque na média | RSI {rsi14[last]:.1f} | Vol>=média"))

    return out

# --------------- Binance ---------------
async def get_klines(session, symbol: str, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()

    open_, high, low, close, volume = [], [], [], [], []
    for k in data:
        open_.append(float(k[1]))
        high.append(float(k[2]))
        low.append(float(k[3]))
        close.append(float(k[4]))
        volume.append(float(k[5]))
    return open_, high, low, close, volume

async def get_24h(session):
    url = f"{BINANCE_HTTP}/api/v3/ticker/24hr"
    async with session.get(url, timeout=15) as r:
        r.raise_for_status()
        return await r.json()

def shortlist_from_24h(tickers, n=40):
    usdt = []
    for t in tickers:
        s = t.get("symbol","")
        if not s.endswith("USDT"): 
            continue
        if any(x in s for x in ("UP","DOWN","BULL","BEAR")):
            continue
        pct = abs(float(t.get("priceChangePercent","0") or 0.0))
        qv  = float(t.get("quoteVolume","0") or 0.0)
        if pct >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# --------------- Anti-spam ---------------
class Monitor:
    def __init__(self):
        self.cooldown = defaultdict(lambda: 0.0)
    def allowed(self, symbol: str) -> bool:
        return time.time() - self.cooldown[symbol] >= COOLDOWN_SEC
    def mark(self, symbol: str):
        self.cooldown[symbol] = time.time()

def kind_emoji(kind: str) -> str:
    return {"PUMP":"🚀","BREAKOUT":"💥","TENDÊNCIA":"📈","REVERSÃO":"🔄","RETESTE":"♻️"}.get(kind,"📌")

def pick_priority_kind(signals):
    prio = {"PUMP":0,"BREAKOUT":1,"REVERSÃO":2,"RETESTE":3,"TENDÊNCIA":4}
    return sorted(signals, key=lambda x: prio.get(x[0],9))[0][0] if signals else "SINAL"

async def candle_worker(session, symbol: str, monitor: Monitor):
    try:
        open_, high, low, close, volume = await get_klines(session, symbol, interval=INTERVAL, limit=200)
        ema9, ma20, ma50, rsi14, vol_ma, hh20 = compute_indicators(open_, high, low, close, volume)
        signals = check_signals(close, high, low, volume, ema9, ma20, ma50, rsi14, vol_ma, hh20)
        if signals and monitor.allowed(symbol):
            last_price = close[-1]
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            first_kind = pick_priority_kind(signals)
            emoji = kind_emoji(first_kind)
            sym_pretty = fmt_symbol(symbol)
            bullets = "\n".join([f"• {kind_emoji(k)} *{k}*: {desc}" for k, desc in signals])
            txt = (
                f"{emoji} *{sym_pretty} — {first_kind} DETECTADO!*\n"
                f"💰 Preço: `{last_price:.6f}`\n"
                f"🧠 Sinal técnico:\n{bullets}\n\n"
                f"⏰ {ts}\n"
                f"🔗 [Abrir na Binance]({binance_pair_link(symbol)})"
            )
            await send_alert(session, txt)
            monitor.mark(symbol)
    except Exception as e:
        print("candle_worker error", symbol, e)

async def main():
    monitor = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        await send_alert(session, f"✅ Monitor online [{INTERVAL}] — acompanhando {len(watchlist)} pares | {ts}")
        print("Shortlist:", watchlist[:10], "… total:", len(watchlist))

        while True:
            await asyncio.gather(*[candle_worker(session, s, monitor) for s in watchlist])
            await asyncio.sleep(180)
            try:
                tickers = await get_24h(session)
                watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
            except Exception as e:
                print("Erro ao atualizar shortlist:", e)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
