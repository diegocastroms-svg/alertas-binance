# main_v11_5_longterm_1h.py
# Base: v11.4 (mantida)
# Alterações únicas:
# 1) Removido alerta "Média 200 Ascendente" (Minervini 200 UP)
# 2) Adicionado longterm_worker (15m + 1h + 4h), cooldown 1h, mensagem toda em negrito
# 3) Reforçado filtro SPOT na função shortlist_from_24h (exclui futures, perp e tokens fora da Binance Spot)
# 4) [ADIÇÃO] Novos alertas longos independentes (mensagens separadas e em negrito):
#    - PRÉ-CONFIRMAÇÃO LONGA (1H) — 1ª vela
#    - TENDÊNCIA LONGA CONFIRMADA (1H) — 2ª vela
#    - PRÉ-CONFIRMAÇÃO (4H) — 1ª vela
#    - TENDÊNCIA 4H CONFIRMADA — 2ª vela

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- Config -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"
INTERVAL_CONF = "15m"
SHORTLIST_N   = 65
COOLDOWN_SEC  = 15 * 60
COOLDOWN_LONGTERM = 60 * 60
MIN_PCT       = 1.0
MIN_QV        = 300_000.0

EMA_FAST = 9
MA_SLOW  = 20
MA_MED   = 50
MA_LONG  = 200
RSI_LEN  = 14
VOL_MA   = 9
BB_LEN   = 20
ADX_LEN  = 14

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
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S") + " 🇧🇷"

async def send_alert(session, text):
    if WEBHOOK_BASE and WEBHOOK_SECRET:
        try:
            await session.post(f"{WEBHOOK_BASE}/{WEBHOOK_SECRET}", json={"message": text}, timeout=10)
        except:
            pass
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            await session.post(url, data=payload, timeout=10)
        except:
            pass

def pct_change(new, old):
    return (new / (old + 1e-12) - 1.0) * 100.0

# ----------------- Indicadores -----------------
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

def rolling_std(seq, n):
    out, q = [], deque()
    for x in seq:
        q.append(x)
        if len(q) > n: q.popleft()
        m = sum(q) / len(q)
        var = sum((v - m) ** 2 for v in q) / len(q)
        out.append(math.sqrt(var))
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

def true_range(h, l, c):
    tr = [0.0]
    for i in range(1, len(c)):
        tr_curr = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        tr.append(tr_curr)
    return tr

def adx(h, l, c, period=14):
    n = len(c)
    if n < period + 1: return [20.0] * n, [0.0]*n, [0.0]*n
    tr = true_range(h, l, c)
    plus_dm  = [0.0]; minus_dm = [0.0]
    for i in range(1, n):
        up_move   = h[i] - h[i-1]
        down_move = l[i-1] - l[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
    atr = [0.0]*n
    atr[period] = sum(tr[1:period+1])
    pdm = [0.0]*n; mdm = [0.0]*n
    pdm[period] = sum(plus_dm[1:period+1]); mdm[period] = sum(minus_dm[1:period+1])
    for i in range(period+1, n):
        atr[i] = atr[i-1] - (atr[i-1] / period) + tr[i]
        pdm[i] = pdm[i-1] - (pdm[i-1] / period) + plus_dm[i]
        mdm[i] = mdm[i-1] - (mdm[i-1] / period) + minus_dm[i]
    atr[:period] = [sum(tr[1:period+1])]*(period)
    pdm[:period] = [sum(plus_dm[1:period+1])]*(period)
    mdm[:period] = [sum(minus_dm[1:period+1])]*(period)
    plus_di  = [0.0]*n; minus_di = [0.0]*n
    for i in range(n):
        plus_di[i]  = 100.0 * (pdm[i] / (atr[i] + 1e-12))
        minus_di[i] = 100.0 * (mdm[i] / (atr[i] + 1e-12))
    dx = [0.0]*n
    for i in range(n):
        dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / (plus_di[i] + minus_di[i] + 1e-12)
    adx_vals = [0.0]*n
    adx_vals[period] = sum(dx[1:period+1]) / period
    for i in range(period+1, n):
        adx_vals[i] = (adx_vals[i-1] * (period - 1) + dx[i]) / period
    for i in range(period):
        adx_vals[i] = adx_vals[period]
    return adx_vals, plus_di, minus_di

def compute_indicators(o,h,l,c,v):
    ema9  = ema(c, EMA_FAST)
    ma20  = sma(c, MA_SLOW)
    ma50  = sma(c, MA_MED)
    ma200 = sma(c, MA_LONG)
    rsi14 = rsi_wilder(c, RSI_LEN)
    volma = sma(v, VOL_MA)
    bb_std = rolling_std(c, BB_LEN)
    bb_up  = [ma20[i] + 2 * bb_std[i] for i in range(len(bb_std))]
    bb_low = [ma20[i] - 2 * bb_std[i] for i in range(len(bb_std))]
    adx14, pdi, mdi = adx(h, l, c, ADX_LEN)
    return ema9, ma20, ma50, ma200, rsi14, volma, bb_up, bb_low, adx14, pdi, mdi

# ----------------- Binance -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    o,h,l,c,v=[],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1])); h.append(float(k[2])); l.append(float(k[3]))
        c.append(float(k[4])); v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

# ✅ Filtro SPOT reforçado
def shortlist_from_24h(tickers, n=400):
    usdt = []
    for t in tickers:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        blocked = (
            "UP","DOWN","BULL","BEAR","PERP","_PERP","USD_","_USD",
            "_BUSD","_FDUSD","_TUSD","_USDC","_DAI","_BTC",
            "_EUR","_TRY","_BRL","_ETH","_BNB","_SOL"
        )
        if any(x in s for x in blocked):
            continue
        pct = float(t.get("priceChangePercent", "0") or 0.0)
        qv  = float(t.get("quoteVolume", "0") or 0.0)
        if abs(pct) >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- Emojis / Mensagens -----------------
def build_msg_long(symbol, title, info):
    sym = fmt_symbol(symbol)
    return (
        f"<b>🌕 {sym} — {title}</b>\n"
        f"<b>{info}</b>\n"
        f"🕒 {ts_brazil_now()}\n"
        f"<b>{binance_links(symbol)}</b>"
    )

# ----------------- Monitor -----------------
class Monitor:
    def __init__(self):
        self.cooldown_long = defaultdict(lambda: 0.0)
    def allowed_long(self, symbol, key="LONGTERM"):
        return time.time() - self.cooldown_long[(symbol, key)] >= COOLDOWN_LONGTERM
    def mark_long(self, symbol, key="LONGTERM"):
        self.cooldown_long[(symbol, key)] = time.time()

# ----------------- Worker LONGO -----------------
async def longterm_worker(session, symbol, monitor: Monitor):
    try:
        # 1h timeframe
        o1,h1,l1,c1,v1 = await get_klines(session, symbol, interval="1h", limit=120)
        if len(c1) < 60: return
        ema9_1, ma20_1, ma50_1, ma200_1, rsi1, volma1, _, _, adx1, _, _ = compute_indicators(o1,h1,l1,c1,v1)
        last1 = len(c1)-1

        # 4h timeframe
        o4,h4,l4,c4,v4 = await get_klines(session, symbol, interval="4h", limit=120)
        if len(c4) < 60: return
        ema9_4, ma20_4, ma50_4, ma200_4, rsi4, volma4, _, _, adx4, _, _ = compute_indicators(o4,h4,l4,c4,v4)
        last4 = len(c4)-1

        # -------- 1H ALERTAS --------
        # Pré-confirmação longa (1ª vela)
        if (
            ema9_1[last1] > ma20_1[last1]
            and ema9_1[last1-1] <= ma20_1[last1-1]
            and 50 <= rsi1[last1] <= 60
            and v1[last1] >= volma1[last1] * 1.05
            and monitor.allowed_long(symbol, "PRECONFIRM_1H")
        ):
            msg = build_msg_long(symbol, "PRÉ-CONFIRMAÇÃO LONGA (1H)",
                f"💰 Preço: <code>{c1[last1]:.6f}</code>\n<b>EMA9 cruzou acima da MA20</b> | <b>RSI:</b> {rsi1[last1]:.1f} | <b>Volume:</b> +5%")
            await send_alert(session, msg)
            monitor.mark_long(symbol, "PRECONFIRM_1H")

        # Tendência longa confirmada (2ª vela)
        if (
            ema9_1[last1] > ma20_1[last1] > ma50_1[last1]
            and ema9_1[last1-1] > ma20_1[last1-1] > ma50_1[last1-1]
            and rsi1[last1] > 55
            and adx1[last1] > 25
            and monitor.allowed_long(symbol, "CONFIRM_1H")
        ):
            msg = build_msg_long(symbol, "TENDÊNCIA LONGA CONFIRMADA (1H)",
                f"💰 Preço: <code>{c1[last1]:.6f}</code>\n<b>EMA9>MA20>MA50</b> | <b>RSI:</b> {rsi1[last1]:.1f} | <b>ADX:</b> {adx1[last1]:.1f}")
            await send_alert(session, msg)
            monitor.mark_long(symbol, "CONFIRM_1H")

        # -------- 4H ALERTAS --------
        # Pré-confirmação longa (1ª vela)
        if (
            ema9_4[last4] > ma20_4[last4]
            and ema9_4[last4-1] <= ma20_4[last4-1]
            and rsi4[last4] > 50
            and monitor.allowed_long(symbol, "PRECONFIRM_4H")
        ):
            msg = build_msg_long(symbol, "PRÉ-CONFIRMAÇÃO (4H)",
                f"💰 Preço: <code>{c4[last4]:.6f}</code>\n<b>EMA9 cruzou acima da MA20</b> | <b>RSI:</b> {rsi4[last4]:.1f}")
            await send_alert(session, msg)
            monitor.mark_long(symbol, "PRECONFIRM_4H")

        # Tendência confirmada (2ª vela)
        if (
            ema9_4[last4] > ma20_4[last4] > ma50_4[last4]
            and ema9_4[last4-1] > ma20_4[last4-1] > ma50_4[last4-1]
            and rsi4[last4] > 55
            and adx4[last4] > 25
            and monitor.allowed_long(symbol, "CONFIRM_4H")
        ):
            msg = build_msg_long(symbol, "TENDÊNCIA 4H CONFIRMADA",
                f"💰 Preço: <code>{c4[last4]:.6f}</code>\n<b>EMA9>MA20>MA50</b> | <b>RSI:</b> {rsi4[last4]:.1f} | <b>ADX:</b> {adx4[last4]:.1f}")
            await send_alert(session, msg)
            monitor.mark_long(symbol, "CONFIRM_4H")

    except Exception as e:
        print("longterm error", symbol, e)

# ----------------- Main -----------------
async def main():
    monitor = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)

        hello = f"💻 v11.5 | Core 5m/15m intacto + LongTerm (1H/4H atualizados) | {len(watchlist)} pares SPOT | {ts_brazil_now()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks = [longterm_worker(session, s, monitor) for s in watchlist]
            await asyncio.gather(*tasks)
            await asyncio.sleep(180)

# ----------------- Flask -----------------
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
        return "✅ Binance Alerts Bot v11.5 — Core intacto (5m/15m) + Tendência Longa (1h/4h) 🇧🇷"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
