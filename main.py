import os, asyncio, time
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone

import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"  # .com para evitar erro 451
INTERVAL = "15m"                # timeframe (mantido em 15m)
SHORTLIST_N = 50                # até 50 pares
COOLDOWN_SEC = 15 * 60          # 1 alerta por símbolo a cada 15 min
MIN_PCT = 1.0                   # filtro inicial 24h (%)
MIN_QV = 300_000.0              # filtro inicial 24h (quote volume)

EMA_FAST = 9
MA_SLOW = 20
MA_MED = 50
MA_LONG = 200                   # NOVO: MA200
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

def binance_links(symbol: str) -> str:
    """
    Dois links para o mesmo par SPOT:
    (A) /trade/<BASE>_USDT?type=spot
    (B) /trade?type=spot&symbol=<BASE>_USDT
    """
    base = symbol.upper().replace("USDT", "")
    a = f"https://www.binance.com/en/trade/{base}_USDT?type=spot"
    b = f"https://www.binance.com/en/trade?type=spot&symbol={base}_USDT"
    return f'🔗 <a href="{a}">Abrir (A)</a> | <a href="{b}">Abrir (B)</a>'

async def send_alert(session: aiohttp.ClientSession, text: str):
    # (1) webhook opcional
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            async with session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10) as r:
                await r.text()
        except Exception as e:
            print("Webhook error:", e)
    # (2) Telegram direto (HTML em MAIÚSCULO)
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
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
    for x in seq:
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
    ma200  = sma(close, MA_LONG)            # NOVO
    rsi14  = rsi_wilder(close, RSI_LEN)
    vol_ma = sma(volume, VOL_MA)
    hh20   = rolling_max(high, HH_WIN)
    res20  = rolling_max(high, 20)          # resistência curta
    res50  = rolling_max(high, 50)          # resistência longa
    return ema9, ma20, ma50, ma200, rsi14, vol_ma, hh20, res20, res50

# --------------- Regras (com MA200 e início de alta) ---------------
def check_signals(open_, close, high, low, volume, ema9, ma20, ma50, ma200, rsi14, vol_ma, hh20, res20, res50):
    n = len(close)
    if n < 60: return []
    last, prev = n - 1, n - 2
    out = []

    price_above_200 = close[last] > ma200[last]
    cross_9_20_up   = (ema9[last-1] <= ma20[last-1] and ema9[last] > ma20[last])

    # 0) ⚠️ Reversão em Observação (EMA9 cruza MA20, mas ainda abaixo da MA200)
    if cross_9_20_up and not price_above_200 and rsi14[last] >= 48 and volume[last] >= vol_ma[last] * 1.0 and close[last] > open_[last]:
        out.append(("REVERSÃO_OBS", f"EMA9↑MA20 abaixo da MA200 | RSI {rsi14[last]:.1f} | Vol>=média"))

    # 1) 🌅 Início de Alta (EMA9 cruzou MA20 e preço acima da MA200)
    if cross_9_20_up and price_above_200 and rsi14[last] >= 50 and volume[last] > vol_ma[last] * 1.2 and close[last] > open_[last]:
        out.append(("INÍCIO_ALTA", f"EMA9 cruzou MA20 ↑ | RSI {rsi14[last]:.1f} | Vol>média | >MA200"))

    # 2) 🚀 PUMP Explosivo (só se acima da MA200)
    if (price_above_200
        and volume[last] > (vol_ma[last] * 2.0)
        and rsi14[last] > 60
        and ema9[last] > ma20[last]
        and close[last] > close[prev] * 1.01):
        out.append(("PUMP", f"Vol {volume[last]:.0f} > 2x média | RSI {rsi14[last]:.1f} | EMA9>MA20 | >MA200"))

    # 3) 💥 Rompimento (Breakout) (só se acima da MA200)
    if (price_above_200
        and close[last] > hh20[last]
        and volume[last] > vol_ma[last] * 1.2
        and rsi14[last] > 55
        and ema9[last] > ma20[last]):
        out.append(("BREAKOUT", f"Fechou acima da máxima 20 | Vol>média | RSI {rsi14[last]:.1f} | >MA200"))

    # 4) 📈 Tendência Sustentada (só se acima da MA200)
    if (price_above_200
        and ema9[last-2] > ma20[last-2] and ema9[last-1] > ma20[last-1] and ema9[last] > ma20[last]
        and ma20[last] > ma50[last]
        and 55 <= rsi14[last] <= 70):
        out.append(("TENDÊNCIA", f"EMA9>MA20>MA50 | RSI {rsi14[last]:.1f} | >MA200"))

    # 5) 🔄 Reversão de Fundo (só se acima da MA200)
    prev_rsi = rsi14[last-3] if last >= 3 else 50.0
    if (price_above_200
        and prev_rsi < 45 and rsi14[last] > 50
        and ema9[last-1] <= ma20[last-1] and ema9[last] > ma20[last]
        and close[last] > close[prev]
        and volume[last] >= vol_ma[last] * 1.10):
        out.append(("REVERSÃO", f"RSI {prev_rsi:.1f}→{rsi14[last]:.1f} | EMA9 cruzou MA20 | Vol>média | >MA200"))

    # 6) ♻️ Reteste / Pullback (só se acima da MA200)
    touched_ma20 = any(low[i] <= ma20[i] for i in range(max(0, last-2), last+1))
    touched_ema9 = any(low[i] <= ema9[i] for i in range(max(0, last-2), last+1))
    if (price_above_200
        and ma20[last] > ma50[last]
        and (touched_ma20 or touched_ema9)
        and close[last] > ema9[last]
        and rsi14[last] > 55
        and volume[last] >= vol_ma[last] * 1.00):
        out.append(("RETESTE", f"Retomada após toque na média | RSI {rsi14[last]:.1f} | Vol>=média | >MA200"))

    # 7) 🧱 Resistência tocada/rompida (independe da MA200)
    if close[last] >= res20[last]:
        out.append(("RESISTÊNCIA_CURTA", f"Fechou acima da resistência 20 ({res20[last]:.4f})"))
    if close[last] >= res50[last]:
        out.append(("RESISTÊNCIA_LONGA", f"Fechou acima da resistência 50 ({res50[last]:.4f})"))

    # 8) 🟨 Suporte/Resistência na MA200 (zona crítica)
    near200 = (abs(close[last] - ma200[last]) / (ma200[last] + 1e-12) < 0.005) or (low[last] <= ma200[last] <= high[last])
    if near200:
        out.append(("SUPORTE_200", f"Preço encostando na MA200 ({ma200[last]:.4f})"))

    # 9) 🟩 Rompimento da MA200 (de baixo para cima)
    if close[prev] < ma200[prev] and close[last] > ma200[last] and volume[last] > vol_ma[last] * 1.1:
        out.append(("ROMPIMENTO_200", f"Cruzou MA200 ↑ | Vol>média"))

    return out

# --------------- Binance ---------------
async def get_klines(session, symbol: str, interval="15m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()

    # 🔒 Evita usar vela ainda aberta (close time em ms fica no k[6])
    now_ms = int(time.time() * 1000)
    if data and now_ms < int(data[-1][6]):
        data = data[:-1]

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

def shortlist_from_24h(tickers, n=400):
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
    return {
        "INÍCIO_ALTA":"🌅","REVERSÃO_OBS":"⚠️",
        "PUMP":"🚀","BREAKOUT":"💥","TENDÊNCIA":"📈",
        "REVERSÃO":"🔄","RETESTE":"♻️",
        "RESISTÊNCIA_CURTA":"🧱","RESISTÊNCIA_LONGA":"🏗️",
        "SUPORTE_200":"🟨","ROMPIMENTO_200":"🟩"
    }.get(kind,"📌")

def pick_priority_kind(signals):
    # Prioridade: início/rápidos > confirmação > contexto
    prio = {
        "INÍCIO_ALTA":0,"PUMP":1,"BREAKOUT":2,"REVERSÃO":3,
        "REVERSÃO_OBS":4,"RETESTE":5,"ROMPIMENTO_200":6,
        "RESISTÊNCIA_CURTA":7,"RESISTÊNCIA_LONGA":8,"TENDÊNCIA":9,"SUPORTE_200":10
    }
    return sorted(signals, key=lambda x: prio.get(x[0], 99))[0][0] if signals else "SINAL"

# --------------- Worker por símbolo ---------------
async def candle_worker(session, symbol: str, monitor: Monitor):
    try:
        open_, high, low, close, volume = await get_klines(session, symbol, interval=INTERVAL, limit=200)
        ema9, ma20, ma50, ma200, rsi14, vol_ma, hh20, res20, res50 = compute_indicators(open_, high, low, close, volume)
        signals = check_signals(open_, close, high, low, volume, ema9, ma20, ma50, ma200, rsi14, vol_ma, hh20, res20, res50)
        if signals and monitor.allowed(symbol):
            last_price = close[-1]
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            first_kind = pick_priority_kind(signals)
            emoji = kind_emoji(first_kind)
            sym_pretty = fmt_symbol(symbol)
            bullets = "\n".join([f"• {kind_emoji(k)} <b>{k}</b>: {desc}" for k, desc in signals])
            txt = (
                f"{emoji} <b>{sym_pretty} — {first_kind} DETECTADO!</b>\n"
                f"💰 Preço: <code>{last_price:.6f}</code>\n"
                f"🧠 Sinal técnico:\n{bullets}\n\n"
                f"⏰ {ts}\n"
                f"🔗 {binance_links(symbol)}"
            )
            await send_alert(session, txt)
            monitor.mark(symbol)
    except Exception as e:
        print("candle_worker error", symbol, e)

# --------------- Loop principal ---------------
async def main():
    monitor = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        await send_alert(session, f"<b>💻 Modo FULL ativo</b> — monitorando {len(watchlist)} pares SPOT | {ts}")
        print(f"💻 Modo FULL ativo — analisando {len(watchlist)} pares.")
        while True:
            await asyncio.gather(*[candle_worker(session, s, monitor) for s in watchlist])
            await asyncio.sleep(600)  # 10 min entre ciclos (alinhado ao 15m)
            try:
                tickers = await get_24h(session)
                watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
            except Exception as e:
                print("Erro ao atualizar shortlist:", e)

# --------------- Execução paralela Flask + bot ---------------
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
        return "✅ Binance Alerts Bot (15m + MA200 + Início de Alta) ativo!"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
