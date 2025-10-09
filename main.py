# main_py15e_final.py
# Base: main_py15 (funcional)
# Alterações: 
#  - removido reteste 5m
#  - adicionado "Tendência pré-confirmada 5m" e "Tendência pré-confirmada 15m"
#  - Flask preservado
#  - sem mudar nada mais

import os, asyncio, time, math
from urllib.parse import urlencode
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
import aiohttp
from flask import Flask

# ----------------- CONFIG -----------------
BINANCE_HTTP = "https://api.binance.com"
INTERVAL_MAIN = "5m"
INTERVAL_CONF = "15m"
SHORTLIST_N = 65
COOLDOWN_SEC = 15 * 60
COOLDOWN_LONGTERM = 60 * 60
MIN_PCT = 1.0
MIN_QV = 300_000.0

EMA_FAST = 9
MA_SLOW = 20
MA_MED = 50
MA_LONG = 200
RSI_LEN = 14
VOL_MA = 9
BB_LEN = 20
ADX_LEN = 14

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ----------------- UTILS -----------------
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

# ----------------- INDICADORES -----------------
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
    # ----------------- BINANCE -----------------
async def get_klines(session, symbol, interval="5m", limit=200):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    url = f"{BINANCE_HTTP}/api/v3/klines?{urlencode(params)}"
    async with session.get(url, timeout=12) as r:
        r.raise_for_status()
        data = await r.json()
    # remove o último candle em formação
    o,h,l,c,v = [],[],[],[],[]
    for k in data[:-1]:
        o.append(float(k[1]))
        h.append(float(k[2]))
        l.append(float(k[3]))
        c.append(float(k[4]))
        v.append(float(k[5]))
    return o,h,l,c,v

async def get_24h(session):
    async with session.get(f"{BINANCE_HTTP}/api/v3/ticker/24hr", timeout=15) as r:
        r.raise_for_status()
        return await r.json()

# ----------------- SHORTLIST (SPOT-ONLY) -----------------
def shortlist_from_24h(tickers, n=400):
    usdt = []
    blocked = (
        "UP","DOWN","BULL","BEAR","PERP","_PERP",
        "_BUSD","_FDUSD","_TUSD","_USDC","_DAI",
        "_BTC","_EUR","_TRY","_BRL"
    )
    for t in tickers:
        s = t.get("symbol","")
        if not s.endswith("USDT"):
            continue
        if any(x in s for x in blocked):
            continue
        try:
            pct = float(t.get("priceChangePercent","0") or 0.0)
            qv  = float(t.get("quoteVolume","0") or 0.0)
        except:
            continue
        if abs(pct) >= MIN_PCT and qv >= MIN_QV:
            usdt.append((s, pct, qv))
    usdt.sort(key=lambda x: (abs(x[1]), x[2]), reverse=True)
    return [x[0] for x in usdt[:n]]

# ----------------- MONITOR / COOLDOWN -----------------
class Monitor:
    def __init__(self):
        # curto: chave (symbol, kind)
        self.cooldown = defaultdict(lambda: 0.0)
        # longo: chave (symbol, kind)
        self.cooldown_long = defaultdict(lambda: 0.0)

    def allowed(self, symbol, kind):
        return time.time() - self.cooldown[(symbol, kind)] >= COOLDOWN_SEC
    def mark(self, symbol, kind):
        self.cooldown[(symbol, kind)] = time.time()

    def allowed_long(self, symbol, kind):
        return time.time() - self.cooldown_long[(symbol, kind)] >= COOLDOWN_LONGTERM
    def mark_long(self, symbol, kind):
        self.cooldown_long[(symbol, kind)] = time.time()

# ----------------- WORKER CURTO (5m + 15m) -----------------
async def candle_worker(session, symbol, monitor: Monitor):
    try:
        # -------- 5m --------
        o,h,l,c,v = await get_klines(session, symbol, interval="5m", limit=200)
        if len(c) < 60: 
            return
        ema9, ma20, ma50, ma200, rsi14, vol_ma, adx14, pdi, mdi = compute_indicators(o,h,l,c,v)
        last = len(c) - 1

        # 🚀 Tendência iniciando (5m) — EMA9 cruza MA20/MA50 + RSI >= 55
        cross_9_20 = (ema9[last-1] <= ma20[last-1] and ema9[last] > ma20[last])
        cross_9_50 = (ema9[last-1] <= ma50[last-1] and ema9[last] > ma50[last])
        if (ema9[last] > ma20[last] and ema9[last] > ma50[last] and (cross_9_20 or cross_9_50) and rsi14[last] >= 55.0):
            if monitor.allowed(symbol, "TENDENCIA_INICIANDO_5M"):
                txt = (
                    f"⭐ {fmt_symbol(symbol)} 🚀 — TENDÊNCIA INICIANDO (5m)\n"
                    f"💰 <code>{c[last]:.6f}</code>\n"
                    f"🧠 EMA9 cruzou MA20/MA50 | RSI {rsi14[last]:.1f}\n"
                    f"⏰ {ts_brazil_now()}\n"
                    f"{binance_links(symbol)}"
                )
                await send_alert(session, txt)
                monitor.mark(symbol, "TENDENCIA_INICIANDO_5M")

        # 🌕 Tendência PRÉ-CONFIRMADA (5m) — EMA9>MA20>MA50>MA200 + RSI>50 + Vol >= média
        if (ema9[last] > ma20[last] > ma50[last] > ma200[last] and
            rsi14[last] > 50.0 and v[last] >= vol_ma[last] * 1.00):
            if monitor.allowed(symbol, "PRECONFIRM_5M"):
                txt = (
                    f"⭐ {fmt_symbol(symbol)} 🌕 — TENDÊNCIA PRÉ-CONFIRMADA (5m)\n"
                    f"💰 <code>{c[last]:.6f}</code>\n"
                    f"🧠 EMA9>MA20>MA50>MA200 | RSI {rsi14[last]:.1f} | Volume ≥ média\n"
                    f"⏰ {ts_brazil_now()}\n"
                    f"{binance_links(symbol)}"
                )
                await send_alert(session, txt)
                monitor.mark(symbol, "PRECONFIRM_5M")

        # -------- 15m --------
        o15,h15,l15,c15,v15 = await get_klines(session, symbol, interval="15m", limit=120)
        if len(c15) < 60:
            return
        ema9_15, ma20_15, ma50_15, ma200_15, rsi15, volma15, adx15, pdi15, mdi15 = compute_indicators(o15,h15,l15,c15,v15)
        last15 = len(c15) - 1

        # 🌕 Tendência PRÉ-CONFIRMADA (15m) — EMA9 cruza MA200 + RSI>50
        if (ema9_15[last15] > ma200_15[last15] and ema9_15[last15-1] <= ma200_15[last15-1] and rsi15[last15] > 50.0):
            if monitor.allowed(symbol, "PRECONFIRM_15M"):
                txt = (
                    f"⭐ {fmt_symbol(symbol)} 🌕 — TENDÊNCIA PRÉ-CONFIRMADA (15m)\n"
                    f"💰 <code>{c15[last15]:.6f}</code>\n"
                    f"🧠 EMA9 cruzou MA200 | RSI {rsi15[last15]:.1f}\n"
                    f"⏰ {ts_brazil_now()}\n"
                    f"{binance_links(symbol)}"
                )
                await send_alert(session, txt)
                monitor.mark(symbol, "PRECONFIRM_15M")

        # 🚀 Tendência CONFIRMADA (15m) — MA20 & MA50 > MA200 + RSI>55 + ADX>=25
        if (ma20_15[last15] > ma200_15[last15] and ma50_15[last15] > ma200_15[last15] and
            rsi15[last15] > 55.0 and adx15[last15] >= 25.0):
            if monitor.allowed(symbol, "CONFIRM_15M"):
                txt = (
                    f"⭐ {fmt_symbol(symbol)} 🚀 — TENDÊNCIA CONFIRMADA (15m)\n"
                    f"💰 <code>{c15[last15]:.6f}</code>\n"
                    f"🧠 MA20 & MA50 > MA200 | RSI {rsi15[last15]:.1f} | ADX {adx15[last15]:.1f}\n"
                    f"⏰ {ts_brazil_now()}\n"
                    f"{binance_links(symbol)}"
                )
                await send_alert(session, txt)
                monitor.mark(symbol, "CONFIRM_15M")

        # 📈 Rompimento da resistência (15m) — Donchian 20-high
        if last15 >= 21:
            donchian_high_15 = max(h15[last15-20:last15])
            if c15[last15] > donchian_high_15 and monitor.allowed(symbol, "BREAKOUT_15M"):
                txt = (
                    f"⭐ {fmt_symbol(symbol)} 📈 — ROMPIMENTO DA RESISTÊNCIA (15m)\n"
                    f"💰 <code>{c15[last15]:.6f}</code>\n"
                    f"🧠 Fechou acima da máxima 20 ({donchian_high_15:.6f}) — 💥 Rompimento confirmado\n"
                    f"⏰ {ts_brazil_now()}\n"
                    f"{binance_links(symbol)}"
                )
                await send_alert(session, txt)
                monitor.mark(symbol, "BREAKOUT_15M")

        # ♻️ Reteste (15m) — toque EMA9/MA20 + reação (continuação da alta)
        touched_ema9_15 = (l15[last15] <= ema9_15[last15] and c15[last15] >= ema9_15[last15])
        touched_ma20_15 = (l15[last15] <= ma20_15[last15] and c15[last15] >= ma20_15[last15])
        if (ema9_15[last15] > ma20_15[last15] > ma50_15[last15] and (touched_ema9_15 or touched_ma20_15)):
            if monitor.allowed(symbol, "RETESTE_15M"):
                base = "EMA9" if touched_ema9_15 else "MA20"
                txt = (
                    f"⭐ {fmt_symbol(symbol)} ♻️ — RETESTE {base} (15m)\n"
                    f"💰 <code>{c15[last15]:.6f}</code>\n"
                    f"🧠 Toque na {base} + reação — Continuação da alta\n"
                    f"⏰ {ts_brazil_now()}\n"
                    f"{binance_links(symbol)}"
                )
                await send_alert(session, txt)
                monitor.mark(symbol, "RETESTE_15M")

    except Exception as e:
        print("worker curto error", symbol, e)

# ----------------- WORKER LONGO (1h + 4h) -----------------
async def longterm_worker(session, symbol, monitor: Monitor):
    try:
        # -------- 1h --------
        o1,h1,l1,c1,v1 = await get_klines(session, symbol, interval="1h", limit=120)
        if len(c1) < 60:
            return
        ema9_1, ma20_1, ma50_1, ma200_1, rsi1, volma1, adx1, pdi1, mdi1 = compute_indicators(o1,h1,l1,c1,v1)
        last1 = len(c1) - 1

        # 🌕 PRÉ-CONFIRMAÇÃO LONGA (1h) — EMA9 cruza MA20 + RSI 50–60 + Volume ≥ média
        if (ema9_1[last1] > ma20_1[last1] and ema9_1[last1-1] <= ma20_1[last1-1] and
            50.0 <= rsi1[last1] <= 60.0 and v1[last1] >= volma1[last1]):
            if monitor.allowed_long(symbol, "LONG_PRECONF_1H"):
                txt = (
                    f"🌕 <b>{fmt_symbol(symbol)} — PRÉ-CONFIRMAÇÃO LONGA (1h)</b>\n"
                    f"<b>💰</b> <code>{c1[last1]:.6f}</code>\n"
                    f"<b>🧠</b> EMA9 cruzou MA20 | RSI {rsi1[last1]:.1f} | Vol ≥ média\n"
                    f"<b>🕒</b> {ts_brazil_now()}\n"
                    f"<b>{binance_links(symbol)}</b>"
                )
                await send_alert(session, txt)
                monitor.mark_long(symbol, "LONG_PRECONF_1H")

        # 🚀 TENDÊNCIA LONGA CONFIRMADA (1h) — EMA9>MA20>MA50 + RSI>55 + ADX≥25
        if (ema9_1[last1] > ma20_1[last1] > ma50_1[last1] and rsi1[last1] > 55.0 and adx1[last1] >= 25.0):
            if monitor.allowed_long(symbol, "LONG_CONF_1H"):
                txt = (
                    f"🚀 <b>{fmt_symbol(symbol)} — TENDÊNCIA LONGA CONFIRMADA (1h)</b>\n"
                    f"<b>💰</b> <code>{c1[last1]:.6f}</code>\n"
                    f"<b>🧠</b> EMA9>MA20>MA50 | RSI {rsi1[last1]:.1f} | ADX {adx1[last1]:.1f}\n"
                    f"<b>🕒</b> {ts_brazil_now()}\n"
                    f"<b>{binance_links(symbol)}</b>"
                )
                await send_alert(session, txt)
                monitor.mark_long(symbol, "LONG_CONF_1H")

        # -------- 4h --------
        o4,h4,l4,c4,v4 = await get_klines(session, symbol, interval="4h", limit=120)
        if len(c4) < 60:
            return
        ema9_4, ma20_4, ma50_4, ma200_4, rsi4, volma4, adx4, pdi4, mdi4 = compute_indicators(o4,h4,l4,c4,v4)
        last4 = len(c4) - 1

        # 🌕 PRÉ-CONFIRMAÇÃO (4h) — EMA9 cruza MA20 + RSI > 50
        if (ema9_4[last4] > ma20_4[last4] and ema9_4[last4-1] <= ma20_4[last4-1] and rsi4[last4] > 50.0):
            if monitor.allowed_long(symbol, "LONG_PRECONF_4H"):
                txt = (
                    f"🌕 <b>{fmt_symbol(symbol)} — PRÉ-CONFIRMAÇÃO (4h)</b>\n"
                    f"<b>💰</b> <code>{c4[last4]:.6f}</code>\n"
                    f"<b>🧠</b> EMA9 cruzou MA20 | RSI {rsi4[last4]:.1f}\n"
                    f"<b>🕒</b> {ts_brazil_now()}\n"
                    f"<b>{binance_links(symbol)}</b>"
                )
                await send_alert(session, txt)
                monitor.mark_long(symbol, "LONG_PRECONF_4H")

        # 🚀 TENDÊNCIA 4h CONFIRMADA — (duas velas mantendo estrutura) + RSI>55
        if (ema9_4[last4] > ma20_4[last4] > ma50_4[last4] and
            ema9_4[last4-1] > ma20_4[last4-1] > ma50_4[last4-1] and
            rsi4[last4] > 55.0):
            if monitor.allowed_long(symbol, "LONG_CONF_4H"):
                txt = (
                    f"🚀 <b>{fmt_symbol(symbol)} — TENDÊNCIA 4h CONFIRMADA</b>\n"
                    f"<b>💰</b> <code>{c4[last4]:.6f}</code>\n"
                    f"<b>🧠</b> Estrutura mantida por 2 velas | RSI {rsi4[last4]:.1f}\n"
                    f"<b>🕒</b> {ts_brazil_now()}\n"
                    f"<b>{binance_links(symbol)}</b>"
                )
                await send_alert(session, txt)
                monitor.mark_long(symbol, "LONG_CONF_4H")

    except Exception as e:
        print("worker longo error", symbol, e)

# ----------------- MAIN -----------------
async def main():
    monitor = Monitor()
    async with aiohttp.ClientSession() as session:
        tickers = await get_24h(session)
        watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
        hello = f"💻 py15e_final | 5m/15m (com pré 5m e pré 15m) + 1h/4h ativos | {len(watchlist)} pares | {ts_brazil_now()}"
        await send_alert(session, hello)
        print(hello)

        while True:
            tasks = []
            for s in watchlist:
                tasks.append(candle_worker(session, s, monitor))
                tasks.append(longterm_worker(session, s, monitor))
            await asyncio.gather(*tasks)

            await asyncio.sleep(180)
            try:
                tickers = await get_24h(session)
                watchlist = shortlist_from_24h(tickers, SHORTLIST_N)
            except Exception as e:
                print("refresh error", e)

# ----------------- FLASK -----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Binance Alerts Bot — py15e_final (5m/15m/1h/4h) 🇧🇷"

def start_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main())

if __name__ == "__main__":
    import threading, os
    threading.Thread(target=start_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
